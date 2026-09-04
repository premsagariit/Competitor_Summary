"""
Single source of truth for which reporting period (FY + Quarter) the pipeline
is currently running against.

`set_period()` must be called before importing any module whose module-level
constants derive a path from the active period (pdf_cache, data_engine,
pdf_report, etc.) - those modules read FY/QUARTER off this module at import
time. cli.py is the only entrypoint that does this ordering
correctly; don't import period-dependent modules before calling set_period().
"""
import os
import re
from datetime import datetime

from competitor_analysis import paths

FY = None
QUARTER = None

# The Data Engine template: headers + row structure, no filled values (or
# any stale values still on it are cleared by clear_period_values() before
# a run). Every pipeline run reads FROM this file and writes its filled
# result to a fresh, timestamped path via data_engine_output_path() -
# this file itself is never overwritten by a run.
DATA_ENGINE_TEMPLATE = str(paths.DATA_ENGINE_TEMPLATE)

GIC_FILENAME = "GIC.xlsx"

# Filename stems are fixed by scraper.py's own naming convention
# (company_key.replace(" ", "_").replace("&", "and") + ".pdf") and are
# independent of period - only the downloads/{FY}/{Quarter}/ prefix varies.
# Lives here (rather than in pdf_cache) so the Streamlit UI can resolve
# expected paths without importing any period-dependent module.
COMPANY_PDF_FILENAMES = {
    "NBHI": "Niva_Bupa_Health_Insurance.pdf",
    "ABHI": "Aditya_Birla_Health_Insurance.pdf",
    "Care Health": "Care_Health_Insurance.pdf",
    "Star Health": "Star_Health_and_Allied_Insurance.pdf",
    "Manipal Cigna": "ManipalCigna_Health_Insurance.pdf",
    "Narayana Health": "Narayana_Health_Insurance.pdf",
    "Galaxy Health": "Galaxy_Health_Insurance.pdf",
}

def download_filename(source_key: str, ext: str = "pdf") -> str:
    """The filename Phase 1 saves a source under, derived from its
    source_links.json key.

    This is the join between the project's two company-naming systems: the
    long portal names in source_links.json ("Star Health & Allied Insurance")
    and the short keys the extraction side uses ("Star Health"). They meet at
    the filename, and always have - if they disagreed, Phase 2 simply would
    not find what Phase 1 downloaded. Keeping the rule in one place makes that
    dependency explicit instead of duplicated."""
    return f"{source_key.replace(' ', '_').replace('&', 'and')}.{ext}"


def short_key_for_source(source_key: str) -> str | None:
    """Short extraction key for a source_links.json company name, or None for
    a source with no per-company PDF (e.g. GIC)."""
    filename = download_filename(source_key)
    for short, fname in COMPANY_PDF_FILENAMES.items():
        if fname == filename:
            return short
    return None


def company_slug(name: str) -> str:
    """URL/JSON-safe identifier for a company, stable across both naming
    systems: the short key is slugified where one exists, so the API, the UI
    and the pipeline all agree on one id per company."""
    short = short_key_for_source(name) or name
    return short.lower().replace(" ", "-").replace("&", "and")


# Canonical financial-year form: the SPAN, e.g. "FY25-26" for April 2025 to
# March 2026. Indian filings label themselves this way ("FY 2025-26"), so the
# span is what a user recognises, what a custom entry can be typed as, and what
# the retrieval agent is told to look for. A single-year form is ambiguous -
# "FY26" could be read as the year it starts or the year it ends - and that
# ambiguity was a real hazard when instructing the agent.
_FY_SPAN_RE = re.compile(r"^FY(\d{2})-(\d{2})$")
# Legacy single-year form, meaning the year the financial year ENDS: "FY26" ==
# "FY25-26". Still accepted on input so existing calls, saved UI state and
# on-disk names keep working; normalised to the span form immediately.
_FY_LEGACY_RE = re.compile(r"^FY(\d{2})$")

FY_FORMAT_HINT = "FY25-26 (April 2025 - March 2026); the legacy form FY26 also works"


def normalize_fy(fy: str) -> str:
    """Canonical 'FY25-26' for any accepted spelling of a financial year.

    Accepts the span form, the legacy single-year form, and the common
    variations a person might type: '2025-26', 'FY 25-26', 'fy2025-2026'.
    Raises ValueError with the expected format on anything else."""
    if fy is None:
        raise ValueError(f"Financial year is required - expected {FY_FORMAT_HINT}.")
    raw = str(fy).strip().upper().replace(" ", "").replace("_", "-").replace("/", "-")
    if raw.startswith("FY"):
        raw = raw[2:]
    # 4-digit years on either side: 2025-2026 / 2025-26
    m = re.match(r"^(\d{4})-(\d{2,4})$", raw)
    if m:
        start = int(m.group(1))
        end_raw = m.group(2)
        end = int(end_raw) if len(end_raw) == 4 else 2000 + int(end_raw)
        return _span(start, end)
    m = re.match(r"^(\d{2})-(\d{2})$", raw)
    if m:
        return _span(2000 + int(m.group(1)), 2000 + int(m.group(2)))
    m = re.match(r"^(\d{2})$", raw)          # legacy: year the FY ends
    if m:
        end = 2000 + int(m.group(1))
        return _span(end - 1, end)
    m = re.match(r"^(\d{4})$", raw)          # legacy, 4-digit end year
    if m:
        end = int(m.group(1))
        return _span(end - 1, end)
    raise ValueError(f"Invalid financial year {fy!r} - expected {FY_FORMAT_HINT}.")


def _span(start_year: int, end_year: int) -> str:
    if end_year != start_year + 1:
        raise ValueError(
            f"Invalid financial year FY{start_year % 100:02d}-{end_year % 100:02d} - "
            f"a financial year spans exactly one year boundary "
            f"(e.g. FY{start_year % 100:02d}-{(start_year + 1) % 100:02d}).")
    return f"FY{start_year % 100:02d}-{end_year % 100:02d}"


def fy_start_year(fy: str = None) -> int:
    """Calendar year the financial year starts in (April)."""
    fy = normalize_fy(fy or FY)
    return 2000 + int(_FY_SPAN_RE.match(fy).group(1))


def fy_end_year(fy: str = None) -> int:
    """Calendar year the financial year ends in (March)."""
    return fy_start_year(fy) + 1


_FY_RE = _FY_SPAN_RE
_QUARTERS = ("Q1", "Q2", "Q3", "Q4")

# Quarter -> (month whose cumulative figures close out that quarter, offset
# from the FY's start calendar year). FY25-26 starts April 2025, so Q1 (June),
# Q2 (September) and Q3 (December) all fall in 2025 - the FY's start year -
# while Q4 (March) falls in 2026, the FY's end year.
_QUARTER_MONTHS = {
    "Q1": ("June", 0),
    "Q2": ("September", 0),
    "Q3": ("December", 0),
    "Q4": ("March", 1),
}


def set_period(fy: str, quarter: str):
    """Set the reporting period. `fy` is normalised to the canonical span
    form, so callers may pass FY25-26, FY26, 2025-26 or 2025-2026."""
    global FY, QUARTER
    fy = normalize_fy(fy)  # raises ValueError with the expected format
    quarter = (quarter or "").strip().upper()
    if quarter not in _QUARTERS:
        raise ValueError(f"Invalid quarter {quarter!r} - expected one of {_QUARTERS}.")
    FY, QUARTER = fy, quarter


def _require(fy, quarter):
    """Resolve and normalise a period, defaulting to the configured one.

    Normalising HERE is what keeps every period-derived path, column header
    and label consistent: this is the single gate they all pass through, so a
    caller still using the legacy 'FY26' form reaches exactly the same
    directory and column as 'FY25-26' rather than quietly creating a parallel
    set of files under the old name."""
    fy = fy or FY
    quarter = quarter or QUARTER
    if fy is None or quarter is None:
        raise RuntimeError("config.set_period() must be called before use.")
    return normalize_fy(fy), str(quarter).strip().upper()


def calendar_mapping(fy: str = None, quarter: str = None) -> dict:
    """Maps e.g. FY25-26 Q3 to {"month": "December", "year": 2025} - the
    calendar month/year whose cumulative closing figures that quarter's
    disclosure covers. Q1-Q3 fall in the FY's start year, Q4 in its end
    year."""
    fy, quarter = _require(fy, quarter)
    start_year = fy_start_year(fy)
    month, year_offset = _QUARTER_MONTHS[quarter]
    return {"month": month, "year": start_year + year_offset}


def prior_fy(fy: str = None) -> str:
    fy = fy or FY
    if fy is None:
        raise RuntimeError("pipeline_config.set_period() must be called before use.")
    start = fy_start_year(fy)
    return _span(start - 1, start)


def next_fy(fy: str = None) -> str:
    """The financial year after this one - FY25-26 -> FY26-27."""
    start = fy_start_year(fy or FY)
    return _span(start + 1, start + 2)


def fy_label(fy: str = None) -> str:
    """'2025-26' - how Indian filings, and the financial-year dropdowns on the
    insurers' disclosure portals, spell a financial year. This is what the
    retrieval agent is told to look for.

    Derived from the FY, never from a quarter's calendar year: Q4 of FY25-26
    falls in March 2026, so deriving the label from the quarter would name
    'FY 2026-27' and send the agent to the wrong dropdown entry."""
    start = fy_start_year(fy or FY)
    return f"{start}-{(start + 1) % 100:02d}"


def fy_label_long(fy: str = None) -> str:
    """'2025-2026' - the other spelling seen on portal filters."""
    start = fy_start_year(fy or FY)
    return f"{start}-{start + 1}"


def fy_options(back: int = 3, forward: int = 1, today=None) -> list[str]:
    """Selectable financial years around the current one, newest first.

    Derived from the clock rather than hardcoded, so the list stays current
    without an annual edit. India's FY starts in April, so before April the
    current FY is still the one that began the previous calendar year."""
    now = today or datetime.now()
    start = now.year if now.month >= 4 else now.year - 1
    years = [_span(start + off, start + off + 1)
             for off in range(forward, -back - 1, -1)]
    return years


def download_dir(fy: str = None, quarter: str = None) -> str:
    fy, quarter = _require(fy, quarter)
    return str(paths.DOWNLOADS_DIR / fy / quarter)


def gic_path(fy: str = None, quarter: str = None) -> str:
    return os.path.join(download_dir(fy, quarter), GIC_FILENAME)


def expected_pdf_path(company: str, fy: str = None, quarter: str = None, root: str = "") -> str:
    """Where a given insurer's PDF must live for this period - the canonical
    destination a hand-uploaded file has to be written to for Phase 2 to
    pick it up."""
    fy, quarter = _require(fy, quarter)
    return os.path.join(root, download_dir(fy, quarter), COMPANY_PDF_FILENAMES[company])


def source_availability(fy: str = None, quarter: str = None, root: str = "") -> dict:
    """Which insurer PDFs and GIC.xlsx are actually on disk for a period.

    A pure filesystem check, deliberately independent of whatever Phase 1
    reported: it's authoritative if a download partially failed, and it also
    picks up files a user supplied by hand afterwards. `root` lets a caller
    whose cwd isn't the Backend directory (e.g. the Streamlit app) resolve
    against an explicit base.
    """
    fy, quarter = _require(fy, quarter)
    base = os.path.join(root, download_dir(fy, quarter))
    found, missing = {}, {}
    for company, filename in COMPANY_PDF_FILENAMES.items():
        path = os.path.join(base, filename)
        (found if os.path.exists(path) else missing)[company] = path
    gic = os.path.join(base, GIC_FILENAME)
    return {
        "download_dir": base,
        "gic_found": os.path.exists(gic),
        "gic_path": gic,
        "companies_found": found,
        "companies_missing": missing,
    }


def audit_dir(fy: str = None, quarter: str = None) -> str:
    fy, quarter = _require(fy, quarter)
    return str(paths.EXTRACTION_AUDIT_DIR / fy / quarter)


def output_pdf_path(fy: str = None, quarter: str = None) -> str:
    fy, quarter = _require(fy, quarter)
    return str(paths.OUTPUT_DIR / f"Competition_Summary_{fy}_{quarter}.pdf")


def data_engine_output_path(fy: str = None, quarter: str = None) -> str:
    """A fresh, timestamped path so every pipeline run produces its own Data
    Engine file instead of overwriting a previous run's - re-running the
    same FY/Quarter twice keeps both results on disk for comparison."""
    fy, quarter = _require(fy, quarter)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(paths.OUTPUT_DIR / f"Data_Engine_{fy}_{quarter}_{ts}.xlsx")


def period_label(fy: str = None, quarter: str = None) -> str:
    """'FY25-26 Q3' - display form."""
    fy, quarter = _require(fy, quarter)
    return f"{fy} {quarter}"


def cur_period_label() -> str:
    return period_label(FY, QUARTER)


def prior_period_label() -> str:
    return period_label(prior_fy(FY), QUARTER)


def months_elapsed(fy: str = None, quarter: str = None) -> int:
    """Months of the financial year covered by a quarter's cumulative ('up to
    the quarter') figures: Q1=3, Q2=6, Q3=9, Q4=12. Use this to turn any YTD
    cumulative amount into a monthly run-rate - hardcoding 9 silently
    misstates every other quarter by up to 4x."""
    fy, quarter = _require(fy, quarter)
    return {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}[quarter]


def period_column(fy: str = None, quarter: str = None) -> str:
    """'FY25-26_Q3' - the Data Engine's column HEADER for a period
    (underscored, unlike period_label's spaced display form).

    Normalises first, so a legacy 'FY26' from an older caller or a saved UI
    state cannot leak a legacy header into the workbook."""
    fy, quarter = _require(fy, quarter)
    return f"{fy}_{quarter}"


def cur_period_column() -> str:
    """Header of the current-period value column, e.g. 'FY25-26_Q3'."""
    return period_column(FY, QUARTER)


def prior_period_column() -> str:
    """Header of the prior-year comparative column, e.g. 'FY24-25_Q3'. Same
    quarter, previous financial year - every Data Engine metric is a
    year-on-year comparison of the same quarter."""
    return period_column(prior_fy(FY), QUARTER)


def period_ending_str(fy: str = None, quarter: str = None) -> str:
    """'31 Dec 2025' - the calendar date the quarter's cumulative figures end on."""
    cal = calendar_mapping(fy, quarter)
    last_day = {"June": 30, "September": 30, "December": 31, "March": 31}[cal["month"]]
    return f"{last_day} {cal['month'][:3]} {cal['year']}"


def cur_period_ending_str() -> str:
    return period_ending_str(FY, QUARTER)


def prior_period_ending_str() -> str:
    return period_ending_str(prior_fy(FY), QUARTER)
