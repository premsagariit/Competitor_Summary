# Competitor Analysis Pipeline

Competitor analysis and report generation for Indian health insurers — the
seven standalone health insurers (SAHI) plus industry-wide GIC statistics.

## Layout

```
Backend/
├── pyproject.toml                    package metadata, deps, pytest config
├── src/competitor_analysis/
│   ├── paths.py                      all filesystem locations (repo-anchored)
│   ├── config.py                     reporting period (FY/quarter) + derived names
│   ├── cli.py                        entrypoint: download / build / all
│   ├── pipeline.py                   Phase 2 stage runner
│   ├── ingestion/scraper.py          Phase 1: retrieve filings
│   ├── extraction/                   Phase 2
│   │   ├── pdf_cache.py              one-pass pdfplumber scan, cached to disk
│   │   ├── forms.py                  IRDAI NL-form line-item lookups
│   │   ├── gemini.py                 LLM extraction (batched, cached, rate-limited)
│   │   ├── metric_specs.py           metric registry / provenance
│   │   └── data_engine.py            writes the Data Engine workbook
│   ├── reporting/                    Phase 3
│   │   ├── data.py                   flat access layer over the workbook
│   │   ├── theme.py                  colours, company-name resolution
│   │   ├── charts.py                 matplotlib chart builders
│   │   └── report.py                 PDF assembly
│   └── api/                          HTTP API for the React dashboard
│       ├── main.py                   FastAPI routes
│       └── runs.py                   run execution + progress tracking
├── app/streamlit_app.py              Phase 4: pipeline control dashboard
├── tests/
├── data/                             INPUTS (version-controlled)
│   ├── downloads/{FY}/{Q}/           retrieved filings
│   ├── templates/                    blank Data Engine workbook
│   ├── assets/logos/
│   └── source_links.json             disclosure portal roots
└── artifacts/                        OUTPUTS (regenerable)
    ├── Data_Engine_UI.xlsx           working workbook
    ├── output/                       reports, dated workbooks
    └── cache/                        PDF-JSON, LLM responses, audits
```

`data/` is what you keep; `artifacts/` is what the pipeline produces. Nothing
resolves paths relative to the current working directory — every location comes
from `paths.py`, so the pipeline runs identically from anywhere.

## Setup

```bash
cd Backend
myenv/Scripts/python.exe -m pip install -e ".[dev]"
```

Create `Backend/.env`:

```
PARALLEL_API_KEY=...   # Phase 1 document retrieval
GEMINI_API_KEY=...     # Phase 2 extraction
```

## Running

```bash
# Everything, for one reporting period
python -m competitor_analysis.cli --fy FY25-26 --quarter Q3 --stage all

# Or in halves, with a human check in between
python -m competitor_analysis.cli --fy FY25-26 --quarter Q3 --stage download
python -m competitor_analysis.cli --fy FY25-26 --quarter Q3 --stage build

# Dashboard (runs the CLI as a subprocess per stage)
streamlit run app/streamlit_app.py

# HTTP API for the React dashboard in Frontend/
python -m uvicorn competitor_analysis.api.main:app --reload --port 8000
```

The API exposes the same phases as the CLI (`POST /api/pipeline/run` with
`stages: ["download", "build"]`), plus run status, the review queue and
artifact downloads. Interactive docs at http://localhost:8000/docs. See
`Frontend/README.md` for the endpoint list and the UI's known gaps.

## Reporting periods

A financial year is written as the **span** it covers: `FY25-26` means April
2025 to March 2026 — the same way the filings themselves are labelled ("FY
2025-26"). This is what the retrieval agent is told to look for, so the
instruction is unambiguous; a single-year form leaves open whether the year
named is the one the FY starts or ends in.

Accepted on input and normalised to the canonical span:

| Typed | Resolves to |
|---|---|
| `FY25-26`, `fy 25-26`, `25-26` | `FY25-26` |
| `2025-26`, `2025-2026` | `FY25-26` |
| `FY26` (legacy — the year the FY *ends*) | `FY25-26` |

`FY25-27` is rejected: a financial year crosses exactly one year boundary.

The period is the only thing you change between quarters. `config.py` derives
the calendar mapping (FY25-26 Q3 → December 2025; Q4 → March 2026), the
download directory (`data/downloads/FY25-26/Q3/`), the workbook column headers
(`FY25-26_Q3` / `FY24-25_Q3`), the financial-year label handed to the agent,
and the months-elapsed divisor for year-to-date run-rates. Nothing hardcodes a
date, and the selectable year list is derived from the clock.

## Testing

```bash
myenv/Scripts/python.exe -m pytest
```

Tests are period-agnostic: expected values are read live from the source
filings at test time rather than hardcoded, so they keep testing the logic
rather than one quarter's numbers. The exception is a small number of tests
that deliberately assert against the current filings' column layouts; those say
so in their docstrings.

## Notes on the two external APIs

**Phase 1 (Parallel AI).** Processors escalate `core` → `pro`, each with its own
short timeout, then a Search API fallback. `ultra` is available but not in the
default ladder (cost); enable it per company with `"processors"` in
`data/source_links.json`. Successful URLs are cached as period-parameterised
templates, so later quarters usually skip the agent entirely.

**Phase 2 (Gemini).** The binding constraint is the requests-per-minute quota
(15 on the free tier), not local parallelism — so batches are as large as the
API accepts (20 metrics; 40 is rejected) to minimise request count, issued
concurrently under one shared rate limiter, with 429-aware retry. Responses are
cached on disk keyed by company + source tables + metric prompts + model +
period, which makes a re-run essentially free. Use `--no-cache` after changing
a metric prompt.
