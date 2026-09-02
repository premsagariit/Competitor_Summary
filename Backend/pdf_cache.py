"""
Persistent PDF -> JSON parsing cache.

Each insurer PDF is parsed with pdfplumber exactly once (per page: page
number, every detected IRDAI NL-form on that page, its extracted tables, and
its raw text) and the result is written to disk under cache/pdf_json/. Every
subsequent call - within the same run or a later one - reads the cached JSON
instead of re-running pdfplumber, as long as the source PDF's mtime/size
haven't changed.

This is the single canonical PDF-scanning path: gemini_extract.py (LLM
extraction) and pdf_extract.py (deterministic line-item lookups) both source
their pages from here instead of each independently re-opening/re-scanning
the PDF.
"""
import json
import os
import re

import pdfplumber

CACHE_ROOT = os.path.join("cache", "pdf_json")
FY, QUARTER = "FY26", "Q3"  # hardcoded for this calibration pass

COMPANY_PDFS = {
    "NBHI": "downloads/FY26/Q3/Niva_Bupa_Health_Insurance.pdf",
    "ABHI": "downloads/FY26/Q3/Aditya_Birla_Health_Insurance.pdf",
    "Care Health": "downloads/FY26/Q3/Care_Health_Insurance.pdf",
    "Star Health": "downloads/FY26/Q3/Star_Health_and_Allied_Insurance.pdf",
    "Manipal Cigna": "downloads/FY26/Q3/ManipalCigna_Health_Insurance.pdf",
    "Narayana Health": "downloads/FY26/Q3/Narayana_Health_Insurance.pdf",
    "Galaxy Health": "downloads/FY26/Q3/Galaxy_Health_Insurance.pdf",
}
_PDF_TO_COMPANY = {os.path.normpath(v): k for k, v in COMPANY_PDFS.items()}

# Canonical form-detection regex set - every NL form either extraction path
# looks for. Keyed by the short tag used in the Data Engine's "Source Tab"
# column.
FORM_PATTERNS = {
    "NL-1": ("Revenue Account (NL-1-B-RA)", r"FORM\s+NL-1-B-RA"),
    "NL-2": ("Profit & Loss Account (NL-2-B-PL)", r"FORM\s+NL-2-B-PL"),
    "NL-3": ("Balance Sheet (NL-3-B-BS)", r"FORM\s+NL-3-B-BS"),
    "NL-4": ("Premium Schedule (NL-4)", r"FORM\s+NL-4"),
    "NL-5": ("Claims Schedule (NL-5)", r"FORM\s+NL-5"),
    "NL-6": ("Commission Schedule (NL-6)", r"FORM\s+NL-6"),
    "NL-7": ("Operating Expenses Schedule (NL-7)", r"FORM\s+NL-7"),
    "NL-12": ("Investment Schedule (NL-12 & 12A)", r"FORM\s+NL-12"),
    "NL-20": ("Analytical Ratios Schedule (NL-20)", r"FORM\s+NL-20"),
    "NL-29": ("Detail Regarding Debt Securities (NL-29)", r"FORM\s+NL-29"),
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
    return os.path.join(CACHE_ROOT, FY, QUARTER, f"{safe}.json")


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
