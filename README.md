# IPCC References Toolkit

End-to-end research pipeline that turns large IPCC report PDFs into a
literature-scale, LLM-analysable bibliometric database — from raw PDF
reference extraction, through Web of Science enrichment, OA full-text
acquisition, Markdown conversion, schema-driven LLM extraction, and
finally a publishable HTML analysis report.

> 中文版 README: see [`README_zh.md`](./README_zh.md)

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Pipeline Architecture](#2-pipeline-architecture)
3. [Stages 1–3: PDF → references → WoS → Merge](#3-stages-13-pdf--references--wos--merge)
4. [Stages 4–7: PDF acquisition → Markdown → LLM extraction → Analysis](#4-stages-47-pdf-acquisition--markdown--llm-extraction--analysis)
5. [Installation](#5-installation)
6. [Quick Start](#6-quick-start)
7. [API Configuration](#7-api-configuration)
8. [Output Files Reference](#8-output-files-reference)
9. [Legal and Ethical Notes](#9-legal-and-ethical-notes)
10. [Known Limitations](#10-known-limitations)
11. [Roadmap](#11-roadmap)
12. [Author and License](#12-author-and-license)

---

## 1. Project Overview

IPCC Assessment Reports cite tens of thousands of papers across dozens of
chapters and three Working Groups. Existing bibliometric tools only see
the metadata layer — citation counts, co-authorship networks. Questions
like *"What fraction of papers cited in AR6 WG2 actually used MRIO methods
on EORA data?"* or *"How does the policy framing of climate adaptation
literature shift between AR5 and AR6?"* require reading the full text,
which is impractical at the ten-thousand-paper scale by hand.

This toolkit automates the entire workflow end-to-end:

```
   IPCC PDF
      │
      ▼
   [1] Extract references             ──►  references.xlsx, wos_queries.txt
      │
      ▼
   [2] WoS lookup (API or browser)    ──►  wos_exports/
      │
      ▼
   [3] Merge                          ──►  Record.csv, Unrecord.csv
      │
      ▼
   [4] OA PDF acquisition             ──►  pdfs/, pdf_index.csv
      │
      ▼
   [5] PDF → Markdown                 ──►  markdown/, markdown_index.csv
      │
      ▼
   [6] LLM structured extraction      ──►  extracted/*.json
      │
      ▼
   [7] Bibliometric analysis          ──►  analysis_report.html, .xlsx, figures/
```

Stages 1–3 are local-or-API; Stage 4 uses **only OA / preprint sources**
(Unpaywall, Crossref, OpenAlex, arXiv) — no paywall circumvention; Stage 5
uses Microsoft MarkItDown with fallbacks; Stage 6 uses the Claude API; and
Stage 7 produces a single self-contained HTML report.

**Designed for:** researchers, PhD students, and bibliometricians working on
IPCC-related analyses, consumption-based emission accounting, climate
adaptation literature reviews, or any project that needs a structured,
WoS-indexed, LLM-enriched corpus from large PDF reports.

---

## 2. Pipeline Architecture

| Stage | Input | Tool | Output | API needed? |
|------:|-------|------|--------|:-----------:|
| 1 | IPCC PDF | PyMuPDF + Crossref | `references.xlsx`, `wos_queries.txt` | No (Crossref is free) |
| 2 | `wos_queries.txt` | WoS Starter API *or* Playwright | `wos_*.xlsx` / `wos_*.json` | WoS recommended |
| 3 | `references.xlsx` + WoS exports | Merger | `Record.csv`, `Unrecord.csv` | No |
| 4 | `Record.csv` | Unpaywall + Crossref + OpenAlex + arXiv | `pdfs/`, `pdf_index.csv` | No (free APIs) |
| 5 | `pdfs/` | MarkItDown → pymupdf4llm → pymupdf | `markdown/`, `markdown_index.csv` | No |
| 6 | `markdown/` | Claude API (Sonnet 4.6 default) | `extracted/*.json` | **Anthropic key required** |
| 7 | `Record.csv` + `extracted/` | pandas + matplotlib | `analysis_report.html`, `analysis_tables.xlsx` | No |

**Architectural guarantees** (apply to all 7 stages):

- **Per-item failure isolation.** A single broken PDF, failed download, or
  unparseable LLM response never aborts the run. Failures are caught,
  logged with full traceback, and processing continues.
- **Never-raise runners.** Each stage's entry function (`run_extraction`,
  `run_acquire_pdfs`, `run_llm_extract`, …) is contractually guaranteed to
  return a `TaskReport` rather than raise — even on catastrophic errors.
  This makes the pipeline scriptable and safe to wrap in any orchestrator.
- **Resumable.** Stages 4–6 maintain on-disk indexes; re-running skips
  work already done. Pass `--no-resume` to force a clean rerun.
- **Structured reports.** Every run produces a `task_report_*.txt` file
  with status, elapsed time, success rate, outputs, and per-item failures.

---

## 3. Stages 1–3: PDF → references → WoS → Merge

Implemented in [`ipcc_refs_gui.py`](./ipcc_refs_gui.py) as a four-tab
Tkinter GUI. Run with:

```bash
python ipcc_refs_gui.py
```

### Tab 1 — Extract PDF

1. Pick your IPCC PDF.
2. Fill in `Report` (e.g. `AR6`), `Working Group` (e.g. `WG2`), and an
   email (used in the Crossref polite-pool User-Agent only).
3. Click **Run**. Progress is shown in the bar; the log streams live.
4. On completion, the GUI pops up a structured summary. Outputs:
   `references.xlsx`, `wos_queries.txt`, `crossref_cache.json`, and (if
   any chapter failed) `failed_chapters.csv` + `extraction_errors.log`.

### Tab 2 — WOS Lookup

The toolkit supports two paths, controlled by the **WOS API key** field
(highlighted in red because the API path is strongly preferred):

- **API path** (paste your Starter key in the red field): direct HTTPS
  calls to `api.clarivate.com/apis/wos-starter`. Fast, ToS-compliant,
  scriptable, unattended.
- **Browser path** (leave the key empty): Playwright drives a Chromium
  window with a persistent profile. You log in once via your
  institution's SSO; the toolkit walks every batch and downloads each
  Excel export. Requires the machine to stay on for the entire run.

### Tab 3 — Merge

Combines `references.xlsx` with the WoS exports folder into:

- **`Record.csv`** — the full WoS schema (≈70 columns), one row per
  reference successfully found in WoS, deduplicated by normalised DOI.
- **`Unrecord.csv`** — slim 8-column schema (Report, WG, Chapter,
  Chapter title, Authors, Article Title, Publisher, Year) for
  references still missing from WoS.

Re-running is safe and idempotent — drop more WoS exports into the
folder and click Run again.

---

## 4. Stages 4–7: PDF acquisition → Markdown → LLM extraction → Analysis

Implemented in [`pipeline_extras.py`](./pipeline_extras.py) as a CLI
script with four subcommands plus an `all` shortcut. CLI-first because
these stages are typically long-running batch jobs that benefit from
running on a server / under `nohup` / in CI.

### Stage 4 — `acquire`: download Open Access PDFs

For each DOI in `Record.csv`, query four free sources in priority order
and download the first PDF that resolves:

1. **Unpaywall API** — purpose-built OA index; requires your email.
2. **Crossref `link` field** — direct publisher PDF URLs flagged as
   text-mining-friendly.
3. **OpenAlex** — aggregates OA locations from many sources.
4. **arXiv** — preprint coverage (significant for IPCC-cited literature).

Each downloaded file is verified by magic bytes (`%PDF`) to catch HTML
error pages disguised as PDFs. A 100 MB safety cap is enforced per file.

```bash
python pipeline_extras.py acquire \
    --records output/Record.csv \
    --out output/stage4_pdfs \
    --email you@your-institution.edu
```

Outputs: `pdfs/<doi_safe>.pdf` + `pdf_index.csv` (with per-record
`status`, `source`, `url`, `pdf_path`, `error`).

Expected OA coverage for climate/environment IPCC references: roughly
40–60% in our tests.

### Stage 5 — `markdown`: PDF → Markdown

Tries Microsoft MarkItDown first, then `pymupdf4llm`, then plain
PyMuPDF text extraction. Each file's converter is recorded so you can
audit conversion quality afterwards.

```bash
python pipeline_extras.py markdown \
    --pdfs output/stage4_pdfs \
    --out output/stage5_markdown \
    --converter markitdown        # or: pymupdf4llm, pymupdf
```

Outputs: `markdown/<doi_safe>.md` + `markdown_index.csv`.

### Stage 6 — `extract`: schema-driven LLM extraction

For each markdown file, ask Claude to produce a JSON object matching a
fixed schema:

```json
{
  "research_question": "...",
  "field": "...",
  "methods": ["..."],
  "data_sources": ["..."],
  "geographic_scope": "...",
  "time_period": "...",
  "key_findings": [
    {"finding": "...", "evidence_quote": "...", "is_quantitative": true}
  ],
  "stated_uncertainty": "...",
  "policy_relevance": "...",
  "limitations": ["..."],
  "ipcc_relevance_tags": ["..."]
}
```

Each finding must include an `evidence_quote` — a verbatim substring of
the source. This is the auditability anchor: spot-checking a random
sample tells you whether the model is hallucinating.

```bash
# Either pass --api-key, or set ANTHROPIC_API_KEY in your environment
export ANTHROPIC_API_KEY=sk-ant-...

python pipeline_extras.py extract \
    --markdown output/stage5_markdown \
    --out output/stage6_extracted \
    --model claude-sonnet-4-6     \
    --max-papers 20               # test on 20 first; remove for full run
```

Outputs: `extracted/<doi_safe>.json` + `extracted_index.csv`. Skipping
already-extracted files is automatic.

**Cost note.** A 30-page paper is roughly 30–50k tokens of input plus
≤4k output. At Sonnet 4.6 list pricing, a corpus of 10,000 papers
typically costs in the low four figures (USD); run on 20 papers first
to dial in the prompt and schema.

### Stage 7 — `analyze`: bibliometric analysis + HTML report

Joins WoS metadata (from `Record.csv`) with LLM-derived dimensions
(from `extracted/*.json`) to produce:

```bash
python pipeline_extras.py analyze \
    --records output/Record.csv \
    --extracted output/stage6_extracted \
    --out output/stage7_analysis
```

Outputs:

- `analysis_report.html` — single-file report with embedded charts
  covering:
    - Standard bibliometrics: papers per year, top journals, top authors.
    - LLM-derived dimensions: methods used, data sources cited, geographic
      scope, IPCC topic tags, stated limitations.
- `analysis_tables.xlsx` — raw counts in pivot tables for further work.
- `figures/` — individual PNGs for inclusion in papers / presentations.

### Run the full pipeline end-to-end

```bash
python pipeline_extras.py all \
    --records output/Record.csv \
    --out output/full_pipeline \
    --email you@your-institution.edu \
    --max-papers 10           # try 10 first, then remove
```

---

## 5. Installation

Tested on macOS, Linux, and Windows with Python 3.9 – 3.12.

```bash
# 1. Clone
git clone https://github.com/<your-account>/ipcc-refs-toolkit.git
cd ipcc-refs-toolkit

# 2. (Recommended) virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows

# 3. Install
pip install -r requirements.txt

# 4. (Optional) browser fallback for Stage 2
playwright install chromium

# 5. (Optional, Linux only) Tkinter
sudo apt install python3-tk        # Ubuntu / Debian
```

The optional packages (`markitdown`, `pymupdf4llm`) gracefully degrade
to the plain PyMuPDF fallback if not installed; the pipeline will still
work but with lower-quality markdown output.

---

## 6. Quick Start

For a complete first run on a small sample:

```bash
# Stage 1-3: GUI
python ipcc_refs_gui.py
# - Tab 1: pick a small IPCC chapter PDF, set Max refs per chapter = 10
# - Tab 2: paste your WoS API key (or use browser path)
# - Tab 3: click Run

# Stages 4-7: CLI
python pipeline_extras.py all \
    --records output/Record.csv \
    --out output/full \
    --email you@example.com \
    --max-papers 5
```

Then open `output/full/stage7_analysis/analysis_report.html` in your
browser to see the report.

---

## 7. API Configuration

### Web of Science Starter API (for Stage 2)

- Endpoint: `https://api.clarivate.com/apis/wos-starter/v1/documents`
- Documentation: <https://developer.clarivate.com/apis/wos-starter>
- **Where to paste:** GUI Tab 2 → red field labelled *"WOS API key
  (★ RECOMMENDED ★)"*.
- Many universities have access included in their WoS subscription —
  ask your library before paying for a personal plan.

### Anthropic Claude API (for Stage 6)

- Endpoint: `https://api.anthropic.com/v1/messages`
- Get a key: <https://console.anthropic.com>
- **Where to pass it:** either `--api-key sk-ant-...` on the command
  line, or `export ANTHROPIC_API_KEY=sk-ant-...` in your shell.
- Default model: `claude-sonnet-4-6` (good balance of cost and quality
  for structured extraction). Override with `--model`.
- The toolkit uses the official `anthropic` Python SDK if installed,
  otherwise falls back to direct HTTPS — both paths work.

### Unpaywall (for Stage 4)

- No key needed; just an email address (the "polite pool").
- Pass `--email you@your-institution.edu` to `acquire`.

---

## 8. Output Files Reference

| Stage | File | Description |
|------:|------|-------------|
| 1 | `references.xlsx` | One row per extracted reference; 15-column schema. |
| 1 | `wos_queries.txt` | DOI batches as WoS Advanced Search queries (50/batch). |
| 1 | `crossref_cache.json` | On-disk cache; safe to delete. |
| 1 | `failed_chapters.csv` *(only on failure)* | Per-chapter failures with error + traceback. |
| 1 | `extraction_errors.log` *(only on failure)* | Human-readable per-chapter tracebacks. |
| 2 | `wos_api_batch_NNN.json` | Raw WoS API responses. |
| 2 | `wos_api_combined.xlsx` | Flattened, deduplicated combined records. |
| 2 | `wos_batch_NNN.xlsx` | Browser path: one Excel per DOI batch. |
| 3 | `Record.csv` | Full WoS schema (~70 columns) for matched references. |
| 3 | `Unrecord.csv` | Slim 8-column schema for unmatched references. |
| 4 | `pdfs/<doi_safe>.pdf` | Downloaded OA PDFs. |
| 4 | `pdf_index.csv` | Per-record status / source / URL / error. |
| 5 | `markdown/<doi_safe>.md` | Markdown-converted text. |
| 5 | `markdown_index.csv` | Per-file converter used + character count. |
| 6 | `extracted/<doi_safe>.json` | Structured LLM extraction. |
| 6 | `extracted_index.csv` | Per-paper extraction status. |
| 7 | `analysis_report.html` | Self-contained HTML report with charts. |
| 7 | `analysis_tables.xlsx` | Raw count tables (multiple sheets). |
| 7 | `figures/*.png` | Individual chart PNGs. |
| *all* | `task_report_*.txt` | Per-run structured summary. |
| *all* | `failed_items_*.csv` | Per-run failed-item details with tracebacks. |

---

## 9. Legal and Ethical Notes

**This toolkit only uses legitimate Open Access sources for full-text
acquisition.** Sci-Hub and paywall circumvention are intentionally NOT
supported.

- Unpaywall, Crossref, OpenAlex, and arXiv are all standard, ToS-compliant
  research APIs.
- Crossref `link` URLs flagged as `text-mining` are explicitly opened by
  publishers for TDM (Text and Data Mining) use.
- If your institution has Elsevier / Wiley / Springer TDM agreements,
  those endpoints can be added to Stage 4 by extending `acquire_one_pdf()`
  — but coordinate with your library first.

The WoS browser fallback in Stage 2 walks the same Web of Science search
UI you would use manually. It is sensitive to Clarivate's terms of use;
prefer the API path whenever you have access.

For the LLM extraction stage:

- Cite the model used in any publication produced from this pipeline.
- Each finding's `evidence_quote` is intentional and required —
  un-grounded LLM "summaries" are not reproducible research and should
  not enter the literature.
- Spot-audit a random subsample of extractions before drawing
  conclusions at scale.

---

## 10. Known Limitations

**Without a Web of Science API key**, Stage 1 still runs entirely
locally and produces a usable references database; Stage 2's browser
fallback works but is ToS-sensitive and requires the machine to stay
powered on for the duration of the run.

**Stage 4 OA coverage is fundamentally limited** by the openness of the
underlying literature: expect roughly 40–60% download success for
climate/environment IPCC references, lower for older or theoretical
papers. The `pdf_index.csv` makes the gap explicit so it can be
reported honestly as a limitation in derivative work.

**MarkItDown quality on scientific PDFs is uneven.** Double-column
layouts, math, complex tables, and scanned pages are hard. The
`markdown_index.csv` records which converter was used so you can audit
quality post-hoc. For known-bad PDFs, run `ocrmypdf` first.

**LLM extraction has a real false-positive rate.** The schema requires
verbatim evidence quotes specifically to make these errors detectable —
but you must actually audit a sample (we suggest 5–10% of the corpus)
before publishing aggregate results.

**Cost.** Sonnet 4.6 on 10,000 papers at ≈40k input tokens each will
typically cost in the four-figures USD range. Always test on
`--max-papers 20` first.

**Reference splitter heuristic** (Stage 1) assumes author-comma-initial
citation style. PDFs with numbered bracketed citations or other
unusual styles may need a custom splitter.

---

## 11. Roadmap

1. **WoS Expanded API support** for full schema (Cited References, funding,
   addresses) in Stage 2.
2. **Persistent config file** (`~/.ipcc_refs_toolkit.yaml`) for API keys
   and default paths.
3. **GUI tabs for Stages 4–7**, so the toolkit becomes single-window
   end-to-end.
4. **Scopus / OpenAlex enrichment** in Stage 2 as an alternative to WoS.
5. **OCR pre-pass** integration for scanned PDFs.
6. **Cross-document analyses** (citation networks, topic clustering,
   temporal trend tests) in Stage 7.
7. **Sample audit tooling** to make Stage 6 auditing systematic rather
   than ad-hoc.
8. **Unit and integration test suite** with sample mini-PDFs.

---

## 12. Author and License

**Author:** Jiacheng Zheng

**Contact / homepage:** <https://karcen.github.io/zhengjiacheng.github.io/>

Built to support IPCC-related bibliometric research. Bug reports,
pull requests, and feedback are welcome via the homepage above.

No license file is bundled; treat the code as research software for
academic use. If you wish to redistribute or reuse it in a commercial
or large-scale project, please contact the author first.
