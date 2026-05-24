# IPCC References Toolkit

A reproducible desktop pipeline that turns large IPCC report PDFs into a
Web of Science (WoS) indexed reference database, with a Tkinter GUI for
non-technical users and full error handling for unattended runs.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Workflow](#2-workflow)
3. [Features](#3-features)
4. [Installation](#4-installation)
5. [Using the GUI](#5-using-the-gui)
6. [API Configuration (Web of Science)](#6-api-configuration-web-of-science)
7. [Output Files Reference](#7-output-files-reference)
8. [Common Errors and Fixes](#8-common-errors-and-fixes)
9. [Known Limitations](#9-known-limitations)
10. [Roadmap](#10-roadmap)
11. [Author and License](#11-author-and-license)

---

## 1. Project Overview

IPCC Assessment Reports cite tens of thousands of papers across dozens of
chapters and three Working Groups. Compiling a clean, citation-indexed
database of those references by hand is impractical. This toolkit
automates the full pipeline end-to-end:

```
   IPCC PDF  ──►  references.xlsx  ──►  WoS lookup  ──►  Record.csv  +  Unrecord.csv
```

Stage 1 (PDF → references.xlsx) is **fully local and requires no API**.
Stages 2 and 3 add Web of Science metadata when access is available, and
degrade gracefully when it is not.

Designed for: researchers and students working on IPCC-related
bibliometric analyses, citation-based emission accounting, or any project
that needs a structured WoS-indexed database derived from large PDF
reports.

---

## 2. Workflow

| Stage | Input | Tool | Output | Needs API? |
|------:|-------|------|--------|:----------:|
| 1 | IPCC PDF | PDF parser + Crossref | `references.xlsx`, `wos_queries.txt` | No (Crossref is free) |
| 2 | `wos_queries.txt` | WoS Starter API *or* Playwright browser | `wos_*.xlsx` / `wos_*.json` | Recommended |
| 3 | `references.xlsx` + WoS exports | Merger | `Record.csv`, `Unrecord.csv` | No |

Each stage is a tab in the GUI. Each tab runs independently in its own
background thread with Pause / Resume / Stop controls, a live log, and a
progress bar. Every run produces a `task_report_*.txt` file summarising
status, elapsed time, success rate, outputs, and any errors.

---

## 3. Features

**Reliability and resilience**

- **Per-chapter failure isolation in Stage 1**: a single broken chapter
  no longer aborts the run. Failed chapters are written to
  `failed_chapters.csv` and `extraction_errors.log` with full Python
  tracebacks, while the rest of the PDF continues to be processed.
- **Structured `TaskReport` for every run**: status, elapsed time, total
  items, success / failure / skipped counts, success rate, outputs,
  failed extractions with tracebacks, and warnings — all captured in
  memory and persisted to a `task_report_*.txt` file in the output
  directory.
- **Never-raise runners**: the three stage functions
  (`run_extraction`, `run_wos_auto` / `run_wos_api`, `run_merge`) are
  designed so that even catastrophic failures return a populated
  `TaskReport` with `status="failed"` rather than crashing the GUI.
- **Crossref disk cache** (`crossref_cache.json`): re-runs are free
  and fast.

**User experience**

- Single-window Tkinter GUI with four tabs.
- Pause / Resume / Stop for every long-running task.
- Live log + progress bar per tab.
- Compact footer with author credit and a clickable contact link.
- Result popup on completion with success summary or warning.

**WoS integration (two paths)**

- **API path** (recommended): direct HTTPS calls to the WoS Starter API
  using your `X-ApiKey`. No browser needed, ToS-compliant, scriptable,
  unattended.
- **Browser path** (fallback): Playwright drives a real Chromium window
  with a persistent profile; you log in once via your institution's
  SSO and the toolkit walks every DOI batch and downloads each
  Excel export. Useful when no API access is available.

---

## 4. Installation

Tested on macOS, Linux, and Windows with Python 3.9 – 3.12.

```bash
# 1. Clone or download the project, then:
cd ipcc_refs_toolkit

# 2. (Recommended) create a virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. (Optional, only if you plan to use the browser path)
playwright install chromium
```

`requirements.txt` pulls in PyMuPDF (`pymupdf`), `pandas`, `openpyxl`,
`requests`, and `playwright`. PyMuPDF wheels are pre-built for all
common platforms.

**Tkinter** ships with most Python distributions on macOS and Windows.
On some Linux distributions you may need to install it separately:

```bash
# Ubuntu / Debian
sudo apt install python3-tk
```

Launch the GUI:

```bash
python ipcc_refs_gui.py
```

---

## 5. Using the GUI

### Tab 1 — Extract PDF

1. Pick your IPCC PDF.
2. Fill in `Report` (e.g. `AR6`), `Working Group` (e.g. `WG2`), and an
   email address (used in the Crossref polite-pool User-Agent, never
   stored or transmitted elsewhere).
3. Choose an output folder.
4. (Optional) `Max refs per chapter` — set to a small number (e.g. 5)
   for a quick first pass, `0` for all.
5. (Optional) `Chapters` — comma-separated chapter numbers to limit
   the run (e.g. `3,5,7`).
6. (Optional) `Skip Crossref` — extract raw references only, no DOI
   resolution. Useful for a very fast preview pass.
7. Click **Run**. Progress is shown in the bar and the log streams in
   real time. Use **Pause** to pause at the next safe point,
   **Resume** to continue, or **Stop** to abort and keep already-written
   outputs.

**On completion**: a popup summarises the run; a `task_report_*.txt`
file is written to the output folder. If any chapter failed,
`failed_chapters.csv` and `extraction_errors.log` are also written,
and the popup will be a yellow warning rather than a green success.

### Tab 2 — WOS Lookup

The most important field on this tab is the **WOS API key** input, which
is highlighted in **red** because it is the recommended path.

- If you paste a valid Starter API key, the toolkit uses HTTPS calls to
  the WoS Starter endpoint and writes one JSON file per batch plus a
  combined `wos_api_combined.xlsx`.
- If the API key field is empty, the toolkit falls back to Playwright
  browser automation. A real Chromium window opens; complete login in
  the browser, then click **Continue after login** in the GUI. The
  toolkit will then walk every DO=(...) batch and download an Excel
  export per batch.

See [§6 API Configuration](#6-api-configuration-web-of-science) for how
to obtain a key.

### Tab 3 — Merge

1. Point at `references.xlsx` (from Stage 1).
2. Point at the folder containing WoS exports (xlsx, xls, or
   tab-delimited txt — the loader auto-detects).
3. Click **Run**.

Outputs are `Record.csv` (full WoS schema, deduplicated by DOI) and
`Unrecord.csv` (eight-column slim schema of references not found in any
WoS export). Re-running is safe and idempotent — just drop more
exports into the folder and click Run again.

### Tab 4 — Help

A concise in-app summary of the above.

---

## 6. API Configuration (Web of Science)

The toolkit supports the **WoS Starter API** out of the box.

- Endpoint: `https://api.clarivate.com/apis/wos-starter/v1/documents`
- Auth header: `X-ApiKey: <your_key>`
- Documentation: <https://developer.clarivate.com/apis/wos-starter>

**How to obtain a key**

1. Sign in at <https://developer.clarivate.com/>.
2. Subscribe to *Web of Science Starter API*. Many universities and
   research institutes already have access included in their WoS
   subscription — check with your library before paying for a personal
   plan.
3. Copy the key from your developer dashboard.

**Where to paste it**

In the GUI: Tab 2 → red field labelled **"WOS API key (★ RECOMMENDED ★)"**.

The key is held in memory only for the lifetime of the GUI process; it
is never written to disk by the toolkit. If you need persistent
configuration, prefix the launch with an environment variable and add a
one-line read in `IpccToolkitGui.__init__`.

**Rate limiting and retries**

The toolkit issues one Starter query per DOI batch (50 DOIs each),
waits one second between batches, and on HTTP 429 (rate-limited) sleeps
30 s and retries once. Failures are recorded in the `TaskReport`
without aborting the run.

**Field coverage**

Starter returns a smaller field set than the official WoS Excel export
(no `Cited References`, no funding metadata, limited address parsing).
For a study that needs the full schema, use the browser path or
upgrade to the WoS Expanded API and adapt `_wos_api_to_row()`.

---

## 7. Output Files Reference

All files are written to the output folder you choose on each tab.

### Stage 1 (Extract PDF)

| File | Description |
|------|-------------|
| `references.xlsx` | One row per reference. 15-column schema (Report, WG, Chapter, Chapter Title, Authors, Article Title, Publisher, Year, Source Title, Raw Citation, DOI (Extracted), DOI Source, Crossref Score, Match Status). |
| `wos_queries.txt` | DOI batches in WoS Advanced Search syntax: `DO=("10.x/y" OR "10.x/z" OR ...)`, 50 DOIs per batch. Used as input to Stage 2. |
| `crossref_cache.json` | On-disk cache of Crossref responses. Safe to delete; will be rebuilt on the next run. |
| `task_report_Extract_PDF_*.txt` | Structured summary of the run. |
| `failed_chapters.csv` | (Only if any chapter failed.) One row per failed chapter with columns `chapter`, `title`, `error`, `traceback`. |
| `extraction_errors.log` | (Only if any chapter failed.) Human-readable per-chapter error report with full tracebacks. |

### Stage 2 (WOS Lookup)

API path:

| File | Description |
|------|-------------|
| `wos_api_batch_NNN.json` | Raw JSON response per batch. |
| `wos_api_combined.xlsx` | Flattened, deduplicated records across all batches. |
| `task_report_WOS_API_Starter_*.txt` | Run summary. |

Browser path:

| File | Description |
|------|-------------|
| `wos_batch_NNN.xlsx` | One file per batch. |
| `wos_batch_NNN.empty` | Marker for batches that returned zero results (so subsequent runs skip them). |
| `error_batch_NNN.png` | Page screenshot if a batch failed (for debugging UI changes). |
| `task_report_WOS_Auto_Browser_*.txt` | Run summary. |

### Stage 3 (Merge)

| File | Description |
|------|-------------|
| `Record.csv` | Full WoS schema (≈70 columns), one row per reference successfully found in WoS, deduplicated by normalised DOI. |
| `Unrecord.csv` | Slim 8-column schema (Report, Working Group, Chapter, Chapter title, Authors, Article Title, Publisher, Year) for references still missing from WoS. |
| `task_report_Merge_WOS_*.txt` | Run summary. |

---

## 8. Common Errors and Fixes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Cannot open PDF` | Corrupt or DRM-locked file | Re-download from the official IPCC site; if it is a scan, run `ocrmypdf input.pdf output.pdf` first. |
| `Chapter detection failed; using single-chapter fallback` | PDF has no usable TOC bookmarks | The toolkit will still extract references, but every ref will be assigned to a synthetic "Chapter 1". Manually edit the Chapter column afterwards if needed. |
| `WARNING — no References section detected` for some chapter | The chapter's References heading is non-standard | The chapter is recorded as zero references but does not stop the run. Inspect the PDF and consider extending `REFERENCES_HEADER_RE` in `ipcc_refs_gui.py`. |
| `Playwright not installed` on Stage 2 (browser path) | Missing browser binaries | `pip install playwright && playwright install chromium`. |
| `Advanced Search textarea not found` | WoS UI has changed | The toolkit tries multiple selector strategies and falls back through them. If all fail, screenshots are saved as `error_batch_NNN.png` for debugging. Update the selector list in `_wos_search_one`. |
| `401 unauthorized` from the API | Wrong, expired, or unsubscribed key | Verify the key in the Clarivate developer dashboard. Check your subscription covers the Starter API. |
| `429 rate-limited` | Exceeding Starter API quotas | The toolkit sleeps 30 s and retries once. If your quota is exhausted, wait for the daily window to reset. |
| `WOS files have no DOI column` warning in Merge | TXT exports were created without the "Full Record" option, or were saved in a non-standard encoding | Re-export from WoS with the `Full Record and Cited References` option selected. The loader handles utf-8-sig, utf-16, utf-8 and latin-1. |

---

## 9. Known Limitations

**Without a Web of Science API key, only Stage 1 runs locally.**

- Stage 1 (PDF reference extraction + Crossref enrichment) is fully
  functional and requires no Clarivate access. It is the only stage you
  can run end-to-end on a personal machine with no institutional API
  subscription.
- Stage 2 needs either a WoS Starter API key or the browser fallback.
  The browser fallback:
    - is ToS-sensitive (Clarivate's terms of use disallow automated
      scraping in general; even with a persistent SSO session your
      institution may flag the behaviour or rate-limit you);
    - **requires the machine to remain powered on, logged in, and
      connected to the network for the entire run**, which can take
      tens of minutes to several hours depending on batch count and
      delay settings. This is incompatible with a single-laptop
      mobile-work setup.
- Stage 3 runs locally and only needs Stage 1's outputs plus whatever
  WoS exports are available.

**Other current limitations**

- Reference splitting is a heuristic based on author-comma-initial
  patterns. PDFs with reference lists in non-standard formats (e.g.
  numbered bracketed citations) may need a custom splitter.
- The Starter API row mapping fills the major fields (UID, title,
  source, year, DOI, authors, document type, WoS citation count) but
  leaves the rest of the WoS schema empty.
- Crossref bibliographic search has a real false-positive rate on very
  short or generic citations. The DOI score is recorded so downstream
  filtering by `Crossref Score` is possible.
- Browser automation is single-tab and single-window; concurrent runs
  on the same machine will interfere with each other.

---

## 10. Roadmap

Planned improvements, in rough priority order:

1. **WoS Expanded API support**: optional toggle that uses the Expanded
   endpoint to fill the full schema (Cited References, funding,
   addresses).
2. **Persistent configuration file** (e.g. `~/.ipcc_refs_toolkit.yaml`)
   for API keys, default output paths, and last-used settings.
3. **Resume-from-checkpoint** for Stage 1: re-running a partial PDF
   should reuse already-extracted chapter data.
4. **OCR pre-pass** integration so scanned IPCC supplementary documents
   can be processed in one click.
5. **Optional Scopus / OpenAlex enrichment** in Stage 2 for citations
   that Crossref cannot match but other indexes can.
6. **Unit and integration test suite** with sample mini-PDFs and
   fixture WoS exports.
7. **Headless mode / CLI entry points** for batch processing on a
   server.

---

## 11. Author and License

**Author:** Jiacheng Zheng

**Contact / homepage:** <https://karcen.github.io/zhengjiacheng.github.io/>

This tool was built to support IPCC-related bibliometric research.
Contributions, bug reports, and feedback are welcome — please reach out
via the homepage above.

No license file is bundled; treat the code as research software for
academic use. If you wish to redistribute or reuse it in another
project, please contact the author first.
