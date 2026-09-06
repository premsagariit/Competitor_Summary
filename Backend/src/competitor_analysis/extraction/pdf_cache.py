"""
Persistent PDF -> JSON parsing cache.

Each insurer PDF is parsed with pdfplumber exactly once (per page: page
number, every detected IRDAI NL-form on that page, its extracted tables, and
its raw text) and the result is written to disk under cache/pdf_json/. Every
subsequent call - within the same run or a later one - reads the cached JSON
instead of re-running pdfplumber, as long as the source PDF's mtime/size
haven't changed.

This is the single canonical PDF-scanning path: extraction/gemini.py (LLM
extraction) and forms.py (deterministic line-item lookups) both source
their pages from here instead of each independently re-opening/re-scanning
the PDF.
"""
import json
import os
import re

import pdfplumber

from competitor_analysis import config as cfg
from competitor_analysis import paths

CACHE_ROOT = str(paths.PDF_JSON_CACHE)

# Canonical per-company filenames now live in pipeline_config (period-
# independent config the UI also needs); re-exported here since callers
# already reference pdf_cache.COMPANY_PDF_FILENAMES.
COMPANY_PDF_FILENAMES = cfg.COMPANY_PDF_FILENAMES


def discover_company_pdfs(fy: str = None, quarter: str = None,
                          announce_missing: bool = True) -> dict:
    """{company: path} restricted to insurers whose PDF actually exists under
    downloads/{fy}/{quarter}/ - a missing insurer is logged and simply
    omitted, so every downstream stage (which iterates this dict's keys)
    skips it instead of crashing."""
    base = cfg.download_dir(fy, quarter)
    out = {}
    for company, filename in COMPANY_PDF_FILENAMES.items():
        path = os.path.join(base, filename)
        if os.path.exists(path):
            out[company] = path
        elif announce_missing:
            print(f"[pdf_cache] {company}: PDF not found at {path} - skipping this insurer.")
    return out


# Mutated IN PLACE by refresh_company_pdfs(), never rebound - forms.py,
# gemini.py and data_engine.py all hold `from ... import COMPANY_PDFS`
# bindings to this exact dict, so rebinding here would leave them stale.
COMPANY_PDFS = {}
_PDF_TO_COMPANY = {}


def refresh_company_pdfs():
    """Re-resolve which insurer PDFs exist for the configured period.

    Registered as a config period-change listener below, and called directly
    by the stages that read the map, because it goes stale two ways:

    1. Period. The map used to be resolved once at import. A fresh CLI
       process is fine (cli.run_build sets the period before importing), but
       the API server is long-lived: after a Q3 run had imported this module,
       a Q4 run in the same process read Q3's PDFs while writing a
       Q4-labelled workbook and Q4 cache entries - silent and plausible.
       api/runs.py's guard does not catch that: it checks which companies are
       known, not which period their paths point at.
    2. Filesystem. Phase 1 downloads into the period's directory *after* the
       period is set, so a scan pinned to the period alone would still miss
       files that landed in between.

    Re-scans every call (seven os.path.exists) but only mutates and reports
    when the result actually differs, so repeat calls are quiet."""
    if cfg.FY is None or cfg.QUARTER is None:
        return COMPANY_PDFS  # period not set yet; the listener will populate
    fresh = discover_company_pdfs(announce_missing=False)
    if fresh == COMPANY_PDFS:
        return COMPANY_PDFS
    missing = sorted(set(COMPANY_PDF_FILENAMES) - set(fresh))
    COMPANY_PDFS.clear()
    COMPANY_PDFS.update(fresh)
    _PDF_TO_COMPANY.clear()
    _PDF_TO_COMPANY.update({os.path.normpath(v): k for k, v in fresh.items()})
    for company in missing:
        print(f"[pdf_cache] {company}: PDF not found in {cfg.download_dir()} "
              f"- skipping this insurer.")
    return COMPANY_PDFS


refresh_company_pdfs()
cfg.on_period_change(refresh_company_pdfs)

# Canonical form-detection regex set - every NL form either extraction path
# looks for. Keyed by the short tag used in the Data Engine's "Source Tab"
# column.
FORM_PATTERNS = {
    "NL-1": ("Revenue Account (NL-1-B-RA)", r"FORM\s+NL-1-B-RA"),
    "NL-2": ("Profit & Loss Account (NL-2-B-PL)", r"FORM\s+NL-2-B-PL"),
    "NL-3": ("Balance Sheet (NL-3-B-BS)", r"FORM\s+NL-3-B-BS"),
    # (?!\d) - not \b - stops this matching "FORM NL-41" while still matching
    # "FORM NL-12A" for NL-12's own pattern below (see KNOWN_ISSUES.md #1).
    "NL-4": ("Premium Schedule (NL-4)", r"FORM\s+NL-4(?!\d)"),
    "NL-5": ("Claims Schedule (NL-5)", r"FORM\s+NL-5"),
    "NL-6": ("Commission Schedule (NL-6)", r"FORM\s+NL-6"),
    "NL-7": ("Operating Expenses Schedule (NL-7)", r"FORM\s+NL-7"),
    "NL-12": ("Investment Schedule (NL-12 & 12A)", r"FORM\s+NL-12"),
    "NL-20": ("Analytical Ratios Schedule (NL-20)", r"FORM\s+NL-20"),
    # Care Health's filing heads this schedule "NL-29 DETAILS REGARDING DEBT
    # SECURITIES" with no "FORM" prefix, so a FORM-anchored pattern missed the
    # page entirely and all 10 debt rating/maturity fields came back not-found.
    # "FORM" is optional here, but the match must start a LINE: index/contents
    # pages list every schedule mid-line ("30 NL-29-DEBT SECURITIES ..."), and
    # an unanchored optional-FORM pattern matches those too - on Star Health
    # that index (page 2) precedes the real page 35, so get_form_page's
    # pages[:1] would have read the index instead. See KNOWN_ISSUES.md.
    "NL-29": ("Detail Regarding Debt Securities (NL-29)", r"(?m)^\s*(?:FORM\s+)?NL-29(?!\d)"),
    "NL-31": ("Statement of Investment and Income on Investment (NL-31)", r"FORM\s+NL-31"),
    "NL-33": ("Reinsurance/Retrocession Risk Concentration (NL-33)", r"FORM\s+NL-33"),
    "NL-34": ("Geographical Distribution of Business (NL-34)", r"FORM\s+NL-34"),
    "NL-36": ("Business - Channels Wise (NL-36)", r"FORM\s+NL-36"),
    "NL-37": ("Claims Data (NL-37)", r"FORM\s+NL-37"),
    "NL-41": ("Offices Information (NL-41)", r"FORM\s+NL-41"),
}


def _company_for_path(pdf_path, company=None):
    if company:
        return company
    return _PDF_TO_COMPANY.get(
        os.path.normpath(pdf_path), os.path.splitext(os.path.basename(pdf_path))[0]
    )


def _cache_path(company):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", company)
    return os.path.join(CACHE_ROOT, cfg.FY, cfg.QUARTER, f"{safe}.json")


def parse_pdf_to_json(pdf_path, company):
    """One pdfplumber pass: every page's number, detected form(s), tables and
    raw text - both tables and text are stored (not just whichever exists)
    since different callers need one or the other."""
    pages_out = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            forms_detected = [
                key for key, (_, pattern) in FORM_PATTERNS.items()
                if re.search(pattern, text, re.IGNORECASE)
            ]
            pages_out.append({
                "page_number": i + 1,
                "forms_detected": forms_detected,
                "tables": page.extract_tables(),
                "text": text,
            })
    stat = os.stat(pdf_path)
    return {
        "company": company,
        "source_path": pdf_path,
        "source_file": os.path.basename(pdf_path),
        "pdf_mtime": stat.st_mtime,
        "pdf_size": stat.st_size,
        "pages": pages_out,
    }


def get_company_json(pdf_path, company=None, force_refresh=False):
    """Cache hit iff the cached file's stored mtime+size match the current
    PDF; otherwise (re)parses and overwrites."""
    company = _company_for_path(pdf_path, company)
    stat = os.stat(pdf_path)
    cache_path = _cache_path(company)
    if not force_refresh and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("pdf_mtime") == stat.st_mtime and cached.get("pdf_size") == stat.st_size:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    doc = parse_pdf_to_json(pdf_path, company)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, default=str)
    return doc


def pages_for_form(doc_json, form_key, max_pages=None):
    pages = [p for p in doc_json["pages"] if form_key in p["forms_detected"]]
    return pages[:max_pages] if max_pages else pages


def pages_for_pattern(doc_json, pattern, page_hint_range=None):
    pages = doc_json["pages"]
    if page_hint_range is not None:
        allowed = set(page_hint_range)
        pages = [p for p in pages if (p["page_number"] - 1) in allowed]
    return [p for p in pages if re.search(pattern, p["text"] or "", re.IGNORECASE)]
