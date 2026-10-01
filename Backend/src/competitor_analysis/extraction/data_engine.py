"""
Phase 2 extraction: populates Data_Engine_UI.xlsx from GIC.xlsx (industry-wide
GDPI statistics) and per-company IRDAI public-disclosure PDFs.

Run stages independently via CLI flags so each can be reviewed before the next
runs:
    python -m competitor_analysis.extraction.data_engine --audit   # missing-row counts
    python -m competitor_analysis.extraction.data_engine --gic     # GIC rows (Slides 3-11)

A note on the "GT" references in the comments below: several unit and
definitional conventions here (which rows want an absolute figure despite a
percentage label, which numerator a given ratio uses, and so on) were
originally settled by cross-checking against a hand-built ground-truth
workbook for one quarter. That workbook is no longer kept in the repo - it
was only ever calibrated for a single period and went stale - but the
conventions it established are still what the sheet expects, so the reasoning
is recorded here rather than lost. Treat those notes as provenance for why a
convention exists, not as something re-verifiable in-tree.
"""
import asyncio
import argparse
import gc
import glob
import json
import math
import os
import re
import sys

import openpyxl

from competitor_analysis import logging_setup
from competitor_analysis.extraction.forms import (COMPANY_PDFS, get_form_page, get_line_item, get_line_item_any,
                          get_form_text, get_line_item_from_text, sum_rows_after,
                          get_segment_line_item, get_segment_line_item_any, sum_lines_after_from_text,
                          extract_nl36 as pdf_extract_nl36, extract_nl36_policies, NL36_CHANNELS,
                          RESOLUTION_LOG, year_frags as pdf_extract_year_frags,
                          _is_cumulative_header, _SNAPSHOT_PHRASE_RE, parse_num)
from competitor_analysis.extraction import gemini as gemini_extract
from competitor_analysis.extraction import schemas
from competitor_analysis import config as cfg
from competitor_analysis import memory
from competitor_analysis import paths
from competitor_analysis.rounding import round_half_up

log = logging_setup.get_logger(__name__)

XLSX_PATH = str(paths.DATA_ENGINE_WORKBOOK)

# pdfplumber (pdfminer.six underneath) holds a full page/character object
# graph in memory per open document, and prefetch_income_statements() parses
# every company's PDF concurrently - so peak memory scales with this number,
# not with PDF size alone. 7 (one thread per company) is fine on a dev
# machine but reliably OOMs a 512MB container; keep the default modest and
# let a memory-constrained deployment override it.
PDF_PARSE_MAX_WORKERS = int(os.getenv("PDF_PARSE_MAX_WORKERS", "3"))

# The two value-column headers follow the configured reporting period, so a
# FY26-27 Q1 run labels its columns FY26-27_Q1/FY25-26_Q1 rather than carrying
# FY25-26 Q3's labels. CUR/PRIOR are stable aliases for the same two columns:
# report modules address them through these, never by a period literal, so
# nothing has to change when the period does.
CUR = "CUR"
PRIOR = "PRIOR"
CUR_PERIOD = cfg.cur_period_column()
PRIOR_PERIOD = cfg.prior_period_column()

HEADERS = ["Slide #", "Category", "Company", "Meric 1", "Metric 2",
           "Source Tab", "Link to Source document", CUR_PERIOD, PRIOR_PERIOD,
           "Growth"]
_CUR_IDX = HEADERS.index(CUR_PERIOD)
_PRIOR_IDX = HEADERS.index(PRIOR_PERIOD)
COL = {h: i + 1 for i, h in enumerate(HEADERS)}
COL[CUR] = COL[CUR_PERIOD]
COL[PRIOR] = COL[PRIOR_PERIOD]


@cfg.on_period_change
def _refresh_period_columns():
    """Re-label the two value columns when the period changes.

    Resolved once at import, these went stale in the long-lived API server
    exactly like pdf_cache's PDF map did, and sync_period_headers - whose
    whole job is stopping a run presenting its figures under the wrong
    quarter's heading - was then defeated by its own stale input. The values
    still landed in the right columns (COL is positional), so the symptom
    was a Q4 workbook headed FY25-26_Q3.

    HEADERS and COL are mutated in place, matching pdf_cache, so a future
    `from ... import COL` cannot silently pin itself to one period."""
    global CUR_PERIOD, PRIOR_PERIOD
    CUR_PERIOD = cfg.cur_period_column()
    PRIOR_PERIOD = cfg.prior_period_column()
    HEADERS[_CUR_IDX] = CUR_PERIOD
    HEADERS[_PRIOR_IDX] = PRIOR_PERIOD
    COL.clear()
    COL.update({h: i + 1 for i, h in enumerate(HEADERS)})
    COL[CUR] = COL[CUR_PERIOD]
    COL[PRIOR] = COL[PRIOR_PERIOD]


def sync_period_headers(ws):
    """Relabel the sheet's two value-column headers to the configured period.

    The workbook is a reusable template, so its saved headers are whatever
    period it was last run for. Rewriting them here keeps the visible labels
    honest about which quarter the numbers underneath belong to - without it, a
    FY26-27 Q1 run would silently present its figures under an 'FY25-26_Q3' heading.
    Returns the (previous, new) label pairs actually changed."""
    changed = []
    for alias, label in ((CUR, CUR_PERIOD), (PRIOR, PRIOR_PERIOD)):
        cell = ws.cell(row=1, column=COL[alias])
        if cell.value != label:
            changed.append((cell.value, label))
            cell.value = label
    return changed


def load_engine(path=XLSX_PATH):
    wb = openpyxl.load_workbook(path)
    return wb, wb["Data Engine"]


def row_dict(ws, r):
    d = {h: ws.cell(row=r, column=COL[h]).value for h in HEADERS}
    # Period-neutral aliases alongside the period-labelled keys, so callers
    # can read d[p2.CUR] instead of d[CUR].
    d[CUR] = d[CUR_PERIOD]
    d[PRIOR] = d[PRIOR_PERIOD]
    return d


def audit(ws):
    from collections import Counter
    missing_by_company = Counter()
    total_missing = 0
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        if d["Slide #"] is None and d["Category"] is None:
            continue
        if d[CUR] is None or d[PRIOR] is None:
            total_missing += 1
            missing_by_company[d["Company"]] += 1
    log.info("Total missing rows: %d", total_missing)
    for k, v in missing_by_company.most_common():
        log.info("  %s: %d", k, v)


def clear_period_values(ws):
    """Blanks the two period value columns and Growth *value* columns (not headers, not any
    other column) for every populated row, so a fresh orchestrator run for a
    new FY/Quarter starts from a clean sheet instead of carrying over the
    previous period's numbers."""
    cleared = 0
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        if d["Slide #"] is None and d["Category"] is None:
            continue
        for h in (CUR, PRIOR, "Growth"):
            if ws.cell(row=r, column=COL[h]).value is not None:
                ws.cell(row=r, column=COL[h]).value = None
                cleared += 1
    return cleared


def growth(cur, prev):
    if cur is None or prev is None:
        return None
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return None
    if prev == 0:
        return None
    return round_half_up((cur - prev) / prev, 4)


# ---------------------------------------------------------------------------
# GIC.xlsx structured readers
# ---------------------------------------------------------------------------

# Verified against a hand-built ground-truth workbook (since removed from
# the repo): its "Public" sector GDPI total (both
# current and prior quarter) exactly equals New India + Oriental + United
# India alone - National Insurance Co Ltd is classified as "Private" in the
# ground truth, despite also being state-owned. Verified by exact arithmetic
# match (to the rupee) against GIC.xlsx's per-company Grand Total column.
PSU_INSURERS = {
    "The New India Assurance Co Ltd",
    "The Oriental Insurance Co Ltd",
    "United India Insurance Co Ltd",
    "National Insurance Co Ltd",
}

# Slide 16's North/South/East/West/Central zone split, derived below from
# Slide 17's per-state values. Deliberately covers every India state/UT, not
# just the ones gemini_extract.STATES currently asks NL-34 to extract by name
# (Uttar Pradesh, Maharashtra, Karnataka, Haryana, Tamil Nadu, Kerala, Delhi)
# - so if that list is ever expanded, this table is already ready rather than
# needing a second update. Any state not a key here (today, that's every
# state gemini_extract.STATES doesn't name, all folded into its "Others")
# lands in "Others (unclassified)" instead of being guessed into a region.
STATE_TO_ZONE = {
    "Maharashtra": "West", "Delhi": "North", "Uttar Pradesh": "North", "Karnataka": "South",
    "Gujarat": "West", "Haryana": "North", "Telangana": "Central", "Tamil Nadu": "South",
    "Punjab": "North", "Kerala": "South", "Rajasthan": "North", "West Bengal": "East",
    "Madhya Pradesh": "Central", "Andhra Pradesh": "Central", "Bihar": "East", "Odisha": "Central",
    "Chhattisgarh": "Central", "Uttarakhand": "North", "Jharkhand": "East", "Assam": "East",
    "Chandigarh": "North", "Goa": "West", "Himachal Pradesh": "North", "Jammu & Kashmir": "North",
    "Tripura": "East", "Manipur": "East", "Puducherry": "South", "Meghalaya": "East",
    "Daman & Diu": "West", "Dadra and Nagar Haveli": "West", "Arunachal Pradesh": "East",
    "Nagaland": "East", "Sikkim": "East", "Mizoram": "East", "Ladakh": "North",
    "Andaman and Nicobar Islands": "South", "Lakshadweep": "South",
}

SAHI_ROW_LABELS = {
    # label as it appears in GIC.xlsx -> canonical short name used in Data Engine
    "Niva bupa health insurance company limited": "Niva Bupa",
    "Aditya Birla Health Insurance Co Ltd": "ABHI",
    "Care Health Insurance Ltd": "CARE",
    "Galaxy Health Insurance Company Ltd": "Galaxy",
    "ManipalCigna Health Insurance Co Ltd": "Manipal Cigna",
    "Narayana Health Insurance Ltd": "Narayana",
    "Star Health & Allied Insurance Co Ltd": "Star",
}

# company_short (COMPANY_PDFS/pipeline key) -> GIC.xlsx's own Segmentwise
# Report row label - same 7 companies as SAHI_ROW_LABELS, just keyed by the
# pipeline's own company identifier instead of SAHI_ROW_LABELS' short display
# code (the two naming conventions don't otherwise line up, e.g. "Star
# Health" here vs "Star" there).
SLIDE8_GIC_LABEL = {
    "NBHI": "Niva bupa health insurance company limited",
    "ABHI": "Aditya Birla Health Insurance Co Ltd",
    "Care Health": "Care Health Insurance Ltd",
    "Galaxy Health": "Galaxy Health Insurance Company Ltd",
    "Manipal Cigna": "ManipalCigna Health Insurance Co Ltd",
    "Narayana Health": "Narayana Health Insurance Ltd",
    "Star Health": "Star Health & Allied Insurance Co Ltd",
}


def gic_gdpi_lookup(gic):
    """{company_short: (cur_cr, prior_cr)} from GIC.xlsx's Segmentwise
    Report "Grand Total" column - the authoritative per-company GDPI this
    pipeline now uses everywhere GDPI/GWP is needed (Slide 8's "Revenue
    Growth (GDPI)" and GWP's base component in extract_income_statement),
    instead of each company's own self-reported NL-4 "Gross Direct Premium"
    line. Already in Rs. Crore, NOT Lakhs (verified against a real filing:
    GIC's Grand Total matches NL-4's own line to the rupee once NL-4's
    Lakhs figure is converted - GIC.xlsx itself reports in Crore already,
    unlike every PDF form this pipeline reads)."""
    if gic is None:
        return {}
    seg = gic.segmentwise
    lookup = {}
    for company_short, gic_label in SLIDE8_GIC_LABEL.items():
        cur = get(seg, gic_label, "Grand Total", "cur")
        prior = get(seg, gic_label, "Grand Total", "prev")
        if cur is not None or prior is not None:
            lookup[company_short] = (cur, prior)
    return lookup


# Set once per Phase-2 run (pipeline.py, right after GIC.xlsx is loaded) so
# extract_income_statement can use it without threading a new parameter
# through its 3 call sites - 2 of which run concurrently across companies.
# Same module-level-state convention as config.set_period().
_GIC_GDPI_LOOKUP = {}


def set_gic_gdpi_lookup(lookup):
    global _GIC_GDPI_LOOKUP
    _GIC_GDPI_LOOKUP = lookup or {}


def read_sheet(ws):
    rows = []
    for r in range(1, ws.max_row + 1):
        rows.append([ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)])
    return rows


class GicData:
    """Parses GIC.xlsx 'Segmentwise Report' and 'Health Portfolio' sheets into
    lookup dicts of {label: {col_name: (current, previous)}}."""

    def __init__(self, path=None):
        # Resolved per call, not as a default argument: a default binds at
        # def time, so the long-lived API server read the first run's
        # quarter of GIC.xlsx for every later run.
        path = path or cfg.gic_path()
        import openpyxl as ox
        wb = ox.load_workbook(path, data_only=True)
        self.segmentwise = self._parse_block_sheet(wb["Segmentwise Report"])
        self.health = self._parse_block_sheet(wb["Health Portfolio"])

    @staticmethod
    def _parse_block_sheet(ws):
        """Generic parser: header row has column names starting at col B.
        Data rows come in (current, 'Previous Year') pairs, grouped under
        section headers (single-cell rows) and terminated by '<Section> sub
        Total' / 'Previous Year Sub Total' pairs, then a '% Growth' row.
        Also captures 'Industry Total' / 'Previous Year Sub Total' / '%
        Growth' / '% Market Share' / 'Previous Year Market Share' footer rows.
        Returns {label: {col_name: {'cur':x,'prev':y}}} plus
        {'__market_share__': {col_name: {'cur':x,'prev':y}}}.
        """
        rows = read_sheet(ws)
        header_idx = next(
            idx for idx, row in enumerate(rows)
            if row[0] is None and len(row) > 1 and isinstance(row[1], str)
        )
        header = rows[header_idx]
        col_names = [c for c in header[1:] if c is not None]
        n_cols = len(col_names)

        data = {}
        market_share = {}
        i = header_idx + 1
        while i < len(rows):
            row = rows[i]
            label = row[0]
            if label is None:
                i += 1
                continue
            vals = row[1:1 + n_cols]
            is_numeric_row = any(isinstance(v, (int, float)) for v in vals)
            if not is_numeric_row:
                # section header (e.g. "General Insurers") - skip
                i += 1
                continue
            if label in ("% Growth",):
                i += 1
                continue
            if label in ("% Market Share", "Previous Year Market Share"):
                target = market_share
                key = "cur" if label == "% Market Share" else "prev"
                for cname, v in zip(col_names, vals):
                    target.setdefault(cname, {})[key] = v
                i += 1
                continue
            # current-year row; next row should be its "Previous Year" pair
            cur_vals = vals
            prev_vals = [None] * n_cols
            if i + 1 < len(rows) and rows[i + 1][0] and "Previous Year" in str(rows[i + 1][0]):
                prev_vals = rows[i + 1][1:1 + n_cols]
                i += 2
            else:
                i += 1
            data[str(label).strip()] = {
                cname: {"cur": cv, "prev": pv}
                for cname, cv, pv in zip(col_names, cur_vals, prev_vals)
            }
        return {"rows": data, "market_share": market_share, "col_names": col_names}


def _norm_seg(s):
    """Fold a Segmentwise Report column name for matching: case, all
    whitespace and punctuation removed. 'Marine  Cargo' -> 'marinecargo',
    'Health ' -> 'health', 'P.A.' -> 'pa'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def get(d, label, col, key):
    try:
        return d["rows"][label][col][key]
    except KeyError:
        return None


def build_gic_lookups(gic: GicData):
    """Derive every figure needed for Slides 3-11 into a flat dict:
    lookups[(slide, company, metric1, metric2)] = (fy26_q3, fy25_q3)
    """
    L = {}
    seg = gic.segmentwise
    hp = gic.health

    seg_cols = seg["col_names"]  # Fire, Marine Total, ..., Grand Total
    grand_total_cur = get(seg, "Industry Total", "Grand Total", "cur")
    grand_total_prev = get(seg, "Previous Year Sub Total", "Grand Total", "cur")
    # NOTE: "Previous Year Sub Total" label collides across sections in our
    # parser since each section's own prev-total row is also literally named
    # "Previous Year Sub Total". We instead re-derive the true prior-year
    # industry total directly from the Industry Total row's own prev pair.
    grand_total_prev = get(seg, "Industry Total", "Grand Total", "prev")

    # ---- Slide 3: Industry market share by Private/Public/SAHI/Specialized ----
    gi_sub_cur = get(seg, "General Insurers Sub Total", "Grand Total", "cur")
    gi_sub_prev = get(seg, "General Insurers Sub Total", "Grand Total", "prev")
    sahi_sub_cur = get(seg, "Stand-alone Health sub Total", "Grand Total", "cur")
    sahi_sub_prev = get(seg, "Stand-alone Health sub Total", "Grand Total", "prev")
    spec_sub_cur = get(seg, "Specialised sub Total", "Grand Total", "cur")
    spec_sub_prev = get(seg, "Specialised sub Total", "Grand Total", "prev")

    def psu_total(sheet, key):
        return sum(get(sheet, lbl, "Grand Total", key) or 0 for lbl in PSU_INSURERS)

    public_cur = psu_total(seg, "cur")
    public_prev = psu_total(seg, "prev")
    private_cur = gi_sub_cur - public_cur if gi_sub_cur is not None else None
    private_prev = gi_sub_prev - public_prev if gi_sub_prev is not None else None

    # NOTE: despite the "Market share" label, a ground-truth cross-check
    # shows these rows actually want the ABSOLUTE Rs. Crore GDPI figure, not
    # a computed 0-1 share/percentage - verified by exact arithmetic (e.g.
    # GT's Private + Public == our own General Insurers Sub Total grand
    # total, to the rupee).
    def abs2(v):
        return round_half_up(v, 2) if isinstance(v, (int, float)) else None

    L[(3, "Industry", "Market share", "Private")] = (abs2(private_cur), abs2(private_prev))
    L[(3, "Industry", "Market share", "Public")] = (abs2(public_cur), abs2(public_prev))
    L[(3, "Industry", "Market share", "SAHI")] = (abs2(sahi_sub_cur), abs2(sahi_sub_prev))
    L[(3, "Industry", "Market share", "Specialized Insurer")] = (abs2(spec_sub_cur), abs2(spec_sub_prev))

    # Slide 3's per-segment rows are matched to the Segmentwise Report's own
    # header row by NORMALIZED name (case/whitespace/punctuation folded), so
    # the sheet's irregular spellings - "Marine  Cargo" and "Marine  Hull"
    # with a double space, "Health " and "Aviation " with a trailing one -
    # resolve without those exact variants being written out here. A future
    # GIC.xlsx that tidies its spacing, reorders columns or adds new ones
    # therefore still maps correctly instead of silently falling through to a
    # missing-column zero.
    seg_metric2_map = {
        m2: m2 for m2 in (
            "Fire", "Marine Total", "Marine Cargo", "Marine Hull", "Engineering",
            "Motor Total", "Motor OD", "Motor TP", "Health", "Aviation",
            "Liability", "P.A.",
            "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)",
        )
    }
    seg_col_by_norm = {_norm_seg(c): c for c in seg_cols}
    for metric2, gic_col in seg_metric2_map.items():
        actual = seg_col_by_norm.get(_norm_seg(gic_col))
        if actual is None:
            log.warning("Slide 3: no Segmentwise column matches %r (sheet columns: %s) - "
                        "leaving row unwritten.", metric2, seg_cols)
            continue
        cur = get(seg, "Industry Total", actual, "cur")
        prev = get(seg, "Industry Total", actual, "prev")
        L[(3, "Industry", "Market share", metric2)] = (abs2(cur), abs2(prev))

    def sahi_pa_total(key):
        return sum(get(seg, lbl, "P.A.", key) or 0 for lbl in SAHI_ROW_LABELS)

    def pvt_gi_pa_total(key):
        total = get(seg, "General Insurers Sub Total", "P.A.", key) or 0
        psu = sum(get(seg, lbl, "P.A.", key) or 0 for lbl in PSU_INSURERS)
        return total - psu

    def public_gi_pa_total(key):
        return sum(get(seg, lbl, "P.A.", key) or 0 for lbl in PSU_INSURERS)

    # ---- Slide 4: Health Industry (incl PA & Travel) market share ----
    health_total_cur = get(hp, "Industry Total", "Grand Total", "cur")
    health_total_prev = get(hp, "Industry Total", "Grand Total", "prev")
    pa_total_cur = seg["market_share"].get("P.A.", {}).get("cur")  # industry-wide PA share, not health-specific
    # Health industry incl PA: add total P.A. GDPI (segmentwise) to Health GDPI (health portfolio)
    pa_gdpi_cur = get(seg, "Industry Total", "P.A.", "cur")
    pa_gdpi_prev = get(seg, "Industry Total", "P.A.", "prev")
    health_incl_pa_cur = (health_total_cur or 0) + (pa_gdpi_cur or 0)
    health_incl_pa_prev = (health_total_prev or 0) + (pa_gdpi_prev or 0)

    def psu_health_total(key):
        return sum(get(hp, lbl, "Grand Total", key) or 0 for lbl in PSU_INSURERS)

    hp_public_cur = psu_health_total("cur")
    hp_public_prev = psu_health_total("prev")
    hp_gi_sub_cur = get(hp, "General Insurers Sub Total", "Grand Total", "cur")
    hp_gi_sub_prev = get(hp, "General Insurers Sub Total", "Grand Total", "prev")
    hp_private_cur = hp_gi_sub_cur - hp_public_cur if hp_gi_sub_cur is not None else None
    hp_private_prev = hp_gi_sub_prev - hp_public_prev if hp_gi_sub_prev is not None else None
    hp_sahi_cur = get(hp, "Stand-alone Health sub Total", "Grand Total", "cur")
    hp_sahi_prev = get(hp, "Stand-alone Health sub Total", "Grand Total", "prev")

    # Same absolute-value convention as Slide 3 (see abs2 above). Slide 4 is
    # "incl. PA and Travel", so each bucket's PA slice (from the segmentwise
    # sheet, since hp's Grand Total column is Health-only) has to be added
    # back in for the top-level Private/Public/SAHI rows - verified exactly
    # against GT (e.g. Private: 40499.35 (Health only) + 4182.08 (PSU-excl.
    # Private's own P.A., == Slide 6's Pvt GI P.A.) = 44681.43).
    hp_private_pa_cur, hp_private_pa_prev = pvt_gi_pa_total("cur"), pvt_gi_pa_total("prev")
    hp_public_pa_cur, hp_public_pa_prev = public_gi_pa_total("cur"), public_gi_pa_total("prev")
    hp_sahi_pa_cur, hp_sahi_pa_prev = sahi_pa_total("cur"), sahi_pa_total("prev")
    L[(4, "Health Industry (Inc. PA and Travel)", "Market share", "Private")] = (
        abs2(hp_private_cur + hp_private_pa_cur), abs2(hp_private_prev + hp_private_pa_prev))
    L[(4, "Health Industry (Inc. PA and Travel)", "Market share", "Public")] = (
        abs2(hp_public_cur + hp_public_pa_cur), abs2(hp_public_prev + hp_public_pa_prev))
    L[(4, "Health Industry (Inc. PA and Travel)", "Market share", "SAHI")] = (
        abs2(hp_sahi_cur + hp_sahi_pa_cur), abs2(hp_sahi_prev + hp_sahi_pa_prev))

    for metric2, hp_col in [("Health-Retail", "Health-Retail"), ("Health-Group", "Health-Group"),
                             ("Health-Government schemes", "Health-Government schemes"),
                             ("Overseas Medical", "Overseas Medical")]:
        cur = get(hp, "Industry Total", hp_col, "cur")
        prev = get(hp, "Industry Total", hp_col, "prev")
        L[(4, "Health Industry (Inc. PA and Travel)", "Market share", metric2)] = (abs2(cur), abs2(prev))
    L[(4, "Health Industry (Inc. PA and Travel)", "Market share", "P.A.")] = (abs2(pa_gdpi_cur), abs2(pa_gdpi_prev))

    # ---- Slide 6/7/10/11: per-company & per-segment Health Portfolio mix ----
    # Slide 7: per-company Health-Retail/Group/Govt/Overseas (from hp) + P.A. (from seg)
    for gic_label, short in SAHI_ROW_LABELS.items():
        for metric2, hp_col in [("Health-Retail", "Health-Retail"), ("Health-Group", "Health-Group"),
                                 ("Health-Government schemes", "Health-Government schemes"),
                                 ("Overseas Medical", "Overseas Medical")]:
            cur = get(hp, gic_label, hp_col, "cur")
            prev = get(hp, gic_label, hp_col, "prev")
            L[(7, gic_label, "Market share", metric2)] = (cur, prev)
        pa_cur = get(seg, gic_label, "P.A.", "cur")
        pa_prev = get(seg, gic_label, "P.A.", "prev")
        L[(7, gic_label, "Market share", "P.A.")] = (pa_cur, pa_prev)

    # Slide 6: SAHI / Pvt GI / Public GI totals by Health-Retail/Group/Govt/Overseas/PA
    def sahi_component_total(hp_col, key):
        return sum(get(hp, lbl, hp_col, key) or 0 for lbl in SAHI_ROW_LABELS)

    def pvt_gi_component_total(hp_col, key):
        # General Insurers Sub Total minus the 4 PSU rows, for a given column
        total = get(hp, "General Insurers Sub Total", hp_col, key) or 0
        psu = sum(get(hp, lbl, hp_col, key) or 0 for lbl in PSU_INSURERS)
        return total - psu

    def public_gi_component_total(hp_col, key):
        return sum(get(hp, lbl, hp_col, key) or 0 for lbl in PSU_INSURERS)

    for grp_name, fn in [("SAHI Market", sahi_component_total), ("Pvt GI", pvt_gi_component_total),
                          ("Public GI", public_gi_component_total)]:
        for metric2, hp_col in [("Health-Retail", "Health-Retail"), ("Health-Group", "Health-Group"),
                                 ("Health-Government schemes", "Health-Government schemes"),
                                 ("Overseas Medical", "Overseas Medical")]:
            cur = fn(hp_col, "cur")
            prev = fn(hp_col, "prev")
            L[(6, grp_name, "Market share", metric2)] = (cur, prev)

    L[(6, "SAHI Market", "Market share", "P.A.")] = (sahi_pa_total("cur"), sahi_pa_total("prev"))
    L[(6, "Pvt GI", "Market share", "P.A.")] = (pvt_gi_pa_total("cur"), pvt_gi_pa_total("prev"))
    L[(6, "Public GI", "Market share", "P.A.")] = (public_gi_pa_total("cur"), public_gi_pa_total("prev"))

    # Both slides are "mix" (composition %) rows: each bucket's absolute
    # value as a fraction of that SAME group/company's own 5-bucket total
    # (Retail+Group+Govt+Travel+PA) - verified exactly against GT for every
    # Slide 11 entity and for Slide 10's SAHI row. NOTE: Slide 10's Pvt. GI /
    # Public GI / Industry rows do NOT fit this (nor a plain YoY-growth
    # reading, which only 2 of the 4 groups happen to match) - GT's own
    # figures for those three groups look internally inconsistent (they
    # don't sum to ~1, and no single formula reproduces all of them), so
    # they're left as absolute Rs. Crore rather than guessed at further.
    def mix5(retail, group, govt, travel, pa):
        total_cur = sum((v[0] or 0) for v in (retail, group, govt, travel, pa))
        total_prev = sum((v[1] or 0) for v in (retail, group, govt, travel, pa))

        def frac(v, total):
            if not isinstance(v, (int, float)):
                return None
            if not total:
                return 0 if v == 0 else None
            return round_half_up(v / total, 4)

        return {
            "Retail": (frac(retail[0], total_cur), frac(retail[1], total_prev)),
            "Group": (frac(group[0], total_cur), frac(group[1], total_prev)),
            "Govt.": (frac(govt[0], total_cur), frac(govt[1], total_prev)),
            "Travel": (frac(travel[0], total_cur), frac(travel[1], total_prev)),
            "PA": (frac(pa[0], total_cur), frac(pa[1], total_prev)),
        }

    # Slide 10: Segment-wise GDPI mix (Retail/Group/Govt/Travel/PA) for SAHI / Pvt.GI / Public GI / Industry
    # "Travel" has no distinct GIC column -> maps to Overseas Medical; PA comes from segmentwise.
    def industry_component_total(hp_col, key):
        return get(hp, "Industry Total", hp_col, key)

    slide10_groups = [
        ("SAHI", sahi_component_total, sahi_pa_total),
        ("Pvt. GI", pvt_gi_component_total, pvt_gi_pa_total),
        ("Public GI", public_gi_component_total, public_gi_pa_total),
        ("Industry", lambda col, key: industry_component_total(col, key),
         lambda key: get(seg, "Industry Total", "P.A.", key)),
    ]
    for grp_name, fn, pa_fn in slide10_groups:
        buckets = {
            "Retail": (fn("Health-Retail", "cur"), fn("Health-Retail", "prev")),
            "Group": (fn("Health-Group", "cur"), fn("Health-Group", "prev")),
            "Govt.": (fn("Health-Government schemes", "cur"), fn("Health-Government schemes", "prev")),
            "Travel": (fn("Overseas Medical", "cur"), fn("Overseas Medical", "prev")),
            "PA": (pa_fn("cur"), pa_fn("prev")),
        }
        if grp_name == "SAHI":
            buckets = mix5(buckets["Retail"], buckets["Group"], buckets["Govt."], buckets["Travel"], buckets["PA"])
        for metric2, val in buckets.items():
            L[(10, "Segment-wise GDPI mix", grp_name, metric2)] = val

    # Slide 11: Segment-wise GDPI mix - SAHI, per named company (Retail/Group/Govt/Travel/PA) + SAHI total
    company_name_map = {
        "Niva Bupa": "Niva bupa health insurance company limited", "Star": "Star Health & Allied Insurance Co Ltd",
        "ABHI": "Aditya Birla Health Insurance Co Ltd", "CARE": "Care Health Insurance Ltd",
        "Manipal Cigna": "ManipalCigna Health Insurance Co Ltd", "Galaxy": "Galaxy Health Insurance Company Ltd",
        "Narayana": "Narayana Health Insurance Ltd",
    }
    for short, gic_label in company_name_map.items():
        mix = mix5(
            (get(hp, gic_label, "Health-Retail", "cur"), get(hp, gic_label, "Health-Retail", "prev")),
            (get(hp, gic_label, "Health-Group", "cur"), get(hp, gic_label, "Health-Group", "prev")),
            (get(hp, gic_label, "Health-Government schemes", "cur"), get(hp, gic_label, "Health-Government schemes", "prev")),
            (get(hp, gic_label, "Overseas Medical", "cur"), get(hp, gic_label, "Overseas Medical", "prev")),
            (get(seg, gic_label, "P.A.", "cur"), get(seg, gic_label, "P.A.", "prev")),
        )
        for metric2, val in mix.items():
            L[(11, "Segment-wise GDPI mix - SAHI", short, metric2)] = val
    sahi_mix = mix5(
        (sahi_component_total("Health-Retail", "cur"), sahi_component_total("Health-Retail", "prev")),
        (sahi_component_total("Health-Group", "cur"), sahi_component_total("Health-Group", "prev")),
        (sahi_component_total("Health-Government schemes", "cur"), sahi_component_total("Health-Government schemes", "prev")),
        (sahi_component_total("Overseas Medical", "cur"), sahi_component_total("Overseas Medical", "prev")),
        (sahi_pa_total("cur"), sahi_pa_total("prev")),
    )
    for metric2, val in sahi_mix.items():
        L[(11, "Segment-wise GDPI mix - SAHI", "SAHI", metric2)] = val

    # ---- Slide 5 / 9: per-company GDPI (Health incl PA) & growth vs SAHI CAGR ----
    def company_health_incl_pa(gic_label, key):
        h = get(hp, gic_label, "Grand Total", key) or 0
        pa = get(seg, gic_label, "P.A.", key) or 0
        return h + pa

    sahi_total_cur = sum(company_health_incl_pa(lbl, "cur") for lbl in SAHI_ROW_LABELS)
    sahi_total_prev = sum(company_health_incl_pa(lbl, "prev") for lbl in SAHI_ROW_LABELS)

    # Exact Metric2 / Company text used on Slide 5 and Slide 9 respectively
    # (the sheet uses inconsistent short names per slide, typo included).
    slide5_metric2 = {
        "Niva bupa health insurance company limited": "Niva Bupa",
        "Aditya Birla Health Insurance Co Ltd": "ABHI",
        "Care Health Insurance Ltd": "CARE",
        "Galaxy Health Insurance Company Ltd": "Galaxy Health",
        "ManipalCigna Health Insurance Co Ltd": "Manipal Cigna",
        "Narayana Health Insurance Ltd": "Naryana Health",  # sic - typo in source sheet
        "Star Health & Allied Insurance Co Ltd": "STAR",
    }
    slide9_company = {
        "Niva bupa health insurance company limited": "Niva bupa health insurance company limited",
        "Aditya Birla Health Insurance Co Ltd": "ABHI",
        "Care Health Insurance Ltd": "CARE",
        "Galaxy Health Insurance Company Ltd": "Galaxy Health",
        "ManipalCigna Health Insurance Co Ltd": "Manipal Cigna",
        "Narayana Health Insurance Ltd": "Narayana Health",
        "Star Health & Allied Insurance Co Ltd": "STAR",
    }
    for gic_label in SAHI_ROW_LABELS:
        cur = company_health_incl_pa(gic_label, "cur")
        prev = company_health_incl_pa(gic_label, "prev")
        # Same absolute-value convention as Slides 3/4 (see abs2 above).
        L[(5, "SAHI Market", "Market share", slide5_metric2[gic_label])] = (
            round_half_up(cur, 2) if cur is not None else None,
            round_half_up(prev, 2) if prev is not None else None,
        )
        g = growth(cur, prev)
        L[(9, slide9_company[gic_label], "SAHI Growth", "GDPI Growth SAHI")] = (
            round_half_up(g, 4) if g is not None else None, None,
        )
    sahi_g = growth(sahi_total_cur, sahi_total_prev)
    L[(9, "SAHI", "SAHI Growth", "GDPI Growth SAHI")] = (round_half_up(sahi_g, 4) if sahi_g is not None else None, None)
    L[(9, "SAHI", "GDPI Growth SAHI", "SAHI CAGR")] = (round_half_up(sahi_g, 4) if sahi_g is not None else None, None)
    # NOTE: rows 93-99 (Metric1="CAGR", Metric2="SAHI CAGR") ask for a true
    # multi-year CAGR, which is not derivable from a single YoY snapshot -
    # intentionally left unmapped/blank rather than guessed.

    # ---- Slide 8: aggregate growth% (Health industry incl. PA, SAHI sub-total) ----
    # "Industry Total" here means the Health industry (this slide is entirely
    # about health-related GDPI growth, not the full P&C industry) - verified
    # against GT: growth of health_incl_pa_cur/prev reproduces GT's figure
    # exactly, whereas growth of the full segmentwise grand total does not.
    industry_g = growth(health_incl_pa_cur, health_incl_pa_prev)
    sahi_health_g = growth(get(seg, "Stand-alone Health sub Total", "Grand Total", "cur"),
                            get(seg, "Stand-alone Health sub Total", "Grand Total", "prev"))
    # GT fills the prior-period slot with a literal 0 for these single-period
    # growth-rate rows (no real "prior growth rate" concept here) rather
    # than leaving it blank.
    L[(8, "Industry Total", "Growth %", "")] = (round_half_up(industry_g, 4) if industry_g is not None else None, 0)
    L[(8, "Stand-alone Health sub Total", "Growth %", "")] = (round_half_up(sahi_health_g, 4) if sahi_health_g is not None else None, 0)

    # ---- Slide 14: Retail Revenue / Retail Accretion (GIC-sourced, per company) ----
    slide14_company = {
        "Niva bupa health insurance company limited": "NBHI",
        "Aditya Birla Health Insurance Co Ltd": "ABHI",
        "Care Health Insurance Ltd": "Care Health",
        "Galaxy Health Insurance Company Ltd": "Galaxy Health",
        "ManipalCigna Health Insurance Co Ltd": "Manipal Cigna",
        "Narayana Health Insurance Ltd": "Narayana Health",
        "Star Health & Allied Insurance Co Ltd": "Star Health",
    }
    for gic_label, short in slide14_company.items():
        cur = get(hp, gic_label, "Health-Retail", "cur")
        prev = get(hp, gic_label, "Health-Retail", "prev")
        L[(14, short, "Retail Revenue", "")] = (cur, prev)
        if cur is not None and prev is not None:
            L[(14, short, "Retail Accretion", "Retail Revenue CY-Retail Revenue PY")] = (round_half_up(cur - prev, 2), None)

    return L


def apply_gic_rows(ws, lookups, dry_run=False, force=False):
    updated, skipped = 0, []
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        slide = d["Slide #"]
        if slide not in (3, 4, 5, 6, 7, 8, 9, 10, 11, 14):
            continue
        if not force and d[CUR] is not None and d[PRIOR] is not None:
            continue
        company = (d["Company"] or "").strip()
        metric1 = (d["Meric 1"] or "").strip()
        metric2 = (d["Metric 2"] or "").strip() if d["Metric 2"] else ""
        key = (slide, company, metric1, metric2)
        if key not in lookups:
            skipped.append((r, key))
            continue
        cur, prev = lookups[key]
        if cur is None and prev is None:
            skipped.append((r, key))
            continue
        if not dry_run:
            if cur is not None:
                ws.cell(row=r, column=COL[CUR]).value = round_half_up(cur, 4) if isinstance(cur, float) else cur
            if prev is not None:
                ws.cell(row=r, column=COL[PRIOR]).value = round_half_up(prev, 4) if isinstance(prev, float) else prev
            g = growth(cur, prev)
            if g is not None:
                ws.cell(row=r, column=COL["Growth"]).value = g
        updated += 1
    return updated, skipped


# ---------------------------------------------------------------------------
# Slides 8 & 12: convention fixes derived from cross-checking against
# a ground-truth cross-check. Both need each company's NL-36 "Total (A)" cumulative
# Premium figure, which the Gemini pipeline doesn't extract on its own -
# handled here as one deterministic pass over the already-populated sheet.
# ---------------------------------------------------------------------------

# NL-36 figures are extracted from each company's own filing at run time by
# forms.extract_nl36 (which resolves the cumulative "Up to the quarter"
# premium column from the form's own header, per filing) rather than being
# transcribed here. An earlier version of this module carried a hand-typed
# table of per-company Rs. Lakhs literals; those were correct only for the
# quarter they were typed for and silently went stale afterwards.
_NL36_CACHE = {}


def nl36_for(company_short):
    """Cached NL-36 extraction for one company, keyed by short name."""
    if company_short not in _NL36_CACHE:
        pdf = COMPANY_PDFS.get(company_short)
        data = pdf_extract_nl36(pdf) if pdf else None
        if data is None:
            log.warning("NL-36: extraction failed for %s - "
                        "Slide 8/12 rows for this company will be left unwritten.", company_short)
        _NL36_CACHE[company_short] = data
    return _NL36_CACHE[company_short]


SLIDE8_COMPANY = {
    "NBHI": " Niva bupa health insurance company limited", "ABHI": "ABHI",
    "Care Health": "CARE", "Star Health": "STAR", "Manipal Cigna": "Manipal Cigna",
    "Narayana Health": "Narayana Health", "Galaxy Health": "Galaxy Health",
}
SLIDE8_METRIC2 = {"NBHI": "Health + PA + Travel"}
SLIDE8_NL4_METRIC1 = "GDPI (NL-4)"


def fix_slide8_and_slide12(ws, gic=None, dry_run=False):
    """Writes Slide 8's per-company GDPI (from GIC.xlsx's Segmentwise
    Report Grand Total - see gic_gdpi_lookup) and Slide 12's channel mix
    (still from each company's own NL-36, which GIC doesn't break down by
    channel).

    Idempotent: every value is derived from the source, not from whatever the
    cell already held. (An earlier version converted Slide 12's cells in place
    by reading them back as its own input, which meant running it twice
    re-divided an already-converted fraction.)"""
    idx = build_row_index(ws)
    written = 0
    log = []
    gdpi_lookup = gic_gdpi_lookup(gic)

    def write_cell(row, cur, prior):
        nonlocal written
        if cur is None and prior is None:
            return
        if not dry_run:
            if cur is not None:
                ws.cell(row=row, column=COL[CUR]).value = cur
            if prior is not None:
                ws.cell(row=row, column=COL[PRIOR]).value = prior
            g = growth(cur, prior)
            if g is not None:
                ws.cell(row=row, column=COL["Growth"]).value = g
        written += 1

    for company in SLIDE8_COMPANY:
        gt_cur, gt_prior = gdpi_lookup.get(company, (None, None))
        # GIC is authoritative for GDPI, but it can print 0 for a year the
        # filer did write business (Narayana's FY25 "Previous Year" row in
        # the FY25-26 Q4 GIC.xlsx, against 237.18 lakh in its own NL-4) -
        # a 0/None GIC figure falls back to the company's NL-4 GDPI, which is
        # also written to its own "GDPI (NL-4)" row for audit.
        nl4_cur, nl4_prior = apply_income_statement_rows._cache.get(company, {}).get("NL-4 GDPI", (None, None))
        written += apply_metric_to_rows(ws, idx, 8, SLIDE8_COMPANY[company], SLIDE8_NL4_METRIC1, None,
                                        nl4_cur, nl4_prior, dry_run, log)
        if not gt_cur and nl4_cur:
            log.append((8, company, "Revenue Growth (GDPI)", "current", f"GIC {gt_cur} -> NL-4 {nl4_cur}"))
            gt_cur = nl4_cur
        if not gt_prior and nl4_prior:
            log.append((8, company, "Revenue Growth (GDPI)", "prior", f"GIC {gt_prior} -> NL-4 {nl4_prior}"))
            gt_prior = nl4_prior
        if gt_cur is not None or gt_prior is not None:
            written += apply_metric_to_rows(
                ws, idx, 8, SLIDE8_COMPANY[company], "Revenue Growth (GDPI)",
                SLIDE8_METRIC2.get(company), gt_cur, gt_prior,
                dry_run, log,
            )

        nl36 = nl36_for(company)
        if nl36 is None:
            continue

        # Slide 12 divides by "Total (A)" instead, NOT by the Grand Total:
        # the per-channel rows on NL-36 add up to Total (A) by construction,
        # so it is the only denominator under which the six channel shares
        # sum to exactly 1.0. Mixing in (B), which has no channel breakdown,
        # would leave an unattributable remainder.
        total_cur, total_prior = nl36["total_a"]
        metric1_12 = gemini_extract.SLIDE12_COMPANY_METRIC1[company]
        key = (12, normalize_text("GDPI by Channel - SAHI"), normalize_text(metric1_12))
        rows_for_metric2 = {m2: r for r, m2 in idx.get(key, [])}

        # "Others" is the residual Total(A) - (the five named buckets), not a
        # named NL-36 line. The sub-channels it covers (Common Service
        # Centres/CSC, Insurance Marketing Firms, Point of Sales, Web
        # Aggregators, MISP, Referral Arrangements, ...) are labelled
        # inconsistently between insurers and new ones appear over time, so a
        # residual both stays correct as those labels drift and guarantees
        # the six shares sum to 1.0.
        buckets = dict(nl36["channels"])
        buckets["Others"] = nl36["others"]

        for metric2, (cur_lakhs, prior_lakhs) in buckets.items():
            row = rows_for_metric2.get(normalize_text(metric2))
            if row is None:
                continue
            frac_cur = (round_half_up(cur_lakhs / total_cur, 4)
                        if isinstance(cur_lakhs, (int, float)) and total_cur else None)
            frac_prior = (round_half_up(prior_lakhs / total_prior, 4)
                          if isinstance(prior_lakhs, (int, float)) and total_prior else None)
            write_cell(row, frac_cur, frac_prior)

    return written, log


def assert_channel_mix_sums(ws, tolerance=0.005):
    """Sheet-wide invariant: every company's six Slide 12 channel shares must
    sum to 1.0 in both periods. Runs after every pipeline execution - this is
    period-agnostic (it never references a date or an expected figure), so it
    catches a dropped or double-counted channel bucket in any future quarter.
    Returns [(company, period, total), ...] for any company that fails."""
    failures = []
    per = {}
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=COL["Slide #"]).value != 12:
            continue
        company = ws.cell(row=r, column=COL["Meric 1"]).value
        for period in (CUR, PRIOR):
            v = ws.cell(row=r, column=COL[period]).value
            if isinstance(v, (int, float)):
                per.setdefault((company, period), []).append(v)
    for (company, period), vals in sorted(per.items(), key=lambda kv: str(kv[0])):
        total = sum(vals)
        # A company with no channel data at all (every share blank) is
        # reported separately rather than as a sum-to-zero failure.
        if len(vals) < 6 or abs(total - 1.0) > tolerance:
            failures.append((company, period, round_half_up(total, 6), len(vals)))
    return failures


# ---------------------------------------------------------------------------
# Slide 18: Income Statement (per-company, from IRDAI PDFs)
# ---------------------------------------------------------------------------

def lakhs_to_cr(v):
    return round_half_up(v / 100, 2) if isinstance(v, (int, float)) else None


# NL-6's reinsurance-accepted commission line, most specific wording first:
# most filers print "Commission on Re-insurance Accepted", Care Health just
# "Add: Re-insurance Accepted". NL-6 has no other "accepted" row, so the bare
# wording can't pick up the wrong line.
# NL-6's Gross Commission is the sum of these rows. Narayana prints the
# Gross row itself as "-" with the figure only in Commission & Remuneration.
GROSS_COMMISSION_COMPONENTS = ("Commission & Remuneration", "Rewards", "Distribution fees")


def _gross_or_components(gross, read):
    """(cur, prior) Gross Commission: the Gross row as read, unless it is
    blank/zero in both periods while its component rows carry figures -
    then the sum of GROSS_COMMISSION_COMPONENTS, each read via `read(label)`
    -> (cur, prior)."""
    if any(v for v in gross if v):
        return gross
    parts = [read(label) for label in GROSS_COMMISSION_COMPONENTS]
    if not any(p[k] for p in parts for k in (0, 1)):
        return gross
    return tuple(sum(p[k] or 0 for p in parts) if any(p[k] is not None for p in parts) else gross[k]
                 for k in (0, 1))


RI_ACCEPTED_COMMISSION_LABELS = [("Commission on Re-insurance Accepted",), ("Re-insurance Accepted",),
                                 ("Reinsurance Accepted",)]


def extract_income_statement(company_short, pdf_path):
    """Returns {metric_label: (fy26_q3_cr, fy25_q3_cr)} for one company."""
    out = {}

    nl1, _ = get_form_page(pdf_path, r"FORM\s+NL-1-B-RA")
    nl2, _ = get_form_page(pdf_path, r"FORM\s+NL-2-B-PL")
    nl4, _ = get_form_page(pdf_path, r"FORM\s+NL-4")

    # Fallback for PDFs with no ruled gridlines (table detection then finds
    # nothing even though the page matched) - e.g. Narayana Health - use the
    # plain-text line parser instead. It resolves the cumulative columns from
    # each page's own period header; do NOT reintroduce a fixed column order
    # here. Narayana's NL-2 prints [UpToQ-cur, ForQ-cur, ForQ-prior,
    # UpToQ-prior] - i.e. its prior-year pair is reversed relative to its
    # current-year pair, and relative to its own NL-20 - so any fixed index
    # is right for one column and wrong for another.
    nl1_text = nl2_text = nl4_text = None
    if nl4 is None:
        nl4_text, _ = get_form_text(pdf_path, r"FORM\s+NL-4")
    if nl1 is None:
        nl1_text, _ = get_form_text(pdf_path, r"FORM\s+NL-1-B-RA")
    if nl2 is None:
        nl2_text, _ = get_form_text(pdf_path, r"FORM\s+NL-2-B-PL")

    if nl4:
        # The short labels ("Gross Direct", "Net Written", "Net Earned") are
        # last-resort variants: Narayana wraps each label over two lines, so
        # its table cells carry only the first line.
        gdp = get_line_item_any(nl4, [("Gross Direct Premium",), ("Gross Direct",)])
        ri_accepted = get_line_item_any(nl4, [("Premium on reinsurance accepted",), ("reinsurance accepted",)])
        nwp = get_line_item_any(nl4, [("Net Written Premium",), ("Net Written",)])
        ep = get_line_item_any(nl4, [("Net Earned Premium",), ("Total Premium Earned (Net)",), ("Premium Earned (Net)",),
                                     ("Net Earned",)])
    elif nl4_text:
        gdp = get_line_item_from_text(nl4_text, "Gross Direct Premium", form="NL-4")
        ri_accepted = get_line_item_from_text(nl4_text, "reinsurance accepted", form="NL-4")
        nwp = get_line_item_from_text(nl4_text, "Net Written Premium", form="NL-4")
        ep = get_line_item_from_text(nl4_text, "Earned Premium", form="NL-4")
    else:
        gdp = ri_accepted = nwp = ep = (None, None)

    def _add(a, b):
        # "Add: Premium on reinsurance accepted" is "-"/blank for most
        # insurers most quarters - treat a missing addend as 0, but a
        # missing GDP itself still propagates as None (don't invent a GWP
        # figure with no base premium at all).
        return None if a is None else a + (b or 0)

    # GDPI (GWP's base component) is authoritatively GIC.xlsx's per-company
    # Grand Total (Segmentwise Report), not this company's own NL-4 line -
    # verified to match NL-4 to the rupee when both are present, so GIC is
    # simply the more authoritative source for the same figure. GIC's Grand
    # Total is already Rs. Crore (not Lakhs like every PDF form here), so
    # it's converted to a Lakhs-equivalent to combine with ri_accepted
    # before this function's one lakhs_to_cr pass at the end. Falls back to
    # this company's own NL-4 "Gross Direct Premium" line only when GIC.xlsx
    # wasn't available at all for this run (e.g. not yet on disk for the
    # target quarter) - not a silent guess, since GT confirms it's the same
    # number either way.
    #
    # The PRIOR year prefers the company's own NL-4 prior column instead:
    # GIC's "Previous Year" row can disagree with both GIC's own figure for
    # that year and the filing - FY25-26 Q4's GIC has CARE's FY25 at
    # 8,296.56 cr against 8,318.25 in last year's GIC and in CARE's NL-4, and
    # Narayana's FY25 at 0 against 2.37. GIC is the fallback when NL-4 has no
    # prior figure.
    gic_gdpi = _GIC_GDPI_LOOKUP.get(company_short)
    gic_cur_cr, gic_prior_cr = gic_gdpi if gic_gdpi is not None else (None, None)
    gdp_final = (
        gic_cur_cr * 100 if gic_cur_cr is not None else gdp[0],
        gdp[1] if gdp[1] is not None else (gic_prior_cr * 100 if gic_prior_cr is not None else None),
    )

    gwp = (_add(gdp_final[0], ri_accepted[0]), _add(gdp_final[1], ri_accepted[1]))
    out["Gross Written Premium"] = tuple(lakhs_to_cr(v) for v in gwp)
    # The filing's own GDPI, both periods - Slide 8's "GDPI (NL-4)" row and
    # the fallback for its GIC-sourced GDPI (fix_slide8_and_slide12).
    out["NL-4 GDPI"] = tuple(lakhs_to_cr(v) for v in gdp)
    out["Net Written Premium"] = tuple(lakhs_to_cr(v) for v in nwp)
    out["Earned Premium"] = tuple(lakhs_to_cr(v) for v in ep)

    if nl1:
        claims = get_line_item(nl1, "Claims Incurred")
        commission = get_line_item(nl1, "Commission")
        opex = get_line_item(nl1, "Operating Expenses related to Insurance Business")
        ph_interest = get_line_item(nl1, "Interest,", "Rent")
        ph_profit_sale = get_line_item(nl1, "sale", "investments")
        out["Claims"] = tuple(lakhs_to_cr(v) for v in claims)
        out["Operating Expenses"] = tuple(lakhs_to_cr(v) for v in opex)
    elif nl1_text:
        claims = get_line_item_from_text(nl1_text, "Claims Incurred", form="NL-1")
        commission = get_line_item_from_text(nl1_text, "Commission", form="NL-1")
        opex = get_line_item_from_text(nl1_text, "Operating Expenses related to Insurance Business", form="NL-1")
        # "Gross" is included in the label match (and so gets stripped along
        # with it) specifically to consume a stray "-" that some insurers
        # print as a typographic dash before "(Gross)" (e.g. "Interest,
        # Dividend and Rent - (Gross)") - left unstripped, _NUM_TOKEN reads
        # that dash as a zero-value placeholder cell and shifts every
        # following column left by one.
        ph_interest = get_line_item_from_text(nl1_text, "Interest, Dividend", "Gross", form="NL-1")
        ph_profit_sale = get_line_item_from_text(nl1_text, "Profit / Loss on Sale", form="NL-1")
        out["Claims"] = tuple(lakhs_to_cr(v) for v in claims)
        out["Operating Expenses"] = tuple(lakhs_to_cr(v) for v in opex)
    else:
        commission = opex = (None, None)
        ph_interest = ph_profit_sale = (None, None)

    # NL-1's own "Commission" and "Claims Incurred" lines already equal NL-6's
    # Net Commission and NL-5's Net Incurred Claims verbatim (NL-1 cites them
    # by schedule number and restates their grand totals - verified against
    # this same filing: both pairs match exactly) - reused here rather than
    # re-fetching NL-5/NL-6 a second time for the same figures.
    out["Net Commission"] = tuple(lakhs_to_cr(v) for v in commission)
    out["Net Incurred Claims"] = tuple(lakhs_to_cr(v) for v in claims)

    # Gross Commission and Commission on Re-insurance Accepted are only on
    # NL-6 itself (NL-1 nets them away into the single "Commission" line
    # above) - needed for the EOM Ratio formula. Some insurers (e.g.
    # ManipalCigna) don't use the label "Gross Commission" at all - their
    # NL-6 calls the same line "Direct Commission" instead. Likewise Care
    # Health's NL-6 labels the RI-accepted line just "Add: Re-insurance
    # Accepted" (see RI_ACCEPTED_COMMISSION_LABELS).
    nl6, _ = get_form_page(pdf_path, r"FORM\s+NL-6")
    nl6_text = None
    if nl6 is None:
        nl6_text, _ = get_form_text(pdf_path, r"FORM\s+NL-6")
    if nl6:
        gross_commission = _gross_or_components(
            get_line_item_any(nl6, [("Gross Commission",), ("Direct Commission",)]),
            lambda label: get_line_item(nl6, label))
        ri_accepted_commission = get_line_item_any(nl6, RI_ACCEPTED_COMMISSION_LABELS)
    elif nl6_text:
        gross_commission = get_line_item_from_text(nl6_text, "Gross Commission", form="NL-6")
        if gross_commission == (None, None):
            gross_commission = get_line_item_from_text(nl6_text, "Direct Commission", form="NL-6")
        ri_accepted_commission = (None, None)
        for (label,) in RI_ACCEPTED_COMMISSION_LABELS:
            ri_accepted_commission = get_line_item_from_text(nl6_text, label, form="NL-6")
            if ri_accepted_commission != (None, None):
                break
    else:
        gross_commission = ri_accepted_commission = (None, None)
    out["Gross Commission"] = tuple(lakhs_to_cr(v) for v in gross_commission)
    out["RI Accepted Commission"] = tuple(lakhs_to_cr(v) for v in ri_accepted_commission)

    # GST is only on NL-7 (item 16, "Goods and Services Tax (GST)") - needed
    # for the EOM Ratio formula, which excludes it from Opex. "Goods and
    # Service" (singular, no trailing s on "Service") matches both that
    # wording and ABHI's own "Goods and Service Tax" - confirmed against a
    # real filing that the plural "Services" version misses ABHI entirely.
    # All NL-7 pages: Star/Galaxy print the prior-year block on a second page.
    nl7, _ = get_form_page(pdf_path, r"FORM\s+NL-7", all_matches=True)
    nl7_text = None
    if nl7 is None:
        nl7_text, _ = get_form_text(pdf_path, r"FORM\s+NL-7")
    if nl7:
        gst = get_line_item(nl7, "Goods and Service")
    elif nl7_text:
        gst = get_line_item_from_text(nl7_text, "Goods and Service", form="NL-7")
    else:
        gst = (None, None)
    out["GST"] = tuple(lakhs_to_cr(v) for v in gst)

    # NL-7 "Others: In House Claim Processing Cost" - printed as a negative
    # (cost moved out of opex into claims) by the filers that have it (Star
    # Health; Galaxy prints the line blank). Half of it comes off manpower
    # cost (see compute_derived_metrics). All NL-7 pages, since Star prints
    # its prior-year block on a second page.
    if nl7:
        in_house = get_line_item(nl7, "In House Claim Processing")
    elif nl7_text:
        in_house = get_line_item_from_text(nl7_text, "In House Claim Processing", form="NL-7")
    else:
        in_house = (None, None)
    out["In House Claim Processing Cost"] = tuple(lakhs_to_cr(abs(v)) if v is not None else None
                                                  for v in in_house)

    # NL-2's "TOTAL (B)" line is PROVISIONS (Other than Taxation, section 4)
    # plus OTHER EXPENSES (section 5) combined - verified against NBHI's own
    # sub-items (65 + 946 = 1,011 = its printed TOTAL (B)). Section 5 includes
    # "(f) Contribution to Policyholders' A/c" - the shareholders' account
    # reimbursing the policyholders' account for something (excess Expense of
    # Management, MD/CEO/WTD remuneration, or an "Others" catch-all) - an
    # internal transfer, not a real operating cost, so it's excluded.
    #
    # That group is NOT one line: NBHI breaks it into three named sub-items -
    # "(i) Towards Excess Expenses of Management", "(ii) Towards remuneration
    # of MD/CEO/WTD/Other KMPs", "(iii) Others" - under a parent "(f)" row
    # that itself carries no values, and WHICH sub-item is actually populated
    # varies by quarter (NBHI's FY26-27 Q1 filing has a value only in (ii);
    # its own FY25-26 Q1 comparative column has a value only in (i)) - so
    # matching just one sub-item's wording silently misses whichever one is
    # populated that quarter for that period. sum_rows_after sums every
    # sub-row under the "(f)" parent instead of guessing which one to name.
    # Narayana's filing instead prints this as ONE already-summed line
    # ("Contribution to Policyholders Funds towards excess EoM") with no
    # sub-item breakdown, which the broad "contribution to policyholders"
    # search matches directly.
    STOP_AFTER_CONTRIBUTION_GROUP = re.compile(r"^\(g\)|total", re.IGNORECASE)
    # NL-2 section 3 "OTHER INCOME" (FX gain, interest income, provisions/
    # liabilities written back, misc) - netted off Total Overheads below.
    # Its sub-items differ by insurer, so every line under the heading is
    # summed, up to "TOTAL (A)", which closes the income section.
    STOP_AFTER_OTHER_INCOME = re.compile(r"total", re.IGNORECASE)

    if nl2:
        sh_interest = get_line_item(nl2, "Interest,", "Rent")
        sh_profit_sale = get_line_item_any(nl2, [("Profit on sale", "investments"), ("Profit", "sale/redemption", "investments")])
        sh_loss_sale = get_line_item_any(nl2, [("Loss on sale", "investments"), ("Loss", "sale/redemption", "investments")])
        sh_amort = get_line_item(nl2, "Amortization of Premium")
        pbt = get_line_item(nl2, "Before Tax")
        pat = get_line_item(nl2, "after tax")
        nl2_total_b = get_line_item(nl2, "TOTAL", "(B)")
        nl2_contribution = sum_rows_after(nl2, ("Contribution to Policyholders",), STOP_AFTER_CONTRIBUTION_GROUP)
        nl2_other_income = sum_rows_after(nl2, ("OTHER INCOME",), STOP_AFTER_OTHER_INCOME, include_anchor=True)
        # "(f)(ii) Towards remuneration of MD/CEO/WTD/Other KMPs" - needed for
        # the EOM Ratio formula, which subtracts it out of Opex.
        ceo_remuneration = get_line_item(nl2, "remuneration of MD")
        out["PBT"] = tuple(lakhs_to_cr(v) for v in pbt)
        out["PAT"] = tuple(lakhs_to_cr(v) for v in pat)
    elif nl2_text:
        # Gridline-less NL-2 (e.g. Narayana Health): the shareholders'-account
        # investment-income lines are read from the same "INCOME FROM
        # INVESTMENTS" section as the gridded case, just via the text parser.
        # Narayana's own layout merges profit/loss on sale into one signed
        # line, so sh_loss_sale is fixed at (0, 0) rather than searched for
        # separately - searching for it too would either find nothing (safe)
        # or double-count a "Loss" substring inside the same profit/loss line.
        sh_interest = get_line_item_from_text(nl2_text, "Interest and Dividend", form="NL-2")
        sh_profit_sale = get_line_item_from_text(nl2_text, "Sale of Investments", form="NL-2")
        sh_loss_sale = (0.0, 0.0)
        sh_amort = get_line_item_from_text(nl2_text, "Amortis", "Premium", form="NL-2")
        if sh_amort == (None, None):
            sh_amort = get_line_item_from_text(nl2_text, "Amortiz", "Premium", form="NL-2")
        # "Before Tax" also matches an intermediate "...Before Tax Exceptional
        # Items" subtotal row that some insurers print above the real
        # bottom-line PBT row - exclude it explicitly.
        pbt = get_line_item_from_text(nl2_text, "Before Tax",
                                      exclude=["Exceptional"], form="NL-2")
        pat = get_line_item_from_text(nl2_text, "after tax", form="NL-2")
        nl2_total_b = get_line_item_from_text(nl2_text, "TOTAL", "(B)", form="NL-2")
        # Try the broad label first (matches Narayana's single combined line
        # directly); the narrower "excess EoM"-only phrasings are a fallback
        # for a gridline-less filing that happens to word it differently.
        nl2_contribution = get_line_item_from_text(nl2_text, "Contribution to Policyholders", form="NL-2")
        if nl2_contribution == (None, None):
            for variant in [("towards excess", "management"), ("excess eom",), ("towards excess", "eom")]:
                nl2_contribution = get_line_item_from_text(nl2_text, *variant, form="NL-2")
                if nl2_contribution != (None, None):
                    break
        ceo_remuneration = get_line_item_from_text(nl2_text, "remuneration of MD", form="NL-2")
        nl2_other_income = sum_lines_after_from_text(nl2_text, ("OTHER INCOME",), STOP_AFTER_OTHER_INCOME,
                                                     form="NL-2")
        out["PBT"] = tuple(lakhs_to_cr(v) for v in pbt)
        out["PAT"] = tuple(lakhs_to_cr(v) for v in pat)
    else:
        sh_interest = sh_profit_sale = sh_loss_sale = sh_amort = (None, None)
        nl2_total_b = nl2_contribution = nl2_other_income = (None, None)
        ceo_remuneration = (None, None)
    out["CEO Remuneration"] = tuple(lakhs_to_cr(v) for v in ceo_remuneration)

    def sum_available(*pairs):
        """Sum whichever pairs have a value for each period - None only when
        NOTHING in `pairs` has a value for that period, so one component this
        run couldn't locate (e.g. a shareholders'-account line on a
        gridline-less filing) degrades the total rather than blanking it."""
        result = []
        for i in (0, 1):
            vals = [p[i] for p in pairs if p[i] is not None]
            result.append(sum(vals) if vals else None)
        return tuple(result)

    inv_income = sum_available(ph_interest, ph_profit_sale, sh_interest, sh_profit_sale, sh_loss_sale, sh_amort)
    out["Investment Income"] = tuple(lakhs_to_cr(v) for v in inv_income)

    # Total Overheads = NL-1's Commission + Operating Expenses (these already
    # equal NL-6's Net Commission / NL-7's Operating Expenses TOTAL - NL-1
    # cites them by schedule number and restates their grand totals verbatim)
    # plus NL-2's Provisions + Other Expenses, net of the Contribution to
    # Policyholders' A/c inter-account transfer described above, and net of
    # NL-2's Other Income (matches the reference deck's figure - verified on
    # FY26-27 Q1 for NBHI/STAR/CARE). Commission/opex missing is still fatal
    # (there is no overheads figure at all without them); each NL-2 term
    # degrades to 0 if it can't be found, rather than blanking an
    # otherwise-good NL-1-derived total.
    nl2_addend = tuple(
        (tb - (contrib or 0)) if tb is not None else None
        for tb, contrib in zip(nl2_total_b, nl2_contribution)
    )
    overheads = tuple(
        (a or 0) + (b or 0) + (nl2_addend[i] or 0) - (nl2_other_income[i] or 0)
        if a is not None and b is not None else None
        for i, (a, b) in enumerate(zip(commission, opex))
    )
    out["Total Overheads"] = tuple(lakhs_to_cr(v) for v in overheads)

    out["Investment Yield"] = extract_investment_yield(pdf_path)
    out["Investment Portfolio"] = extract_investment_portfolio(pdf_path)
    out["Average Claim Size"] = extract_average_claim_size(pdf_path)
    out["NL-45 Claims"] = (extract_nl45_claims(pdf_path), None)
    out["NL-41 On-roll"] = (extract_nl41_onroll(pdf_path), None)
    out["NL-36 Total Policies"] = extract_nl36_policies(pdf_path)
    out["CSR Amount"] = (extract_nl37_amount_csr(pdf_path), None)
    out["NL-45 Complaint Ratios"] = extract_nl45_complaint_ratios(pdf_path)  # (policy, claim), not (cur, prior)
    out["IT Capex"] = (lakhs_to_cr(extract_it_capex(pdf_path)), None)
    out["Office Counts"] = extract_office_counts(pdf_path)  # (opening, closing), not (cur, prior)
    out["Cumulative Capital"] = extract_cumulative_capital(pdf_path)

    return out


# A well-formed figure as these forms print it: Indian ("1,19,190") or plain
# grouping, optional decimals, optional sign/brackets.
_WELL_FORMED_NUM_RE = re.compile(r"^\(?-?(?:\d{1,3}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?$")


def _nl31_cell_num(row, idx):
    if idx >= len(row) or row[idx] is None:
        return None
    raw = " ".join(str(row[idx]).replace("%", "").split())
    if raw in ("", "-"):
        return None
    joined = raw.replace(" ", "")
    # pdfplumber splits one figure across a stray space ("4 ,93,346") - and
    # occasionally merges two adjacent cells into one ("7,194 167": Star
    # Health's NL-31 investment 7,194 with its income 167). Joining is right
    # for the first and 1000x wrong for the second, so only join when the
    # result is itself well-formed; otherwise take the first figure.
    if not _WELL_FORMED_NUM_RE.match(joined) and " " in raw:
        first = raw.split(" ")[0]
        if _WELL_FORMED_NUM_RE.match(first):
            joined = first
    s = joined.replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def _nl31_table_and_blocks(pdf_path):
    """Locates NL-31's table and its two YTD column blocks (current/prior).
    Returns (table, cur_start, prior_start), or (None, None, None) if the
    form or its blocks can't be found. Shared by extract_investment_yield
    (reads the TOTAL row only) and extract_investment_portfolio (reads
    every per-category row)."""
    fp, _ = get_form_page(pdf_path, r"FORM\s+NL-31")
    if not fp:
        return None, None, None
    table = fp.tables[0]
    cur_start = prior_start = None
    for row in table[:8]:
        for i, cell in enumerate(row):
            if not cell:
                continue
            text = " ".join(str(cell).split()).lower()
            # Reuses forms._is_cumulative_header's generic phrase check
            # (was a narrower hand-rolled "year to date"/"period ended"
            # check that missed ABHI's "Upto the Year Ended ..." wording -
            # confirmed against a real filing).
            if "year to date" not in text and not _is_cumulative_header(text):
                continue
            # "previous"/"corresponding" phrasing, or the prior year
            # itself printed in the header. The year comes from the
            # configured period rather than a literal, so this still
            # separates the two blocks in a later quarter.
            _cur_frag, _prior_frag = pdf_extract_year_frags()
            is_prior = ("previous" in text or "corresponding" in text
                        or any(f.lstrip("-") in text for f in _prior_frag))
            if is_prior:
                prior_start = i if prior_start is None else min(prior_start, i)
            else:
                cur_start = i if cur_start is None else min(cur_start, i)
    # Only the current block is required. ABHI's third block is "Upto the
    # Year Ended 31st March <cur year>" - the previous FULL year, not the
    # same period last year - so no prior block is found and prior_start
    # stays None (the prior is then backfilled from last year's Data Engine).
    if cur_start is None:
        return None, None, None
    return table, cur_start, prior_start


def extract_investment_yield(pdf_path):
    """NL-31's own TOTAL row already carries a precomputed 'Gross Yield (%)'
    - year to date, NOT annualized (a Q1 filing prints ~1.8%; the trend
    chart annualizes it, see historical.ANNUALIZED_TABLES) - verified to match GT exactly (e.g. NBHI: 5.45% cur,
    5.55% prior), so this is read directly rather than computed from AUM.
    Each YTD block is [Investment, Income, Gross Yield, Net Yield]; take
    the TOTAL row's 3rd sub-column within each block.
    """
    table, cur_start, prior_start = _nl31_table_and_blocks(pdf_path)
    if table is not None:
        total_row = None
        for row in table:
            for cell in row:
                if cell and " ".join(str(cell).split()).strip().upper() in ("TOTAL", "GRAND TOTAL"):
                    total_row = row
                    break
            if total_row:
                break
        if total_row is None:
            # Some insurers (e.g. Star Health) leave the summary row's own
            # label blank - it's still reliably the table's last row.
            total_row = table[-1]

        def yield_at(block_start):
            if block_start is None:
                return None
            # Preferred: the row's own precomputed Gross Yield sub-column.
            pct = _nl31_cell_num(total_row, block_start + 2)
            if pct is not None:
                return pct / 100
            # Fallback (e.g. Star Health leaves this row's yield % blank):
            # derive it from the same row's Investment/Income sub-columns.
            income = _nl31_cell_num(total_row, block_start + 1)
            investment = _nl31_cell_num(total_row, block_start)
            return round_half_up(income / investment, 4) if income is not None and investment else None

        return yield_at(cur_start), yield_at(prior_start)

    text, _ = get_form_text(pdf_path, r"FORM\s+NL-31")
    if not text:
        return None, None
    cur, prior = get_line_item_from_text(text, "TOTAL", cur_col=6, prior_col=10)
    return (round_half_up(cur / 100, 4) if cur is not None else None,
            round_half_up(prior / 100, 4) if prior is not None else None)


# Slide 24: NL-31 lists investments as ~20-55 granular "Category of
# Investment" rows, each carrying IRDAI's own 4-letter Category Code (e.g.
# CGSB = Central Government Bonds) in its own column - verified against a
# real filing (Niva Bupa FY26-27 Q1: 20 rows, every code present below).
# CATEGORY_CODE_TO_BUCKET rolls every code up into the 5 buckets Slide 24
# actually shows. This replaces the earlier approach of asking Gemini to
# sum NL-12/12A's own GRAND TOTAL lines by description - wrong form for
# this slide, and not deterministic.
CATEGORY_CODE_TO_BUCKET = {
    "CGSB": "Govt Bonds", "CGSL": "Govt Bonds", "CTRB": "Govt Bonds",
    "SGGB": "Govt Bonds", "SGGL": "Govt Bonds",
    # Sovereign Green Bonds (Care Health), Special Deposits, Deposit under
    # Sec 7 of the Insurance Act (Star Health) - all central-government paper.
    "CSGB": "Govt Bonds", "CSPD": "Govt Bonds", "CDSS": "Govt Bonds",
    "SGOA": "Corporate Bonds/Debentures", "HTDN": "Corporate Bonds/Debentures",
    "HTDA": "Corporate Bonds/Debentures", "HTLN": "Corporate Bonds/Debentures",
    "HTHD": "Corporate Bonds/Debentures", "ICCP": "Corporate Bonds/Debentures",
    "IPTD": "Corporate Bonds/Debentures", "ICTD": "Corporate Bonds/Debentures",
    "ICFD": "Corporate Bonds/Debentures", "IDDF": "Corporate Bonds/Debentures",
    "EPBT": "Corporate Bonds/Debentures", "ILBI": "Corporate Bonds/Debentures",
    "ECOS": "Corporate Bonds/Debentures", "ECCP": "Corporate Bonds/Debentures",
    "HORD": "Corporate Bonds/Debentures", "IODS": "Corporate Bonds/Debentures",
    "IORD": "Corporate Bonds/Debentures", "OLDB": "Corporate Bonds/Debentures",
    "HODS": "Corporate Bonds/Debentures", "EINP": "Corporate Bonds/Debentures",
    "EUPD": "Corporate Bonds/Debentures", "EPPD": "Corporate Bonds/Debentures",
    "ECBO": "Corporate Bonds/Debentures", "EDPG": "Corporate Bonds/Debentures",
    "ORAD": "Corporate Bonds/Debentures", "HDPG": "Corporate Bonds/Debentures",
    "EAPB": "Corporate Bonds/Debentures", "EAPS": "Corporate Bonds/Debentures",
    "ECDB": "Deposits", "EDCD": "Deposits", "ECMR": "Deposits",
    "ECAM": "Equity/Invits/REIT", "OEPU": "Equity/Invits/REIT",
    "EAEQ": "Equity/Invits/REIT", "ERIT": "Equity/Invits/REIT",
    "OIIT": "Equity/Invits/REIT", "EETF": "Equity/Invits/REIT",
    "OETF": "Equity/Invits/REIT", "EIIT": "Equity/Invits/REIT",
    "ODCI": "Equity/Invits/REIT", "IDIT": "Equity/Invits/REIT",
    "EDRT": "Equity/Invits/REIT", "OAFB": "Equity/Invits/REIT",
    "OESH": "Equity/Invits/REIT", "EACE": "Equity/Invits/REIT",
    "ITCE": "Equity/Invits/REIT", "ITPE": "Equity/Invits/REIT",
    "EGMF": "Mutual Funds", "EMPG": "Mutual Funds", "OMGS": "Mutual Funds",
}

# Matches slide_28's series_names in reporting/report.py exactly.
INVESTMENT_PORTFOLIO_BUCKETS = (
    "Govt Bonds", "Corporate Bonds/Debentures", "Deposits", "Equity/Invits/REIT", "Mutual Funds",
)


def extract_investment_portfolio(pdf_path):
    """Sums NL-31's per-category-code Investment (book value) rows into
    Slide 24's 5 buckets via CATEGORY_CODE_TO_BUCKET. Returns
    {bucket_label: (cur_cr, prior_cr)}; a bucket with no rows this quarter
    is omitted rather than written as 0, same "don't guess" convention as
    everywhere else in this file. A code not in the table is logged and
    excluded, not silently dropped."""
    table, cur_start, prior_start = _nl31_table_and_blocks(pdf_path)
    if table is None:
        return {}

    # The Category Code column's position varies by insurer (Niva Bupa:
    # index 2; ABHI: index 3, since ABHI's table has an extra leading blank
    # column shifting everything right) - located dynamically by its own
    # header text, same technique as cur_start/prior_start above, rather
    # than assumed by a fixed position (verified against a real filing:
    # the fixed-position version silently returned nothing at all for ABHI).
    code_col = None
    for row in table[:8]:
        for i, cell in enumerate(row):
            if cell and "category code" in " ".join(str(cell).split()).lower():
                code_col = i
                break
        if code_col is not None:
            break
    if code_col is None:
        return {}

    cur_by_bucket, prior_by_bucket = {}, {}
    total_row = None
    for row in table:
        cells = [" ".join(str(c).split()).upper() for c in row if c]
        if cells and cells[0] in ("TOTAL", "GRAND TOTAL") and total_row is None:
            total_row = row
        code = row[code_col] if code_col < len(row) and row[code_col] else None
        if not code:
            continue
        code = " ".join(str(code).split()).strip().upper()
        if code == "CATEGORY CODE":  # the header row itself
            continue
        cur_v = _nl31_cell_num(row, cur_start)
        prior_v = _nl31_cell_num(row, prior_start) if prior_start is not None else None
        bucket = CATEGORY_CODE_TO_BUCKET.get(code)
        if bucket is None:
            # Only worth a warning when the row actually carries money.
            if cur_v or prior_v:
                RESOLUTION_LOG.append(f"NL-31 category code {code!r} not in CATEGORY_CODE_TO_BUCKET - "
                                      f"excluded from Slide 28 ({cur_v} / {prior_v} lakh)")
            continue
        if cur_v is not None:
            cur_by_bucket[bucket] = cur_by_bucket.get(bucket, 0) + cur_v
        if prior_v is not None:
            prior_by_bucket[bucket] = prior_by_bucket.get(bucket, 0) + prior_v

    # Reconcile with the form's own TOTAL row: a mismatch means a code
    # missing from CATEGORY_CODE_TO_BUCKET or a misread cell.
    if total_row is not None:
        for label, start, by_bucket in (("current", cur_start, cur_by_bucket), ("prior", prior_start, prior_by_bucket)):
            printed = _nl31_cell_num(total_row, start) if start is not None else None
            summed = sum(by_bucket.values())
            if printed and abs(summed - printed) > 0.005 * printed:
                RESOLUTION_LOG.append(f"NL-31 {label} buckets sum to {summed:,.0f} lakh but the form's TOTAL "
                                      f"is {printed:,.0f} - Slide 28 may be incomplete ({pdf_path})")
    return {b: (lakhs_to_cr(cur_by_bucket.get(b)), lakhs_to_cr(prior_by_bucket.get(b)))
            for b in INVESTMENT_PORTFOLIO_BUCKETS if b in cur_by_bucket or b in prior_by_bucket}


def _nl39_blocks(tables):
    """Every Line-of-Business block on NL-39, as (claims_count, amount_lakhs)
    summed across ALL of that block's line-of-business rows (Health, PA,
    Travel and any other line the insurer writes).

    A block starts at a header row naming the "Total No. of claims paid" /
    "Total amount of claims paid" columns - located by that text on each
    block's own header, since the column positions (like every position on
    this form) vary by insurer. Filers lay the quarter and year-to-date
    blocks out differently: two separate tables (Care, ManipalCigna - which
    also prints a leading title-only table), or two row groups in one table
    (ABHI), or a single block (Niva Bupa's Q1) - so every table is scanned
    and split on those header rows."""
    blocks = []
    for table in tables:
        cur = None
        for row in table:
            cells = [" ".join(str(c).split()).lower() if c else "" for c in row]
            count_col = next((i for i, t in enumerate(cells) if "total no" in t and "claims paid" in t), None)
            amount_col = next((i for i, t in enumerate(cells) if "total amount" in t and "claims paid" in t), None)
            if count_col is not None and amount_col is not None:
                cur = {"count_col": count_col, "amount_col": amount_col, "count": 0.0, "amount": 0.0}
                blocks.append(cur)
                continue
            if cur is None:
                continue
            # Skip any printed total row so it isn't counted on top of the
            # line-of-business rows it sums.
            if any("total" in t for t in cells[:cur["count_col"]]):
                continue
            count = _nl31_cell_num(row, cur["count_col"])
            if count is None:
                continue
            cur["count"] += count
            cur["amount"] += _nl31_cell_num(row, cur["amount_col"]) or 0
    return [(b["count"], b["amount"]) for b in blocks]


def extract_average_claim_size(pdf_path):
    """ACS = Total amount of claims paid / Total no. of claims paid, from
    NL-39 (Ageing of Claims), summed across every line of business - the
    company-level figure, not Health alone (Health-only understated CARE's
    and ABHI's, whose PA/Travel claims are few but large).

    Year-to-date, matching this pipeline's convention everywhere else. In
    Q2-Q4 the form prints a quarter block and a year-to-date block; the
    YTD one is the block with the most claims (it contains the quarter's),
    which picks it however the filer labels or lays the two out. In Q1 both
    blocks are identical.

    Returns (acs_cur, None) in Rs. (an average claim size, not a portfolio
    total, so not converted via lakhs_to_cr) - current period only, since
    the form has no prior-year comparative."""
    fp, _ = get_form_page(pdf_path, r"FORM\s+NL-39", all_matches=True)
    if not fp:
        return None, None
    blocks = [b for b in _nl39_blocks(fp.tables) if b[0]]
    if not blocks:
        return None, None
    count, amount_lakhs = max(blocks, key=lambda b: b[0])
    return round_half_up(amount_lakhs * 1e5 / count, 2), None


_NL14_PAGE_RE = re.compile(r"NL\s*-?\s*14\b")
# IT hardware ("Information Technology", "IT Equipments", "Computers") and IT
# intangibles ("Software", "Website", "Intangibles" - in these filings an
# intangible is software, and Goodwill is always its own row).
_NL14_IT_ROW_RE = re.compile(r"information\s*technology|\bit\s+equip|computer|software|website|intangible",
                             re.IGNORECASE)
_NL14_EXCLUDE_ROW_RE = re.compile(r"goodwill|work\s*in\s*progress|total|previous", re.IGNORECASE)


def extract_it_capex(pdf_path):
    """IT spend = the Additions to IT hardware and IT intangibles in the
    period, from NL-14 (Fixed Assets schedule), Rs. Lakhs.

    NL-14 isn't in pdf_cache.FORM_PATTERNS (the balance sheet only cites it
    as a schedule reference), so its page is found by "NL-14" + "Fixed
    Asset" + "Additions" together. The Additions column is located from
    each table's own header row - some filers leave cells blank, and a
    fixed "second value" would read the wrong column. Summed rows are those
    naming IT hardware or intangibles; a group header such as NBHI's bare
    "Intangibles" over "a) Software's"/"b) Website" carries no values, so
    nothing is counted twice.

    Current period only - NL-14 has no prior-year Additions column (the
    prior is backfilled from last year's Data Engine). Returns None if the
    schedule or its Additions column isn't found."""
    from competitor_analysis.extraction import pdf_cache
    doc = pdf_cache.get_company_json(pdf_path)
    for page in doc["pages"]:
        text = page["text"] or ""
        # No "Additions" check on the page text: ManipalCigna's FY24-25 Q4
        # text layer drops that header word (the table cells keep it), and a
        # page without an Additions column contributes nothing below anyway.
        if not (_NL14_PAGE_RE.search(text) and re.search(r"fixed\s+asset", text, re.IGNORECASE)):
            continue
        total, found = 0.0, False
        for table in page["tables"]:
            add_col = None
            for row in table:
                cells = [" ".join(str(c).split()) if c is not None else "" for c in row]
                col = next((i for i, c in enumerate(cells) if c.lower().startswith("addition")), None)
                if col is not None:
                    add_col = col
                    continue
                label = next((c for c in cells if c), "")
                if add_col is None or not _NL14_IT_ROW_RE.search(label) or _NL14_EXCLUDE_ROW_RE.search(label):
                    continue
                if add_col < len(cells) and cells[add_col]:
                    v = parse_num(cells[add_col])
                    if v is not None:
                        total += v
                        found = True
        if found:
            return total
    return None


_NL41_OPENING_RE = re.compile(r"offices?\s+at\s+the\s+beginning", re.IGNORECASE)
_NL41_CLOSING_RE = re.compile(r"(?:branches|offices)\s+at\s+the\s+end", re.IGNORECASE)


def extract_office_counts(pdf_path):
    """(offices at the beginning, branches at the end) of the period, from
    NL-41's Office Information table (rows 1 and 6) - read by label, since
    filers word the period "year" or "period". Either is None if its row
    isn't found. Searched on every page mentioning NL-41, as some filers
    print the form without a "FORM" prefix."""
    from competitor_analysis.extraction import pdf_cache
    doc = pdf_cache.get_company_json(pdf_path)
    opening = closing = None
    for page in doc["pages"]:
        if "NL-41" not in (page["text"] or ""):
            continue
        for table in page["tables"]:
            for row in table:
                cells = [" ".join(str(c).split()) for c in row if c and str(c).strip()]
                if len(cells) < 2:
                    continue
                label = " ".join(cells[:-1])
                if opening is None and _NL41_OPENING_RE.search(label):
                    opening = parse_num(cells[-1])
                elif closing is None and _NL41_CLOSING_RE.search(label):
                    closing = parse_num(cells[-1])
        if opening is not None and closing is not None:
            break
    return opening, closing


_NL41_ONROLL_RE = re.compile(r"on[\s-]*roll", re.IGNORECASE)
_NL41_ONROLL_TEXT_RE = re.compile(r"on[\s-]*roll[^\d\n]*?(\d[\d ,]*\d|\d)", re.IGNORECASE)


def extract_nl41_onroll(pdf_path):
    """On-roll employees at the end of the period, from NL-41 item 11 "(a)
    On-roll" - read directly rather than via Gemini, which misread CARE's
    FY24-25 Q4 "1 1,518" (11,518) as 13,518. Table cells first (the row's
    last number), else the page text, where pdfplumber's split digits
    ("1 1,518") are rejoined. None if not found."""
    from competitor_analysis.extraction import pdf_cache
    doc = pdf_cache.get_company_json(pdf_path)
    for page in doc["pages"]:
        text = page["text"] or ""
        if "NL-41" not in text:
            continue
        for table in page["tables"]:
            for row in table:
                cells = [" ".join(str(c).split()) for c in row if c and str(c).strip()]
                label_at = next((i for i, c in enumerate(cells) if _NL41_ONROLL_RE.search(c)), None)
                if label_at is None:
                    continue
                # The first number after "On-roll", across the label cell
                # and those right of it (the item number "11" sits left).
                # Some filers merge a/b/c into one cell pair - NBHI's
                # "(a) On-roll (b) Off-roll (c) Total" | "(a) 8,936 (b) 408
                # (c) 9,344" - so neither the last number nor a per-cell
                # parse is safe.
                m = _NL41_ONROLL_TEXT_RE.search(" ".join(cells[label_at:]))
                if m:
                    v = parse_num(m.group(1).replace(" ", ""))
                    if v:
                        return v
        m = _NL41_ONROLL_TEXT_RE.search(text)
        if m:
            v = parse_num(m.group(1).replace(" ", ""))
            if v:
                return v
    return None


_GRIEVANCE_PAGE_RE = re.compile(r"GR(?:IE|EI)VANCE\s+DISPOSAL", re.IGNORECASE)  # Care spells it "GREIVANCE"
_NL45_CLAIMS_ROW_RE = re.compile(r"no\.?\s*of\s*claims\s*during", re.IGNORECASE)


def extract_nl45_complaint_ratios(pdf_path):
    """(policy complaints per 10,000 policies, claim complaints per 10,000
    claims) - NL-45 items 6 and 7, current period, as the filer prints
    them. Picked by item number, like extract_nl45_claims: the wording
    varies ("Claim Complaints", Narayana's "Claim Grievance"), and ABHI's
    table has a merged cell spanning items 2-7. ABHI also prints a second,
    blank block, so the first non-zero value wins. Either is None if its
    row isn't found."""
    from competitor_analysis.extraction import pdf_cache
    doc = pdf_cache.get_company_json(pdf_path)
    found = {}
    for page in doc["pages"]:
        if not _GRIEVANCE_PAGE_RE.search(page["text"] or ""):
            continue
        for table in page["tables"]:
            for row in table:
                cells = [" ".join(str(c).split()) for c in row if c and str(c).strip()]
                if len(cells) < 3 or not re.search(r"per\s*10,?000", " ".join(cells[1:-1]), re.IGNORECASE):
                    continue
                for item in ("6", "7"):
                    if item in cells and item not in found:
                        value = _nl31_cell_num(cells, len(cells) - 1)
                        if value:
                            found[item] = value
        if found:
            break
    return found.get("6"), found.get("7")


# NL-37 prints a "No. of claims" block then an amount block (Rs. lakh),
# each opening with "Claims O/S at the beginning". Headings differ by
# filer: "(Amount in Rs. Lakhs)", "(Rs in Lakhs)" (ManipalCigna), "Amt of
# Claim (In lakhs)" (Narayana, whose amount block is on the next, untagged
# page).
_NL37_BLOCK_START_RE = re.compile(r"o/s\s+at\s+the\s+begin", re.IGNORECASE)
_NL37_COUNT_HEAD_RE = re.compile(r"no\.?\s*of\s*claims|nos?\s+of\s+claim", re.IGNORECASE)
_NL37_AMOUNT_HEAD_RE = re.compile(r"amount\s+in|amt\s+of\s+claim|rs\.?\s*in\s+lakh", re.IGNORECASE)
_NL37_AGEING_RE = re.compile(r"less\s+than\s+3|3\s*months?\s+to\s+6|6\s*months?\s+to\s+1|1\s*(?:year|period)\s+and\s+above",
                             re.IGNORECASE)


def _nl37_block_totals(rows):
    """(os_start, reported, settled, os_end) from one NL-37 block, given as
    [(label, total_value)] in order. A blank/zero total row falls back to
    the sum of its own sub-rows (Care Health leaves "reported", "settled"
    and "O/S at end" blank and fills only (a)/(b)/(c) or the ageing rows)."""
    def find(pattern):
        return next((i for i, (lab, _) in enumerate(rows) if re.search(pattern, lab, re.IGNORECASE)), None)

    i_start, i_rep = find(r"o/s\s+at\s+the\s+begin"), find(r"reported\s+during")
    i_set, i_rep_rej = find(r"settled\s+during"), find(r"repudiated\s+during")
    i_end = find(r"o/s\s+at\s+end")

    def total(i, sub_from, sub_to, sub_filter=None):
        own = rows[i][1] if i is not None else None
        if own:
            return own
        lo = (i + 1) if i is not None else sub_from
        subs = [v for lab, v in rows[lo:sub_to] if v is not None and (sub_filter is None or sub_filter(lab))]
        return sum(subs) if subs else own

    os_start = rows[i_start][1] if i_start is not None else None
    reported = total(i_rep, None, i_set)
    settled = total(i_set, None, i_rep_rej)
    ageing_from = i_end if i_end is not None else (i_rep_rej or 0)
    os_end = total(i_end, ageing_from, len(rows), lambda lab: bool(_NL37_AGEING_RE.search(lab)))
    return os_start, reported, settled, os_end


def _nl37_text_value(line):
    """Last figure on an NL-37 text line (its Total column)."""
    line = re.sub(r"(\d)\s+([.,]\d)", r"\1\2", line)
    nums = re.findall(r"\(?-?\d[\d,]*\.?\d*\)?", line)
    return parse_num(nums[-1]) if nums else None


def extract_nl37_amount_csr(pdf_path):
    """Claim Settlement Ratio by AMOUNT - the Slide 23 formula applied to
    NL-37's amount block: settled / (O/S at beginning + reported - O/S at
    end), from the Total column, year to date.

    Blocks are found in the page text (headings tell count from amount,
    and quarter from year-to-date where a filer prints both - ABHI); values
    come from the matching table block where the tables have it (cell-level
    parsing handles pdfplumber's split digits), else from the text line.
    Returns None if no amount block is found."""
    from competitor_analysis.extraction import pdf_cache
    doc = pdf_cache.get_company_json(pdf_path)
    idxs = [i for i, p in enumerate(doc["pages"]) if "NL-37" in p["forms_detected"]]
    if not idxs:
        return None
    pages = [doc["pages"][i] for i in idxs]
    nxt = idxs[-1] + 1
    if nxt < len(doc["pages"]) and _NL37_BLOCK_START_RE.search(doc["pages"][nxt]["text"] or ""):
        pages.append(doc["pages"][nxt])

    # Text blocks, each tagged count/amount and quarter/ytd by its nearest heading.
    lines = [l for p in pages for l in (p["text"] or "").splitlines()]
    starts = [i for i, l in enumerate(lines) if _NL37_BLOCK_START_RE.search(l)]
    text_blocks = []
    for n, s in enumerate(starts):
        kind, heading = None, ""
        for j in range(s - 1, max(s - 15, -1), -1):
            if _NL37_AMOUNT_HEAD_RE.search(lines[j]):
                kind, heading = "amount", lines[j]
                break
            if _NL37_COUNT_HEAD_RE.search(lines[j]):
                kind, heading = "count", lines[j]
                break
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        rows = [(l, _nl37_text_value(l)) for l in lines[s:end]]
        text_blocks.append({"kind": kind, "quarter": bool(re.search(r"for\s+the\s+quarter", heading, re.I)),
                            "rows": rows})

    # Table blocks, in page order, split at each "O/S at the beginning" row.
    table_blocks = []
    for p in pages:
        for t in p["tables"]:
            for row in t:
                cells = [" ".join(str(c).split()) for c in row if c and str(c).strip()]
                if not cells:
                    continue
                label = next((c for c in cells if re.search(r"[A-Za-z]", c)), "")
                if _NL37_BLOCK_START_RE.search(label):
                    table_blocks.append([])
                if table_blocks and label:
                    last = cells[-1]
                    value = None if re.search(r"[A-Za-z]", last) else _nl31_cell_num(cells, len(cells) - 1)
                    table_blocks[-1].append((label, value))

    amount_idx = [i for i, b in enumerate(text_blocks) if b["kind"] == "amount"]
    if not amount_idx:
        return None
    pick = next((i for i in amount_idx if not text_blocks[i]["quarter"]), amount_idx[-1])
    rows = table_blocks[pick] if len(table_blocks) == len(text_blocks) else text_blocks[pick]["rows"]
    os_start, reported, settled, os_end = _nl37_block_totals(rows)
    if settled is None or os_start is None or reported is None:
        return None
    denom = os_start + reported - (os_end or 0)
    return round_half_up(settled / denom, 4) if denom else None


def extract_nl45_claims(pdf_path):
    """Total no. of claims during the current year - NL-45 (Grievance
    Disposal) item 5, the company's own year-to-date claim count across
    every line of business.

    NL-45 isn't in pdf_cache.FORM_PATTERNS (not every filer prints "FORM
    NL-45" on the page), so the page is found by its "Grievance Disposal"
    heading plus a claims row. Items 2-5 print previous-year policies,
    previous-year claims, current policies, current claims; the label text
    varies ("current year", "current period", "period ended June 30, 2026",
    "current year: 30th June 2026"), so the row is picked by its item
    number, 5, rather than its wording. Some filers (ABHI) print a second,
    all-blank block for intermediary complaints - the first non-zero value
    wins. Returns the count, or None if the page or row isn't found."""
    from competitor_analysis.extraction import pdf_cache
    doc = pdf_cache.get_company_json(pdf_path)
    for page in doc["pages"]:
        if not _GRIEVANCE_PAGE_RE.search(page["text"] or ""):
            continue
        for table in page["tables"]:
            for row in table:
                cells = [" ".join(str(c).split()) for c in row if c and str(c).strip()]
                if "5" not in cells or not any(_NL45_CLAIMS_ROW_RE.search(c) for c in cells):
                    continue
                value = parse_num(cells[-1])
                if value:
                    return value
    return None


def _snapshot_cols(fp):
    """(cur_col, prior_col), or (None, None) if not found: the current/prior
    column indices of an "As at <date>"/"As at <date-1yr>" balance-sheet-
    style table (NL-3, NL-10), located dynamically from that header text
    (via _SNAPSHOT_PHRASE_RE + the configured year fragments) rather than
    assumed to be a row's last two columns - some insurers' tables (e.g.
    ABHI's NL-3/NL-10) have a genuine trailing blank column after the real
    data, which "last two columns" silently misreads as the prior-period
    value instead (confirmed against a real filing: Share Capital's
    prior-year value came back None because of this)."""
    return _snapshot_cols_for_table(fp.tables[0])


def _snapshot_cols_for_table(table):
    """_snapshot_cols for one table - for a form whose figures sit in a
    table other than the page's first (ManipalCigna's NL-10)."""
    cur_col = prior_col = None
    cur_frag, prior_frag = pdf_extract_year_frags()
    for row in table[:5]:
        for i, cell in enumerate(row):
            if not cell:
                continue
            text = " ".join(str(cell).split())
            if not _SNAPSHOT_PHRASE_RE.search(text):
                continue
            if any(f.lstrip("-") in text for f in cur_frag):
                cur_col = i
            elif any(f.lstrip("-") in text for f in prior_frag):
                prior_col = i
    return cur_col, prior_col


def _bs_row(fp, *label_substrings):
    """NL-3/NL-10 are both "As at <date>"/"As at <date-1yr>" balance-sheet-
    style forms, not the "For the quarter/Up to the quarter" cumulative-
    block layout get_line_item expects elsewhere in this pipeline (its
    year_frags-based column detection returns (None, None) against this
    header wording, verified against a real filing). See _snapshot_cols
    for how the current/prior columns are located."""
    row, _, _ = fp.find_row(*label_substrings)
    if not row:
        return None, None
    cur_col, prior_col = _snapshot_cols(fp)
    if cur_col is None or prior_col is None:
        # Fall back to the previous "last two columns" behavior if the "As
        # at" header couldn't be matched at all (safer than returning
        # nothing outright).
        return _nl31_cell_num(row, len(row) - 2), _nl31_cell_num(row, len(row) - 1)
    return _nl31_cell_num(row, cur_col), _nl31_cell_num(row, prior_col)


_SERIAL_RE = re.compile(r"^\d{1,2}\.?$")
_SP_CLOSING_RE = re.compile(r"at\s+the\s+end|closing\s+balance", re.IGNORECASE)
_SP_OPENING_RE = re.compile(r"opening\s+balance|at\s+the\s+begin", re.IGNORECASE)
_SP_LESS_RE = re.compile(r"\bless\b|utili[sz]ed|deduction", re.IGNORECASE)
_SP_ADD_RE = re.compile(r"\badd\b|addition", re.IGNORECASE)


def _row_values(row, cur_col, prior_col):
    """(cur, prior) of one snapshot-table row - from the header-located
    columns when known, else the row's last two filled numeric cells. A
    blank cell is None (not 0), so a valueless group header adds nothing."""
    def val(cell):
        text = " ".join(str(cell).split()) if cell is not None else ""
        return parse_num(text) if text else None
    if cur_col is not None and prior_col is not None:
        return (val(row[cur_col]) if cur_col < len(row) else None,
                val(row[prior_col]) if prior_col < len(row) else None)
    nums = [v for v in (val(c) for c in row) if v is not None]
    return (nums[-2], nums[-1]) if len(nums) >= 2 else (None, None)


def _share_premium(tables):
    """(cur, prior) Share Premium balance at the period end, from NL-10.

    The block runs from the row naming "Share Premium" to the next numbered
    item (e.g. "4 General Reserves"), searched across every table on the
    page (ManipalCigna's sits in the second). Rows are read by label, not
    position: filers print it as a valueless "Share Premium" header over
    opening/additions rows (NBHI, CARE, ManipalCigna), with a closing row
    too (ABHI), or with the opening value on the "Share Premium at the
    beginning" row itself (Star Health), or as that one row alone carrying
    the balance (Star Health's Q4). The printed closing balance is used
    when present; otherwise opening + additions - deductions; otherwise the
    "Share Premium" row's own values."""
    for table in tables:
        start = next((i for i, row in enumerate(table)
                      if any(c and "share premium" in " ".join(str(c).split()).lower() for c in row)), None)
        if start is None:
            continue
        cur_col, prior_col = _snapshot_cols_for_table(table)
        own = _row_values(table[start], cur_col, prior_col)
        parts = {"opening": [None, None], "add": [None, None], "less": [None, None], "closing": [None, None]}
        for ridx in range(start, len(table)):
            row = table[ridx]
            first = next((" ".join(str(c).split()) for c in row if c and str(c).strip()), "")
            if ridx != start and _SERIAL_RE.match(first):
                break
            label = " ".join(" ".join(str(c).split()) for c in row
                             if c and re.search(r"[A-Za-z]", str(c)))
            kind = ("closing" if _SP_CLOSING_RE.search(label) else
                    "less" if _SP_LESS_RE.search(label) else
                    "add" if _SP_ADD_RE.search(label) else
                    "opening" if _SP_OPENING_RE.search(label) else None)
            if kind is None:
                continue
            for i, v in enumerate(_row_values(row, cur_col, prior_col)):
                if v is not None:
                    parts[kind][i] = (parts[kind][i] or 0) + v
        result = []
        for i in (0, 1):
            if parts["closing"][i] is not None:
                result.append(parts["closing"][i])
            elif any(parts[k][i] is not None for k in ("opening", "add", "less")):
                result.append((parts["opening"][i] or 0) + (parts["add"][i] or 0)
                              - abs(parts["less"][i] or 0))
            else:
                result.append(own[i])
        return tuple(result)
    return None, None


def extract_cumulative_capital(pdf_path):
    """Cumulative Capital = Share Capital + Share Application Money Pending
    Allotment (both NL-3) + Share Premium (NL-10's period-end balance - see
    _share_premium). A gridline-less NL-3 (Narayana Health) is read from its
    page text instead: its lines print "<label> <schedule ref> <current>
    <prior>", the IRDAI balance-sheet column order."""
    nl3, _ = get_form_page(pdf_path, r"FORM\s+NL-3-B-BS")
    if nl3:
        share_capital = _bs_row(nl3, "SHARE CAPITAL")
        share_app_money = _bs_row(nl3, "SHARE APPLICATION MONEY")
    else:
        nl3_text, _ = get_form_text(pdf_path, r"FORM\s+NL-3-B-BS")
        if not nl3_text:
            return None, None
        share_capital = get_line_item_from_text(nl3_text, "Share Capital", cur_col=0, prior_col=1, form="NL-3")
        share_app_money = get_line_item_from_text(nl3_text, "Share Application Money", cur_col=0, prior_col=1,
                                                  form="NL-3")

    nl10, _ = get_form_page(pdf_path, r"FORM\s+NL-10", all_matches=True)
    share_premium_cur, share_premium_prior = _share_premium(nl10.tables) if nl10 else (None, None)

    def combine(sc, sam, sp):
        # Share Capital is the mandatory base term (no Cumulative Capital
        # figure makes sense without it); Share Application Money/Share
        # Premium degrade to 0 if either can't be located this quarter.
        return None if sc is None else sc + (sam or 0) + (sp or 0)

    cum_cur = combine(share_capital[0], share_app_money[0], share_premium_cur)
    cum_prior = combine(share_capital[1], share_app_money[1], share_premium_prior)
    return lakhs_to_cr(cum_cur), lakhs_to_cr(cum_prior)


def apply_income_statement_rows(ws, dry_run=False):
    updated, skipped = 0, []
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        if d["Slide #"] != 18:
            continue
        if d[CUR] is not None and d[PRIOR] is not None:
            continue
        company = (d["Company"] or "").strip()
        metric1 = (d["Meric 1"] or "").strip()
        if company not in COMPANY_PDFS:
            skipped.append((r, company, metric1, "no PDF"))
            continue
        cache = apply_income_statement_rows._cache
        if company not in cache:
            log.info("extracting Income Statement for %s ...", company)
            cache[company] = extract_income_statement(company, COMPANY_PDFS[company])
        vals = cache[company].get(metric1)
        if vals is None:
            skipped.append((r, company, metric1, "metric not found"))
            continue
        cur, prev = vals
        if cur is None and prev is None:
            skipped.append((r, company, metric1, "value not found in PDF"))
            continue
        if not dry_run:
            if cur is not None:
                ws.cell(row=r, column=COL[CUR]).value = cur
            if prev is not None:
                ws.cell(row=r, column=COL[PRIOR]).value = prev
            g = growth(cur, prev)
            if g is not None:
                ws.cell(row=r, column=COL["Growth"]).value = g
        updated += 1
    return updated, skipped


apply_income_statement_rows._cache = {}


# ---------------------------------------------------------------------------
# Segment-wise (Health / Personal Accident / Travel) underwriting P&L
# ---------------------------------------------------------------------------

def extract_segment_income_statement(company_short, pdf_path, segment):
    """Returns {metric_label: (cur_cr, prior_cr)} for one company's ONE
    line-of-business segment (segment="Health"/"Personal Accident"/"Travel",
    see forms._SEGMENT_ALIASES), sourced directly from NL-4 (premium), NL-5
    (claims), NL-6 (commission) and NL-7 (operating expenses).

    Unlike extract_income_statement, there is no NL-1/NL-2 income-statement
    summary to read pre-netted company-wide figures from at this
    granularity - NL-1/NL-2 are company-wide only - so every raw line here
    is read from its own segmented schedule via get_segment_line_item(_any),
    and Net Commission/Net Incurred Claims are each schedule's OWN "Net"
    line (not restated from NL-1 the way extract_income_statement reuses
    them).

    "Gross Commission" and "Gross Written Premium" each need a manual add of
    the schedule's own "Commission/Premium on Re-insurance Accepted" line -
    confirmed against a real filing (ManipalCigna) that its schedule's own
    "Direct Commission"/"Premium from direct business written" rows exclude
    that component even though NBHI's analogously-named rows happen to
    already include it (NBHI's RI-accepted commission for Health is ~0, so
    omitting the add there was invisible until cross-checked against a
    company whose RI-accepted figure isn't zero)."""
    out = {}

    nl4, _ = get_form_page(pdf_path, r"FORM\s+NL-4(?!\d)")
    if nl4:
        gdp = get_segment_line_item_any(nl4, segment, [("Gross Direct Premium",),
                                                        ("Premium from direct business written",),
                                                        ("Gross Direct",)])
        ri_prem_accepted = get_segment_line_item(nl4, segment, "reinsurance accepted")
        nwp = get_segment_line_item_any(nl4, segment, [("Net Written Premium",), ("Net Written",)])
        ep = get_segment_line_item_any(nl4, segment, [("Net Earned Premium",),
                                                       ("Total Premium Earned (Net)",),
                                                       ("Premium Earned (Net)",),
                                                       ("Net Earned",)])
    else:
        gdp = ri_prem_accepted = nwp = ep = (None, None)

    nl5, _ = get_form_page(pdf_path, r"FORM\s+NL-5")
    claims = get_segment_line_item_any(nl5, segment, [("Net Incurred Claims",), ("Incurred Claims",)]) \
        if nl5 else (None, None)

    nl6, _ = get_form_page(pdf_path, r"FORM\s+NL-6")
    if nl6:
        gross_comm = _gross_or_components(
            get_segment_line_item_any(nl6, segment, [("Gross Commission",), ("Direct Commission",)]),
            lambda label: get_segment_line_item(nl6, segment, label))
        ri_comm_accepted = get_segment_line_item_any(nl6, segment, RI_ACCEPTED_COMMISSION_LABELS)
        # "Less: Commission" last: Narayana's wrapped label keeps only that
        # first line of "Less: Commission on Re-insurance Ceded".
        ri_comm_ceded = get_segment_line_item_any(nl6, segment, [("Commission on Re-insurance Ceded",),
                                                                  ("Re-insurance Ceded",), ("Reinsurance Ceded",),
                                                                  ("Less: Commission",)])
        net_comm = get_segment_line_item(nl6, segment, "Net Commission")
    else:
        gross_comm = ri_comm_accepted = ri_comm_ceded = net_comm = (None, None)

    # all_matches: some filers (Star Health, Galaxy Health) print NL-7's
    # prior-year block as a second page with its own headers - reading only
    # the first page left every prior-period opex (and the UW/Combined/
    # Expense figures derived from it) blank. Each page's columns are
    # resolved from its own period headers, so the two years don't mix.
    nl7, _ = get_form_page(pdf_path, r"FORM\s+NL-7", all_matches=True)
    opex = get_segment_line_item(nl7, segment, "TOTAL") if nl7 else (None, None)

    def _add(a, b):
        return tuple(None if x is None else x + (y or 0) for x, y in zip(a, b))

    def _neg(a):
        return tuple(None if x is None else -x for x in a)

    gwp = _add(gdp, ri_prem_accepted)
    gross_comm_full = _add(gross_comm, ri_comm_accepted)
    # Brackets on the ceded line mean different things by filer: Narayana
    # brackets an ordinary deduction ("Less: Commission (19.37)", net 102.43
    # = 121.80 - 19.37), while NBHI's FY25 Q1 Travel "(132)" is a genuine
    # reversal (net 327 = 195 + 132). The printed Net Commission decides:
    # use whichever sign of the ceded figure reproduces it.
    def _ceded_as_deduction(g, c, n):
        if c is None or g is None or n is None:
            return c
        return -c if abs(g + c - n) < abs(g - c - n) else c
    ri_comm_ceded = tuple(_ceded_as_deduction(gross_comm_full[k], ri_comm_ceded[k], net_comm[k]) for k in (0, 1))
    ri_comm_signed = _neg(ri_comm_ceded)

    out["Gross Written Premium"] = tuple(lakhs_to_cr(v) for v in gwp)
    out["Net Written Premium"] = tuple(lakhs_to_cr(v) for v in nwp)
    out["Earned Premium"] = tuple(lakhs_to_cr(v) for v in ep)
    out["Claims"] = tuple(lakhs_to_cr(v) for v in claims)
    out["Gross Commission"] = tuple(lakhs_to_cr(v) for v in gross_comm_full)
    out["RI Commission"] = tuple(lakhs_to_cr(v) for v in ri_comm_signed)
    out["Net Commission"] = tuple(lakhs_to_cr(v) for v in net_comm)
    out["Operating Expenses"] = tuple(lakhs_to_cr(v) for v in opex)

    # Derived, pure arithmetic on the already-extracted Crore figures above -
    # no further extraction needed. UW Profit/(Loss) = EP - Claims - Net
    # Commission - Opex; Loss Ratio = Claims/EP; Expense Ratio = (Net
    # Commission + Opex)/NWP; Combined Ratio = Loss + Expense.
    def _derived(ep_v, nwp_v, claims_v, net_comm_v, opex_v):
        uw = (ep_v - claims_v - net_comm_v - opex_v) if None not in (ep_v, claims_v, net_comm_v, opex_v) else None
        loss = (claims_v / ep_v) if (claims_v is not None and ep_v) else None
        expense = ((net_comm_v + opex_v) / nwp_v) if (None not in (net_comm_v, opex_v) and nwp_v) else None
        combined = (loss + expense) if None not in (loss, expense) else None
        return uw, loss, expense, combined

    cur = _derived(out["Earned Premium"][0], out["Net Written Premium"][0], out["Claims"][0],
                    out["Net Commission"][0], out["Operating Expenses"][0])
    prior = _derived(out["Earned Premium"][1], out["Net Written Premium"][1], out["Claims"][1],
                      out["Net Commission"][1], out["Operating Expenses"][1])
    out["UW Profit/(Loss)"] = (cur[0], prior[0])
    out["Loss Ratio"] = (cur[1], prior[1])
    out["Expense Ratio"] = (cur[2], prior[2])
    out["Combined Ratio"] = (cur[3], prior[3])
    return out


def apply_segment_income_statement_rows(ws, slide_no, segment, dry_run=False):
    """apply_income_statement_rows' counterpart for one segment-wise slide -
    fills Slide #==slide_no rows from extract_segment_income_statement(...,
    segment), cached per (company, segment) so each is only extracted once
    even though this is called once per segment slide."""
    updated, skipped = 0, []
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        if d["Slide #"] != slide_no:
            continue
        if d[CUR] is not None and d[PRIOR] is not None:
            continue
        company = (d["Company"] or "").strip()
        metric1 = (d["Meric 1"] or "").strip()
        if company not in COMPANY_PDFS:
            skipped.append((r, company, metric1, "no PDF"))
            continue
        cache = apply_segment_income_statement_rows._cache
        cache_key = (company, segment)
        if cache_key not in cache:
            log.info("extracting %s income statement for %s ...", segment, company)
            cache[cache_key] = extract_segment_income_statement(company, COMPANY_PDFS[company], segment)
        vals = cache[cache_key].get(metric1)
        if vals is None:
            skipped.append((r, company, metric1, "metric not found"))
            continue
        cur, prev = vals
        if cur is None and prev is None:
            skipped.append((r, company, metric1, "value not found in PDF"))
            continue
        if not dry_run:
            if cur is not None:
                ws.cell(row=r, column=COL[CUR]).value = cur
            if prev is not None:
                ws.cell(row=r, column=COL[PRIOR]).value = prev
            g = growth(cur, prev)
            if g is not None:
                ws.cell(row=r, column=COL["Growth"]).value = g
        updated += 1
    return updated, skipped


apply_segment_income_statement_rows._cache = {}


# ---------------------------------------------------------------------------
# Slides 13-35: hybrid PDF-JSON -> Gemini -> Excel pipeline
# ---------------------------------------------------------------------------

COMPANY_FULL_NAME = {
    "NBHI": "Niva Bupa Health Insurance Company Limited",
    "ABHI": "Aditya Birla Health Insurance Co. Limited",
    "Care Health": "Care Health Insurance Ltd",
    "Star Health": "Star Health & Allied Insurance Co Ltd",
    "Manipal Cigna": "ManipalCigna Health Insurance Co Ltd",
    "Narayana Health": "Narayana Health Insurance Ltd",
    "Galaxy Health": "Galaxy Health Insurance Company Ltd",
}


# Slide 13's second row set (Data Engine "Meric 1"): each channel's commission
# as a fraction of that channel's own premium, NL-6 / NL-36.
SLIDE13_RATE_METRIC1 = "Channel-wise Commission % to Channel Premium"

# Slide 23's "No. of claims to No. of policies" inputs, written alongside the
# ratio so a reviewer can check it (the report doesn't chart them).
SLIDE23_CLAIMS_METRIC1 = "Total no. of claims (NL-45)"
SLIDE23_POLICIES_METRIC1 = "Total no. of policies (NL-36)"


def normalize_text(s):
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s


def convert_value(v, kind):
    if v is None or not isinstance(v, (int, float)):
        return None
    if kind == "money":
        return round_half_up(v / 100, 2)
    if kind == "percent":
        return round_half_up(v / 100, 4)
    if kind == "ratio":
        return round_half_up(v, 4)
    if kind == "count":
        return round_half_up(v)
    return v


def build_row_index(ws):
    idx = {}
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        slide = d["Slide #"]
        if slide is None:
            continue
        key = (slide, normalize_text(d["Company"]), normalize_text(d["Meric 1"]))
        idx.setdefault(key, []).append((r, normalize_text(d["Metric 2"])))
    return idx


def apply_metric_to_rows(ws, idx, slide, company, metric1, metric2, cur, prior, dry_run, log):
    if cur is None and prior is None:
        return 0
    key = (slide, normalize_text(company), normalize_text(metric1))
    candidates = idx.get(key, [])
    if metric2 is not None:
        target = normalize_text(metric2)
        candidates = [c for c in candidates if c[1] == target]
    if not candidates:
        log.append((slide, company, metric1, metric2, "no matching row"))
        return 0
    written = 0
    for row, _ in candidates:
        d = row_dict(ws, row)
        if d[CUR] is not None and d[PRIOR] is not None:
            continue
        if not dry_run:
            if cur is not None:
                ws.cell(row=row, column=COL[CUR]).value = cur
            if prior is not None:
                ws.cell(row=row, column=COL[PRIOR]).value = prior
            g = growth(cur, prior)
            if g is not None:
                ws.cell(row=row, column=COL["Growth"]).value = g
        written += 1
    return written


# (slide, metric1, metric2) targets whose PRIOR-period gap isn't a bug in
# THIS run's extraction - the source schedule genuinely has no prior-year
# comparative (NL-37 claims count, NL-41 offices/employees, both point-in-
# time-only), or the gap is simply how Slides 16/17's zone/state mix landed
# this quarter. The only place a real number for that same quarter-last-year
# still exists is last year's OWN filing, captured back when it was the
# "current" period - see backfill_prior_from_last_year().
PRIOR_BACKFILL_TARGETS = (
    [(16, z, None) for z in ("North", "South", "East", "West", "Central")]
    + [(17, s, None) for s in ("Uttar Pradesh", "Maharashtra", "Karnataka", "Haryana",
                                "Tamil Nadu", "Kerala", "Delhi", "Others")]
    + [(23, m, None) for m in ("Average Claim Size", "No. of claims to No. of policies",
                                "Claims Settlement Ratio",
                                # Written current-period only, so their prior
                                # comes from the same place as the ratio's.
                                "Total no. of claims (NL-45)", "Total no. of policies (NL-36)")]
    + [(24, "IT spend to GWP ratio", None)]
    + [(27, m, None) for m in ("Claim Settlement Ratio (Amount)", "Claim Complaints per 10,000 claims",
                                "Policy Complaints per 10,000 policies")]
    # Only fills a gap - e.g. ABHI's NL-31 has no same-period prior block.
    + [(28, b, None) for b in ("Govt Bonds", "Corporate Bonds/Debentures", "Deposits",
                                "Equity/Invits/REIT", "Mutual Funds")]
    + [(25, m, None) for m in ("Manpower cost per employee", "Facility rental per office per month")]
)


def _latest_prior_period_archive():
    """Path to the most recently saved Data_Engine_{prior_fy}_{QUARTER}_*.xlsx
    under artifacts/output/, or None if last year's same quarter was never
    run (or its output no longer exists on disk). That file's own CURRENT-
    period column holds exactly the figures this run wants as its prior-
    period fallback - same quarter, one financial year earlier."""
    pattern = str(paths.OUTPUT_DIR / f"Data_Engine_{cfg.prior_fy()}_{cfg.QUARTER}_*.xlsx")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


_FY_START_ONROLL = {}


def _fy_start_onroll(company):
    """On-roll employees at the start of the reporting financial year - the
    CURRENT "Employees / On-roll Employee" value in the newest saved
    Data_Engine_{prior_fy}_Q4_*.xlsx (last year-end's NL-41 count). Matched
    by Meric 1/Metric 2 text, not slide number, so a workbook from before a
    slide renumbering still resolves. None if no such workbook or row."""
    from competitor_analysis.reporting.theme import canonical_company
    matches = sorted(glob.glob(str(paths.OUTPUT_DIR / f"Data_Engine_{cfg.prior_fy()}_Q4_*.xlsx")))
    if not matches:
        return None
    path = matches[-1]
    # Keyed by modification time too: a workbook edited in place during the
    # review pause must not keep serving its pre-edit value to the server.
    key = (path, os.path.getmtime(path))
    if key not in _FY_START_ONROLL:
        found = {}
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb["Data Engine"]
            rows = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(rows)]
            col = cfg.period_column(cfg.prior_fy(), "Q4")
            if col in header:
                ci, mi, m1, m2 = (header.index(col), header.index("Company"),
                                  header.index("Meric 1"), header.index("Metric 2"))
                for r in rows:
                    if (str(r[m1] or "").strip() == "Employees" and str(r[m2] or "").lower().startswith("on-roll")
                            and isinstance(r[ci], (int, float))):
                        key = canonical_company(str(r[mi] or ""))
                        if key:
                            found.setdefault(key, r[ci])
        finally:
            wb.close()
        _FY_START_ONROLL[key] = found
        log.info("Opening on-roll headcounts from %s: %d companies", os.path.basename(path), len(found))
    return _FY_START_ONROLL[key].get(canonical_company(company))


def backfill_prior_from_last_year(ws, dry_run=False):
    """Fills PRIOR-period gaps listed in PRIOR_BACKFILL_TARGETS from last
    year's own Data Engine output for the same quarter, whose CUR column is
    exactly the number this run wants as PRIOR. Never overwrites a prior
    value this run's own extraction already found - a real number in THIS
    filing always wins over a backfilled one, and a cell is only ever
    touched if it's still empty. Returns (written_count, log)."""
    archive_path = _latest_prior_period_archive()
    if archive_path is None:
        return 0, []
    # NOT read_only=True: read-only worksheets only stream efficiently via
    # iter_rows() - row_dict()'s per-header ws.cell(row=, column=) random
    # access degrades to an XML re-scan per call in that mode (observed:
    # 300s+ for one archive file vs. under a second fully loaded), the same
    # reason load_engine() never uses it either.
    archive_wb = openpyxl.load_workbook(archive_path, data_only=True)
    try:
        archive_ws = archive_wb["Data Engine"]
        idx = build_row_index(ws)
        archive_idx = build_row_index(archive_ws)
        written = 0
        log = []
        for slide, metric1, metric2 in PRIOR_BACKFILL_TARGETS:
            for company in COMPANY_PDFS:
                key = (slide, normalize_text(company), normalize_text(metric1))
                candidates = idx.get(key, [])
                arch_candidates = archive_idx.get(key, [])
                if metric2 is not None:
                    target_m2 = normalize_text(metric2)
                    candidates = [c for c in candidates if c[1] == target_m2]
                    arch_candidates = [c for c in arch_candidates if c[1] == target_m2]
                if not candidates or not arch_candidates:
                    continue
                arch_cur = row_dict(archive_ws, arch_candidates[0][0])[CUR]
                if arch_cur is None:
                    continue
                for row, _ in candidates:
                    d = row_dict(ws, row)
                    if d[PRIOR] is not None:
                        continue
                    if not dry_run:
                        ws.cell(row=row, column=COL[PRIOR]).value = arch_cur
                        g = growth(d[CUR], arch_cur)
                        if g is not None:
                            ws.cell(row=row, column=COL["Growth"]).value = g
                    written += 1
                    log.append((slide, company, metric1, metric2))
        return written, log
    finally:
        archive_wb.close()


def _converted_value(regrouped, kind_by_key, key):
    """(cur, prior) for one metric key, unit-converted exactly as the flat
    dict path always has - the only thing that changed is reading the raw
    figure off a typed schemas.FORM_SCHEMAS field instead of a dict lookup."""
    form, field = schemas.KEY_TO_FIELD[key]
    ev = getattr(regrouped[form], field)
    kind = kind_by_key[key]
    return (convert_value(ev.current, kind), convert_value(ev.prior, kind))


def compute_derived_metrics(company, regrouped, kind_by_key, income):
    """regrouped: {form_tag: schemas.FORM_SCHEMAS[form_tag] instance} from
    schemas.regroup_by_form() - one validated Pydantic object per in-scope
    NL-form. kind_by_key: {gemini_key: kind}, from master_metric_specs() -
    still needed here since a field's unit isn't recoverable from the
    Pydantic model alone.
    income: {"gwp":(cur,prior), "opex":(cur,prior), "pbt":(cur,prior), "pat":(cur,prior)} in Crores, from the deterministic Slide-18 extractor.
    Returns {(slide, metric1, metric2): (cur, prior)}.

    The formulas below are unchanged from the pre-schemas version - only
    get()'s own body changed (dict lookup -> typed attribute read); every
    call site downstream still just calls get(key) and gets back the same
    (cur, prior) tuple in the same units as before.
    """
    D = {}

    def get(key):
        return _converted_value(regrouped, kind_by_key, key)

    def safe_div(a, b):
        if a is None or b is None or b == 0:
            return None
        return round_half_up(a / b, 4)

    gwp_cur, gwp_prior = income.get("gwp", (None, None))
    opex_cur, opex_prior = income.get("opex", (None, None))
    # "Opex" for Slides 21/22 means Operating Expenses ALONE (NL-7), not
    # Total Overheads (Commission + Opex combined, used elsewhere e.g.
    # Slide 30) - verified against GT: opex_alone/GWP reproduces GT's ratio,
    # whereas the combined figure over-states it by however much of that
    # company's book runs through commission-paying channels.
    opex_alone_cur, opex_alone_prior = income.get("opex_alone", (None, None))
    pbt_cur, pbt_prior = income.get("pbt", (None, None))
    pat_cur, pat_prior = income.get("pat", (None, None))

    # Slides 19/28/29: Expense/Loss/Combined/EOM Ratios, recomputed from
    # their own components rather than read off NL-20's own printed ratio
    # cells (QC review feedback: NL-20's own figures shouldn't be trusted
    # directly - recalculate using the formula instead).
    nwp_cur, nwp_prior = income.get("nwp", (None, None))
    ep_cur, ep_prior = income.get("ep", (None, None))
    net_commission_cur, net_commission_prior = income.get("net_commission", (None, None))
    net_incurred_claims_cur, net_incurred_claims_prior = income.get("net_incurred_claims", (None, None))
    gross_commission_cur, gross_commission_prior = income.get("gross_commission", (None, None))
    ri_accepted_commission_cur, ri_accepted_commission_prior = income.get("ri_accepted_commission", (None, None))
    gst_cur, gst_prior = income.get("gst", (None, None))
    ceo_remuneration_cur, ceo_remuneration_prior = income.get("ceo_remuneration", (None, None))

    def _sum_opt(*vals):
        # Degrade to whichever addends are present, same "don't blank a
        # whole total over one missing component" convention used
        # throughout extract_income_statement's own sum_available().
        present = [v for v in vals if v is not None]
        return sum(present) if present else None

    # Expense Ratio = (Net Commission + Operating Expenses) / Net Written Premium
    expense_ratio_cur = safe_div(_sum_opt(net_commission_cur, opex_alone_cur), nwp_cur)
    expense_ratio_prior = safe_div(_sum_opt(net_commission_prior, opex_alone_prior), nwp_prior)
    # Loss Ratio = Net Incurred Claims / Net Earned Premium
    loss_ratio_cur = safe_div(net_incurred_claims_cur, ep_cur)
    loss_ratio_prior = safe_div(net_incurred_claims_prior, ep_prior)
    # Combined Ratio = Expense Ratio + Loss Ratio
    combined_ratio_cur = (expense_ratio_cur + loss_ratio_cur
                           if expense_ratio_cur is not None and loss_ratio_cur is not None else None)
    combined_ratio_prior = (expense_ratio_prior + loss_ratio_prior
                             if expense_ratio_prior is not None and loss_ratio_prior is not None else None)
    # EOM Ratio = (Gross Commission + Commission on RI Accepted + Opex - GST - CEO Remuneration) / GWP
    eom_num_cur = _sum_opt(gross_commission_cur, ri_accepted_commission_cur, opex_alone_cur)
    eom_num_cur = None if eom_num_cur is None else eom_num_cur - (gst_cur or 0) - (ceo_remuneration_cur or 0)
    eom_num_prior = _sum_opt(gross_commission_prior, ri_accepted_commission_prior, opex_alone_prior)
    eom_num_prior = None if eom_num_prior is None else eom_num_prior - (gst_prior or 0) - (ceo_remuneration_prior or 0)
    eom_ratio_cur = safe_div(eom_num_cur, gwp_cur)
    eom_ratio_prior = safe_div(eom_num_prior, gwp_prior)

    D[(22, "Expense Ratio", None)] = (expense_ratio_cur, expense_ratio_prior)
    D[(22, "Loss Ratio", None)] = (loss_ratio_cur, loss_ratio_prior)
    D[(22, "Combined Ratio", None)] = (combined_ratio_cur, combined_ratio_prior)
    D[(32, "Loss Ratio", None)] = (loss_ratio_cur, loss_ratio_prior)
    D[(32, "Combined Ratio", None)] = (combined_ratio_cur, combined_ratio_prior)
    D[(33, "Expense Ratio", None)] = (expense_ratio_cur, expense_ratio_prior)
    D[(33, "Expense of Management Ratio", None)] = (eom_ratio_cur, eom_ratio_prior)

    # Slide 24: expense ratios to GWP
    manpower_cur, manpower_prior = get("manpower_cost")
    # Manpower cost net of half the in-house claim processing cost (NL-7,
    # where a filer reports one) - that share of staff cost is claims
    # handling, not operating overhead. Feeds every manpower row (Slides
    # 24/25); no line, no change.
    in_house_cur, in_house_prior = income.get("in_house_claims_cost", (None, None))
    if manpower_cur is not None and in_house_cur:
        manpower_cur = round_half_up(manpower_cur - 0.5 * in_house_cur, 2)
    if manpower_prior is not None and in_house_prior:
        manpower_prior = round_half_up(manpower_prior - 0.5 * in_house_prior, 2)
    # IT spend = NL-14 Additions to IT hardware + IT intangibles (see
    # extract_it_capex), not NL-7's IT expense line. Current period only -
    # the prior is backfilled from last year's Data Engine.
    it_cur, _ = income.get("it_capex", (None, None))
    D[(24, "Opex. To GWP ratio", None)] = (safe_div(opex_alone_cur, gwp_cur), safe_div(opex_alone_prior, gwp_prior))
    D[(24, "Manpower to GWP ratio", None)] = (safe_div(manpower_cur, gwp_cur), safe_div(manpower_prior, gwp_prior))
    D[(24, "IT spend to GWP ratio", None)] = (safe_div(it_cur, gwp_cur), None)

    # Slide 25: manpower/facility metrics (Rs. Lakhs, not Rs. - verified
    # against GT)
    employees_cur, _ = get("employees_onroll")
    # Average on-roll headcount over the year to date: the on-roll count at
    # the start of the financial year (last year's Q4 Data Engine) and at
    # the end of this period (NL-41) - the reference deck's "Manpower cost /
    # Average head count (excluding outsourced manpower count)". NL-41's own
    # movement table can't supply the opening count: it is quarterly and
    # some filers (Care) include off-roll staff in it. Falls back to the
    # closing count alone when last year's Q4 workbook isn't on disk.
    opening_employees = _fy_start_onroll(company)
    avg_employees_cur = ((opening_employees + employees_cur) / 2 if opening_employees and employees_cur
                         else employees_cur)
    # Offices = the average of NL-41's opening and closing counts for the
    # period (rent accrues across the whole period, over which the office
    # count changes). Falls back to whichever count exists, then to the
    # model's closing count.
    opening_offices, closing_offices = income.get("office_counts", (None, None))
    counts = [c for c in (opening_offices, closing_offices) if c]
    offices_cur = sum(counts) / len(counts) if counts else get("offices_count")[0]
    D[(25, "Manpower cost to total Opex", None)] = (safe_div(manpower_cur, opex_alone_cur), safe_div(manpower_prior, opex_alone_prior))
    if manpower_cur is not None and avg_employees_cur:
        D[(25, "Manpower cost per employee", None)] = (round_half_up(manpower_cur * 100 / avg_employees_cur, 4), None)
    rent_cur, rent_prior = get("rent_expense")
    if rent_cur is not None and offices_cur:
        # Rent is a cumulative YTD figure, so the monthly run-rate divides by
        # however many months of the financial year this quarter covers (9 for
        # Q3, but 3/6/12 for Q1/Q2/Q4) - never a hardcoded 9.
        months = cfg.months_elapsed()
        D[(25, "Facility rental per office per month", None)] = (
            round_half_up(rent_cur * 100 / months / offices_cur, 4), None)

    # Slide 23: Net Worth = Share Capital + Reserves&Surplus - Debit balance
    # in P&L Account (per the authoritative formula sheet - no Fair Value
    # Change addend; an earlier version added "bs_fair_value_change_sh" here,
    # which the formula sheet doesn't include).
    cap_cur, cap_prior = get("capital")
    res_cur, res_prior = get("bs_reserves_surplus")
    dr_cur, dr_prior = get("bs_debit_balance_pl")

    def net_worth(cap, res, dr):
        if cap is None or res is None:
            return None
        return round_half_up(cap + res - (dr or 0), 2)

    nw_cur = net_worth(cap_cur, res_cur, dr_cur)
    nw_prior = net_worth(cap_prior, res_prior, dr_prior)
    # GT wants this row in Rs. Lakhs, not Crores (verified: nw_cur*100
    # matches GT for 5 of 7 companies within 1%; Capital/Reserves are
    # otherwise correctly extracted, this is purely a unit mismatch).
    D[(26, "Net Worth", None)] = (
        round_half_up(nw_cur * 100, 2) if nw_cur is not None else None,
        round_half_up(nw_prior * 100, 2) if nw_prior is not None else None,
    )

    # Slide 26's "Capital" row = Cumulative Capital = Share Capital + Share
    # Application Money Pending Allotment + Share Premium, read directly off
    # NL-3/NL-10 (see extract_cumulative_capital) - not computed here. The
    # Data Engine template has no separate "Cumulative Capital" row at all
    # (confirmed against data/templates/Data_Engine_Template.xlsx: Slide 26
    # only has PBT/Capital/Net Worth) - "Capital" IS this figure, not plain
    # Share Capital alone (which is still used, unmodified, in Net Worth's
    # own formula below via get("capital")).
    D[(26, "Capital", None)] = income.get("cumulative_capital", (None, None))

    # Slide 30: Historical Trends duplicate GWP/PBT from Slide 18
    D[(31, "GWP", None)] = (gwp_cur, gwp_prior)
    # Slides 26/30's PBT rows repeat Slide 18's cumulative PBT, for every
    # company without exception. An earlier version substituted the
    # single-quarter figure here for the one insurer whose NL-2 has no ruled
    # gridlines; that was a workaround for the text parser mis-resolving that
    # filing's column order, which get_line_item_from_text now reads from the
    # form's own header instead.
    pbt2327_cur, pbt2327_prior = pbt_cur, pbt_prior
    D[(31, "PBT", None)] = (pbt2327_cur, pbt2327_prior)
    # Slide 26 also has its own PBT row (alongside Capital/Net Worth) - same figure.
    D[(26, "PBT", None)] = (pbt2327_cur, pbt2327_prior)

    # Slide 35: Investment Yield, read directly off NL-31's own TOTAL row
    # (see extract_investment_yield) - not computed here.
    D[(36, "Investment Yield", None)] = income.get("investment_yield", (None, None))

    # Slide 28: Investment Portfolio, read directly off NL-31's per-category
    # rows summed by bucket (see extract_investment_portfolio) - not computed here.
    for bucket, (cur_v, prior_v) in income.get("investment_portfolio", {}).items():
        D[(28, bucket, None)] = (cur_v, prior_v)

    # Slide 33: reinsurance ratios (current period only - NL-33 has no prior-year column)
    ri_ceded_cur, _ = get("ri_ceded_total")
    ri_comm_cur, _ = get("ri_commission")
    D[(34, "RI Ceding to GWP Ratio", "Risk Ceded")] = (safe_div(ri_ceded_cur, gwp_cur), None)
    D[(34, "RI Commission to RI Ceding", "Risk Ceded")] = (safe_div(ri_comm_cur, ri_ceded_cur), None)

    # Slide 34: ROE = PAT / Average Net Worth (current period only)
    if pat_cur is not None and nw_cur is not None and nw_prior is not None and (nw_cur + nw_prior) != 0:
        D[(35, "ROE (SAHI)", "PAT/Avg. Net Worth")] = (round_half_up(pat_cur / ((nw_cur + nw_prior) / 2), 4), None)

    # Slide 13: despite the "% to GDPI" label, GT wants the absolute
    # commission amount in Rs. Lakhs, not a computed ratio - verified
    # exactly against GT (e.g. NBHI Individual Agents: comm_cur*100 ==
    # 30114 == GT's figure, which is also consistent with commission/
    # premium reproducing the ratio this row's name implies).
    for form_label, metric2 in gemini_extract.CHANNELS_36:
        if metric2 not in gemini_extract.SLIDE13_METRIC2:
            continue
        comm_cur, comm_prior = get(f"commission_ch_{metric2}")
        D[(13, "Channel-wise Gross Commision % to GDPI", gemini_extract.SLIDE13_METRIC2[metric2])] = (
            round_half_up(comm_cur * 100, 2) if comm_cur is not None else None,
            round_half_up(comm_prior * 100, 2) if comm_prior is not None else None,
        )
        # The channel's commission rate: its NL-6 commission over its own
        # NL-36 premium (both already Rs. Crore here). The row above stays the
        # absolute amount - it gives the bar's size and total on the chart -
        # while this is the % each segment is labelled with.
        prem_cur, prem_prior = get(f"channel_premium_{metric2}")
        D[(13, SLIDE13_RATE_METRIC1, gemini_extract.SLIDE13_METRIC2[metric2])] = (
            safe_div(comm_cur, prem_cur), safe_div(comm_prior, prem_prior))

    # Extraction (gemini_extract.STATES) asks NL-34 for every state/UT by
    # name, not just the 7 Slide 17 shows - the rest all feed Slide 16's
    # zone totals below. raw_others_* is NL-34's own "Others" catch-all
    # field (whatever the form itself couldn't attribute to a named state),
    # kept separate from any individual state's value.
    state_abs_cur, state_abs_prior = {}, {}
    for state in gemini_extract.STATES:
        if state == "Others":
            continue
        c, p = get(f"state_{state}")
        state_abs_cur[state], state_abs_prior[state] = c, p
    raw_others_cur, raw_others_prior = get("state_Others")

    # Slide 17: state-wise GDPI as a fraction of company GWP (not the
    # absolute Rs. Crore figure) - verified exactly against GT for its 7
    # named states. "Others" here sums every OTHER extracted state plus
    # NL-34's own "Others" field - real data now that every state is
    # extracted, not a GWP residual (which only ever worked as a stand-in
    # for that sum).
    for state in gemini_extract.STATES8_NAMED:
        D[(17, state, None)] = (safe_div(state_abs_cur.get(state), gwp_cur),
                                safe_div(state_abs_prior.get(state), gwp_prior))

    other_states = [s for s in state_abs_cur if s not in gemini_extract.STATES8_NAMED]

    def sum_or_none(abs_dict, states, raw_extra):
        vals = [abs_dict[s] for s in states if abs_dict.get(s) is not None]
        if not vals and raw_extra is None:
            return None
        return sum(vals) + (raw_extra or 0)

    others_cur = sum_or_none(state_abs_cur, other_states, raw_others_cur)
    others_prior = sum_or_none(state_abs_prior, other_states, raw_others_prior)
    D[(17, "Others", None)] = (safe_div(others_cur, gwp_cur), safe_div(others_prior, gwp_prior))

    # Slide 16: zone-wise GDPI as a fraction of company GWP, summing EVERY
    # extracted state's absolute value by zone via STATE_TO_ZONE - same
    # fraction-of-GWP convention as Slide 17.
    def zone_total(zone, abs_dict):
        vals = [abs_dict[s] for s, z in STATE_TO_ZONE.items() if z == zone and abs_dict.get(s) is not None]
        return sum(vals) if vals else None

    for zone in ("North", "South", "East", "West", "Central"):
        zc, zp = zone_total(zone, state_abs_cur), zone_total(zone, state_abs_prior)
        D[(16, zone, None)] = (safe_div(zc, gwp_cur), safe_div(zp, gwp_prior))

    # "Others (unclassified)" = any extracted state with no zone in
    # STATE_TO_ZONE (none today - every named state above has one) + NL-34's
    # own "Others" field, which by definition can't belong to a zone. Every
    # named state is already accounted for in a zone above, so this does
    # NOT reuse Slide 17's "Others" (that would double-count the same
    # states both there and here).
    unmapped = [s for s in state_abs_cur if s not in STATE_TO_ZONE]
    unclass_cur = sum_or_none(state_abs_cur, unmapped, raw_others_cur)
    unclass_prior = sum_or_none(state_abs_prior, unmapped, raw_others_prior)
    D[(16, "Others (unclassified)", None)] = (safe_div(unclass_cur, gwp_cur), safe_div(unclass_prior, gwp_prior))

    # Slide 12: GDPI by channel (coarser 6-bucket split, company-specific metric1)
    metric1_12 = gemini_extract.SLIDE12_COMPANY_METRIC1.get(company)
    if metric1_12:
        for metric2, sheet_metric2 in gemini_extract.SLIDE12_DIRECT_METRIC2.items():
            D[(12, "__SLIDE12__" + metric1_12, sheet_metric2)] = get(f"channel_premium_{metric2}")
        others_cur = others_prior = 0
        any_val = False
        for k in gemini_extract.SLIDE12_OTHERS_KEYS:
            c, p = get(f"channel_premium_{k}")
            if c is not None:
                others_cur += c
                any_val = True
            if p is not None:
                others_prior += p
        if any_val:
            D[(12, "__SLIDE12__" + metric1_12, "Others")] = (round_half_up(others_cur, 2), round_half_up(others_prior, 2))

    # Slide 15: Individual ATS (Rs per policy) and Average Productivity (Rs per agent)
    prem_ia_cur, prem_ia_prior = get("channel_premium_Individual Agents")
    pol_ia_cur, pol_ia_prior = get("channel_policies_Individual Agents")
    D[(15, "Individual ATS", "Individual agents GWP/Individual agents no. of policies")] = (
        round_half_up(prem_ia_cur * 1e7 / pol_ia_cur, 2) if prem_ia_cur and pol_ia_cur else None,
        round_half_up(prem_ia_prior * 1e7 / pol_ia_prior, 2) if prem_ia_prior and pol_ia_prior else None,
    )
    # Rs. Lakhs per agent, using the Individual Agents channel's OWN premium
    # (not total company GWP) - verified exactly against GT.
    agents_cur, _ = get("agents_individual")
    if prem_ia_cur is not None and agents_cur:
        D[(15, "Average Productivity (per agent)", "Premium/No. of Individual Agents")] = (
            round_half_up(prem_ia_cur * 100 / agents_cur, 4), None)

    # Slide 23: Claims Settlement Ratio = Claims Settled during the period /
    # (Claims O/S at beginning + Claims reported during the period - Claims
    # O/S at End) - all 4 count fields are on NL-37's own "Total" column
    # (overall company, every line of business summed). Current period only
    # - NL-37 has no prior-year comparative column at all.
    settled_cur, _ = get("claims_settled")
    reported_cur, _ = get("claims_reported")
    os_start_cur, _ = get("claims_os_start")
    os_end_cur, _ = get("claims_os_end")
    claims_denom_cur = None
    if os_start_cur is not None and reported_cur is not None:
        claims_denom_cur = os_start_cur + reported_cur - (os_end_cur or 0)
    D[(23, "Claims Settlement Ratio", None)] = (safe_div(settled_cur, claims_denom_cur), None)

    # Total policy count = NL-36's own Grand Total (A+B) "No. of Policies",
    # read directly - every channel, Direct Business/MISP included. Summing
    # the named Gemini channels (the fallback below) misses those: CARE's
    # Q1 FY27 sum came to ~8.3 lakh against a printed total of 9.46 lakh.
    total_policies_cur = 0
    any_policy_count = False
    for _, metric2 in gemini_extract.CHANNELS_36:
        c, _ = get(f"channel_policies_{metric2}")
        if c is not None:
            total_policies_cur += c
            any_policy_count = True
    total_policies_cur = total_policies_cur if any_policy_count else None
    nl36_total_cur, _ = income.get("nl36_total_policies", (None, None))
    if nl36_total_cur:
        total_policies_cur = nl36_total_cur

    # Average Claim Size = Total amount of claims paid / Total no. of claims
    # paid, read directly off NL-39 (see extract_average_claim_size) -
    # current-quarter only, since NL-39 itself has no YTD/prior-year column.
    D[(23, "Average Claim Size", None)] = income.get("average_claim_size", (None, None))
    # No. of claims to No. of policies = NL-45's total claims (current year,
    # item 5) / NL-36's total policies (up to the quarter, every channel).
    # Current period only - the prior value is backfilled from last year's
    # own Data Engine (PRIOR_BACKFILL_TARGETS).
    # Slide 27: claims & grievances, current period only (read directly off
    # NL-37's amount block and NL-45 items 6/7); the prior is backfilled
    # from last year's Data Engine.
    D[(27, "Claim Settlement Ratio (Amount)", None)] = income.get("csr_amount", (None, None))
    policy_complaints, claim_complaints = income.get("complaint_ratios", (None, None))
    D[(27, "Claim Complaints per 10,000 claims", None)] = (claim_complaints, None)
    D[(27, "Policy Complaints per 10,000 policies", None)] = (policy_complaints, None)

    nl45_claims_cur, _ = income.get("nl45_claims", (None, None))
    D[(23, SLIDE23_CLAIMS_METRIC1, None)] = (nl45_claims_cur, None)
    D[(23, SLIDE23_POLICIES_METRIC1, None)] = (total_policies_cur, None)
    if nl45_claims_cur is not None and total_policies_cur:
        D[(23, "No. of claims to No. of policies", None)] = (
            round_half_up(nl45_claims_cur / total_policies_cur, 6), None)

    return D


def prefetch_income_statements(companies, max_workers=PDF_PARSE_MAX_WORKERS,
                               on_company_done=None, should_cancel=None):
    """Stage 2's per-company PDF extraction, run concurrently.

    Independent per company and I/O/CPU-bound in pdfplumber, so a thread pool
    is enough; results land in the same cache the sequential path used, so the
    sheet-writing stage is unchanged.

    `should_cancel()`, if given, is checked after each company finishes -
    a worker thread already running a parse can't be interrupted (Python
    can't safely kill a thread), so cancelling here only stops the REMAINING
    not-yet-started ones (via Future.cancel(), which drops anything still
    queued) rather than waiting for every company to finish first."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from competitor_analysis.cancellation import PipelineCancelled
    todo = [c for c in companies if c not in apply_income_statement_rows._cache]
    if not todo:
        return {}

    def one(company):
        try:
            return company, extract_income_statement(company, COMPANY_PDFS[company]), None
        except memory.MemoryBudgetExceeded as e:
            # This filing is too heavy to parse within the container's
            # budget. Give up on it rather than on the process: an empty
            # result caches as "nothing extracted", so the sheet-writing
            # stage skips its rows with a reason instead of re-parsing and
            # hitting the same wall.
            gc.collect()
            return company, {}, str(e)

    errors = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(todo))) as pool:
        # submit_with_phase, not pool.submit: a plain ThreadPoolExecutor does
        # not copy the calling contextvars Context into its worker threads,
        # so a bare pool.submit(one, c) would log with whatever phase is
        # active at process start rather than the caller's "Phase 2 / ..."
        futures = [logging_setup.submit_with_phase(pool, one, c) for c in todo]
        # as_completed, not pool.map: map yields in submission order, so a
        # company that finished early would not be reported until every
        # company queued ahead of it had also finished - which is what made
        # the progress UI look like it updated all at once at the end.
        cancelled = False
        for fut in as_completed(futures):
            company, result, error = fut.result()
            apply_income_statement_rows._cache[company] = result
            if error:
                errors[company] = error
                log.warning("%s: skipped - %s", company, error)
            if on_company_done is not None:
                on_company_done(company)
            if not cancelled and should_cancel is not None and should_cancel():
                cancelled = True
                for f in futures:
                    f.cancel()  # no-op for ones already running/done
        if cancelled:
            raise PipelineCancelled()
    return errors


def prefetch_gemini_metrics(companies, on_company_done=None, should_cancel=None):
    """Stage 3's model calls for ALL companies, issued concurrently under one
    shared concurrency gate, instead of company-by-company then batch-by-batch.

    Only the network calls are parallel - every worksheet write still happens
    sequentially afterwards, since openpyxl is not thread-safe.

    `should_cancel()`, if given, is polled every 0.4s while the model calls
    are in flight (this stage alone can run tens of minutes - see
    pipeline.py's Stage 3 comment) - and cancels them at the next await point
    the moment it returns True, rather than waiting for the whole batch."""
    specs = gemini_extract.master_metric_specs()
    jobs = [(c, COMPANY_FULL_NAME[c], gemini_extract.COMPANY_PDFS[c]) for c in companies]
    n_batches = -(-len(specs) // gemini_extract.DEFAULT_BATCH_SIZE)
    log.info("prefetching %d metrics x %d companies (%d calls, up to %d concurrent)...",
              len(specs), len(jobs), n_batches * len(jobs), gemini_extract.MAX_CONCURRENT_GEMINI)
    coro = gemini_extract.extract_many_companies_async(
        jobs, specs, all_forms=gemini_extract.ALL_FORMS, on_company_done=on_company_done)
    if should_cancel is not None:
        from competitor_analysis.cancellation import run_cancellable
        results = run_cancellable(coro, should_cancel)
    else:
        results = asyncio.run(coro)
    apply_company_gemini_pipeline._raw_cache.update(
        {c: r for c, r in results.items() if r})
    return results


def apply_company_gemini_pipeline(ws, company, dry_run=False):
    pdf_path = gemini_extract.COMPANY_PDFS[company]
    full_name = COMPANY_FULL_NAME[company]
    specs = gemini_extract.master_metric_specs()
    # Prefer a result prefetched concurrently by prefetch_gemini_metrics; only
    # fall back to a per-company call if this company wasn't prefetched (e.g.
    # when this function is driven directly from the CLI).
    raw = apply_company_gemini_pipeline._raw_cache.pop(company, None)
    if raw is None:
        log.info("fetching Gemini metrics for %s (%d metrics)...", company, len(specs))
        raw = gemini_extract.extract_company_metrics(
            full_name, pdf_path, specs, all_forms=gemini_extract.ALL_FORMS)

    kind_by_key = {m["key"]: m["kind"] for m in specs}
    rows_by_key = {m["key"]: m["rows"] for m in specs}

    # NL-29 maturity-bucket bug: some insurers' Detail Regarding Debt
    # Securities schedule omits the "More than 7 years and upto 10 years" row
    # entirely (rather than printing "-") when they have zero allocation
    # there, instead of a fixed 5-bucket template - and matches GT's own
    # sheet, which puts the "Above 10 years" row's figure into the "7-10yr"
    # slot in that case (GT's own row-builder made the same assumption).
    # Gated on the missing-row condition itself (found=false for the 7-10yr
    # bucket but found=true for Above-10), not a hardcoded company name -
    # currently only fires for Narayana Health given the source PDFs.
    # Applied to a COPY of raw, not raw itself: write_extraction_audit below
    # must still see the model's original, unshimmed answer.
    raw_for_derivation = dict(raw)
    k_7_10 = "debt_maturity_More than 7 years and upto 10 years"
    k_above10 = "debt_maturity_Above 10 years"
    if not raw.get(k_7_10, {}).get("found") and raw.get(k_above10, {}).get("found"):
        raw_for_derivation[k_7_10] = raw[k_above10]
        raw_for_derivation[k_above10] = {"fy26_q3": None, "fy25_q3": None, "found": False,
                                         "source_form": None, "page_number": None,
                                         "evidence": None, "notes": None}
    if company not in apply_income_statement_rows._cache:
        apply_income_statement_rows._cache[company] = extract_income_statement(company, pdf_path)
    inc = apply_income_statement_rows._cache[company]

    # On-roll headcount read straight off NL-41 wins over the model's answer
    # (which read CARE's FY24-25 Q4 "1 1,518" as 13,518). It feeds both the
    # Employees row - next year's opening headcount - and this period's
    # manpower cost per employee.
    onroll, _ = inc.get("NL-41 On-roll", (None, None))
    if onroll:
        raw_for_derivation["employees_onroll"] = {
            **raw.get("employees_onroll", {}), "fy26_q3": onroll, "found": True,
            "notes": "NL-41 on-roll, read directly from the filing"}
    regrouped = schemas.regroup_by_form(raw_for_derivation, specs)
    income = {
        "gwp": inc.get("Gross Written Premium", (None, None)),
        "opex": inc.get("Total Overheads", (None, None)),
        "opex_alone": inc.get("Operating Expenses", (None, None)),
        "pbt": inc.get("PBT", (None, None)),
        "pat": inc.get("PAT", (None, None)),
        "investment_yield": inc.get("Investment Yield", (None, None)),
        "investment_portfolio": inc.get("Investment Portfolio", {}),
        "claims": inc.get("Claims", (None, None)),
        "nwp": inc.get("Net Written Premium", (None, None)),
        "ep": inc.get("Earned Premium", (None, None)),
        "net_commission": inc.get("Net Commission", (None, None)),
        "net_incurred_claims": inc.get("Net Incurred Claims", (None, None)),
        "gross_commission": inc.get("Gross Commission", (None, None)),
        "ri_accepted_commission": inc.get("RI Accepted Commission", (None, None)),
        "gst": inc.get("GST", (None, None)),
        "ceo_remuneration": inc.get("CEO Remuneration", (None, None)),
        "average_claim_size": inc.get("Average Claim Size", (None, None)),
        "nl45_claims": inc.get("NL-45 Claims", (None, None)),
        "nl36_total_policies": inc.get("NL-36 Total Policies", (None, None)),
        "it_capex": inc.get("IT Capex", (None, None)),
        "in_house_claims_cost": inc.get("In House Claim Processing Cost", (None, None)),
        "office_counts": inc.get("Office Counts", (None, None)),
        "csr_amount": inc.get("CSR Amount", (None, None)),
        "complaint_ratios": inc.get("NL-45 Complaint Ratios", (None, None)),
        "cumulative_capital": inc.get("Cumulative Capital", (None, None)),
    }

    idx = build_row_index(ws)
    # NOT named `log` - this function also uses the module-level logger
    # above (log.info(...) when a company falls back to a live per-company
    # call), and a local `log` assigned anywhere in this function makes
    # Python treat every `log` reference in it as that local for the WHOLE
    # function body - including the logger call above, which then raises
    # UnboundLocalError before it ever gets there.
    applied_log = []
    written = 0

    for key, rows in rows_by_key.items():
        cur, prior = _converted_value(regrouped, kind_by_key, key)
        if key == "solvency_ratio":
            # Solvency Ratio is a "No. of times" multiple everywhere (typically
            # 1.5-10x) - some insurers' NL-20 schedule prints it as a percentage
            # instead (e.g. "184%"), which the model can extract literally as
            # 184 despite the spec asking for a multiple (GT-verified case:
            # Manipal Cigna). >20 is never a real multiple, so treat it as a
            # percentage and rescale, regardless of which company it is.
            cur = cur / 100 if cur is not None and cur > 20 else cur
            prior = prior / 100 if prior is not None and prior > 20 else prior
        for (slide, metric1, metric2) in rows:
            written += apply_metric_to_rows(ws, idx, slide, company, metric1, metric2, cur, prior, dry_run, applied_log)

    derived = compute_derived_metrics(company, regrouped, kind_by_key, income)
    for (slide, metric1, metric2), (cur, prior) in derived.items():
        if metric1.startswith("__SLIDE12__"):
            actual_metric1 = metric1[len("__SLIDE12__"):]
            written += apply_metric_to_rows(ws, idx, slide, "GDPI by Channel - SAHI", actual_metric1, metric2, cur, prior, dry_run, applied_log)
        else:
            written += apply_metric_to_rows(ws, idx, slide, company, metric1, metric2, cur, prior, dry_run, applied_log)

    write_extraction_audit(company, raw, rows_by_key, kind_by_key, derived)

    return written, applied_log, raw


# Populated by prefetch_gemini_metrics so the write stage can consume results
# that were fetched concurrently; entries are popped as they are applied.
apply_company_gemini_pipeline._raw_cache = {}


def write_extraction_audit(company, raw, rows_by_key, kind_by_key, derived):
    """Persists, per company, exactly which Excel cell every LLM-extracted or
    Python-derived value was written to, alongside the LLM's own evidence
    fields (source_form/page_number/evidence/notes) - so any filled Excel
    cell can be traced back to source form/page/table evidence."""
    entries = []
    for key, v in raw.items():
        entries.append({
            "key": key, "source": "llm", "kind": kind_by_key.get(key),
            "fy26_q3": v.get("fy26_q3"), "fy25_q3": v.get("fy25_q3"), "found": v.get("found"),
            "source_form": v.get("source_form"), "page_number": v.get("page_number"),
            "evidence": v.get("evidence"), "notes": v.get("notes"),
            "target_rows": [{"slide": s, "metric1": m1, "metric2": m2} for (s, m1, m2) in rows_by_key.get(key, [])],
        })
    for (slide, metric1, metric2), (cur, prior) in derived.items():
        entries.append({
            "key": None, "source": "derived",
            "fy26_q3": cur, "fy25_q3": prior,
            "target_rows": [{"slide": slide, "metric1": metric1.removeprefix("__SLIDE12__"), "metric2": metric2}],
        })
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", company)
    out_dir = cfg.audit_dir()
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{safe}.json"), "w", encoding="utf-8") as f:
        json.dump({"company": company, "entries": entries}, f, ensure_ascii=False, indent=2, default=str)


def main():
    logging_setup.configure()
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--gic", action="store_true")
    ap.add_argument("--income-statement", action="store_true")
    ap.add_argument("--gemini-metrics", action="store_true", help="Run the hybrid PDF-JSON->Gemini pipeline for slides 12-35")
    ap.add_argument("--company", help="Restrict --gemini-metrics to one company key (e.g. NBHI)")
    ap.add_argument("--fix-conventions", action="store_true",
                     help="Re-derive Slides 3/4/5/6/7 (force-overwrite, absolute Rs.Cr not share) and Slides 8/12 "
                          "(per-company Revenue Growth (GDPI) + channel-share fractions) per a ground-truth cross-check")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wb, ws = load_engine()

    if args.audit:
        audit(ws)
        return

    if args.gic:
        gic = GicData()
        lookups = build_gic_lookups(gic)
        updated, skipped = apply_gic_rows(ws, lookups, dry_run=args.dry_run)
        log.info("GIC pass: matched %d rows, %d unmatched (left blank).", updated, len(skipped))
        if skipped:
            log.info("Unmatched keys (first 40):")
            for r, k in skipped[:40]:
                log.info("  row %s: %s", r, k)
        if not args.dry_run:
            wb.save(XLSX_PATH)
            log.info("Saved %s", XLSX_PATH)
        return

    if args.fix_conventions:
        gic = GicData()
        lookups = build_gic_lookups(gic)
        updated, skipped = apply_gic_rows(ws, lookups, dry_run=args.dry_run, force=True)
        log.info("GIC re-derive pass (forced): matched %d rows, %d unmatched.", updated, len(skipped))
        written, fix_log = fix_slide8_and_slide12(ws, gic, dry_run=args.dry_run)
        log.info("Slide 8/12 fix pass: wrote %d cell-pairs, %d unmatched sheet targets.",
                  written, len(fix_log))
        if fix_log:
            for item in fix_log[:20]:
                log.info("    %s", item)
        if not args.dry_run:
            wb.save(XLSX_PATH)
            log.info("Saved %s", XLSX_PATH)
        return

    if args.income_statement:
        updated, skipped = apply_income_statement_rows(ws, dry_run=args.dry_run)
        log.info("Income statement pass: matched %d rows, %d unmatched.", updated, len(skipped))
        if skipped:
            log.info("Unmatched (first 40):")
            for r, company, metric1, reason in skipped[:40]:
                log.info("  row %s: %s / %s -> %s", r, company, metric1, reason)
        if not args.dry_run:
            wb.save(XLSX_PATH)
            log.info("Saved %s", XLSX_PATH)
        return

    if args.gemini_metrics:
        companies = [args.company] if args.company else list(gemini_extract.COMPANY_PDFS.keys())
        total_written = 0
        all_raw = {}
        for company in companies:
            written, apply_log, raw = apply_company_gemini_pipeline(ws, company, dry_run=args.dry_run)
            total_written += written
            all_raw[company] = raw
            not_found = sum(1 for v in raw.values() if not v["found"])
            log.info("%s: wrote %d cell-pairs, %d/%d metrics not found in source.",
                      company, written, not_found, len(raw))
            if apply_log:
                log.info("    unmatched sheet targets (first 10 of %d):", len(apply_log))
                for item in apply_log[:10]:
                    log.info("      %s", item)
        log.info("Gemini metrics pass: %d cell-pairs written across %d companies.",
                  total_written, len(companies))
        if not args.dry_run:
            wb.save(XLSX_PATH)
            log.info("Saved %s", XLSX_PATH)
        else:
            with open("gemini_raw_dump.json", "w") as f:
                json.dump(all_raw, f, indent=2)
            log.info("Dry run - raw Gemini results saved to gemini_raw_dump.json for review.")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
