#!/usr/bin/env python3
"""
IPCC References Toolkit (Unified GUI)
======================================
A reproducible pipeline for turning IPCC report PDFs into a Web-of-Science
indexed reference database.

Stages:
  1. Extract        PDF -> references.xlsx + wos_queries.txt
  2. WOS lookup     Either:
                       (a) WOS Starter API   [recommended; needs key]
                       (b) Browser automation [Playwright; ToS-sensitive]
  3. Merge          references.xlsx + WOS exports -> Record.csv + Unrecord.csv

All long-running tasks support Pause / Resume / Stop and produce a structured
TaskReport written to disk on completion.

Run:
    python ipcc_refs_gui.py
"""

import hashlib
import json
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
import requests

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False


# ============================================================================
# Constants
# ============================================================================
CROSSREF_API = "https://api.crossref.org/works"
CROSSREF_TIMEOUT = 20
CROSSREF_RETRIES = 3

WOS_BATCH_SIZE = 50
WOS_ADVANCED_URL = "https://www.webofscience.com/wos/woscc/advanced-search"
WOS_HOME_URL = "https://www.webofscience.com/wos/woscc/basic-search"
WOS_API_URL = "https://api.clarivate.com/apis/wos-starter/v1/documents"

# Footer / branding
AUTHOR_NAME_CN = "Jiacheng Zheng 制作"
AUTHOR_CONTACT_LABEL = "联系我"
AUTHOR_HOMEPAGE = "https://karcen.github.io/zhengjiacheng.github.io/"
APP_TITLE = "IPCC References Toolkit"

# Schemas
UNRECORD_FIELDS = [
    "Report", "Working Group", "Chapter", "Chapter title",
    "Authors", "Article Title", "Publisher", "Year",
]

WOS_FIELDS = [
    "Publication Type", "Authors", "Book Authors", "Book Editors",
    "Book Group Authors", "Author Full Names", "Book Author Full Names",
    "Group Authors", "Article Title", "Source Title", "Book Series Title",
    "Book Series Subtitle", "Language", "Document Type", "Conference Title",
    "Conference Date", "Conference Location", "Conference Sponsor",
    "Conference Host", "Author Keywords", "Keywords Plus", "Abstract",
    "Addresses", "Affiliations", "Reprint Addresses", "Email Addresses",
    "Researcher Ids", "ORCIDs", "Funding Orgs", "Funding Name Preferred",
    "Funding Text", "Cited References", "Cited Reference Count",
    "Times Cited, WoS Core", "Times Cited, All Databases",
    "180 Day Usage Count", "Since 2013 Usage Count", "Publisher",
    "Publisher City", "Publisher Address", "ISSN", "eISSN", "ISBN",
    "Journal Abbreviation", "Journal ISO Abbreviation", "Publication Date",
    "Publication Year", "Volume", "Issue", "Part Number", "Supplement",
    "Special Issue", "Meeting Abstract", "Start Page", "End Page",
    "Article Number", "DOI", "DOI Link", "Book DOI", "Early Access Date",
    "Number of Pages", "WoS Categories", "Web of Science Index",
    "Research Areas", "IDS Number", "Pubmed Id", "Open Access Designations",
    "Highly Cited Status", "Hot Paper Status", "Date of Export",
    "UT (Unique WOS ID)", "Web of Science Record",
]

EXTRACT_FIELDS = [
    "Report", "Working Group", "Chapter", "Chapter Title", "Chapter Authors",
    "Authors", "Article Title", "Publisher", "Year", "Source Title",
    "Raw Citation", "DOI (Extracted)", "DOI Source", "Crossref Score",
    "Match Status",
]

# Regex
DOI_PATTERNS = [
    re.compile(r"\bdoi:\s*(10\.\d{4,9}/[-._;()/:A-Z0-9a-z]+?)(?=[\s,;.)\]]|$)", re.IGNORECASE),
    re.compile(r"https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9a-z]+?)(?=[\s,;.)\]]|$)", re.IGNORECASE),
    re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9a-z]+?)(?=[\s,;.)\]]|$)"),
]
CHAPTER_HEADING_RE = re.compile(r"^\s*(?:Chapter\s+)?(\d{1,2})\.?\s+(.+)$", re.IGNORECASE)
REF_START_RE = re.compile(r"^[A-ZÀ-Ý][a-zà-ÿ\-\']+(?:[\s\-][A-ZÀ-Ý][a-zà-ÿ\-\']+)*,\s+[A-ZÀ-Ý]\.")
REFERENCES_HEADER_RE = re.compile(r"(?:\A|\n)\s*References\s*(?:\n|\Z)", re.IGNORECASE)
REFERENCES_END_RE = re.compile(
    r"\n\s*(Frequently Asked Questions|Appendix|Supplementary Material|Cross-Chapter|FAQ)",
    re.IGNORECASE,
)


# ============================================================================
# Compatibility helpers
# ============================================================================
def df_map_strings(df: pd.DataFrame, fn: Callable) -> pd.DataFrame:
    """Apply a per-cell function. pandas>=2.1 uses .map; older uses .applymap."""
    if hasattr(df, "map") and callable(getattr(df, "map", None)) and \
       "func" in getattr(df.map, "__doc__", "") if df.map.__doc__ else True:
        try:
            return df.map(fn)
        except (AttributeError, TypeError):
            pass
    return df.applymap(fn)


def truncate_excel_cells(df: pd.DataFrame, max_len: int = 32000) -> pd.DataFrame:
    """Excel cell limit is 32767. Truncate any oversized string cell."""
    def fn(v):
        if isinstance(v, str) and len(v) > max_len:
            return v[:max_len]
        return v
    try:
        return df.map(fn)
    except (AttributeError, TypeError):
        return df.applymap(fn)


# ============================================================================
# Task coordination (pause / resume / stop / user-continue)
# ============================================================================
class TaskState:
    """Thread-safe coordination for a single worker task."""

    def __init__(self):
        self.pause_event = threading.Event(); self.pause_event.set()
        self.stop_event = threading.Event()
        self.user_event = threading.Event()
        self._lock = threading.Lock()
        self._status = "idle"
        self._prompt = ""

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def prompt(self) -> str:
        with self._lock:
            return self._prompt

    def _set_status(self, s: str):
        with self._lock:
            self._status = s

    def check_pause(self) -> bool:
        """Block while paused. Returns False if Stop was requested."""
        was_paused = False
        while not self.pause_event.is_set():
            if self.stop_event.is_set():
                return False
            if not was_paused:
                self._set_status("paused")
                was_paused = True
            time.sleep(0.1)
        if self.stop_event.is_set():
            return False
        if was_paused:
            self._set_status("running")
        return True

    def interruptible_sleep(self, seconds: float) -> bool:
        """Sleep but wake on Stop. Returns False if Stop was requested."""
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return False
            if not self.check_pause():
                return False
            time.sleep(min(0.3, max(0.0, end - time.time())))
        return True

    def wait_for_user(self, prompt: str) -> bool:
        """Block until user clicks Continue. Returns False if Stop was requested."""
        with self._lock:
            self._prompt = prompt
            self._status = "waiting_user"
        self.user_event.clear()
        while not self.user_event.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.2)
        with self._lock:
            self._prompt = ""
            self._status = "running"
        return True

    def pause(self):         self.pause_event.clear()
    def resume(self):        self.pause_event.set()
    def user_continue(self): self.user_event.set()

    def stop(self):
        """Signal Stop; unblock any waiting condition."""
        self.stop_event.set()
        self.pause_event.set()
        self.user_event.set()

    def reset(self):
        self.pause_event.set()
        self.stop_event.clear()
        self.user_event.clear()
        with self._lock:
            self._status = "idle"
            self._prompt = ""

    def start(self):
        self.reset()
        self._set_status("running")


# ============================================================================
# Task report (structured metrics + per-chapter failure tracking)
# ============================================================================
class TaskReport:
    """
    Structured summary of a task run.

    Fields beyond the basic counters:
      - failed_extractions: list of {chapter, title, error, traceback}
        for chapter-level failures (extraction stage).
      - error_log:          flat list of warning/error messages collected
                            anywhere in the task.
    Both are capped at MAX_ERRORS to prevent unbounded memory growth on
    pathological inputs.
    """
    MAX_ERRORS = 1000

    def __init__(self, name: str):
        self.name = name
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.status = "running"     # running | completed | completed_with_errors | failed | stopped
        self.total = 0
        self.success_count = 0
        self.failure_count = 0
        self.skipped_count = 0
        self.outputs: List[str] = []
        self.errors: List[str] = []
        self.metadata: Dict[str, str] = {}
        self.failed_extractions: List[Dict[str, str]] = []
        self.error_log: List[str] = []

    # Counters & adders
    def set_total(self, n: int):       self.total = int(n)
    def add_success(self, n: int = 1): self.success_count += n
    def add_skipped(self, n: int = 1): self.skipped_count += n
    def add_output(self, path):        self.outputs.append(str(path))
    def set_meta(self, k: str, v):     self.metadata[k] = str(v)

    def add_failure(self, msg: str = ""):
        self.failure_count += 1
        if msg and len(self.errors) < self.MAX_ERRORS:
            self.errors.append(msg)

    def add_chapter_failure(self, chapter_id, chapter_title, error_msg: str, tb: str = ""):
        """Record a per-chapter extraction failure with full traceback."""
        if len(self.failed_extractions) < self.MAX_ERRORS:
            self.failed_extractions.append({
                "chapter": str(chapter_id),
                "title": str(chapter_title)[:300],
                "error": str(error_msg)[:600],
                "traceback": str(tb)[:8000],
            })
        # Chapter failures also count as failures in the summary
        self.failure_count += 1

    def log_error(self, msg: str):
        """Log a warning/error that does not stop the task."""
        if len(self.error_log) < self.MAX_ERRORS:
            self.error_log.append(str(msg)[:500])

    # Output helpers
    def finish(self, status: Optional[str] = None):
        self.end_time = time.time()
        if status:
            self.status = status
        elif self.failure_count > 0 or self.failed_extractions:
            self.status = "completed_with_errors"
        else:
            self.status = "completed"

    def elapsed_str(self) -> str:
        end = self.end_time or time.time()
        secs = max(0.0, end - self.start_time)
        if secs < 60:
            return f"{secs:.1f}s"
        m, s = divmod(secs, 60)
        if m < 60:
            return f"{int(m)}m {int(s)}s"
        h, m = divmod(m, 60)
        return f"{int(h)}h {int(m)}m {int(s)}s"

    def success_rate(self) -> float:
        attempted = self.success_count + self.failure_count
        return 100 * self.success_count / attempted if attempted else 0.0

    def render(self) -> str:
        lines = [
            "",
            "=" * 68,
            f"  Task Report — {self.name}",
            "=" * 68,
            f"  Status        : {self.status}",
            f"  Elapsed time  : {self.elapsed_str()}",
        ]
        if self.total:
            lines.append(f"  Total items   : {self.total}")
        if self.success_count or self.failure_count or self.skipped_count:
            lines.append(f"  Succeeded     : {self.success_count}")
            if self.failure_count:
                lines.append(f"  Failed        : {self.failure_count}")
            if self.skipped_count:
                lines.append(f"  Skipped       : {self.skipped_count}")
            if self.success_count + self.failure_count > 0:
                lines.append(f"  Success rate  : {self.success_rate():.1f}%")
        if self.metadata:
            lines.append("  " + "-" * 54)
            for k, v in self.metadata.items():
                lines.append(f"  {k:<18}: {v}")
        if self.failed_extractions:
            lines.append("  " + "-" * 54)
            shown = min(10, len(self.failed_extractions))
            lines.append(f"  Failed chapters    : ({len(self.failed_extractions)} total, showing {shown})")
            for f in self.failed_extractions[:shown]:
                lines.append(f"      Ch{f['chapter']} \"{f['title'][:48]}\" -> {f['error'][:120]}")
            if len(self.failed_extractions) > shown:
                lines.append(f"      ... ({len(self.failed_extractions) - shown} more in failed_chapters.csv / extraction_errors.log)")
        if self.error_log:
            lines.append("  " + "-" * 54)
            shown = min(5, len(self.error_log))
            lines.append(f"  Warnings           : ({len(self.error_log)} total, showing {shown})")
            for e in self.error_log[:shown]:
                lines.append(f"      - {e[:160]}")
        if self.errors:
            lines.append("  " + "-" * 54)
            shown = min(5, len(self.errors))
            lines.append(f"  Errors             : ({len(self.errors)} total, showing {shown})")
            for e in self.errors[:shown]:
                lines.append(f"      - {e[:160]}")
        if self.outputs:
            lines.append("  " + "-" * 54)
            lines.append("  Output files       :")
            for o in self.outputs:
                lines.append(f"      - {o}")
        lines.append("=" * 68)
        return "\n".join(lines)

    def save(self, out_dir: Path) -> Optional[Path]:
        """Persist the rendered report. Never raises."""
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.name).strip("_")
            path = out_dir / f"task_report_{safe}_{ts}.txt"
            path.write_text(self.render(), encoding="utf-8")
            return path
        except Exception:
            return None


# ============================================================================
# PDF extraction helpers (each is best-effort and isolates its own errors)
# ============================================================================
def _open_pdf_safely(pdf_path: Path) -> Tuple[Optional[fitz.Document], Optional[str]]:
    """Return (doc, None) or (None, error_message). Never raises."""
    try:
        doc = fitz.open(pdf_path)
        return doc, None
    except Exception as e:
        return None, f"Cannot open PDF: {e}"


def extract_chapters(pdf_path: Path, log: Callable[[str], None]) -> List[Dict]:
    """Detect chapter structure from TOC bookmarks; falls back to single-chapter."""
    doc, err = _open_pdf_safely(pdf_path)
    if err or doc is None:
        raise RuntimeError(err or "Could not open PDF")
    try:
        toc = doc.get_toc(simple=True)
        candidates = []
        for entry in toc:
            try:
                level, title, page = entry[:3]
            except Exception:
                continue
            m = CHAPTER_HEADING_RE.match(str(title).strip())
            if m and level <= 2:
                candidates.append({
                    "number": m.group(1),
                    "title": m.group(2).strip(),
                    "start_page": max(0, int(page) - 1),
                })
        npages = len(doc)
        chapters: List[Dict] = []
        if candidates:
            for i, c in enumerate(candidates):
                c["end_page"] = (candidates[i + 1]["start_page"] - 1
                                 if i + 1 < len(candidates) else npages - 1)
                # Clamp
                c["start_page"] = min(max(0, c["start_page"]), npages - 1)
                c["end_page"] = min(max(c["start_page"], c["end_page"]), npages - 1)
                chapters.append(c)
        else:
            log("Warning: no chapter structure in TOC; treating entire PDF as one chapter")
            chapters.append({
                "number": "1", "title": "Full Document",
                "start_page": 0, "end_page": npages - 1,
            })
        return chapters
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _strip_page_noise(text: str) -> str:
    """Remove standalone numeric lines (page numbers)."""
    lines = [ln for ln in text.split("\n") if not re.match(r"^\s*\d{1,4}\s*$", ln)]
    return "\n".join(lines)


def extract_chapter_text(pdf_path: Path, chapter: Dict) -> str:
    """Read text for a chapter; tolerates single bad pages."""
    doc, err = _open_pdf_safely(pdf_path)
    if err or doc is None:
        raise RuntimeError(err or "Could not open PDF")
    try:
        start = max(0, int(chapter.get("start_page", 0)))
        end = min(len(doc) - 1, int(chapter.get("end_page", len(doc) - 1)))
        parts = []
        for pg in range(start, end + 1):
            try:
                parts.append(_strip_page_noise(doc[pg].get_text()))
            except Exception:
                # Individual page failure is non-fatal
                parts.append("")
        return "\n".join(parts)
    finally:
        try:
            doc.close()
        except Exception:
            pass


def extract_chapter_authors(chapter_text: str) -> str:
    """Best-effort author block extraction from the first few pages."""
    head = chapter_text[:6000]
    patterns = [
        r"(?:Coordinating Lead Authors?)\s*:?\s*([\s\S]+?)(?:\n\s*(?:Contributing|Review|Lead Authors?|Authors?)\s*[:\n])",
        r"(?:Lead Authors?)\s*:?\s*([\s\S]+?)(?:\n\s*(?:Contributing|Review|Chapter Scientists)\s*[:\n])",
        r"(?:Authors?)\s*:?\s*([\s\S]+?)(?:\n\s*(?:Contributing|Review|This chapter|Abstract)\s*[:\n])",
    ]
    found = []
    for pat in patterns:
        try:
            m = re.search(pat, head, re.IGNORECASE)
            if m:
                cleaned = re.sub(r"\([^)]*\)", "", m.group(1))
                cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(",;")
                if cleaned:
                    found.append(cleaned)
        except Exception:
            continue
    return " | ".join(found)[:1000]


def extract_references_section(chapter_text: str) -> str:
    """Slice the References section out of a chapter."""
    m = REFERENCES_HEADER_RE.search(chapter_text)
    if not m:
        return ""
    ref_text = chapter_text[m.end():]
    end_m = REFERENCES_END_RE.search(ref_text)
    if end_m:
        ref_text = ref_text[:end_m.start()]
    return ref_text


def split_references(ref_text: str) -> List[str]:
    """Split a References block into individual citations using a heuristic."""
    if not ref_text:
        return []
    lines = [ln.strip() for ln in ref_text.split("\n")]
    refs, current = [], []
    for line in lines:
        if not line:
            continue
        if REF_START_RE.match(line) and current:
            refs.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        refs.append(" ".join(current))
    cleaned = []
    for r in refs:
        if len(r) < 40 or not re.search(r"\b(19|20)\d{2}\b", r):
            continue
        cleaned.append(re.sub(r"\s+", " ", r).strip())
    return cleaned


def extract_embedded_doi(citation: str) -> Optional[str]:
    """Pull a DOI out of the citation string if present."""
    for pat in DOI_PATTERNS:
        try:
            m = pat.search(citation)
        except Exception:
            continue
        if m:
            doi = m.group(1).rstrip(".,;)")
            if "/" in doi and len(doi) > 7:
                return doi.lower()
    return None


# ============================================================================
# Crossref client
# ============================================================================
class CrossrefClient:
    """Thin wrapper around the Crossref REST API with on-disk caching."""

    def __init__(self, email: str, cache_path: Path):
        self.cache_path = Path(cache_path)
        self.cache: Dict[str, dict] = {}
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self.cache = {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"IPCC-Refs-Toolkit/1.0 (mailto:{email or 'anonymous@example.com'})"
        })

    def _save(self):
        try:
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _key(prefix: str, val: str) -> str:
        return f"{prefix}:{hashlib.md5(val.encode('utf-8', errors='ignore')).hexdigest()}"

    def lookup_by_doi(self, doi: str) -> Optional[dict]:
        k = self._key("doi", doi.lower())
        if k in self.cache:
            return self.cache[k] or None
        for attempt in range(CROSSREF_RETRIES):
            try:
                r = self.session.get(f"{CROSSREF_API}/{doi}", timeout=CROSSREF_TIMEOUT)
                if r.status_code == 200:
                    item = r.json().get("message", {})
                    item["_score"] = 100.0
                    self.cache[k] = item; self._save(); return item
                if r.status_code == 404:
                    self.cache[k] = {}; self._save(); return None
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                else:
                    return None
            except requests.RequestException:
                time.sleep(2)
        return None

    def lookup_by_citation(self, citation: str) -> Optional[dict]:
        k = self._key("cite", citation[:300])
        if k in self.cache:
            return self.cache[k] or None
        params = {"query.bibliographic": citation[:500], "rows": 1}
        for attempt in range(CROSSREF_RETRIES):
            try:
                r = self.session.get(CROSSREF_API, params=params, timeout=CROSSREF_TIMEOUT)
                if r.status_code == 200:
                    items = r.json().get("message", {}).get("items", [])
                    item = items[0] if items else {}
                    self.cache[k] = item; self._save()
                    return item if item else None
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                else:
                    return None
            except requests.RequestException:
                time.sleep(2)
        return None


def crossref_to_fields(item: dict) -> Dict[str, str]:
    """Flatten Crossref item -> our extract row fields."""
    if not item:
        return {}
    title_raw = item.get("title", [""])
    title = title_raw[0] if isinstance(title_raw, list) and title_raw \
            else (title_raw if isinstance(title_raw, str) else "")
    authors = []
    for a in item.get("author", []) or []:
        family, given = a.get("family", ""), a.get("given", "")
        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)
    year = ""
    for f in ("published-print", "published-online", "published", "issued"):
        d = item.get(f)
        if d and "date-parts" in d and d["date-parts"]:
            try:
                year = str(d["date-parts"][0][0])
                break
            except Exception:
                continue
    cont_raw = item.get("container-title", [""])
    container = cont_raw[0] if isinstance(cont_raw, list) and cont_raw \
                else (cont_raw if isinstance(cont_raw, str) else "")
    return {
        "Authors": "; ".join(authors),
        "Article Title": title,
        "Publisher": item.get("publisher", ""),
        "Year": year,
        "Source Title": container,
        "DOI (Extracted)": (item.get("DOI", "") or "").lower(),
        "Crossref Score": item.get("_score", item.get("score", "")),
    }


# ============================================================================
# Stage 1 - PDF extraction (per-chapter isolated; never crashes the run)
# ============================================================================
def run_extraction(pdf_path: str, report: str, wg: str, email: str,
                   out_dir: str, max_refs: int, chapters_filter: str,
                   skip_crossref: bool,
                   state: TaskState,
                   log: Callable[[str], None],
                   progress: Callable[[int, int], None]) -> TaskReport:
    """
    Stage 1 entry point.

    Design contract:
      - NEVER raises. Always returns a TaskReport.
      - Per-chapter failures are caught and logged to failed_extractions.
      - Per-reference failures are caught and counted in failure_count.
      - On total catastrophic failure, status='failed' and reason in errors.
      - Writes references.xlsx, wos_queries.txt, failed_chapters.csv,
        extraction_errors.log as appropriate.
    """
    task = TaskReport("Extract PDF")
    out_dir_p: Optional[Path] = None

    try:
        pdf_path_p = Path(pdf_path)
        out_dir_p = Path(out_dir)
        out_dir_p.mkdir(parents=True, exist_ok=True)
        task.set_meta("PDF", str(pdf_path_p))
        task.set_meta("Report", f"{report} / {wg}")

        # ---- PDF sanity check ----
        if not pdf_path_p.exists():
            task.add_failure(f"PDF not found: {pdf_path_p}")
            log(f"ERROR: PDF not found: {pdf_path_p}")
            task.finish("failed"); return task

        doc, open_err = _open_pdf_safely(pdf_path_p)
        if open_err or doc is None:
            task.add_failure(open_err or "Could not open PDF")
            log(f"ERROR: {open_err}")
            task.finish("failed"); return task
        try:
            npages = len(doc)
        except Exception:
            npages = 0
        finally:
            try: doc.close()
            except Exception: pass

        if npages == 0:
            task.add_failure("PDF has no pages")
            log("ERROR: PDF has no pages")
            task.finish("failed"); return task
        log(f"PDF opened: {npages} pages")
        task.set_meta("PDF pages", npages)

        # ---- Chapter detection (with safe fallback) ----
        try:
            chapters = extract_chapters(pdf_path_p, log)
        except Exception as e:
            tb = traceback.format_exc()
            log(f"Chapter detection failed: {e}; using single-chapter fallback")
            task.log_error(f"Chapter detection failed: {e}\n{tb}")
            chapters = [{"number": "1", "title": "Full Document",
                         "start_page": 0, "end_page": npages - 1}]

        if chapters_filter.strip():
            wanted = {x.strip() for x in chapters_filter.split(",") if x.strip()}
            chapters = [c for c in chapters if c.get("number") in wanted]
            log(f"Filtered to {len(chapters)} chapters")
        task.set_meta("Chapters detected", len(chapters))

        # ---- Crossref client (optional) ----
        client = None
        if not skip_crossref:
            try:
                client = CrossrefClient(email, out_dir_p / "crossref_cache.json")
            except Exception as e:
                task.log_error(f"Crossref disabled: {e}")
                log(f"WARNING: Crossref client init failed: {e}")

        # ---- Per-chapter text + references (isolated failure) ----
        chapter_data: List[Tuple[Dict, str, List[str]]] = []
        n_chapters_ok = 0
        for ch in chapters:
            if not state.check_pause():
                task.finish("stopped"); return task

            ch_num = ch.get("number", "?")
            ch_title = ch.get("title", "")
            try:
                text = extract_chapter_text(pdf_path_p, ch)
                authors = extract_chapter_authors(text)
                ref_text = extract_references_section(text)
                refs = split_references(ref_text)
                if max_refs > 0:
                    refs = refs[:max_refs]
                if not refs:
                    msg = f"Chapter {ch_num}: no References section found"
                    task.log_error(msg)
                    log(f"  Ch{ch_num}: WARNING — no References section detected")
                chapter_data.append((ch, authors, refs))
                n_chapters_ok += 1
                log(f"  Ch{ch_num}: {len(refs)} refs — {ch_title[:60]}")
            except Exception as e:
                tb = traceback.format_exc()
                task.add_chapter_failure(ch_num, ch_title, str(e), tb)
                log(f"  Ch{ch_num}: EXTRACTION FAILED — {e}")
                # Continue with next chapter — do NOT abort

        task.set_meta("Chapters succeeded", n_chapters_ok)
        task.set_meta("Chapters failed", len(task.failed_extractions))

        # ---- Per-reference processing (isolated failure) ----
        total = sum(len(r) for _, _, r in chapter_data)
        task.set_total(total)
        progress(0, total)
        done = 0
        all_records: List[Dict] = []
        all_dois: List[str] = []
        n_embedded = n_crossref = n_unmatched = 0

        for ch, ch_authors, refs in chapter_data:
            for raw in refs:
                if not state.check_pause():
                    task.finish("stopped"); break

                record = {f: "" for f in EXTRACT_FIELDS}
                record.update({
                    "Report": report, "Working Group": wg,
                    "Chapter": ch.get("number", ""),
                    "Chapter Title": ch.get("title", ""),
                    "Chapter Authors": ch_authors,
                    "Raw Citation": raw[:32000],
                    "Match Status": "unmatched", "DOI Source": "none",
                })
                try:
                    doi = extract_embedded_doi(raw)
                    cr_item = None
                    if doi:
                        record["DOI (Extracted)"] = doi
                        record["DOI Source"] = "embedded"
                        record["Match Status"] = "embedded_only"
                        if client:
                            try:
                                cr_item = client.lookup_by_doi(doi)
                            except Exception as ce:
                                task.log_error(f"Crossref DOI lookup failed: {ce}")
                    if not cr_item and client:
                        try:
                            cr_item = client.lookup_by_citation(raw)
                            if cr_item and not doi:
                                record["DOI Source"] = "crossref"
                        except Exception as ce:
                            task.log_error(f"Crossref bib lookup failed: {ce}")

                    if cr_item:
                        for k, v in crossref_to_fields(cr_item).items():
                            if v != "":
                                record[k] = v
                        if record.get("DOI (Extracted)"):
                            record["Match Status"] = "matched"

                    src = record["DOI Source"]
                    if src == "embedded": n_embedded += 1
                    elif src == "crossref": n_crossref += 1
                    else: n_unmatched += 1

                    all_records.append(record)
                    if record["DOI (Extracted)"]:
                        all_dois.append(record["DOI (Extracted)"])
                    task.add_success()
                except Exception as e:
                    task.add_failure(f"row {done+1}: {e}")
                    log(f"  ROW {done+1} failed: {e}")
                    all_records.append(record)  # keep raw row even on failure

                done += 1
                if done % 5 == 0:
                    progress(done, total)
                # Light rate limit on Crossref bibliographic queries
                if client and not record.get("DOI (Extracted)"):
                    time.sleep(0.1)
            else:
                continue
            break  # outer break if inner broke (stop)

        progress(total, total)

        # ---- Write references.xlsx ----
        try:
            df = pd.DataFrame(all_records).reindex(columns=EXTRACT_FIELDS)
            df = truncate_excel_cells(df, 32000)
            refs_path = out_dir_p / "references.xlsx"
            df.to_excel(refs_path, index=False, engine="openpyxl")
            log(f"Wrote references.xlsx ({len(df)} rows) -> {refs_path}")
            task.add_output(refs_path)
        except Exception as e:
            task.add_failure(f"Could not write references.xlsx: {e}")
            log(f"ERROR writing references.xlsx: {e}")

        # ---- Write wos_queries.txt ----
        dois_unique = list(dict.fromkeys(all_dois))
        if dois_unique:
            try:
                wos_path = out_dir_p / "wos_queries.txt"
                with open(wos_path, "w", encoding="utf-8") as f:
                    n_batches = (len(dois_unique) - 1) // WOS_BATCH_SIZE + 1
                    f.write(f"# {len(dois_unique)} unique DOIs in {n_batches} batches\n\n")
                    for i in range(0, len(dois_unique), WOS_BATCH_SIZE):
                        batch = dois_unique[i:i + WOS_BATCH_SIZE]
                        query = "DO=(" + " OR ".join(f'"{d}"' for d in batch) + ")"
                        f.write(f"# Batch {i//WOS_BATCH_SIZE + 1} ({len(batch)} DOIs)\n{query}\n\n")
                log(f"Wrote wos_queries.txt -> {wos_path}")
                task.add_output(wos_path)
            except Exception as e:
                task.log_error(f"Could not write wos_queries.txt: {e}")

        # ---- Write failure logs ----
        if task.failed_extractions:
            try:
                fc_path = out_dir_p / "failed_chapters.csv"
                pd.DataFrame(task.failed_extractions).to_csv(
                    fc_path, index=False, encoding="utf-8-sig")
                task.add_output(fc_path)

                err_log_path = out_dir_p / "extraction_errors.log"
                with open(err_log_path, "w", encoding="utf-8") as f:
                    for entry in task.failed_extractions:
                        f.write(f"=== Chapter {entry['chapter']}: {entry['title']} ===\n")
                        f.write(f"Error: {entry['error']}\n")
                        f.write(f"Traceback:\n{entry['traceback']}\n\n")
                task.add_output(err_log_path)
                log(f"Wrote failure logs (failed_chapters.csv, extraction_errors.log)")
            except Exception as e:
                log(f"WARNING: could not write failure logs: {e}")

        # ---- Aggregate metadata ----
        task.set_meta("Embedded DOIs", n_embedded)
        task.set_meta("Crossref matches", n_crossref)
        task.set_meta("Unmatched", n_unmatched)
        task.set_meta("Unique DOIs", len(dois_unique))
        if total > 0:
            task.set_meta("DOI coverage", f"{100*(n_embedded+n_crossref)/total:.1f}%")

        task.finish()  # auto: completed | completed_with_errors
        return task

    except Exception as e:
        # Should be rare — top-level catastrophic error
        tb = traceback.format_exc()
        task.add_failure(str(e))
        task.log_error(tb)
        task.finish("failed")
        log(f"\nFATAL: {e}\n{tb}")
        return task
    finally:
        try:
            log("\n" + task.render())
        except Exception:
            pass
        if out_dir_p:
            task.save(out_dir_p)


# ============================================================================
# Stage 2a - WOS browser automation (Playwright; ToS-sensitive)
# ============================================================================
def parse_wos_queries(path: Path) -> List[str]:
    """Extract DO=(...) lines from wos_queries.txt."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("DO=") and line.endswith(")"):
            out.append(line)
    return out


def _wos_dismiss_popups(page):
    for txt in ("Accept", "Accept All", "I agree", "Got it", "Close", "Continue"):
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{txt}$", re.I)).first
            if btn.is_visible(timeout=1000):
                btn.click()
                page.wait_for_timeout(300)
        except Exception:
            pass


def _wos_search_one(page, query: str, out_path: Path) -> str:
    """Run one DO=(...) query and download Excel. Returns 'ok' or 'empty'."""
    page.goto(WOS_ADVANCED_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except PWTimeout:
        pass
    _wos_dismiss_popups(page)

    textarea = None
    for getter in (
        lambda: page.get_by_role("textbox", name=re.compile(r"query|search", re.I)),
        lambda: page.locator("textarea#advancedSearchInputArea"),
        lambda: page.locator("textarea[aria-label*='search' i]"),
        lambda: page.locator("textarea").first,
    ):
        try:
            t = getter()
            t.wait_for(state="visible", timeout=4000)
            textarea = t; break
        except Exception:
            continue
    if textarea is None:
        raise RuntimeError("Advanced Search textarea not found (WOS UI may have changed)")
    textarea.click(); textarea.fill(""); page.wait_for_timeout(200)
    textarea.fill(query); page.wait_for_timeout(300)

    clicked = False
    for getter in (
        lambda: page.get_by_role("button", name=re.compile(r"^search$", re.I)).last,
        lambda: page.locator("button:has-text('Search')").last,
    ):
        try:
            b = getter(); b.wait_for(state="visible", timeout=3000); b.click()
            clicked = True; break
        except Exception:
            continue
    if not clicked:
        raise RuntimeError("Search button not found")

    try:
        page.wait_for_url(re.compile(r"summary|results"), timeout=60000)
    except PWTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except PWTimeout:
        pass

    body = (page.locator("body").inner_text() or "").lower()
    if "0 results" in body or "no records" in body or "no results" in body:
        return "empty"

    page.wait_for_timeout(800)
    clicked = False
    for getter in (
        lambda: page.get_by_role("button", name=re.compile(r"^export", re.I)).first,
        lambda: page.locator("button:has-text('Export')").first,
    ):
        try:
            b = getter(); b.wait_for(state="visible", timeout=3000); b.click()
            clicked = True; break
        except Exception:
            continue
    if not clicked:
        raise RuntimeError("Export button not found")

    page.wait_for_timeout(600)
    clicked = False
    for getter in (
        lambda: page.get_by_role("menuitem", name=re.compile(r"excel", re.I)),
        lambda: page.locator("button:has-text('Excel')"),
        lambda: page.get_by_text(re.compile(r"^Excel$", re.I)).first,
    ):
        try:
            b = getter(); b.wait_for(state="visible", timeout=3000); b.click()
            clicked = True; break
        except Exception:
            continue
    if not clicked:
        raise RuntimeError("Excel option not found")

    page.wait_for_timeout(1500)
    try:
        radio = page.locator(
            "input[type='radio'][value*='range' i], label:has-text('Records from')"
        ).first
        if radio.is_visible(timeout=1500):
            radio.click(); page.wait_for_timeout(200)
        from_inp = page.locator("input[id*='markFrom' i], input[name*='from' i]").first
        to_inp = page.locator("input[id*='markTo' i], input[name*='to' i]").first
        if from_inp.is_visible(timeout=1200):
            from_inp.fill("1")
        if to_inp.is_visible(timeout=1200):
            to_inp.fill("1000")
    except Exception:
        pass
    try:
        for getter in (
            lambda: page.locator("select").filter(has_text=re.compile(r"record", re.I)).first,
            lambda: page.get_by_role("combobox").first,
        ):
            try:
                sel = getter()
                if sel.is_visible(timeout=1200):
                    sel.select_option(label=re.compile(r"full record", re.I))
                    break
            except Exception:
                continue
        try:
            page.get_by_text(re.compile(r"^full record$", re.I)).first.click(timeout=1200)
        except Exception:
            pass
    except Exception:
        pass
    page.wait_for_timeout(400)

    try:
        with page.expect_download(timeout=120000) as dl_info:
            for getter in (
                lambda: page.locator("div[role='dialog'] button:has-text('Export')").last,
                lambda: page.locator("button:has-text('Export')").last,
                lambda: page.get_by_role("button", name=re.compile(r"^export$", re.I)).last,
            ):
                try:
                    b = getter()
                    if b.is_visible(timeout=1200):
                        b.click(); break
                except Exception:
                    continue
        dl = dl_info.value
        dl.save_as(str(out_path))
    except PWTimeout:
        raise RuntimeError("Download timed out")
    return "ok"


def run_wos_auto(queries_file: str, out_dir: str, session_dir: str, delay: float,
                 state: TaskState,
                 log: Callable[[str], None],
                 progress: Callable[[int, int], None]) -> TaskReport:
    """Stage 2a entry point. Browser-based WOS automation. Never raises."""
    task = TaskReport("WOS Auto (Browser)")
    task.set_meta("Queries file", queries_file)
    task.set_meta("Method", "Playwright (browser)")
    out_p: Optional[Path] = None

    try:
        if not PLAYWRIGHT_OK:
            task.add_failure("Playwright not installed")
            log("ERROR: Playwright not installed. Install with:\n"
                "  pip install playwright\n  playwright install chromium")
            task.finish("failed"); return task

        qp = Path(queries_file)
        if not qp.exists():
            task.add_failure(f"queries file not found: {qp}")
            log(f"ERROR: {qp} not found. Run Stage 1 first.")
            task.finish("failed"); return task

        out_p = Path(out_dir); out_p.mkdir(parents=True, exist_ok=True)
        sess_p = Path(session_dir); sess_p.mkdir(parents=True, exist_ok=True)

        queries = parse_wos_queries(qp)
        log(f"Parsed {len(queries)} batch queries")
        task.set_total(len(queries))
        if not queries:
            log("No queries to run."); task.finish(); return task
        progress(0, len(queries))

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(sess_p),
                headless=False,
                accept_downloads=True,
                viewport={"width": 1400, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx.set_default_timeout(30000)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            try:
                try:
                    page.goto(WOS_HOME_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                except Exception as e:
                    task.add_failure(f"Could not load WOS: {e}")
                    log(f"ERROR loading WOS: {e}")
                    task.finish("failed"); return task

                url = (page.url or "").lower()
                need_login = ("login" in url or "/auth/" in url or "/sso/" in url)
                if need_login:
                    log("Login required. Use the browser to log in, then click 'Continue after login'.")
                    if not state.wait_for_user(
                        "Log in to Web of Science in the browser, then click 'Continue after login'."
                    ):
                        log("Stopped by user."); task.finish("stopped"); return task
                    log("Continuing after login.")

                total = len(queries); n_empty = 0
                for i, query in enumerate(queries, start=1):
                    if not state.check_pause():
                        log("Stopped by user."); task.finish("stopped"); return task

                    out_path = out_p / f"wos_batch_{i:03d}.xlsx"
                    empty_marker = out_p / f"wos_batch_{i:03d}.empty"
                    if out_path.exists() or empty_marker.exists():
                        log(f"[{i}/{total}] already exists, skip")
                        task.add_skipped(); progress(i, total); continue

                    log(f"[{i}/{total}] running batch...")
                    try:
                        result = _wos_search_one(page, query, out_path)
                        if result == "ok" and out_path.exists():
                            log(f"  ok: {out_path.name}")
                            task.add_success(); task.add_output(out_path)
                        elif result == "empty":
                            empty_marker.write_text("0 results", encoding="utf-8")
                            log(f"  0 results (marked)")
                            task.add_success(); n_empty += 1
                        else:
                            log(f"  warning: no file produced")
                            task.add_failure(f"batch {i}: no output")
                    except Exception as e:
                        log(f"  FAILED: {e}")
                        task.add_failure(f"batch {i}: {e}")
                        try:
                            shot = out_p / f"error_batch_{i:03d}.png"
                            page.screenshot(path=str(shot), full_page=True)
                            log(f"  screenshot: {shot.name}")
                        except Exception:
                            pass

                    progress(i, total)
                    sleep_s = max(2.0, delay + random.uniform(-2, 4))
                    log(f"  waiting {sleep_s:.1f}s before next batch...")
                    if not state.interruptible_sleep(sleep_s):
                        log("Stopped by user."); task.finish("stopped"); return task

                task.set_meta("Empty batches", n_empty)
                task.finish()
                return task
            finally:
                try: ctx.close()
                except Exception: pass

    except Exception as e:
        tb = traceback.format_exc()
        task.add_failure(str(e)); task.log_error(tb)
        task.finish("failed")
        log(f"\nFATAL: {e}\n{tb}")
        return task
    finally:
        try: log("\n" + task.render())
        except Exception: pass
        if out_p:
            task.save(out_p)


# ============================================================================
# Stage 2b - WOS Starter API (recommended)
# ============================================================================
def _wos_api_to_row(hit: dict) -> Dict[str, str]:
    """Map one WOS Starter API hit to our WOS_FIELDS row (limited fields)."""
    row = {f: "" for f in WOS_FIELDS}
    try:
        row["UT (Unique WOS ID)"] = hit.get("uid", "") or ""

        title = hit.get("title")
        if isinstance(title, dict):
            row["Article Title"] = title.get("title", "") or title.get("value", "") or ""
        elif isinstance(title, str):
            row["Article Title"] = title

        source = hit.get("source") or {}
        if isinstance(source, dict):
            row["Source Title"] = source.get("sourceTitle", "") or source.get("title", "") or ""
            row["Publication Year"] = str(source.get("publishYear", "") or "")
            row["Volume"] = str(source.get("volume", "") or "")
            row["Issue"] = str(source.get("issue", "") or "")
            pages = source.get("pages") or {}
            if isinstance(pages, dict):
                row["Start Page"] = str(pages.get("begin", "") or "")
                row["End Page"] = str(pages.get("end", "") or "")
            row["ISSN"] = source.get("issn", "") or ""
            row["eISSN"] = source.get("eissn", "") or ""

        idents = hit.get("identifiers") or {}
        if isinstance(idents, dict):
            row["DOI"] = idents.get("doi", "") or ""
        elif isinstance(idents, list):
            for it in idents:
                if isinstance(it, dict) and (it.get("type") or "").lower() == "doi":
                    row["DOI"] = it.get("value", "") or ""; break

        names = hit.get("names") or {}
        if isinstance(names, dict):
            authors = names.get("authors", []) or []
            au_short, au_full = [], []
            for a in authors:
                if not isinstance(a, dict): continue
                short = a.get("wosStandard") or a.get("displayName") or ""
                full = a.get("displayName") or short
                if short: au_short.append(short)
                if full:  au_full.append(full)
            row["Authors"] = "; ".join(au_short)
            row["Author Full Names"] = "; ".join(au_full)

        types = hit.get("types") or []
        if isinstance(types, list) and types:
            row["Document Type"] = "; ".join(str(t) for t in types if t)

        cit = hit.get("citations") or []
        if isinstance(cit, list):
            for c in cit:
                if isinstance(c, dict) and (c.get("db") or "").upper() in ("WOS", "WOSCC"):
                    row["Times Cited, WoS Core"] = str(c.get("count", "") or "")
                    break
    except Exception:
        # Best effort — return whatever we got
        pass
    return row


def run_wos_api(queries_file: str, out_dir: str, api_key: str,
                state: TaskState,
                log: Callable[[str], None],
                progress: Callable[[int, int], None]) -> TaskReport:
    """Stage 2b entry point. WOS Starter API. Never raises."""
    task = TaskReport("WOS API (Starter)")
    task.set_meta("Queries file", queries_file)
    task.set_meta("Method", "WOS Starter API")
    out_p: Optional[Path] = None

    try:
        if not api_key:
            task.add_failure("API key is empty")
            log("ERROR: API key required for the API path."); task.finish("failed"); return task

        qp = Path(queries_file)
        if not qp.exists():
            task.add_failure(f"queries file not found: {qp}")
            task.finish("failed"); return task
        out_p = Path(out_dir); out_p.mkdir(parents=True, exist_ok=True)

        queries = parse_wos_queries(qp)
        log(f"Parsed {len(queries)} batch queries")
        task.set_total(len(queries))
        if not queries:
            task.finish(); return task
        progress(0, len(queries))

        headers = {"X-ApiKey": api_key, "Accept": "application/json"}
        all_rows: List[Dict] = []
        total = len(queries)
        n_records = 0

        for i, query in enumerate(queries, start=1):
            if not state.check_pause():
                log("Stopped by user."); task.finish("stopped"); return task

            log(f"[{i}/{total}] querying WOS Starter API...")
            try:
                params = {"db": "WOS", "q": query, "limit": 50, "page": 1}
                r = requests.get(WOS_API_URL, params=params, headers=headers, timeout=60)
                if r.status_code == 200:
                    data = r.json()
                    hits = data.get("hits") or []
                    log(f"  {len(hits)} records returned")
                    try:
                        out_json = out_p / f"wos_api_batch_{i:03d}.json"
                        out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
                        task.add_output(out_json)
                    except Exception as e:
                        task.log_error(f"Could not save JSON for batch {i}: {e}")
                    for hit in hits:
                        all_rows.append(_wos_api_to_row(hit))
                    n_records += len(hits)
                    task.add_success()
                elif r.status_code == 401:
                    log(f"  AUTH FAILED — check API key"); task.add_failure(f"batch {i}: 401 unauthorized")
                elif r.status_code == 429:
                    log("  rate limited, waiting 30s and retrying once...")
                    if not state.interruptible_sleep(30.0):
                        log("Stopped."); task.finish("stopped"); return task
                    try:
                        r2 = requests.get(WOS_API_URL, params=params, headers=headers, timeout=60)
                        if r2.status_code == 200:
                            data = r2.json()
                            hits = data.get("hits") or []
                            log(f"  {len(hits)} records (after retry)")
                            for hit in hits:
                                all_rows.append(_wos_api_to_row(hit))
                            n_records += len(hits); task.add_success()
                        else:
                            task.add_failure(f"batch {i}: retry HTTP {r2.status_code}")
                    except Exception as e:
                        task.add_failure(f"batch {i}: retry error {e}")
                else:
                    log(f"  HTTP {r.status_code}: {r.text[:200]}")
                    task.add_failure(f"batch {i}: HTTP {r.status_code}")
            except requests.RequestException as e:
                log(f"  network error: {e}"); task.add_failure(f"batch {i}: network {e}")
            except Exception as e:
                log(f"  ERROR: {e}"); task.add_failure(f"batch {i}: {e}")

            progress(i, total)
            if not state.interruptible_sleep(1.0):
                log("Stopped."); task.finish("stopped"); return task

        if all_rows:
            try:
                df = pd.DataFrame(all_rows).reindex(columns=WOS_FIELDS)
                # Dedupe only among rows with a non-empty DOI; keep DOI-less rows as-is.
                if "DOI" in df.columns:
                    df["_doi_norm"] = df["DOI"].apply(normalize_doi)
                    has = df["_doi_norm"] != ""
                    keep = pd.concat([
                        df[has].drop_duplicates(subset=["_doi_norm"], keep="first"),
                        df[~has],
                    ], ignore_index=True).drop(columns=["_doi_norm"])
                else:
                    keep = df
                combined = out_p / "wos_api_combined.xlsx"
                keep.to_excel(combined, index=False, engine="openpyxl")
                log(f"Wrote combined xlsx: {combined} ({len(keep)} unique records)")
                task.add_output(combined)
            except Exception as e:
                task.log_error(f"Could not write combined xlsx: {e}")

        task.set_meta("Records retrieved", n_records)
        task.finish()
        return task
    except Exception as e:
        tb = traceback.format_exc()
        task.add_failure(str(e)); task.log_error(tb)
        task.finish("failed")
        log(f"\nFATAL: {e}\n{tb}")
        return task
    finally:
        try: log("\n" + task.render())
        except Exception: pass
        if out_p:
            task.save(out_p)


# ============================================================================
# Stage 3 - Merge
# ============================================================================
def normalize_doi(doi: str) -> str:
    if not doi: return ""
    s = str(doi).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix):]; break
    return s.strip()


def load_wos_export(path: Path) -> pd.DataFrame:
    """Load .xlsx / .xls / tab-delimited .txt — tries several encodings."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str).fillna("")
    last_err = None
    for enc in ("utf-8-sig", "utf-16", "utf-8", "latin-1"):
        try:
            try:
                return pd.read_csv(path, sep="\t", encoding=enc, dtype=str,
                                   quoting=3, on_bad_lines="skip").fillna("")
            except TypeError:
                # pandas < 1.3 uses error_bad_lines
                return pd.read_csv(path, sep="\t", encoding=enc, dtype=str,
                                   quoting=3, error_bad_lines=False).fillna("")
        except (UnicodeError, UnicodeDecodeError) as e:
            last_err = e
    raise RuntimeError(f"Cannot read {path}: {last_err}")


def find_wos_files(folder: Path) -> List[Path]:
    if not folder.exists():
        return []
    files = []
    for ext in ("*.xlsx", "*.xls", "*.txt"):
        files.extend(folder.glob(ext))
    return sorted(files)


def run_merge(refs_xlsx: str, wos_folder: str, out_dir: str,
              state: TaskState, log: Callable[[str], None]) -> TaskReport:
    """Stage 3 entry point. Never raises."""
    task = TaskReport("Merge WOS")
    task.set_meta("References file", refs_xlsx)
    task.set_meta("WOS folder", wos_folder)
    out_p: Optional[Path] = None

    try:
        refs_path = Path(refs_xlsx)
        wos_p = Path(wos_folder) if wos_folder else None
        out_p = Path(out_dir); out_p.mkdir(parents=True, exist_ok=True)

        if not refs_path.exists():
            task.add_failure(f"references.xlsx not found: {refs_path}")
            log(f"ERROR: {refs_path} not found")
            task.finish("failed"); return task

        if not state.check_pause():
            task.finish("stopped"); return task

        log(f"Loading references: {refs_path}")
        try:
            refs_df = pd.read_excel(refs_path, dtype=str).fillna("")
        except Exception as e:
            task.add_failure(f"Cannot read references.xlsx: {e}")
            log(f"ERROR: {e}"); task.finish("failed"); return task

        refs_df["_doi_norm"] = (refs_df["DOI (Extracted)"].apply(normalize_doi)
                                if "DOI (Extracted)" in refs_df.columns else "")
        log(f"  {len(refs_df)} references loaded")
        task.set_meta("References loaded", len(refs_df))

        wos_frames = []
        if wos_p:
            files = find_wos_files(wos_p)
            log(f"Found {len(files)} WOS export files in {wos_p}")
            task.set_meta("WOS files found", len(files))
            for f in files:
                if not state.check_pause():
                    task.finish("stopped"); return task
                try:
                    df = load_wos_export(f)
                    log(f"  ok  {f.name}: {len(df)} rows")
                    wos_frames.append(df); task.add_success()
                except Exception as e:
                    log(f"  err {f.name}: {e}")
                    task.add_failure(f"{f.name}: {e}")

        matched_dois = set()
        if wos_frames:
            wos_df = pd.concat(wos_frames, ignore_index=True).fillna("")
            doi_col = next((c for c in wos_df.columns if c.strip().lower() == "doi"), None)
            if doi_col:
                wos_df["_doi_norm"] = wos_df[doi_col].apply(normalize_doi)
                before = len(wos_df)
                has = wos_df["_doi_norm"] != ""
                # Dedupe only rows with a DOI; preserve DOI-less rows
                wos_df = pd.concat([
                    wos_df[has].drop_duplicates(subset=["_doi_norm"], keep="first"),
                    wos_df[~has],
                ], ignore_index=True)
                log(f"WOS rows after dedup by DOI: {before} -> {len(wos_df)}")
                matched_dois = set(wos_df["_doi_norm"]) - {""}
            else:
                log("Warning: WOS files have no DOI column; DOI match not possible")
                task.log_error("No DOI column in WOS files; no matching possible")
                wos_df = pd.DataFrame()
        else:
            wos_df = pd.DataFrame()

        if not wos_df.empty:
            record_df = pd.DataFrame()
            for col in WOS_FIELDS:
                src = next((c for c in wos_df.columns
                            if c.strip().lower() == col.strip().lower()), None)
                record_df[col] = wos_df[src] if src else ""
        else:
            record_df = pd.DataFrame(columns=WOS_FIELDS)
        try:
            rec_path = out_p / "Record.csv"
            record_df.to_csv(rec_path, index=False, encoding="utf-8-sig")
            log(f"Wrote Record.csv ({len(record_df)} rows) -> {rec_path}")
            task.add_output(rec_path)
        except Exception as e:
            task.add_failure(f"Could not write Record.csv: {e}")
            log(f"ERROR writing Record.csv: {e}")

        has_match = refs_df["_doi_norm"].apply(lambda d: bool(d) and d in matched_dois)
        unrecorded = refs_df[~has_match].copy()
        field_src_map = {
            "Report": "Report", "Working Group": "Working Group",
            "Chapter": "Chapter", "Chapter title": "Chapter Title",
            "Authors": "Authors", "Article Title": "Article Title",
            "Publisher": "Publisher", "Year": "Year",
        }
        unrec_df = pd.DataFrame()
        for out_col, src_col in field_src_map.items():
            unrec_df[out_col] = unrecorded[src_col] if src_col in unrecorded.columns else ""
        try:
            unrec_path = out_p / "Unrecord.csv"
            unrec_df.to_csv(unrec_path, index=False, encoding="utf-8-sig")
            log(f"Wrote Unrecord.csv ({len(unrec_df)} rows) -> {unrec_path}")
            task.add_output(unrec_path)
        except Exception as e:
            task.add_failure(f"Could not write Unrecord.csv: {e}")
            log(f"ERROR writing Unrecord.csv: {e}")

        task.set_meta("Recorded", len(record_df))
        task.set_meta("Unrecorded", len(unrec_df))
        if len(refs_df) > 0:
            rate = 100 * (len(refs_df) - len(unrec_df)) / len(refs_df)
            task.set_meta("Coverage", f"{rate:.1f}%")
        task.finish()
        return task
    except Exception as e:
        tb = traceback.format_exc()
        task.add_failure(str(e)); task.log_error(tb)
        task.finish("failed")
        log(f"\nFATAL: {e}\n{tb}")
        return task
    finally:
        try: log("\n" + task.render())
        except Exception: pass
        if out_p:
            task.save(out_p)


# ============================================================================
# Help text (concise; full guide is in README.md)
# ============================================================================
HELP_TEXT = """\
IPCC References Toolkit — Quick Guide
=====================================

This is the in-app summary. See README.md for the full documentation.

WORKFLOW
  Stage 1  Extract PDF      runs locally, no API needed
  Stage 2  WOS lookup       either WOS Starter API (recommended) or browser
  Stage 3  Merge            Record.csv + Unrecord.csv

PAUSE / RESUME / STOP
  Each tab has Pause / Resume / Stop. Pause completes the current item then
  waits. Stop exits at the next safe point; already-written outputs are kept.

STAGE 1 — Extract PDF (Tab 1)
  - Pick the PDF and fill Report / Working Group / Email.
  - Click Run. Output goes to references.xlsx + wos_queries.txt.
  - If extraction fails for one or more chapters, the rest still run.
    A failed_chapters.csv and extraction_errors.log are written, and the
    final TaskReport breaks down what succeeded and what failed.

STAGE 2 — WOS lookup (Tab 2)
  The toolkit supports two paths:

  (a) API path (recommended): paste your WOS Starter API key in the red field.
      Endpoint:  api.clarivate.com/apis/wos-starter
      Apply for a key at https://developer.clarivate.com/apis/wos-starter
      Many institutions have access included in their WOS subscription.

  (b) Browser path: leave the API key empty. A Chromium window opens; you log
      in once manually; the toolkit walks every DO=(...) batch and downloads
      Excel files. Note: this is ToS-sensitive and requires your machine to
      stay on and connected. Use the API path whenever possible.

STAGE 3 — Merge (Tab 3)
  Combines references.xlsx with the WOS exports folder into Record.csv
  (full WOS schema) and Unrecord.csv (rows still missing). Safe to re-run
  as you add more WOS exports.

TIPS
  - Crossref cache (crossref_cache.json) makes re-runs free and fast.
  - Use Max refs per chapter = 20 for a quick test pass before the full run.
  - For scanned PDFs run `ocrmypdf` first.
"""


# ============================================================================
# GUI
# ============================================================================
class IpccToolkitGui:
    """
    Single-window app with 4 tabs. Each tab owns its own TaskState, log queue,
    and progress queue, so tabs do not interfere with each other (only one tab
    should be Run at a time in practice, but the design is safe if multiple
    are running concurrently).
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("1024x800")
        root.minsize(900, 680)

        # Per-tab state
        self.state_extract = TaskState()
        self.state_wos = TaskState()
        self.state_merge = TaskState()

        # Cross-thread queues
        self.q_extract = queue.Queue()
        self.q_wos = queue.Queue()
        self.q_merge = queue.Queue()
        self.q_progress = queue.Queue()  # tuples (tab_name, done, total)

        # Build footer FIRST so it stays at bottom across resizes
        self._build_footer()

        # Notebook
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.tab_extract = ttk.Frame(nb); nb.add(self.tab_extract, text="1. Extract PDF")
        self.tab_wos = ttk.Frame(nb);     nb.add(self.tab_wos,     text="2. WOS Lookup")
        self.tab_merge = ttk.Frame(nb);   nb.add(self.tab_merge,   text="3. Merge")
        self.tab_help = ttk.Frame(nb);    nb.add(self.tab_help,    text="? Help")

        self._build_extract_tab()
        self._build_wos_tab()
        self._build_merge_tab()
        self._build_help_tab()

        # Pollers
        self.root.after(120, self._poll_queues)
        self.root.after(220, self._poll_status)

        # Trap window close to make sure no zombie threads keep GUI hanging
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -------------------- Generic helpers --------------------
    def _file_row(self, parent, label, row, var, kind="file", filetypes=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(parent, textvariable=var, width=70).grid(row=row, column=1, padx=4, pady=4, sticky="we")
        cmd = (lambda: self._browse_file(var, filetypes)) if kind == "file" else (lambda: self._browse_dir(var))
        ttk.Button(parent, text="Browse...", command=cmd).grid(row=row, column=2, padx=4, pady=4)

    def _browse_file(self, var, filetypes):
        p = filedialog.askopenfilename(filetypes=filetypes or [("All files", "*.*")])
        if p: var.set(p)

    def _browse_dir(self, var):
        p = filedialog.askdirectory()
        if p: var.set(p)

    def _open_folder(self, path: str):
        if not path: return
        try:
            p = Path(path); p.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(p)
            elif sys.platform == "darwin":
                os.system(f'open "{p}"')
            else:
                os.system(f'xdg-open "{p}"')
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e))

    def _log_to(self, q: queue.Queue, msg: str):
        q.put(msg + "\n")

    def _make_log_widget(self, parent):
        w = scrolledtext.ScrolledText(parent, height=18, state="disabled", font=("Menlo", 10))
        w.pack(fill="both", expand=True, padx=8, pady=4)
        return w

    def _make_control_bar(self, parent, on_run, on_pause, on_resume, on_stop, open_dir_var=None):
        """Standard Run/Pause/Resume/Stop bar; returns dict of widgets including the bar itself."""
        bar = ttk.Frame(parent); bar.pack(fill="x", padx=8, pady=4)
        btns = {"bar": bar}
        btns["run"] = ttk.Button(bar, text="Run", command=on_run); btns["run"].pack(side="left", padx=2)
        btns["pause"] = ttk.Button(bar, text="Pause", command=on_pause, state="disabled"); btns["pause"].pack(side="left", padx=2)
        btns["resume"] = ttk.Button(bar, text="Resume", command=on_resume, state="disabled"); btns["resume"].pack(side="left", padx=2)
        btns["stop"] = ttk.Button(bar, text="Stop", command=on_stop, state="disabled"); btns["stop"].pack(side="left", padx=2)
        if open_dir_var is not None:
            ttk.Button(bar, text="Open output folder",
                       command=lambda: self._open_folder(open_dir_var.get())).pack(side="right", padx=2)
        btns["status"] = ttk.Label(bar, text="idle", width=14, anchor="e")
        btns["status"].pack(side="right", padx=8)
        return btns

    # -------------------- Tab 1: Extract --------------------
    def _build_extract_tab(self):
        frm = ttk.LabelFrame(self.tab_extract, text="Inputs")
        frm.pack(fill="x", padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        self.v_pdf = tk.StringVar()
        self.v_report = tk.StringVar(value="AR6")
        self.v_wg = tk.StringVar(value="WG2")
        self.v_email = tk.StringVar(value="your_email@example.com")
        self.v_out = tk.StringVar(value=str(Path.cwd() / "output"))
        self.v_maxrefs = tk.StringVar(value="0")
        self.v_chapters = tk.StringVar(value="")
        self.v_skip_cr = tk.BooleanVar(value=False)

        self._file_row(frm, "PDF file", 0, self.v_pdf, "file",
                       [("PDF files", "*.pdf"), ("All files", "*.*")])
        ttk.Label(frm, text="Report").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.v_report, width=20).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(frm, text="Working Group").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.v_wg, width=20).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(frm, text="Email (Crossref polite pool)").grid(row=3, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.v_email, width=40).grid(row=3, column=1, sticky="w", padx=4)
        self._file_row(frm, "Output folder", 4, self.v_out, "dir")
        ttk.Label(frm, text="Max refs per chapter (0=all)").grid(row=5, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.v_maxrefs, width=10).grid(row=5, column=1, sticky="w", padx=4)
        ttk.Label(frm, text="Chapters (e.g. 3,5,7)").grid(row=6, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.v_chapters, width=20).grid(row=6, column=1, sticky="w", padx=4)
        ttk.Checkbutton(frm, text="Skip Crossref (extract only, fast test)",
                        variable=self.v_skip_cr).grid(row=7, column=1, sticky="w", padx=4, pady=4)

        prog = ttk.Frame(self.tab_extract); prog.pack(fill="x", padx=8, pady=4)
        self.pb_extract = ttk.Progressbar(prog, mode="determinate")
        self.pb_extract.pack(side="left", fill="x", expand=True, padx=4)
        self.lbl_pb_extract = ttk.Label(prog, text="0/0", width=16); self.lbl_pb_extract.pack(side="left", padx=4)

        self.btns_extract = self._make_control_bar(
            self.tab_extract,
            on_run=self.on_run_extract,
            on_pause=lambda: self._pause_task(self.state_extract, self.btns_extract),
            on_resume=lambda: self._resume_task(self.state_extract, self.btns_extract),
            on_stop=lambda: self._stop_task(self.state_extract, self.btns_extract),
            open_dir_var=self.v_out,
        )

        ttk.Label(self.tab_extract, text="Log:").pack(anchor="w", padx=8)
        self.log_extract = self._make_log_widget(self.tab_extract)

    def on_run_extract(self):
        # Validate inputs
        if not self.v_pdf.get() or not Path(self.v_pdf.get()).exists():
            messagebox.showerror("Error", "Pick a valid PDF first"); return
        try:
            max_refs = int(self.v_maxrefs.get() or "0")
        except ValueError:
            messagebox.showerror("Error", "Max refs must be a number"); return

        self.pb_extract["value"] = 0
        self.state_extract.start()
        self._set_buttons_running(self.btns_extract)

        def progress(d, t): self.q_progress.put(("extract", d, t))

        def task():
            report = None
            try:
                report = run_extraction(
                    pdf_path=self.v_pdf.get(), report=self.v_report.get(),
                    wg=self.v_wg.get(), email=self.v_email.get(),
                    out_dir=self.v_out.get(), max_refs=max_refs,
                    chapters_filter=self.v_chapters.get(),
                    skip_crossref=self.v_skip_cr.get(),
                    state=self.state_extract,
                    log=lambda m: self._log_to(self.q_extract, m),
                    progress=progress,
                )
            except Exception as e:
                # Defensive: run_extraction shouldn't raise, but if it does...
                self._log_to(self.q_extract, f"\nUNEXPECTED ERROR: {e}")
                self._log_to(self.q_extract, traceback.format_exc())
            finally:
                self.state_extract.reset()
                self.root.after(0, lambda: self._set_buttons_idle(self.btns_extract))
                if report is not None:
                    self.root.after(0, lambda r=report: self._show_task_outcome(r))

        threading.Thread(target=task, daemon=True).start()

    # -------------------- Tab 2: WOS --------------------
    def _build_wos_tab(self):
        frm = ttk.LabelFrame(self.tab_wos, text="Inputs")
        frm.pack(fill="x", padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        self.v_wos_queries = tk.StringVar(value=str(Path.cwd() / "output" / "wos_queries.txt"))
        self.v_wos_out = tk.StringVar(value=str(Path.cwd() / "output" / "wos_exports"))
        self.v_wos_session = tk.StringVar(value=str(Path.cwd() / ".wos_session"))
        self.v_wos_delay = tk.StringVar(value="8")
        self.v_wos_api_key = tk.StringVar(value="")

        self._file_row(frm, "wos_queries.txt", 0, self.v_wos_queries, "file",
                       [("Text files", "*.txt"), ("All files", "*.*")])
        self._file_row(frm, "Output folder", 1, self.v_wos_out, "dir")
        self._file_row(frm, "Browser session folder", 2, self.v_wos_session, "dir")
        ttk.Label(frm, text="Delay between batches (s)").grid(row=3, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.v_wos_delay, width=10).grid(row=3, column=1, sticky="w", padx=4)

        # ===== Highlighted WOS API key row (RED label + RED border) =====
        api_lbl = tk.Label(frm, text="WOS API key   (★ RECOMMENDED ★)",
                           fg="#d32f2f", font=("TkDefaultFont", 10, "bold"))
        api_lbl.grid(row=4, column=0, sticky="e", padx=4, pady=4)
        api_entry = tk.Entry(
            frm, textvariable=self.v_wos_api_key, width=60, show="*",
            highlightthickness=2, highlightcolor="#d32f2f",
            highlightbackground="#d32f2f", relief="flat", bd=1,
        )
        api_entry.grid(row=4, column=1, sticky="we", padx=4, pady=4)
        tk.Label(
            frm,
            text=("If a key is provided, the Starter API is used "
                  "(faster, ToS-compliant, no browser needed).\n"
                  "If left empty, the toolkit falls back to browser automation "
                  "via Playwright (requires you to keep the machine on)."),
            fg="#666", justify="left",
        ).grid(row=5, column=1, sticky="w", padx=4)
        tk.Label(
            frm,
            text="Get a Starter API key:  https://developer.clarivate.com/apis/wos-starter",
            fg="#1a73e8", cursor="hand2",
        ).grid(row=6, column=1, sticky="w", padx=4)
        frm.grid_rowconfigure(4, minsize=32)

        # Login prompt banner (shown only when worker is waiting for login)
        self.lbl_login_prompt = ttk.Label(
            self.tab_wos, text="", foreground="#b06c00",
            font=("TkDefaultFont", 10, "bold"))
        self.lbl_login_prompt.pack(fill="x", padx=8)

        prog = ttk.Frame(self.tab_wos); prog.pack(fill="x", padx=8, pady=4)
        self.pb_wos = ttk.Progressbar(prog, mode="determinate")
        self.pb_wos.pack(side="left", fill="x", expand=True, padx=4)
        self.lbl_pb_wos = ttk.Label(prog, text="0/0", width=16); self.lbl_pb_wos.pack(side="left", padx=4)

        self.btns_wos = self._make_control_bar(
            self.tab_wos,
            on_run=self.on_run_wos,
            on_pause=lambda: self._pause_task(self.state_wos, self.btns_wos),
            on_resume=lambda: self._resume_task(self.state_wos, self.btns_wos),
            on_stop=lambda: self._stop_task(self.state_wos, self.btns_wos),
            open_dir_var=self.v_wos_out,
        )
        # Add Continue-after-login button to the same bar (explicit reference)
        self.btn_continue = ttk.Button(
            self.btns_wos["bar"], text="Continue after login",
            command=self.on_continue_login, state="disabled")
        self.btn_continue.pack(side="left", padx=8)

        if not PLAYWRIGHT_OK:
            ttk.Label(self.tab_wos,
                      text=("Playwright is not installed. The browser path will not work. "
                            "Install with:  pip install playwright  &&  playwright install chromium"),
                      foreground="red").pack(anchor="w", padx=8, pady=4)

        ttk.Label(self.tab_wos, text="Log:").pack(anchor="w", padx=8)
        self.log_wos = self._make_log_widget(self.tab_wos)

    def on_run_wos(self):
        if not Path(self.v_wos_queries.get()).exists():
            messagebox.showerror("Error", "wos_queries.txt not found. Run Stage 1 first."); return
        try:
            delay = float(self.v_wos_delay.get() or "8")
        except ValueError:
            messagebox.showerror("Error", "Delay must be a number"); return

        api_key = self.v_wos_api_key.get().strip()
        use_api = bool(api_key)

        if not use_api and not PLAYWRIGHT_OK:
            messagebox.showerror(
                "Error",
                "No API key provided and Playwright is not installed.\n\n"
                "Either:\n"
                "  - Paste a WOS Starter API key in the red field, or\n"
                "  - Install Playwright:  pip install playwright && playwright install chromium")
            return

        self.pb_wos["value"] = 0
        self.state_wos.start()
        self._set_buttons_running(self.btns_wos)
        self.btn_continue.config(state="disabled")

        def progress(d, t): self.q_progress.put(("wos", d, t))

        def task():
            report = None
            try:
                if use_api:
                    report = run_wos_api(
                        queries_file=self.v_wos_queries.get(),
                        out_dir=self.v_wos_out.get(),
                        api_key=api_key,
                        state=self.state_wos,
                        log=lambda m: self._log_to(self.q_wos, m),
                        progress=progress,
                    )
                else:
                    report = run_wos_auto(
                        queries_file=self.v_wos_queries.get(),
                        out_dir=self.v_wos_out.get(),
                        session_dir=self.v_wos_session.get(),
                        delay=delay,
                        state=self.state_wos,
                        log=lambda m: self._log_to(self.q_wos, m),
                        progress=progress,
                    )
            except Exception as e:
                self._log_to(self.q_wos, f"\nUNEXPECTED ERROR: {e}")
                self._log_to(self.q_wos, traceback.format_exc())
            finally:
                self.state_wos.reset()
                def cleanup():
                    self._set_buttons_idle(self.btns_wos)
                    self.btn_continue.config(state="disabled")
                    self.lbl_login_prompt.config(text="")
                self.root.after(0, cleanup)
                if report is not None:
                    self.root.after(0, lambda r=report: self._show_task_outcome(r))

        threading.Thread(target=task, daemon=True).start()

    def on_continue_login(self):
        self.state_wos.user_continue()
        self.btn_continue.config(state="disabled")
        self.lbl_login_prompt.config(text="")

    # -------------------- Tab 3: Merge --------------------
    def _build_merge_tab(self):
        frm = ttk.LabelFrame(self.tab_merge, text="Inputs")
        frm.pack(fill="x", padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        self.v_refs = tk.StringVar(value=str(Path.cwd() / "output" / "references.xlsx"))
        self.v_wos_folder = tk.StringVar(value=str(Path.cwd() / "output" / "wos_exports"))
        self.v_merge_out = tk.StringVar(value=str(Path.cwd() / "output"))

        self._file_row(frm, "references.xlsx", 0, self.v_refs, "file",
                       [("Excel files", "*.xlsx *.xls"), ("All files", "*.*")])
        self._file_row(frm, "WOS exports folder", 1, self.v_wos_folder, "dir")
        self._file_row(frm, "Output folder", 2, self.v_merge_out, "dir")

        self.btns_merge = self._make_control_bar(
            self.tab_merge,
            on_run=self.on_run_merge,
            on_pause=lambda: self._pause_task(self.state_merge, self.btns_merge),
            on_resume=lambda: self._resume_task(self.state_merge, self.btns_merge),
            on_stop=lambda: self._stop_task(self.state_merge, self.btns_merge),
            open_dir_var=self.v_merge_out,
        )
        ttk.Button(self.btns_merge["bar"], text="Open WOS folder",
                   command=lambda: self._open_folder(self.v_wos_folder.get())).pack(side="left", padx=4)

        ttk.Label(self.tab_merge,
                  text="Drop every xlsx exported from WOS into the folder above, then click Run.",
                  foreground="gray").pack(anchor="w", padx=8)

        ttk.Label(self.tab_merge, text="Log:").pack(anchor="w", padx=8, pady=(8, 0))
        self.log_merge = self._make_log_widget(self.tab_merge)

    def on_run_merge(self):
        if not Path(self.v_refs.get()).exists():
            messagebox.showerror("Error", "references.xlsx not found. Run Stage 1 first."); return

        self.state_merge.start()
        self._set_buttons_running(self.btns_merge)

        def task():
            report = None
            try:
                report = run_merge(
                    refs_xlsx=self.v_refs.get(),
                    wos_folder=self.v_wos_folder.get(),
                    out_dir=self.v_merge_out.get(),
                    state=self.state_merge,
                    log=lambda m: self._log_to(self.q_merge, m),
                )
            except Exception as e:
                self._log_to(self.q_merge, f"\nUNEXPECTED ERROR: {e}")
                self._log_to(self.q_merge, traceback.format_exc())
            finally:
                self.state_merge.reset()
                self.root.after(0, lambda: self._set_buttons_idle(self.btns_merge))
                if report is not None:
                    self.root.after(0, lambda r=report: self._show_task_outcome(r))

        threading.Thread(target=task, daemon=True).start()

    # -------------------- Tab 4: Help --------------------
    def _build_help_tab(self):
        w = scrolledtext.ScrolledText(self.tab_help, wrap="word", font=("Menlo", 11))
        w.insert("1.0", HELP_TEXT)
        w.configure(state="disabled")
        w.pack(fill="both", expand=True, padx=8, pady=8)

    # -------------------- Footer (author + contact link) --------------------
    def _build_footer(self):
        """Compact footer at the bottom of the main window. Stays visible across all tabs."""
        footer = ttk.Frame(self.root)
        footer.pack(side="bottom", fill="x", padx=8, pady=(0, 6))

        sep = ttk.Separator(footer, orient="horizontal")
        sep.pack(fill="x", pady=(0, 4))

        bar = ttk.Frame(footer)
        bar.pack(fill="x")

        # Left: author tag
        ttk.Label(bar, text=AUTHOR_NAME_CN, foreground="#666").pack(side="left", padx=(2, 6))
        ttk.Label(bar, text="·", foreground="#999").pack(side="left")

        # Right after: clickable contact link
        link = tk.Label(
            bar, text=AUTHOR_CONTACT_LABEL,
            fg="#1a73e8", cursor="hand2",
            font=("TkDefaultFont", 10, "underline"),
        )
        link.pack(side="left", padx=(6, 2))
        link.bind("<Button-1>", lambda e: self._open_homepage())
        # Tooltip-ish: also show URL in muted text on hover
        link.bind("<Enter>", lambda e: link.config(fg="#0b5cd0"))
        link.bind("<Leave>", lambda e: link.config(fg="#1a73e8"))

        # Right: small version / app tag
        ttk.Label(bar, text=APP_TITLE, foreground="#999").pack(side="right", padx=2)

    def _open_homepage(self):
        try:
            webbrowser.open_new(AUTHOR_HOMEPAGE)
        except Exception as e:
            messagebox.showerror("Could not open browser", str(e))

    # -------------------- Button state helpers --------------------
    def _set_buttons_running(self, btns):
        btns["run"].config(state="disabled")
        btns["pause"].config(state="normal")
        btns["resume"].config(state="disabled")
        btns["stop"].config(state="normal")
        btns["status"].config(text="running")

    def _set_buttons_idle(self, btns):
        btns["run"].config(state="normal")
        btns["pause"].config(state="disabled")
        btns["resume"].config(state="disabled")
        btns["stop"].config(state="disabled")
        btns["status"].config(text="idle")

    def _pause_task(self, state, btns):
        state.pause()
        btns["pause"].config(state="disabled")
        btns["resume"].config(state="normal")
        btns["status"].config(text="paused")

    def _resume_task(self, state, btns):
        state.resume()
        btns["pause"].config(state="normal")
        btns["resume"].config(state="disabled")
        btns["status"].config(text="running")

    def _stop_task(self, state, btns):
        state.stop()
        btns["status"].config(text="stopping")

    # -------------------- Queue polling (UI thread) --------------------
    def _poll_queues(self):
        for q, w in ((self.q_extract, self.log_extract),
                     (self.q_wos, self.log_wos),
                     (self.q_merge, self.log_merge)):
            try:
                while True:
                    msg = q.get_nowait()
                    w.configure(state="normal")
                    w.insert("end", msg)
                    w.see("end")
                    w.configure(state="disabled")
            except queue.Empty:
                pass
        try:
            while True:
                tab, done, total = self.q_progress.get_nowait()
                if tab == "extract":
                    pb, lbl = self.pb_extract, self.lbl_pb_extract
                elif tab == "wos":
                    pb, lbl = self.pb_wos, self.lbl_pb_wos
                else:
                    continue
                if total > 0:
                    pct = 100 * done / total
                    pb["value"] = pct
                    lbl.config(text=f"{done}/{total} ({pct:.1f}%)")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queues)

    def _poll_status(self):
        # WOS login prompt
        if self.state_wos.status == "waiting_user":
            prompt = self.state_wos.prompt or "Waiting for you to log in..."
            self.lbl_login_prompt.config(text=prompt)
            if str(self.btn_continue["state"]) != "normal":
                self.btn_continue.config(state="normal")
        else:
            if self.lbl_login_prompt["text"]:
                self.lbl_login_prompt.config(text="")

        # Sync status labels
        for state, btns in ((self.state_extract, self.btns_extract),
                            (self.state_wos, self.btns_wos),
                            (self.state_merge, self.btns_merge)):
            s = state.status
            if s in ("running", "paused", "waiting_user", "idle"):
                if s != btns["status"]["text"]:
                    btns["status"].config(text=s)
        self.root.after(250, self._poll_status)

    # -------------------- Outcome popups --------------------
    def _show_task_outcome(self, report: TaskReport):
        """Show a friendly result popup (called on the UI thread)."""
        try:
            title = f"{report.name} — {report.status}"
            summary = f"Status: {report.status}\nElapsed: {report.elapsed_str()}"
            if report.success_count + report.failure_count > 0:
                summary += f"\nSucceeded: {report.success_count}"
                if report.failure_count:
                    summary += f"\nFailed:    {report.failure_count}"
                if report.success_count + report.failure_count > 0:
                    summary += f"\nSuccess rate: {report.success_rate():.1f}%"
            if report.failed_extractions:
                summary += f"\n\n{len(report.failed_extractions)} chapter(s) failed."
                summary += "\nSee failed_chapters.csv and extraction_errors.log."
            if report.outputs:
                summary += "\n\nOutputs:\n  " + "\n  ".join(report.outputs[:6])
                if len(report.outputs) > 6:
                    summary += f"\n  ... and {len(report.outputs) - 6} more"
            if report.status == "failed":
                messagebox.showerror(title, summary)
            elif report.status == "completed_with_errors":
                messagebox.showwarning(title, summary)
            elif report.status == "stopped":
                messagebox.showinfo(title, summary)
            else:
                messagebox.showinfo(title, summary)
        except Exception:
            pass

    # -------------------- Window close --------------------
    def _on_close(self):
        # Best-effort: signal all tasks to stop, then close
        for s in (self.state_extract, self.state_wos, self.state_merge):
            try: s.stop()
            except Exception: pass
        # Daemon threads die with the process
        self.root.after(120, self.root.destroy)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        names = style.theme_names()
        if "aqua" in names:
            style.theme_use("aqua")
        elif "clam" in names:
            style.theme_use("clam")
    except Exception:
        pass
    IpccToolkitGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
