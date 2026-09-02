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

import pdf_cache
from pdf_cache import COMPANY_PDFS


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


def _header_events(table):
    """[(row_idx, 'cur'|'prior', col_idx), ...] sorted by row_idx, for every
    cumulative-column header cell found anywhere in the table."""
    events = []
    for ridx, row in enumerate(table):
        for i, cell in enumerate(row):
            if not cell:
                continue
            text = " ".join(str(cell).split())
            if not _is_cumulative_header(text):
                continue
            events.append((ridx, i, text))
    return events


def _col_for(events, row_idx, year_frag):
    """Most recent (row_idx' <= row_idx) header column whose text contains
    year_frag; None if no such header precedes this row."""
    best = None
    best_ridx = None
    for ridx, col, text in events:
        if ridx <= row_idx and year_frag in text:
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


def get_line_item_from_text(text, *label_substrings, cur_col=0, prior_col=2):
    """Line-based fallback: find the line containing all label_substrings,
    pull all numeric tokens from it, and return (cur_col, prior_col)-th ones.
    Assumes the page's stated column order (verify against a header line
    before relying on this for a new source PDF)."""
    if not text:
        return None, None
    for line in text.splitlines():
        low = line.lower()
        if all(s.lower() in low for s in label_substrings):
            # drop the label text itself so it can't be mistaken for a number
            rest = line
            for s in label_substrings:
                idx = rest.lower().find(s.lower())
                if idx != -1:
                    rest = rest[idx + len(s):]
            tokens = _NUM_TOKEN.findall(rest)
            vals = [parse_num(t) for t in tokens]
            cur = vals[cur_col] if cur_col < len(vals) else None
            prior = vals[prior_col] if prior_col < len(vals) else None
            return cur, prior
    return None, None


def get_line_item_any(fp: FormPage, label_variants, cur_year_frag="2025", prior_year_frag="2024"):
    """Try each label variant (a list of substring-tuples) in turn - insurers
    don't all use the same wording for the same line item (e.g. 'Net Earned
    Premium' vs 'Total Premium Earned (Net)') - and return the first that
    finds a match."""
    for variant in label_variants:
        cur, prior = get_line_item(fp, *variant, cur_year_frag=cur_year_frag, prior_year_frag=prior_year_frag)
        if cur is not None or prior is not None:
            return cur, prior
    return None, None


def get_line_item(fp: FormPage, *label_substrings, cur_year_frag="2025", prior_year_frag="2024"):
    """Find a labeled row (possibly appearing more than once, once per
    year-block) and return its (current, prior) cumulative 'Up to the
    quarter'/'Period ended' values."""
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
