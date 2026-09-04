"""
Generic helpers for pulling line-item figures out of IRDAI quarterly public
disclosure PDFs (Form NL-1 through NL-46). All seven downloaded PDFs share the
same form layout/order, just with different page offsets, so these helpers
locate content by form-name / column-header text rather than fixed page
numbers.

All monetary figures in the source PDFs are "Amount in Rs. Lakhs"; callers
divide by 100 to get Crores where the Data Engine expects Crores.

Two distinct table layouts show up across insurers for the same form:
  - side-by-side: one row has both current- and prior-year cumulative columns
    (e.g. NBHI/Star NL-1, NL-2).
  - stacked blocks: the whole label set repeats once per year, current-year
    block first then prior-year block below it, in the same table (ABHI NL-4).
`_col_for` below resolves each label-row's columns from whichever header
last appeared above it in reading order, which handles both cases uniformly.
"""
import re

from competitor_analysis.extraction import pdf_cache
from competitor_analysis import config as cfg
from competitor_analysis.extraction.pdf_cache import COMPANY_PDFS

# Every time a column/period cannot be resolved from a form's own header text
# and a documented fallback is used instead, the reason is appended here
# rather than being applied silently. The pipeline prints this after each run
# and the regression tests assert against it.
RESOLUTION_LOG = []


def _log(msg):
    RESOLUTION_LOG.append(msg)
    return msg


def year_frags(fy=None, quarter=None):
    """(current_year_fragments, prior_year_fragments) for the reporting period
    configured in pipeline_config - e.g. FY26 Q3 closes December 2025, giving
    (("2025", "-25"), ("2024", "-24")). Derived from the configured period
    rather than hardcoded, so next quarter's filings (whose headers print
    different years) resolve without a code change. Insurers spell the year
    either in full ("December 31, 2025") or 2-digit ("31-Dec-25"), so both
    spellings are offered for each year."""
    y = cfg.calendar_mapping(fy, quarter)["year"]
    return ((str(y), f"-{y % 100:02d}"), (str(y - 1), f"-{(y - 1) % 100:02d}"))


def parse_num(s):
    """'1,45,357' -> 145357.0 ; '(1,234)' -> -1234.0 ; '-' / '' / None -> 0.0.
    Some PDFs (e.g. ManipalCigna) render large numbers with a stray internal
    space, e.g. '1 ,54,542' - strip ALL whitespace, not just leading/trailing."""
    if s is None:
        return 0.0
    s = re.sub(r"\s+", "", str(s))
    if s in ("", "-", "–", "—"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("–", "-").strip()
    if s in ("", "-"):
        return 0.0
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _is_cumulative_header(text):
    """Cumulative ('up to the quarter' / YTD) column headers use different
    phrasing across insurers - e.g. NBHI/Star: 'Up to the quarter ended ...';
    ABHI: 'For The Period Ended ...' (vs. their single-quarter 'For The
    Quarter Ended ...'). Detect generically rather than hardcoding one
    phrasing."""
    low = text.lower()
    return "up to" in low or "upto" in low or "period ended" in low


_PERIOD_PHRASE_RE = re.compile(
    r"(up\s*to|upto|for)\s+the\s+(?:corresponding\s+)?(quarter|period)", re.I)

# Point-in-time schedules (Balance Sheet, Investment/AUM, Offices) don't use
# "up to"/"for the quarter" phrasing at all - they print a snapshot date pair
# instead, e.g. "As at December 31, 2025" / "As at December 31, 2024". Still a
# genuine current/prior comparison, just not a cumulative-vs-quarter one - so
# it gets its own "snapshot" kind rather than being force-fit into either.
_SNAPSHOT_PHRASE_RE = re.compile(r"as\s+(?:at|on)\b", re.I)


def classify_period_columns(table, cur_year_frag=None, prior_year_frag=None):
    """For a pdfplumber-extracted table (a list of rows, each a list of
    cells), classify every header-bearing column by which reporting period it
    refers to.

    Returns a list of {"row": row_idx, "col": col_idx,
    "period": "current_cumulative" | "prior_cumulative" | "current_quarter" |
    "prior_quarter" | "current_snapshot" | "prior_snapshot" | "unknown"
    (period phrasing found but no year fragment matched - never guessed),
    "header_text": the (possibly forward-filled) header text,
    "forward_filled": bool}, in table reading order. A table can carry
    several such events for the very same column - a "stacked block" layout
    repeats the same column position once per year block, each at a
    different row - so a caller resolves a specific data row's column by the
    nearest PRECEDING event, never by assuming one classification per column
    (see _col_for below).

    Merged header cells surface from pdfplumber as None for every column
    after a merge's first (leftmost) one; those are forward-filled here with
    the last real header text seen earlier in the row, so every column of
    the merged span carries it. A literal "-" is a printed nil in the filing,
    not a merge continuation - it is never forward-filled itself, and it
    never becomes the fill value for a later None.

    "Cumulative" detection reuses _is_cumulative_header's looser substring
    check (not just the stricter _PERIOD_PHRASE_RE below it), so this stays a
    strict superset of every header this module has ever recognized as
    cumulative - callers that only ever wanted that (get_line_item, via
    _header_events) see identical behavior to before this function existed.
    "Quarter" and "snapshot" are new classifications this module didn't
    previously make at all, needed by callers that must label every column
    rather than just find the one cumulative column they want. Neither form
    that actually feeds get_line_item today (NL-1, NL-2, NL-4) uses "as at"
    phrasing, so adding snapshot detection cannot change get_line_item's
    behavior - verified against all 7 companies' real filings.
    """
    if cur_year_frag is None or prior_year_frag is None:
        _c, _p = year_frags()
        cur_year_frag = cur_year_frag or _c
        prior_year_frag = prior_year_frag or _p

    events = []
    for ridx, row in enumerate(table):
        last = None
        for cidx, cell in enumerate(row):
            forward_filled = cell is None
            if forward_filled:
                cell = last
            elif cell and str(cell).strip() != "-":
                last = cell
            if not cell:
                continue
            text = " ".join(str(cell).split())

            if _is_cumulative_header(text):
                kind = "cumulative"
            else:
                m = _PERIOD_PHRASE_RE.search(text)
                if m:
                    kind = "cumulative" if m.group(1).lower().replace(" ", "").startswith(("upto", "up")) else "quarter"
                elif _SNAPSHOT_PHRASE_RE.search(text):
                    kind = "snapshot"
                else:
                    continue

            if any(f.lstrip("-") in text for f in cur_year_frag):
                period = f"current_{kind}"
            elif any(f.lstrip("-") in text for f in prior_year_frag):
                period = f"prior_{kind}"
            else:
                period = "unknown"
            events.append({"row": ridx, "col": cidx, "period": period,
                           "header_text": text, "forward_filled": forward_filled})
    return events


def _is_decorative_row(row):
    """A row with at most one non-empty, non-'-' cell is a page-wide
    caption/title/section-divider (spanning the table only because a
    downstream forward-fill would spread its one real cell across every
    column) rather than a per-column header - e.g. a form's title line, or a
    bare section label like "STATES" with nothing else in the row. It
    carries no per-column information, so it must be dropped before either
    column classifier sees the table: left in, it forward-fills as a single
    spurious "header" spanning every column, indistinguishable from a
    genuine one."""
    real = [c for c in row if c and str(c).strip() not in ("", "-")]
    return len(real) <= 1 and len(row) > 2


def strip_decorative_rows(table):
    """Drop decorative rows (see _is_decorative_row) before classifying a
    table with classify_period_columns() and/or classify_group_columns().
    Returns a new table; row indices in classifier output are relative to
    THIS returned table, not the original."""
    return [row for row in table if not _is_decorative_row(row)]


def classify_group_columns(table):
    """For a pdfplumber-extracted table, classify every column by which
    named GROUP/CATEGORY it belongs to - a line-of-business breakdown
    (Health / Personal Accident / Travel / Total, NL-34), a fund breakdown
    (Shareholders / Policyholders / Total, NL-12), a premium-vs-policy-count
    breakdown (NL-36) - any header text that is NOT itself a period phrase.
    This is the second axis classify_period_columns doesn't cover: a caller
    composes the two (period x group) to identify one specific column in a
    multi-category table, e.g. NL-34's "Total" segment's cumulative column,
    not Health's or Personal Accident's.

    Returns a list of {"row": row_idx, "col": col_idx, "label": text,
    "forward_filled": bool}, one entry per header cell that is NOT a period/
    quarter/snapshot header (a cell is classified by exactly one of
    classify_period_columns or this function, never both). Forward-fills
    merged header cells exactly as classify_period_columns does (a literal
    "-" is never forward-filled itself, nor does it become the fill value
    for a later None).

    Callers should run this against a table that has already had decorative
    rows stripped (strip_decorative_rows) - a page-wide title row would
    otherwise surface here as a spurious group label spanning every column,
    exactly as it would for the period axis.
    """
    events = []
    for ridx, row in enumerate(table):
        last = None
        for cidx, cell in enumerate(row):
            forward_filled = cell is None
            if forward_filled:
                cell = last
            elif cell and str(cell).strip() != "-":
                last = cell
            if not cell:
                continue
            text = " ".join(str(cell).split())
            if _is_cumulative_header(text) or _PERIOD_PHRASE_RE.search(text) or _SNAPSHOT_PHRASE_RE.search(text):
                continue
            events.append({"row": ridx, "col": cidx, "label": text, "forward_filled": forward_filled})
    return events


def _header_events(table):
    """[(row_idx, col_idx, text), ...] sorted by row_idx, for every
    cumulative-column header cell found anywhere in the table.

    A thin back-compat view over classify_period_columns(): restricted to
    genuine (non-forward-filled) cumulative header cells, in the exact shape
    _col_for below has always consumed. Excluding forward-filled columns
    matters - without it, a merged period header spanning several sub-columns
    would hand _col_for the RIGHTMOST sub-column instead of the anchor
    _extend_to_total_column expects to walk rightward from."""
    return [(e["row"], e["col"], e["header_text"])
            for e in classify_period_columns(table)
            if not e["forward_filled"] and e["period"].endswith("cumulative")]


def _col_for(events, row_idx, year_frag):
    """Most recent (row_idx' <= row_idx) header column whose text contains
    year_frag; None if no such header precedes this row. year_frag may be a
    single substring or a tuple/list of candidate substrings (tried as an OR)
    - most insurers spell the full 4-digit year in their column headers, but
    at least one (Narayana) prints dates as 'Up to the quarter 31-Dec-25', a
    2-digit year with no 4-digit substring anywhere in the header text."""
    frags = (year_frag,) if isinstance(year_frag, str) else tuple(year_frag)
    best = None
    best_ridx = None
    for ridx, col, text in events:
        if ridx <= row_idx and any(f in text for f in frags):
            best, best_ridx = col, ridx
    return best, best_ridx


def _extend_to_total_column(table, anchor_row_idx, anchor_col_idx):
    """Some forms merge a period header (e.g. 'For The Period Ended ...') across
    several sub-columns (Health/PA/Travel/Total), so pdfplumber only places the
    header text in the leftmost sub-column - the true rightmost "Total"/"Grand
    Total" data column for THAT period-block is further right. Extend the
    anchor to the rightmost "total"-labeled column within its own block, bounded
    by where the next block starts (found by locating the anchor's own group
    label - e.g. 'Miscellaneous' or 'Health' - and finding where that same
    label repeats, which marks the next block's start). No-op if the anchor
    already IS the right column (every column individually period-labeled)."""
    if anchor_row_idx is None or anchor_col_idx is None:
        return anchor_col_idx
    anchor_label = None
    for row in table[max(0, anchor_row_idx - 1)::-1]:
        if anchor_col_idx < len(row) and row[anchor_col_idx]:
            text = " ".join(str(row[anchor_col_idx]).split())
            low = text.lower()
            if text and "quarter" not in low and "period" not in low:
                anchor_label = text
                break
    upper_bound = None
    if anchor_label:
        for row in table[: anchor_row_idx + 1]:
            for i, cell in enumerate(row):
                if i <= anchor_col_idx or not cell:
                    continue
                if " ".join(str(cell).split()) == anchor_label:
                    upper_bound = i if upper_bound is None else min(upper_bound, i)
    best = anchor_col_idx
    for row in table[: anchor_row_idx + 1]:
        for i, cell in enumerate(row):
            if i < anchor_col_idx or not cell:
                continue
            if upper_bound is not None and i >= upper_bound:
                continue
            if "total" in " ".join(str(cell).split()).lower():
                best = max(best, i)
    return best


class FormPage:
    """Wraps the pdfplumber tables found on a form's page(s). Lookups are
    always scoped to a single table (a page can contain multiple tables -
    e.g. a main schedule + a "Note" breakout - with different column
    layouts), and resolve columns from the nearest preceding header row so
    both side-by-side and stacked-block year layouts work uniformly."""

    def __init__(self, tables):
        self.tables = tables  # list of list-of-rows
        self._events_cache = {}

    def _events(self, table_idx):
        if table_idx not in self._events_cache:
            self._events_cache[table_idx] = _header_events(self.tables[table_idx])
        return self._events_cache[table_idx]

    def find_rows(self, *label_substrings, table_idx=None):
        """Returns [(row, table_idx, row_idx), ...] for every row containing
        all given substrings (case-insensitive) in some cell."""
        table_idxs = range(len(self.tables)) if table_idx is None else [table_idx]
        out = []
        for ti in table_idxs:
            for ridx, row in enumerate(self.tables[ti]):
                for cell in row:
                    if not cell:
                        continue
                    text = " ".join(str(cell).split()).lower()
                    if all(s.lower() in text for s in label_substrings):
                        out.append((row, ti, ridx))
                        break
        return out

    def find_row(self, *label_substrings, table_idx=None):
        matches = self.find_rows(*label_substrings, table_idx=table_idx)
        return matches[0] if matches else (None, None, None)


def get_form_page(pdf_path, form_pattern, page_hint_range=None, all_matches=False):
    """Locate page(s) whose text matches `form_pattern` (regex) in the header,
    return a FormPage built from that page's (or those pages') extracted
    tables. Sourced from the on-disk PDF-JSON cache (pdf_cache.py) rather than
    re-opening/re-scanning the PDF."""
    doc = pdf_cache.get_company_json(pdf_path)
    pages = pdf_cache.pages_for_pattern(doc, form_pattern, page_hint_range=page_hint_range)
    if not all_matches:
        pages = pages[:1]
    matched_tables = []
    matched_idxs = []
    for p in pages:
        matched_tables.extend(p["tables"])
        matched_idxs.append(p["page_number"] - 1)
    if not matched_tables:
        return None, []
    return FormPage(matched_tables), matched_idxs


_NUM_TOKEN = re.compile(r"\(?-?[\d,]+\.?\d*\)?|-(?=\s|$)")


def get_form_text(pdf_path, form_pattern, page_hint_range=None):
    """Fallback for pages with no ruled gridlines (pdfplumber's table
    detection needs both horizontal & vertical lines) - returns raw
    extract_text() for the first matching page instead of a FormPage.
    Sourced from the on-disk PDF-JSON cache (pdf_cache.py)."""
    doc = pdf_cache.get_company_json(pdf_path)
    pages = pdf_cache.pages_for_pattern(doc, form_pattern, page_hint_range=page_hint_range)
    if not pages:
        return None, None
    p = pages[0]
    return p["text"], p["page_number"] - 1


_PERIOD_LABEL_RE = re.compile(
    r"(up\s*to|upto|for)\s+the\s+(?:corresponding\s+)?(quarter|period)", re.I)
_FULL_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_SHORT_YEAR_RE = re.compile(r"-(\d{2})\b")


def _text_period_columns(text, cur_year_frag=None, prior_year_frag=None, form=""):
    """Resolve which numeric column of a gridline-less form holds the
    CUMULATIVE ("up to the quarter"/"period ended") figure for the current and
    prior year, by reading the form's OWN header lines - never by assuming a
    fixed column order.

    This matters because the block order genuinely varies between insurers,
    and even between forms inside a single filing. Narayana Health's NL-2
    prints [Upto-cur, For-cur, For-prior, Upto-prior] while its own NL-20
    prints [Upto-cur, For-cur, Upto-prior, For-prior] - the prior-year pair is
    swapped between two forms of the same document. Any fixed index is
    therefore right for one and wrong for the other.

    Returns (cur_col, prior_col) as indices into the row's numeric tokens, or
    (None, None) if the header could not be read at all.
    """
    if cur_year_frag is None or prior_year_frag is None:
        _c, _p = year_frags()
        cur_year_frag = cur_year_frag or _c
        prior_year_frag = prior_year_frag or _p

    lines = text.splitlines()
    for i, line in enumerate(lines):
        periods = [(m.start(), m.group(1).lower().replace(" ", ""))
                   for m in _PERIOD_LABEL_RE.finditer(line)]
        if len(periods) < 2:
            continue
        kinds = ["cum" if p.startswith(("upto", "up")) else "qtr" for _, p in periods]

        # Years may sit on the same line as the period labels, or on one of
        # the next few lines (insurers wrap the header over 2-3 lines).
        years = []
        for cand in lines[i:i + 4]:
            found = _FULL_YEAR_RE.findall(cand) or [
                f"-{y}" for y in _SHORT_YEAR_RE.findall(cand)]
            if len(found) >= len(periods):
                years = found[:len(periods)]
                break

        cur_col = prior_col = None
        if years:
            for idx, (kind, yr) in enumerate(zip(kinds, years)):
                if kind != "cum":
                    continue
                if any(f.lstrip("-") in yr for f in cur_year_frag) and cur_col is None:
                    cur_col = idx
                elif any(f.lstrip("-") in yr for f in prior_year_frag) and prior_col is None:
                    prior_col = idx
        if cur_col is None or prior_col is None:
            # Some forms label prior-year blocks only as "for the
            # corresponding quarter of the previous year", printing no year at
            # all (e.g. Narayana's NL-36). Reading order still disambiguates:
            # the first cumulative block is the current period, the second is
            # the prior-year comparative.
            cum_idxs = [idx for idx, k in enumerate(kinds) if k == "cum"]
            if len(cum_idxs) >= 2:
                _log(f"{form or 'form'}: header printed no usable year for both "
                     f"cumulative columns; defaulting to reading order "
                     f"(1st cumulative block={cum_idxs[0]} as current, "
                     f"2nd={cum_idxs[1]} as prior) because the header labels "
                     f"{kinds} were resolvable but the years {years or '[]'} were not.")
                cur_col = cum_idxs[0] if cur_col is None else cur_col
                prior_col = cum_idxs[1] if prior_col is None else prior_col
        if cur_col is not None and prior_col is not None:
            return cur_col, prior_col
    return None, None


def get_line_item_from_text(text, *label_substrings, cur_col=None, prior_col=None,
                            exclude=None, form=""):
    """Line-based fallback for pages with no ruled gridlines: find the line
    containing all label_substrings (and none of `exclude`, for disambiguating
    e.g. a subtotal row like "...Before Tax Exceptional Items" from the
    bottom-line "...Before Tax" row it's a substring-superset of), pull all
    numeric tokens from it, and return the cumulative current/prior ones.

    Column indices are resolved from the form's own period header by
    `_text_period_columns` unless the caller pins them explicitly, so a filing
    that orders its quarter/cumulative blocks differently still reads
    correctly."""
    if not text:
        return None, None
    if cur_col is None or prior_col is None:
        rc, rp = _text_period_columns(text, form=form)
        if rc is None or rp is None:
            _log(f"{form or 'form'}: could not resolve cumulative columns from "
                 f"header text for label {label_substrings}; returning no value "
                 f"rather than guessing a column order.")
            return None, None
        cur_col = rc if cur_col is None else cur_col
        prior_col = rp if prior_col is None else prior_col
    exclude = exclude or []
    for line in text.splitlines():
        low = line.lower()
        if any(s.lower() in low for s in exclude):
            continue
        if all(s.lower() in low for s in label_substrings):
            # drop the label text itself so it can't be mistaken for a number
            rest = line
            for s in label_substrings:
                idx = rest.lower().find(s.lower())
                if idx != -1:
                    rest = rest[idx + len(s):]
            # Strip inline schedule references (e.g. "NL-5", "NL-6") before
            # tokenizing - _NUM_TOKEN has no word-boundary requirement before
            # "-", so "NL-5" would otherwise be misread as the number -5.
            rest = re.sub(r"\bNL-\d+\b", "", rest, flags=re.IGNORECASE)
            # Collapse a stray space right after "(" or before ")" (an
            # occasional pdfplumber text-extraction artifact, e.g.
            # "( 310.06)") - _NUM_TOKEN requires "(" immediately followed by
            # a digit to recognize a negative/parenthesized number, so an
            # un-collapsed gap here would silently drop the sign.
            rest = re.sub(r"\(\s+", "(", rest)
            rest = re.sub(r"\s+\)", ")", rest)
            # Same artifact class, mid-number: pdfplumber sometimes splits a
            # figure across a stray space, e.g. "9 .13" (=9.13) or
            # "1 ,102.73" (=1,102.73). _NUM_TOKEN would otherwise read those
            # as two separate values ("9" and ".13"), silently shifting every
            # later column left. Only a space directly before a "." or ","
            # is collapsed - a genuine separate figure never begins with
            # either, so this cannot merge two real numbers.
            rest = re.sub(r"(\d)\s+([.,]\d)", r"\1\2", rest)
            tokens = _NUM_TOKEN.findall(rest)
            vals = [parse_num(t) for t in tokens]
            cur = vals[cur_col] if cur_col < len(vals) else None
            prior = vals[prior_col] if prior_col < len(vals) else None
            return cur, prior
    return None, None


def get_line_item_any(fp: FormPage, label_variants, cur_year_frag=None, prior_year_frag=None):
    """Try each label variant (a list of substring-tuples) in turn - insurers
    don't all use the same wording for the same line item (e.g. 'Net Earned
    Premium' vs 'Total Premium Earned (Net)') - and return the first that
    finds a match."""
    for variant in label_variants:
        cur, prior = get_line_item(fp, *variant, cur_year_frag=cur_year_frag, prior_year_frag=prior_year_frag)
        if cur is not None or prior is not None:
            return cur, prior
    return None, None


def get_line_item(fp: FormPage, *label_substrings, cur_year_frag=None, prior_year_frag=None):
    """Find a labeled row (possibly appearing more than once, once per
    year-block) and return its (current, prior) cumulative 'Up to the
    quarter'/'Period ended' values."""
    if cur_year_frag is None or prior_year_frag is None:
        _cur, _prior = year_frags()
        cur_year_frag = cur_year_frag or _cur
        prior_year_frag = prior_year_frag or _prior
    matches = fp.find_rows(*label_substrings)
    if not matches:
        return None, None
    cur_val = prior_val = None
    for row, ti, ridx in matches:
        events = fp._events(ti)
        table = fp.tables[ti]
        cur_idx, cur_hdr_ridx = _col_for(events, ridx, cur_year_frag)
        prior_idx, prior_hdr_ridx = _col_for(events, ridx, prior_year_frag)
        cur_idx = _extend_to_total_column(table, cur_hdr_ridx, cur_idx)
        prior_idx = _extend_to_total_column(table, prior_hdr_ridx, prior_idx)
        # Prefer a later non-zero match over an earlier zero one - some forms
        # repeat a label as a bare section header (e.g. "Gross Direct Premium
        # :" with placeholder "-" values) before the row with real figures.
        if (cur_val is None or cur_val == 0) and cur_idx is not None and cur_idx < len(row):
            v = parse_num(row[cur_idx])
            if v is not None and (cur_val is None or v != 0):
                cur_val = v
        if (prior_val is None or prior_val == 0) and prior_idx is not None and prior_idx < len(row):
            v = parse_num(row[prior_idx])
            if v is not None and (prior_val is None or v != 0):
                prior_val = v
    return cur_val, prior_val


def get_single_value(fp: FormPage, *label_substrings):
    """For single-column point-in-time schedules (e.g. NL-41's 'Statement
    as on' snapshot, no cur/prior column structure at all) - find the
    labeled row and return its last non-blank cell as a number."""
    row, _, _ = fp.find_row(*label_substrings)
    if row is None:
        return None
    for cell in reversed(row):
        v = parse_num(cell)
        if v is not None and str(cell).strip() not in ("", "-"):
            return v
    return None


# ---------------------------------------------------------------------------
# NL-36: Business Acquisition Through Different Channels
# ---------------------------------------------------------------------------

# The five channel buckets the Data Engine reports individually. Everything
# else on the form (CSC, Insurance Marketing Firms, Point of Sales, Web
# Aggregators, Micro Agents, MISP, Referral Arrangements, "Other", plus any
# channel type a future filing introduces) is reported as one "Others"
# residual, so no label list needs maintaining. Insurers punctuate and
# hyphenate these labels inconsistently, so matching is on normalized
# substrings.
NL36_CHANNELS = {
    "Individual Agents": ("individualagent",),
    "Corporate Agents - Banks": ("corporateagentsbank", "corporateagentbank"),
    "Corporate Agents - Others": ("corporateagentsother", "corporateagentother"),
    "Brokers": ("broker",),
    "Direct Business": ("directbusiness",),
}


def _norm_label(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _nl36_blocks(table):
    """Map NL-36's two header rows onto numeric column indices.

    NL-36 splits each period into two sub-columns ("No. of Policies" and
    "Premium"), and the sub-column ORDER is not consistent between insurers -
    Narayana Health prints Premium first, every other filing prints policies
    first. So the premium column is located by reading the sub-header text,
    never by assuming it is the second of the pair.

    Returns (cur_premium_col, prior_premium_col) as absolute column indices.
    """
    period_row = period_cells = None
    for row in table[:6]:
        hits = [(i, " ".join(str(c).split())) for i, c in enumerate(row)
                if c and _PERIOD_LABEL_RE.search(" ".join(str(c).split()))]
        if len(hits) >= 2:
            period_row, period_cells = row, hits
            break
    if not period_cells:
        return None, None

    # Sub-header row: the row carrying "Premium" / "No. of Policies" labels.
    sub_row = None
    for row in table[:8]:
        if sum(1 for c in row if c and "premium" in str(c).lower()) >= 2:
            sub_row = row
            break

    blocks = []
    for idx, (col, text) in enumerate(period_cells):
        kind = "cum" if re.search(r"up\s*to|upto", text, re.I) else "qtr"
        end = period_cells[idx + 1][0] if idx + 1 < len(period_cells) else len(period_row)
        blocks.append((kind, col, end, text))

    def premium_col(start, end):
        if sub_row:
            for c in range(start, min(end, len(sub_row))):
                if sub_row[c] and "premium" in str(sub_row[c]).lower():
                    return c
        # No readable sub-header (text extraction occasionally drops the
        # "Premium" label entirely) - fall back to the pair's second column,
        # which is the layout every filing but Narayana's uses.
        _log("NL-36: no readable 'Premium' sub-header in columns "
             f"{start}-{end}; defaulting to the second column of the pair "
             "because that is the majority layout.")
        return start + 1

    cum = [(b, premium_col(b[1], b[2])) for b in blocks if b[0] == "cum"]
    if len(cum) < 2:
        return None, None
    cur_frag, prior_frag = year_frags()
    cur_col = prior_col = None
    for (kind, start, end, text), pcol in cum:
        if any(f.lstrip("-") in text for f in cur_frag) and cur_col is None:
            cur_col = pcol
        elif any(f.lstrip("-") in text for f in prior_frag) and prior_col is None:
            prior_col = pcol
    if cur_col is None or prior_col is None:
        # Narayana's NL-36 labels its prior-year blocks "for the
        # corresponding quarter of the previous year" with no year printed.
        _log("NL-36: cumulative block headers did not both carry a resolvable "
             "year; defaulting to reading order (1st cumulative block as "
             "current, 2nd as prior) because that is what the form's own "
             "left-to-right layout implies.")
        cur_col = cum[0][1] if cur_col is None else cur_col
        prior_col = cum[1][1] if prior_col is None else prior_col
    return cur_col, prior_col


def extract_nl36(pdf_path):
    """Per-company NL-36 channel premiums and totals, in Rs. Lakhs.

    Returns {"total_a": (cur, prior), "grand_total": (cur, prior),
             "channels": {bucket: (cur, prior)}, "others": (cur, prior)}
    where "others" is the RESIDUAL Total(A) minus the five named buckets -
    computed rather than read, so it captures whichever sub-channels a given
    filing lists (CSC/IMF/POS/Web Aggregators, and any new channel type)
    despite their labels varying between insurers.
    """
    fp, _ = get_form_page(pdf_path, r"FORM\s+NL-36")
    if fp is None or not fp.tables:
        _log(f"NL-36: no table found in {pdf_path}; no channel figures extracted.")
        return None
    table = max(fp.tables, key=len)
    cur_col, prior_col = _nl36_blocks(table)
    if cur_col is None:
        _log(f"NL-36: could not resolve premium columns in {pdf_path}.")
        return None

    def row_vals(row):
        cur = parse_num(row[cur_col]) if cur_col < len(row) else None
        prior = parse_num(row[prior_col]) if prior_col < len(row) else None
        return cur, prior

    def first_label(row):
        # Skip the leading serial-number column - only a cell containing
        # letters is a channel label.
        return next((str(c) for c in row[:3]
                     if c and re.search(r"[A-Za-z]", str(c))), "")

    out = {"channels": {}, "total_a": (None, None), "grand_total": (None, None)}
    for row in table:
        n = _norm_label(first_label(row))
        if not n:
            continue
        if n.startswith("totala"):
            out["total_a"] = row_vals(row)
            continue
        if n.startswith("grandtotal"):
            out["grand_total"] = row_vals(row)
            continue
        for bucket, frags in NL36_CHANNELS.items():
            if bucket in out["channels"]:
                continue
            if any(f in n for f in frags):
                out["channels"][bucket] = row_vals(row)
                break

    # Direct Business is printed as a bare header with its real figures only
    # in indented sub-rows ("-Officers/Employees", "-Online", "-Others") on
    # some filings. When the header row itself is blank/zero, sum the
    # sub-rows that follow it rather than reading the header literally.
    db = out["channels"].get("Direct Business")
    if db is not None and not any(db):
        start = None
        for i, row in enumerate(table):
            if "directbusiness" in _norm_label(first_label(row)):
                start = i
                break
        if start is not None:
            tot = [0.0, 0.0]
            for row in table[start + 1:]:
                raw = next((c for c in row[:2] if c), "")
                if not str(raw).strip().startswith("-"):
                    break
                v = row_vals(row)
                for k in (0, 1):
                    tot[k] += v[k] or 0
            if any(tot):
                out["channels"]["Direct Business"] = tuple(tot)
                _log("NL-36: 'Direct Business' header row was blank; used the "
                     "sum of its indented sub-rows instead.")

    named = [out["channels"].get(b) for b in NL36_CHANNELS]
    others = []
    for k in (0, 1):
        total = out["total_a"][k]
        if total is None or any(v is None for v in named):
            others.append(None)
        else:
            others.append(round(total - sum((v[k] or 0) for v in named), 2))
    out["others"] = tuple(others)
    return out
