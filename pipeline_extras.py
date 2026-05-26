#!/usr/bin/env python3
"""
IPCC References Toolkit — Stages 4-7 (Post-WoS pipeline)
==========================================================

This module continues the pipeline from where ipcc_refs_gui.py leaves off:
once you have a Record.csv (output of Stage 3), this script takes you all
the way to a literature-scale bibliometric analysis using LLMs.

Pipeline:
    Stage 4   acquire     PDFs via OA sources (Unpaywall / Crossref / OpenAlex / arXiv)
    Stage 5   markdown    PDF -> Markdown via MarkItDown (with fallbacks)
    Stage 6   extract     Schema-driven structured extraction via Claude API
    Stage 7   analyze     Bibliometric aggregation + HTML report

Design contract (matches Stages 1-3):
    - Every runner returns a TaskReport. Runners never raise.
    - Each item is processed independently; one failure does not abort the run.
    - All results are cached on disk and runs are idempotent / resumable.

Usage:
    python pipeline_extras.py acquire  --records output/Record.csv --out output/pdfs   --email you@example.com
    python pipeline_extras.py markdown --pdfs    output/pdfs       --out output/markdown
    python pipeline_extras.py extract  --markdown output/markdown   --out output/extracted --api-key sk-ant-...
    python pipeline_extras.py analyze  --records output/Record.csv --extracted output/extracted --out output/analysis

LEGAL / ETHICAL:
    - Only OA / preprint sources are queried. Sci-Hub or paywall-circumvention
      is intentionally NOT supported.
    - You are responsible for respecting publishers' Text and Data Mining
      (TDM) policies when running this on records covered by institutional
      subscriptions.

Author: Jiacheng Zheng  ·  https://karcen.github.io/zhengjiacheng.github.io/
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pandas as pd
import requests


# ============================================================================
# Constants
# ============================================================================
UNPAYWALL_URL = "https://api.unpaywall.org/v2/{doi}"
CROSSREF_URL = "https://api.crossref.org/works/{doi}"
OPENALEX_URL = "https://api.openalex.org/works/doi:{doi}"
ARXIV_API = "http://export.arxiv.org/api/query"

PDF_MAGIC = b"%PDF"
DEFAULT_HTTP_TIMEOUT = 30
DEFAULT_DOWNLOAD_TIMEOUT = 120
RATE_LIMIT_SLEEP = 0.5     # base seconds between requests to the same provider
DOWNLOAD_RETRY = 2

DEFAULT_LLM_MODEL = "claude-sonnet-4-6"  # balanced default
LLM_MAX_INPUT_CHARS = 180_000            # ~45k tokens; safe for any modern model
LLM_MAX_OUTPUT_TOKENS = 4096

AUTHOR_NAME = "Jiacheng Zheng"
AUTHOR_HOMEPAGE = "https://karcen.github.io/zhengjiacheng.github.io/"


# ============================================================================
# Lightweight task coordination (mirrors ipcc_refs_gui.py - no tkinter dep)
# ============================================================================
class TaskState:
    """Thread-safe pause/resume/stop coordination. Identical contract to gui module."""

    def __init__(self):
        self.pause_event = threading.Event(); self.pause_event.set()
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._status = "idle"

    @property
    def status(self) -> str:
        with self._lock: return self._status

    def _set_status(self, s: str):
        with self._lock: self._status = s

    def check_pause(self) -> bool:
        """Block while paused. Returns False if Stop requested."""
        while not self.pause_event.is_set():
            if self.stop_event.is_set(): return False
            time.sleep(0.1)
        return not self.stop_event.is_set()

    def interruptible_sleep(self, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set(): return False
            if not self.check_pause(): return False
            time.sleep(min(0.3, max(0.0, end - time.time())))
        return True

    def pause(self):  self.pause_event.clear(); self._set_status("paused")
    def resume(self): self.pause_event.set();   self._set_status("running")
    def stop(self):   self.stop_event.set();    self.pause_event.set()

    def reset(self):
        self.pause_event.set(); self.stop_event.clear(); self._set_status("idle")

    def start(self):
        self.reset(); self._set_status("running")


class TaskReport:
    """Structured per-run report with per-item failure tracking."""
    MAX_ERRORS = 1000

    def __init__(self, name: str):
        self.name = name
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.status = "running"
        self.total = 0
        self.success_count = 0
        self.failure_count = 0
        self.skipped_count = 0
        self.outputs: List[str] = []
        self.errors: List[str] = []
        self.metadata: Dict[str, str] = {}
        self.failed_items: List[Dict[str, str]] = []   # {item_id, error, traceback}
        self.error_log: List[str] = []

    def set_total(self, n: int):       self.total = int(n)
    def add_success(self, n: int = 1): self.success_count += n
    def add_skipped(self, n: int = 1): self.skipped_count += n
    def add_output(self, p):           self.outputs.append(str(p))
    def set_meta(self, k: str, v):     self.metadata[k] = str(v)

    def add_failure(self, msg: str = ""):
        self.failure_count += 1
        if msg and len(self.errors) < self.MAX_ERRORS:
            self.errors.append(msg)

    def add_item_failure(self, item_id: str, error: str, tb: str = ""):
        """Record a per-item failure with traceback."""
        if len(self.failed_items) < self.MAX_ERRORS:
            self.failed_items.append({
                "item_id": str(item_id)[:300],
                "error": str(error)[:600],
                "traceback": str(tb)[:8000],
            })
        self.failure_count += 1

    def log_error(self, msg: str):
        if len(self.error_log) < self.MAX_ERRORS:
            self.error_log.append(str(msg)[:500])

    def finish(self, status: Optional[str] = None):
        self.end_time = time.time()
        if status: self.status = status
        elif self.failure_count > 0 or self.failed_items:
            self.status = "completed_with_errors"
        else:
            self.status = "completed"

    def elapsed_str(self) -> str:
        end = self.end_time or time.time()
        secs = max(0.0, end - self.start_time)
        if secs < 60: return f"{secs:.1f}s"
        m, s = divmod(secs, 60)
        if m < 60: return f"{int(m)}m {int(s)}s"
        h, m = divmod(m, 60)
        return f"{int(h)}h {int(m)}m {int(s)}s"

    def success_rate(self) -> float:
        att = self.success_count + self.failure_count
        return 100 * self.success_count / att if att else 0.0

    def render(self) -> str:
        lines = [
            "", "=" * 68,
            f"  Task Report — {self.name}",
            "=" * 68,
            f"  Status        : {self.status}",
            f"  Elapsed time  : {self.elapsed_str()}",
        ]
        if self.total: lines.append(f"  Total items   : {self.total}")
        if self.success_count or self.failure_count or self.skipped_count:
            lines.append(f"  Succeeded     : {self.success_count}")
            if self.failure_count: lines.append(f"  Failed        : {self.failure_count}")
            if self.skipped_count: lines.append(f"  Skipped       : {self.skipped_count}")
            if self.success_count + self.failure_count > 0:
                lines.append(f"  Success rate  : {self.success_rate():.1f}%")
        if self.metadata:
            lines.append("  " + "-" * 54)
            for k, v in self.metadata.items():
                lines.append(f"  {k:<18}: {v}")
        if self.failed_items:
            lines.append("  " + "-" * 54)
            shown = min(10, len(self.failed_items))
            lines.append(f"  Failed items       : ({len(self.failed_items)} total, showing {shown})")
            for f in self.failed_items[:shown]:
                lines.append(f"      {f['item_id'][:60]}  ->  {f['error'][:100]}")
        if self.error_log:
            lines.append("  " + "-" * 54)
            shown = min(5, len(self.error_log))
            lines.append(f"  Warnings           : ({len(self.error_log)} total, showing {shown})")
            for e in self.error_log[:shown]:
                lines.append(f"      - {e[:160]}")
        if self.outputs:
            lines.append("  " + "-" * 54)
            lines.append("  Output files       :")
            for o in self.outputs[:10]:
                lines.append(f"      - {o}")
            if len(self.outputs) > 10:
                lines.append(f"      ... and {len(self.outputs) - 10} more")
        lines.append("=" * 68)
        return "\n".join(lines)

    def save(self, out_dir: Path) -> Optional[Path]:
        try:
            out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.name).strip("_")
            path = out_dir / f"task_report_{safe}_{ts}.txt"
            path.write_text(self.render(), encoding="utf-8")
            # Also persist failed_items as CSV if present
            if self.failed_items:
                try:
                    pd.DataFrame(self.failed_items).to_csv(
                        out_dir / f"failed_items_{safe}_{ts}.csv",
                        index=False, encoding="utf-8-sig")
                except Exception:
                    pass
            return path
        except Exception:
            return None


# ============================================================================
# Helpers
# ============================================================================
def normalize_doi(doi: str) -> str:
    """Normalize a DOI string to canonical lowercase form, no URL prefix."""
    if not doi: return ""
    s = str(doi).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix):]; break
    return s.strip()


def doi_to_filename(doi: str) -> str:
    """Map a DOI to a filesystem-safe filename stem."""
    s = normalize_doi(doi)
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s[:200] if s else "no_doi"


def looks_like_pdf(path: Path) -> bool:
    """Quick magic-byte check that a file is actually a PDF."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return head.startswith(PDF_MAGIC)
    except Exception:
        return False


def safe_get(d: dict, *keys, default=None):
    """Walk nested dicts/lists safely."""
    cur = d
    for k in keys:
        if cur is None: return default
        if isinstance(k, int):
            try: cur = cur[k]
            except (IndexError, TypeError): return default
        else:
            if not isinstance(cur, dict): return default
            cur = cur.get(k)
    return cur if cur is not None else default


# ============================================================================
# Stage 4 — PDF acquisition (OA sources only; never crashes the run)
# ============================================================================
@dataclass
class AcquisitionResult:
    """Result of trying to find a PDF for one record."""
    doi: str
    status: str = "not_found"  # found / downloaded / failed / not_found / skipped
    url: str = ""
    source: str = ""           # unpaywall / crossref_link / openalex / arxiv
    pdf_path: str = ""
    error: str = ""


def _request_with_retry(method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """HTTP request with one retry on 429/5xx. Returns Response or None."""
    for attempt in range(2):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return r
        except requests.RequestException:
            time.sleep(1 + attempt)
    return None


def find_pdf_unpaywall(doi: str, email: str) -> Optional[Tuple[str, str]]:
    """Returns (url, 'unpaywall') if an OA PDF URL is available, else None."""
    if not doi or not email: return None
    url = UNPAYWALL_URL.format(doi=quote_plus(doi))
    r = _request_with_retry("GET", url,
                            params={"email": email},
                            timeout=DEFAULT_HTTP_TIMEOUT)
    if not r or r.status_code != 200: return None
    try:
        data = r.json()
    except Exception:
        return None
    # Prefer best_oa_location, then any oa_location with a pdf url
    candidates = []
    best = data.get("best_oa_location") or {}
    if isinstance(best, dict) and best.get("url_for_pdf"):
        candidates.append(best["url_for_pdf"])
    for loc in data.get("oa_locations") or []:
        if isinstance(loc, dict) and loc.get("url_for_pdf"):
            candidates.append(loc["url_for_pdf"])
    # Fallback: landing pages (often have an embedded PDF link, but we
    # only return ones that look like a direct PDF)
    seen = set()
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            return (c, "unpaywall")
    return None


def find_pdf_crossref(doi: str) -> Optional[Tuple[str, str]]:
    """Crossref's `link` field sometimes has direct PDF URLs from publishers."""
    if not doi: return None
    url = CROSSREF_URL.format(doi=quote_plus(doi))
    r = _request_with_retry("GET", url, timeout=DEFAULT_HTTP_TIMEOUT)
    if not r or r.status_code != 200: return None
    try:
        msg = r.json().get("message", {})
    except Exception:
        return None
    links = msg.get("link") or []
    # Prefer application/pdf with intended-application "similarity-checking"
    # or "text-mining" (these tend to be open) over "vor" (version of record).
    for link in links:
        if not isinstance(link, dict): continue
        ct = (link.get("content-type") or "").lower()
        ia = (link.get("intended-application") or "").lower()
        if "pdf" in ct and ia in ("text-mining", "similarity-checking"):
            return (link.get("URL", ""), "crossref_link")
    for link in links:
        if isinstance(link, dict) and "pdf" in (link.get("content-type") or "").lower():
            return (link.get("URL", ""), "crossref_link")
    return None


def find_pdf_openalex(doi: str) -> Optional[Tuple[str, str]]:
    """OpenAlex aggregates OA locations from multiple sources."""
    if not doi: return None
    url = OPENALEX_URL.format(doi=quote_plus(doi))
    r = _request_with_retry("GET", url, timeout=DEFAULT_HTTP_TIMEOUT)
    if not r or r.status_code != 200: return None
    try:
        data = r.json()
    except Exception:
        return None
    # primary_location -> open_access -> oa_url, plus locations[]
    primary = data.get("primary_location") or {}
    if isinstance(primary, dict):
        pdf_url = primary.get("pdf_url")
        if pdf_url: return (pdf_url, "openalex")
    for loc in data.get("locations") or []:
        if isinstance(loc, dict) and loc.get("pdf_url"):
            return (loc["pdf_url"], "openalex")
    oa = data.get("open_access") or {}
    if isinstance(oa, dict) and oa.get("oa_url") and oa.get("is_oa"):
        return (oa["oa_url"], "openalex")
    return None


def find_pdf_arxiv(doi: str, title: str = "") -> Optional[Tuple[str, str]]:
    """Search arXiv by DOI; some IPCC-cited papers have arXiv preprints."""
    query_parts = []
    if doi: query_parts.append(f"doi:{doi}")
    if not query_parts and title:
        # very rough title fallback
        clean_title = re.sub(r"[^A-Za-z0-9 ]", " ", title)[:120]
        if len(clean_title.strip()) < 10: return None
        query_parts.append(f'ti:"{clean_title.strip()}"')
    if not query_parts: return None

    r = _request_with_retry("GET", ARXIV_API,
                            params={"search_query": " AND ".join(query_parts),
                                    "max_results": 1},
                            timeout=DEFAULT_HTTP_TIMEOUT)
    if not r or r.status_code != 200: return None
    # arXiv returns Atom XML; do a minimal parse
    text = r.text or ""
    m = re.search(r'<link[^>]+title="pdf"[^>]+href="([^"]+)"', text)
    if m:
        return (m.group(1), "arxiv")
    return None


def download_pdf(url: str, dest_path: Path, timeout: int = DEFAULT_DOWNLOAD_TIMEOUT) -> Tuple[bool, str]:
    """
    Download a PDF to dest_path. Returns (success, error_message).

    Verifies the resulting file actually starts with %PDF; if not, deletes it
    and returns failure (this catches HTML 'access denied' pages disguised
    as application/pdf and similar).
    """
    if not url:
        return False, "empty url"
    headers = {
        "User-Agent": f"IPCC-Refs-Toolkit/1.0 (mailto:research@example.com)",
        "Accept": "application/pdf,*/*;q=0.8",
    }
    try:
        with requests.get(url, headers=headers, timeout=timeout,
                          stream=True, allow_redirects=True) as r:
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest_path.with_suffix(dest_path.suffix + ".part")
            written = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        if written > 100 * 1024 * 1024:  # 100 MB safety cap
                            f.close()
                            try: tmp.unlink()
                            except Exception: pass
                            return False, "file too large (>100MB)"
            # Verify
            if not looks_like_pdf(tmp):
                try: tmp.unlink()
                except Exception: pass
                return False, "downloaded file is not a PDF"
            tmp.rename(dest_path)
            return True, ""
    except requests.RequestException as e:
        return False, f"network: {e}"
    except Exception as e:
        return False, f"{e}"


def acquire_one_pdf(doi: str, title: str, email: str, out_dir: Path) -> AcquisitionResult:
    """Try each OA source in priority order. Returns AcquisitionResult."""
    res = AcquisitionResult(doi=doi)
    doi_norm = normalize_doi(doi)
    if not doi_norm and not title:
        res.status = "skipped"; res.error = "no DOI and no title"
        return res

    fname = doi_to_filename(doi_norm) + ".pdf"
    target = out_dir / fname

    if target.exists() and looks_like_pdf(target):
        res.status = "skipped"
        res.pdf_path = str(target)
        res.error = "already exists"
        return res

    # Try sources in priority order
    for finder in (
        lambda: find_pdf_unpaywall(doi_norm, email),
        lambda: find_pdf_crossref(doi_norm),
        lambda: find_pdf_openalex(doi_norm),
        lambda: find_pdf_arxiv(doi_norm, title),
    ):
        try:
            found = finder()
        except Exception as e:
            res.error = (res.error + f" | {e}").strip(" |")
            continue
        if found:
            url, source = found
            res.url = url; res.source = source
            ok, err = download_pdf(url, target)
            if ok:
                res.status = "downloaded"
                res.pdf_path = str(target)
                return res
            else:
                res.error = (res.error + f" | {source}: {err}").strip(" |")
        time.sleep(RATE_LIMIT_SLEEP)

    if not res.status or res.status == "not_found":
        res.status = "not_found" if not res.error else "failed"
    return res


def run_acquire_pdfs(record_csv: str, out_dir: str, email: str,
                     state: Optional[TaskState] = None,
                     log: Callable[[str], None] = print,
                     progress: Callable[[int, int], None] = lambda d, t: None,
                     resume: bool = True) -> TaskReport:
    """Stage 4 entry point. Never raises."""
    task = TaskReport("Acquire PDFs")
    task.set_meta("Records file", record_csv)
    task.set_meta("Email", email)
    state = state or TaskState(); state.start()
    out_dir_p: Optional[Path] = None

    try:
        recs = Path(record_csv)
        if not recs.exists():
            task.add_failure(f"Record.csv not found: {recs}")
            log(f"ERROR: {recs} not found")
            task.finish("failed"); return task
        if not email or "@" not in email:
            task.add_failure("Email required (used by Unpaywall API)")
            log("ERROR: pass --email; Unpaywall requires it for the polite pool")
            task.finish("failed"); return task

        out_dir_p = Path(out_dir); out_dir_p.mkdir(parents=True, exist_ok=True)
        pdfs_dir = out_dir_p / "pdfs"; pdfs_dir.mkdir(exist_ok=True)
        index_path = out_dir_p / "pdf_index.csv"

        try:
            df = pd.read_csv(recs, dtype=str).fillna("")
        except Exception as e:
            task.add_failure(f"Cannot read Record.csv: {e}")
            log(f"ERROR: {e}"); task.finish("failed"); return task

        doi_col = next((c for c in df.columns if c.strip().lower() == "doi"), None)
        title_col = next((c for c in df.columns if c.strip().lower() in
                          ("article title", "title")), None)
        if not doi_col:
            task.log_error("No DOI column; will rely on title-based arXiv lookup only")
        log(f"Loaded {len(df)} records (DOI col='{doi_col}', title col='{title_col}')")

        # Resume support: read existing index if present
        existing: Dict[str, dict] = {}
        if resume and index_path.exists():
            try:
                old = pd.read_csv(index_path, dtype=str).fillna("")
                for _, row in old.iterrows():
                    existing[normalize_doi(row.get("doi", ""))] = row.to_dict()
                log(f"Resume: {len(existing)} previously-tried records")
            except Exception:
                pass

        results: List[AcquisitionResult] = []
        task.set_total(len(df))
        progress(0, len(df))

        for i, (_, row) in enumerate(df.iterrows(), start=1):
            if not state.check_pause():
                log("Stopped by user."); task.finish("stopped"); break
            doi = row[doi_col] if doi_col else ""
            title = row[title_col] if title_col else ""
            doi_norm = normalize_doi(doi)

            # Skip if previously downloaded successfully
            prev = existing.get(doi_norm)
            if resume and prev and prev.get("status") == "downloaded":
                p = Path(prev.get("pdf_path", ""))
                if p.exists() and looks_like_pdf(p):
                    res = AcquisitionResult(
                        doi=doi_norm, status="skipped",
                        pdf_path=str(p), source=prev.get("source", ""),
                        error="resume: already downloaded",
                    )
                    results.append(res); task.add_skipped()
                    progress(i, len(df)); continue

            try:
                res = acquire_one_pdf(doi, title, email, pdfs_dir)
                results.append(res)
                if res.status == "downloaded":
                    task.add_success()
                    log(f"[{i}/{len(df)}] OK   {res.source:12s} {Path(res.pdf_path).name}")
                elif res.status == "skipped":
                    task.add_skipped()
                    log(f"[{i}/{len(df)}] SKIP {res.error}")
                elif res.status == "not_found":
                    task.add_failure("not found in any OA source")
                    log(f"[{i}/{len(df)}] MISS no OA copy ({doi_norm or 'no-doi'})")
                else:
                    task.add_item_failure(doi_norm or title[:60], res.error or "unknown")
                    log(f"[{i}/{len(df)}] FAIL {res.error}")
            except Exception as e:
                tb = traceback.format_exc()
                task.add_item_failure(doi_norm or title[:60], str(e), tb)
                log(f"[{i}/{len(df)}] EXC  {e}")
                results.append(AcquisitionResult(doi=doi_norm, status="failed", error=str(e)))

            progress(i, len(df))
            # Be polite even between successes
            if not state.interruptible_sleep(RATE_LIMIT_SLEEP):
                log("Stopped."); task.finish("stopped"); break

        # Write/update index
        try:
            idx_df = pd.DataFrame([{
                "doi": r.doi, "status": r.status,
                "source": r.source, "url": r.url,
                "pdf_path": r.pdf_path, "error": r.error,
            } for r in results])
            idx_df.to_csv(index_path, index=False, encoding="utf-8-sig")
            task.add_output(index_path)
        except Exception as e:
            task.log_error(f"Could not write pdf_index.csv: {e}")

        # Stats by status / source
        by_status = pd.Series([r.status for r in results]).value_counts().to_dict()
        by_source = pd.Series([r.source for r in results if r.status == "downloaded"])\
                      .value_counts().to_dict()
        task.set_meta("By status", json.dumps(by_status, ensure_ascii=False))
        task.set_meta("By source", json.dumps(by_source, ensure_ascii=False))

        if task.status == "running":  # not stopped
            task.finish()
        return task
    except Exception as e:
        tb = traceback.format_exc()
        task.add_failure(str(e)); task.log_error(tb)
        task.finish("failed"); log(f"FATAL: {e}\n{tb}")
        return task
    finally:
        try: log("\n" + task.render())
        except Exception: pass
        if out_dir_p: task.save(out_dir_p)


# ============================================================================
# Stage 5 — PDF -> Markdown (MarkItDown primary, pymupdf4llm + pymupdf fallbacks)
# ============================================================================
def convert_pdf_to_markdown(pdf_path: Path, preferred: str = "markitdown") -> Tuple[str, str, str]:
    """
    Convert one PDF to markdown text.

    Returns (markdown_text, converter_used, error_message).
    Tries the preferred converter first, falls back through the chain.
    """
    converters = []
    pref = preferred.lower()
    # Build order with preferred first
    order = ["markitdown", "pymupdf4llm", "pymupdf"]
    if pref in order:
        order.remove(pref); order.insert(0, pref)

    last_err = ""
    for name in order:
        try:
            if name == "markitdown":
                try:
                    from markitdown import MarkItDown  # type: ignore
                except ImportError:
                    last_err = "markitdown not installed"; continue
                md = MarkItDown()
                result = md.convert(str(pdf_path))
                text = getattr(result, "text_content", None) or getattr(result, "markdown", None) or ""
                if text and len(text) > 50:
                    return text, "markitdown", ""
                last_err = "markitdown returned empty text"
            elif name == "pymupdf4llm":
                try:
                    import pymupdf4llm  # type: ignore
                except ImportError:
                    last_err = "pymupdf4llm not installed"; continue
                text = pymupdf4llm.to_markdown(str(pdf_path))
                if text and len(text) > 50:
                    return text, "pymupdf4llm", ""
                last_err = "pymupdf4llm returned empty text"
            elif name == "pymupdf":
                import fitz  # always available since Stage 1 needs it
                with fitz.open(pdf_path) as doc:
                    parts = []
                    for page in doc:
                        try:
                            parts.append(page.get_text())
                        except Exception:
                            parts.append("")
                    text = "\n\n".join(parts)
                if text and len(text) > 50:
                    return text, "pymupdf", ""
                last_err = "pymupdf returned empty text"
        except Exception as e:
            last_err = f"{name}: {e}"
            continue
    return "", "none", last_err or "all converters failed"


def run_convert_markdown(pdfs_dir: str, out_dir: str,
                         state: Optional[TaskState] = None,
                         log: Callable[[str], None] = print,
                         progress: Callable[[int, int], None] = lambda d, t: None,
                         preferred: str = "markitdown",
                         resume: bool = True) -> TaskReport:
    """Stage 5 entry point. Never raises."""
    task = TaskReport("PDF to Markdown")
    task.set_meta("PDFs dir", pdfs_dir)
    task.set_meta("Preferred converter", preferred)
    state = state or TaskState(); state.start()
    out_dir_p: Optional[Path] = None

    try:
        pdfs_p = Path(pdfs_dir)
        if not pdfs_p.exists():
            task.add_failure(f"PDFs dir not found: {pdfs_p}")
            log(f"ERROR: {pdfs_p} not found")
            task.finish("failed"); return task
        out_dir_p = Path(out_dir); out_dir_p.mkdir(parents=True, exist_ok=True)
        md_dir = out_dir_p / "markdown"; md_dir.mkdir(exist_ok=True)
        index_path = out_dir_p / "markdown_index.csv"

        # Get list of PDF files; support both flat 'pdfs/' and 'pdfs/pdfs/' layouts
        candidates = []
        for sub in (pdfs_p, pdfs_p / "pdfs"):
            if sub.exists() and sub.is_dir():
                candidates.extend(sub.glob("*.pdf"))
        # Dedupe preserving order
        seen = set(); pdfs = []
        for p in candidates:
            r = p.resolve()
            if r not in seen: seen.add(r); pdfs.append(p)
        log(f"Found {len(pdfs)} PDF files")
        task.set_total(len(pdfs))
        if not pdfs:
            task.finish(); return task

        # Resume support
        existing: Dict[str, dict] = {}
        if resume and index_path.exists():
            try:
                old = pd.read_csv(index_path, dtype=str).fillna("")
                for _, row in old.iterrows():
                    existing[row.get("pdf_path", "")] = row.to_dict()
                log(f"Resume: {len(existing)} previous conversion records")
            except Exception:
                pass

        rows = []
        progress(0, len(pdfs))
        for i, pdf in enumerate(pdfs, start=1):
            if not state.check_pause():
                log("Stopped."); task.finish("stopped"); break

            md_name = pdf.stem + ".md"
            md_path = md_dir / md_name
            prev = existing.get(str(pdf))
            if resume and prev and prev.get("status") == "ok" and md_path.exists():
                rows.append(prev); task.add_skipped()
                log(f"[{i}/{len(pdfs)}] SKIP {md_name}")
                progress(i, len(pdfs)); continue

            try:
                text, conv, err = convert_pdf_to_markdown(pdf, preferred=preferred)
                if text:
                    md_path.write_text(text, encoding="utf-8")
                    rows.append({
                        "pdf_path": str(pdf), "md_path": str(md_path),
                        "converter": conv, "chars": len(text),
                        "status": "ok", "error": "",
                    })
                    task.add_success(); task.add_output(md_path)
                    log(f"[{i}/{len(pdfs)}] OK   ({conv}, {len(text):,} chars) {md_name}")
                else:
                    rows.append({
                        "pdf_path": str(pdf), "md_path": "",
                        "converter": conv, "chars": 0,
                        "status": "failed", "error": err[:300],
                    })
                    task.add_item_failure(pdf.name, err)
                    log(f"[{i}/{len(pdfs)}] FAIL {err}")
            except Exception as e:
                tb = traceback.format_exc()
                rows.append({
                    "pdf_path": str(pdf), "md_path": "",
                    "converter": "exception", "chars": 0,
                    "status": "failed", "error": str(e)[:300],
                })
                task.add_item_failure(pdf.name, str(e), tb)
                log(f"[{i}/{len(pdfs)}] EXC  {e}")

            progress(i, len(pdfs))

        try:
            pd.DataFrame(rows).to_csv(index_path, index=False, encoding="utf-8-sig")
            task.add_output(index_path)
        except Exception as e:
            task.log_error(f"Could not write markdown_index.csv: {e}")

        # Stats
        by_conv = pd.Series([r.get("converter", "") for r in rows if r.get("status") == "ok"])\
                    .value_counts().to_dict()
        task.set_meta("By converter", json.dumps(by_conv, ensure_ascii=False))

        if task.status == "running": task.finish()
        return task
    except Exception as e:
        tb = traceback.format_exc()
        task.add_failure(str(e)); task.log_error(tb)
        task.finish("failed"); log(f"FATAL: {e}\n{tb}")
        return task
    finally:
        try: log("\n" + task.render())
        except Exception: pass
        if out_dir_p: task.save(out_dir_p)


# ============================================================================
# Stage 6 — Structured LLM extraction (Claude API)
# ============================================================================

# Schema for the LLM's JSON output. Span-grounded: every key finding must
# include an exact quote from the paper. This is auditable: spot-checking
# a sample tells you whether the LLM is hallucinating.
EXTRACTION_SCHEMA_DOC = """\
You will extract a structured summary from one academic paper. Return ONLY a JSON
object matching this exact schema (no commentary before or after the JSON):

{
  "research_question": "one-sentence statement of the question the paper addresses",
  "field": "primary research field (e.g. 'climate science', 'environmental economics')",
  "methods": ["list of methods/techniques used; concise phrases"],
  "data_sources": ["list of named datasets, models, or empirical data sources"],
  "geographic_scope": "geographic coverage (country / region / global / N/A)",
  "time_period": "time period studied (e.g. '1990-2015', 'pre-industrial to 2100')",
  "key_findings": [
    {
      "finding": "one specific finding in the authors' own framing",
      "evidence_quote": "exact verbatim quote from the paper supporting this finding (<= 200 chars)",
      "is_quantitative": true
    }
  ],
  "stated_uncertainty": "how the paper characterizes its own uncertainty (or 'not stated')",
  "policy_relevance": "stated policy implications, if any (or 'not stated')",
  "limitations": ["explicit limitations the authors acknowledge"],
  "ipcc_relevance_tags": ["which IPCC topics this paper most relates to; concise tags"]
}

RULES:
- Be faithful to the paper. Do NOT invent findings.
- If a field cannot be determined from the text, use "not stated" (string fields)
  or [] (list fields), not nulls and not guesses.
- evidence_quote must be a near-exact substring of the source text. Short
  quotes preferred. Truncate with [...] if necessary.
- Output JSON only. No preface, no postscript, no markdown code fences.
"""


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort JSON extraction from LLM text output."""
    if not text: return None
    # Strip code fences if present
    text = text.strip()
    if text.startswith("```"):
        # remove first fence line and trailing fence
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to locate the outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def llm_extract_one(markdown_text: str, doi: str, api_key: str,
                    model: str = DEFAULT_LLM_MODEL,
                    max_input_chars: int = LLM_MAX_INPUT_CHARS) -> Tuple[Optional[dict], str]:
    """
    Run one extraction. Returns (parsed_json_or_None, error_message).

    Uses the Anthropic SDK if present; otherwise direct HTTP to the Messages API.
    """
    try:
        from anthropic import Anthropic  # type: ignore
        client = Anthropic(api_key=api_key)
        use_sdk = True
    except ImportError:
        use_sdk = False

    truncated = markdown_text[:max_input_chars]
    if len(markdown_text) > max_input_chars:
        truncated += f"\n\n[... truncated from {len(markdown_text)} to {max_input_chars} chars ...]"

    user_prompt = (
        f"{EXTRACTION_SCHEMA_DOC}\n\n"
        f"PAPER DOI: {doi or 'unknown'}\n\n"
        f"PAPER TEXT (markdown):\n---\n{truncated}\n---\n\n"
        f"Return the JSON object now."
    )

    try:
        if use_sdk:
            resp = client.messages.create(
                model=model,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = "".join(
                getattr(b, "text", "") for b in (resp.content or [])
                if getattr(b, "type", "") == "text"
            )
        else:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": LLM_MAX_OUTPUT_TOKENS,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=120,
            )
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")

        parsed = _extract_json(text)
        if parsed is None:
            return None, f"could not parse JSON; first 200 chars: {text[:200]}"
        return parsed, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def run_llm_extract(markdown_dir: str, out_dir: str, api_key: str,
                    state: Optional[TaskState] = None,
                    log: Callable[[str], None] = print,
                    progress: Callable[[int, int], None] = lambda d, t: None,
                    model: str = DEFAULT_LLM_MODEL,
                    max_papers: int = 0,
                    resume: bool = True) -> TaskReport:
    """Stage 6 entry point. Never raises."""
    task = TaskReport("LLM Extraction")
    task.set_meta("Markdown dir", markdown_dir)
    task.set_meta("Model", model)
    state = state or TaskState(); state.start()
    out_dir_p: Optional[Path] = None

    try:
        if not api_key:
            task.add_failure("api_key is empty")
            log("ERROR: pass --api-key sk-ant-... (or set ANTHROPIC_API_KEY)")
            task.finish("failed"); return task

        md_p = Path(markdown_dir)
        # Accept either the parent dir (containing 'markdown/') or the markdown dir itself
        if (md_p / "markdown").exists():
            md_p = md_p / "markdown"
        if not md_p.exists():
            task.add_failure(f"markdown dir not found: {md_p}")
            task.finish("failed"); return task

        out_dir_p = Path(out_dir); out_dir_p.mkdir(parents=True, exist_ok=True)
        ext_dir = out_dir_p / "extracted"; ext_dir.mkdir(exist_ok=True)
        index_path = out_dir_p / "extracted_index.csv"

        md_files = sorted(md_p.glob("*.md"))
        log(f"Found {len(md_files)} markdown files")
        if max_papers > 0:
            md_files = md_files[:max_papers]
            log(f"Limited to first {len(md_files)} (--max-papers)")
        task.set_total(len(md_files))

        # Reload an existing index if any
        existing: Dict[str, dict] = {}
        if resume and index_path.exists():
            try:
                old = pd.read_csv(index_path, dtype=str).fillna("")
                for _, row in old.iterrows():
                    existing[row.get("md_path", "")] = row.to_dict()
            except Exception:
                pass

        rows = []
        progress(0, len(md_files))
        for i, md_file in enumerate(md_files, start=1):
            if not state.check_pause():
                log("Stopped."); task.finish("stopped"); break

            doi_safe = md_file.stem
            out_json = ext_dir / f"{doi_safe}.json"

            # Resume: skip if already extracted
            if resume and out_json.exists():
                try:
                    json.loads(out_json.read_text(encoding="utf-8"))
                    rows.append({
                        "md_path": str(md_file), "json_path": str(out_json),
                        "status": "ok", "error": "", "model": model,
                    })
                    task.add_skipped()
                    log(f"[{i}/{len(md_files)}] SKIP {md_file.name}")
                    progress(i, len(md_files)); continue
                except Exception:
                    pass  # corrupt; re-extract

            try:
                md_text = md_file.read_text(encoding="utf-8")
            except Exception as e:
                rows.append({"md_path": str(md_file), "json_path": "",
                             "status": "failed", "error": f"read: {e}", "model": model})
                task.add_item_failure(md_file.name, f"read: {e}")
                log(f"[{i}/{len(md_files)}] READ {e}")
                progress(i, len(md_files)); continue

            # Synthesise a DOI hint from filename
            doi_hint = doi_safe.replace("_", "/", 1)

            t0 = time.time()
            parsed, err = llm_extract_one(md_text, doi_hint, api_key, model=model)
            dt = time.time() - t0

            if parsed:
                try:
                    out_json.write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
                    rows.append({"md_path": str(md_file), "json_path": str(out_json),
                                 "status": "ok", "error": "", "model": model})
                    task.add_success(); task.add_output(out_json)
                    log(f"[{i}/{len(md_files)}] OK   ({dt:.1f}s) {md_file.name}")
                except Exception as e:
                    rows.append({"md_path": str(md_file), "json_path": "",
                                 "status": "failed", "error": f"write: {e}",
                                 "model": model})
                    task.add_item_failure(md_file.name, f"write: {e}")
                    log(f"[{i}/{len(md_files)}] WR   {e}")
            else:
                rows.append({"md_path": str(md_file), "json_path": "",
                             "status": "failed", "error": err[:300], "model": model})
                task.add_item_failure(md_file.name, err)
                log(f"[{i}/{len(md_files)}] FAIL {err[:120]}")

            progress(i, len(md_files))
            # Gentle pacing; the SDK already handles per-request retries on
            # rate limits, but we still avoid hammering.
            if not state.interruptible_sleep(0.3):
                log("Stopped."); task.finish("stopped"); break

        try:
            pd.DataFrame(rows).to_csv(index_path, index=False, encoding="utf-8-sig")
            task.add_output(index_path)
        except Exception as e:
            task.log_error(f"Could not write extracted_index.csv: {e}")

        if task.status == "running": task.finish()
        return task
    except Exception as e:
        tb = traceback.format_exc()
        task.add_failure(str(e)); task.log_error(tb)
        task.finish("failed"); log(f"FATAL: {e}\n{tb}")
        return task
    finally:
        try: log("\n" + task.render())
        except Exception: pass
        if out_dir_p: task.save(out_dir_p)


# ============================================================================
# Stage 7 — Bibliometric analysis (charts + HTML report)
# ============================================================================
def _safe_year(s) -> Optional[int]:
    try:
        s = str(s).strip()
        if not s: return None
        m = re.search(r"(19|20)\d{2}", s)
        return int(m.group(0)) if m else None
    except Exception:
        return None


def _flatten_findings(extracted: List[dict]) -> pd.DataFrame:
    """Flatten extracted JSONs into a long-format dataframe of findings."""
    rows = []
    for e in extracted:
        doi = e.get("_doi", "")
        for k in ("methods", "data_sources", "limitations", "ipcc_relevance_tags"):
            for v in (e.get(k) or []):
                rows.append({"doi": doi, "field": k, "value": str(v)[:200]})
        for finding in (e.get("key_findings") or []):
            if isinstance(finding, dict):
                rows.append({"doi": doi, "field": "key_findings",
                             "value": str(finding.get("finding", ""))[:200]})
    return pd.DataFrame(rows)


def _chart_to_base64_png(fig) -> str:
    """Convert a matplotlib figure to a base64-embedded PNG <img> tag."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    return f'<img src="data:image/png;base64,{b64}" style="max-width:100%;height:auto;"/>'


def _value_counts_bar(series: pd.Series, top_n: int = 15, title: str = "", xlabel: str = ""):
    """Return a base64 <img> for a horizontal bar chart of top N value counts."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""
    counts = series.dropna().astype(str).value_counts().head(top_n)
    if counts.empty: return ""
    fig, ax = plt.subplots(figsize=(8, max(3, 0.35 * len(counts) + 1)))
    counts.iloc[::-1].plot(kind="barh", ax=ax, color="#3b82c4")
    ax.set_title(title); ax.set_xlabel(xlabel or "Count")
    ax.tick_params(axis="y", labelsize=9)
    fig.tight_layout()
    img = _chart_to_base64_png(fig)
    plt.close(fig)
    return img


def _year_histogram(years: pd.Series, title: str = "Publications per year"):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""
    y = years.dropna().astype(int)
    if y.empty: return ""
    fig, ax = plt.subplots(figsize=(9, 3.5))
    y.value_counts().sort_index().plot(kind="bar", ax=ax, color="#3b82c4", width=0.85)
    ax.set_title(title); ax.set_xlabel("Year"); ax.set_ylabel("Count")
    ax.tick_params(axis="x", rotation=45, labelsize=9)
    fig.tight_layout()
    img = _chart_to_base64_png(fig)
    plt.close(fig)
    return img


def run_analyze(record_csv: str, extracted_dir: str, out_dir: str,
                state: Optional[TaskState] = None,
                log: Callable[[str], None] = print) -> TaskReport:
    """Stage 7 entry point. Never raises. Produces analysis_report.html + tables.xlsx"""
    task = TaskReport("Bibliometric Analysis")
    task.set_meta("Records file", record_csv)
    task.set_meta("Extracted dir", extracted_dir)
    state = state or TaskState(); state.start()
    out_dir_p: Optional[Path] = None

    try:
        rec_p = Path(record_csv)
        ext_p = Path(extracted_dir)
        if not rec_p.exists():
            task.add_failure(f"Record.csv not found: {rec_p}")
            log(f"ERROR: {rec_p} not found"); task.finish("failed"); return task
        # Allow either parent dir or extracted/ itself
        if (ext_p / "extracted").exists():
            ext_p = ext_p / "extracted"

        out_dir_p = Path(out_dir); out_dir_p.mkdir(parents=True, exist_ok=True)
        figs_dir = out_dir_p / "figures"; figs_dir.mkdir(exist_ok=True)

        # ---- Load Record.csv ----
        try:
            recs = pd.read_csv(rec_p, dtype=str).fillna("")
        except Exception as e:
            task.add_failure(f"read Record.csv: {e}")
            task.finish("failed"); return task
        log(f"Records: {len(recs)} rows, {len(recs.columns)} columns")
        task.set_meta("Records", len(recs))

        # ---- Load extracted JSONs ----
        extracted: List[dict] = []
        if ext_p.exists():
            for jf in sorted(ext_p.glob("*.json")):
                try:
                    obj = json.loads(jf.read_text(encoding="utf-8"))
                    obj["_doi"] = jf.stem.replace("_", "/", 1)
                    extracted.append(obj)
                except Exception as e:
                    task.log_error(f"bad JSON {jf.name}: {e}")
        log(f"Extracted: {len(extracted)} JSON files loaded")
        task.set_meta("Extracted", len(extracted))

        # ---- Standard bibliometrics ----
        sections = []
        year_col = next((c for c in recs.columns if c.strip().lower() in
                         ("publication year", "year")), None)
        source_col = next((c for c in recs.columns if c.strip().lower() == "source title"), None)
        authors_col = next((c for c in recs.columns if c.strip().lower() == "authors"), None)

        year_chart = ""
        if year_col is not None:
            years = recs[year_col].apply(_safe_year)
            year_chart = _year_histogram(years)
        sections.append(("Publications per year", year_chart or "<p><i>No year data.</i></p>"))

        journal_chart = ""
        if source_col is not None:
            journal_chart = _value_counts_bar(recs[source_col], top_n=20,
                                              title="Top journals / sources",
                                              xlabel="Number of papers")
        sections.append(("Top sources", journal_chart or "<p><i>No source data.</i></p>"))

        author_chart = ""
        if authors_col is not None:
            # split semi-colon then comma; take family-name first token
            all_authors = []
            for a in recs[authors_col].dropna():
                for piece in str(a).split(";"):
                    name = piece.strip().split(",")[0].strip()
                    if name: all_authors.append(name)
            author_chart = _value_counts_bar(pd.Series(all_authors), top_n=20,
                                             title="Top authors (by paper count)",
                                             xlabel="Papers")
        sections.append(("Top authors", author_chart or "<p><i>No author data.</i></p>"))

        # ---- LLM-derived dimensions ----
        ll_sections = []
        if extracted:
            df_flat = _flatten_findings(extracted)
            for fld, title in (("methods", "Methods used"),
                               ("data_sources", "Data sources"),
                               ("ipcc_relevance_tags", "IPCC topic tags"),
                               ("limitations", "Stated limitations")):
                sub = df_flat[df_flat["field"] == fld]
                chart = _value_counts_bar(sub["value"], top_n=20,
                                          title=title, xlabel="Mentions")
                ll_sections.append((title, chart or "<p><i>No data.</i></p>"))
            # Geographic scope
            geo = pd.Series([e.get("geographic_scope", "") for e in extracted])
            geo_chart = _value_counts_bar(geo, top_n=15,
                                          title="Geographic scope",
                                          xlabel="Papers")
            ll_sections.append(("Geographic scope", geo_chart or "<p><i>No data.</i></p>"))

        # ---- Build XLSX with raw tables ----
        try:
            xlsx_path = out_dir_p / "analysis_tables.xlsx"
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as wr:
                if year_col is not None:
                    recs[year_col].apply(_safe_year).value_counts().sort_index()\
                        .to_frame("count").to_excel(wr, sheet_name="by_year")
                if source_col is not None:
                    recs[source_col].value_counts().head(50).to_frame("count")\
                        .to_excel(wr, sheet_name="top_sources")
                if extracted:
                    df_flat = _flatten_findings(extracted)
                    for fld in ("methods", "data_sources",
                                "ipcc_relevance_tags", "limitations"):
                        sub = df_flat[df_flat["field"] == fld]["value"]
                        sub.value_counts().head(50).to_frame("count")\
                            .to_excel(wr, sheet_name=fld[:31])
                    # Full extracted table
                    rows = []
                    for e in extracted:
                        rows.append({
                            "doi": e.get("_doi", ""),
                            "research_question": e.get("research_question", ""),
                            "field": e.get("field", ""),
                            "geographic_scope": e.get("geographic_scope", ""),
                            "time_period": e.get("time_period", ""),
                            "n_findings": len(e.get("key_findings") or []),
                            "stated_uncertainty": e.get("stated_uncertainty", ""),
                            "policy_relevance": e.get("policy_relevance", ""),
                        })
                    pd.DataFrame(rows).to_excel(wr, sheet_name="extracted_overview", index=False)
            task.add_output(xlsx_path)
        except Exception as e:
            task.log_error(f"Could not write xlsx: {e}")

        # ---- Build HTML report ----
        try:
            html_path = out_dir_p / "analysis_report.html"
            html = _render_html_report(
                title="IPCC Literature — Bibliometric Report",
                meta={
                    "Records (WoS)": len(recs),
                    "Papers analysed by LLM": len(extracted),
                    "Generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                bibliometric_sections=sections,
                llm_sections=ll_sections,
            )
            html_path.write_text(html, encoding="utf-8")
            task.add_output(html_path)
            log(f"Wrote {html_path}")
        except Exception as e:
            task.log_error(f"Could not write HTML: {e}")
            log(f"WARNING: HTML report failed: {e}")

        task.finish()
        return task
    except Exception as e:
        tb = traceback.format_exc()
        task.add_failure(str(e)); task.log_error(tb)
        task.finish("failed"); log(f"FATAL: {e}\n{tb}")
        return task
    finally:
        try: log("\n" + task.render())
        except Exception: pass
        if out_dir_p: task.save(out_dir_p)


def _render_html_report(title: str, meta: Dict, bibliometric_sections: List[Tuple[str, str]],
                        llm_sections: List[Tuple[str, str]]) -> str:
    """Build a single-file HTML report (charts inlined as base64 PNGs)."""
    meta_html = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in meta.items())
    bib_html = "".join(f"<section><h3>{t}</h3>{c}</section>" for t, c in bibliometric_sections)
    llm_html = "".join(f"<section><h3>{t}</h3>{c}</section>" for t, c in llm_sections) \
               or "<p><i>No LLM-derived analysis (run Stage 6 first).</i></p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1100px;
         margin: 24px auto; padding: 0 16px; color: #222; }}
 h1 {{ border-bottom: 2px solid #3b82c4; padding-bottom: 6px; }}
 h2 {{ color: #3b82c4; margin-top: 32px; }}
 h3 {{ color: #444; margin-top: 24px; }}
 table.meta {{ border-collapse: collapse; margin: 12px 0; }}
 table.meta th {{ text-align: left; padding: 4px 12px 4px 0; color: #666; font-weight: 500; }}
 table.meta td {{ padding: 4px 0; font-family: Menlo, monospace; }}
 section {{ background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 6px;
            padding: 12px 16px; margin: 16px 0; }}
 footer {{ margin-top: 60px; padding-top: 12px; border-top: 1px solid #e3e6ea;
           color: #888; font-size: 0.9em; }}
 footer a {{ color: #3b82c4; }}
</style></head><body>
<h1>{title}</h1>
<table class="meta">{meta_html}</table>
<h2>1. Standard bibliometrics (from WoS metadata)</h2>
{bib_html}
<h2>2. LLM-derived dimensions (from full-text extraction)</h2>
{llm_html}
<footer>
  Generated by IPCC References Toolkit — Jiacheng Zheng ·
  <a href="{AUTHOR_HOMEPAGE}">{AUTHOR_HOMEPAGE}</a>
</footer>
</body></html>
"""


# ============================================================================
# CLI
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="IPCC References Toolkit — Stages 4-7 (post-WoS pipeline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in (__doc__ or "") else "",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Stage 4
    p4 = sub.add_parser("acquire", help="Stage 4 — download OA PDFs")
    p4.add_argument("--records", required=True, help="Path to Record.csv from Stage 3")
    p4.add_argument("--out", required=True, help="Output directory")
    p4.add_argument("--email", required=True, help="Your email (Unpaywall polite pool)")
    p4.add_argument("--no-resume", action="store_true", help="Re-attempt all records")

    # Stage 5
    p5 = sub.add_parser("markdown", help="Stage 5 — PDFs to Markdown")
    p5.add_argument("--pdfs", required=True, help="Directory containing PDFs (or Stage 4 out)")
    p5.add_argument("--out", required=True, help="Output directory")
    p5.add_argument("--converter", default="markitdown",
                    choices=["markitdown", "pymupdf4llm", "pymupdf"],
                    help="Preferred converter (falls back to others on failure)")
    p5.add_argument("--no-resume", action="store_true")

    # Stage 6
    p6 = sub.add_parser("extract", help="Stage 6 — schema-driven LLM extraction")
    p6.add_argument("--markdown", required=True, help="Directory of markdown files (or Stage 5 out)")
    p6.add_argument("--out", required=True, help="Output directory")
    p6.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""),
                    help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    p6.add_argument("--model", default=DEFAULT_LLM_MODEL,
                    help=f"Claude model id (default: {DEFAULT_LLM_MODEL})")
    p6.add_argument("--max-papers", type=int, default=0,
                    help="Limit to first N papers (for testing; 0 = no limit)")
    p6.add_argument("--no-resume", action="store_true")

    # Stage 7
    p7 = sub.add_parser("analyze", help="Stage 7 — bibliometric analysis + HTML report")
    p7.add_argument("--records", required=True, help="Path to Record.csv")
    p7.add_argument("--extracted", required=True, help="Directory of extracted JSONs (or Stage 6 out)")
    p7.add_argument("--out", required=True, help="Output directory")

    # Full pipeline shortcut
    pa = sub.add_parser("all", help="Run Stages 4-7 in sequence with defaults")
    pa.add_argument("--records", required=True)
    pa.add_argument("--out", required=True)
    pa.add_argument("--email", required=True)
    pa.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))
    pa.add_argument("--model", default=DEFAULT_LLM_MODEL)
    pa.add_argument("--max-papers", type=int, default=0)

    args = parser.parse_args()

    if args.cmd == "acquire":
        r = run_acquire_pdfs(args.records, args.out, args.email,
                             resume=not args.no_resume)
        sys.exit(0 if r.status in ("completed", "completed_with_errors") else 2)
    elif args.cmd == "markdown":
        r = run_convert_markdown(args.pdfs, args.out,
                                 preferred=args.converter,
                                 resume=not args.no_resume)
        sys.exit(0 if r.status in ("completed", "completed_with_errors") else 2)
    elif args.cmd == "extract":
        r = run_llm_extract(args.markdown, args.out, args.api_key,
                            model=args.model, max_papers=args.max_papers,
                            resume=not args.no_resume)
        sys.exit(0 if r.status in ("completed", "completed_with_errors") else 2)
    elif args.cmd == "analyze":
        r = run_analyze(args.records, args.extracted, args.out)
        sys.exit(0 if r.status in ("completed", "completed_with_errors") else 2)
    elif args.cmd == "all":
        base = Path(args.out)
        print("\n========== Stage 4: acquire ==========")
        r4 = run_acquire_pdfs(args.records, str(base / "stage4_pdfs"), args.email)
        if r4.status == "failed":
            sys.exit(2)
        print("\n========== Stage 5: markdown ==========")
        r5 = run_convert_markdown(str(base / "stage4_pdfs"), str(base / "stage5_markdown"))
        if r5.status == "failed":
            sys.exit(2)
        print("\n========== Stage 6: LLM extract ==========")
        r6 = run_llm_extract(str(base / "stage5_markdown"),
                             str(base / "stage6_extracted"),
                             args.api_key, model=args.model,
                             max_papers=args.max_papers)
        if r6.status == "failed":
            sys.exit(2)
        print("\n========== Stage 7: analyze ==========")
        r7 = run_analyze(args.records, str(base / "stage6_extracted"),
                         str(base / "stage7_analysis"))
        sys.exit(0 if r7.status in ("completed", "completed_with_errors") else 2)


if __name__ == "__main__":
    main()
