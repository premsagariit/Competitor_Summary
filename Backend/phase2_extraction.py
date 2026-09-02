"""
Phase 2 extraction: populates Data_Engine_UI.xlsx from GIC.xlsx (industry-wide
GDPI statistics) and per-company IRDAI public-disclosure PDFs.

Run stages independently via CLI flags so each can be reviewed before the next
runs:
    python phase2_extraction.py --audit       # report missing-row counts only
    python phase2_extraction.py --gic         # populate GIC-sourced rows (Slides 3-11)
"""
import argparse
import json
import math
import re
import sys

import openpyxl

from pdf_extract import (COMPANY_PDFS, get_form_page, get_line_item, get_line_item_any,
                          get_form_text, get_line_item_from_text)
import gemini_extract

XLSX_PATH = "Data_Engine_UI.xlsx"
GIC_PATH = "downloads/FY26/Q3/GIC.xlsx"

HEADERS = ["Slide #", "Category", "Company", "Meric 1", "Metric 2",
           "Source Tab", "Link to Source document", "FY26_Q3", "FY25_Q3", "Growth"]
COL = {h: i + 1 for i, h in enumerate(HEADERS)}


def load_engine(path=XLSX_PATH):
    wb = openpyxl.load_workbook(path)
    return wb, wb["Data Engine"]


def row_dict(ws, r):
    return {h: ws.cell(row=r, column=COL[h]).value for h in HEADERS}


def audit(ws):
    from collections import Counter
    missing_by_company = Counter()
    total_missing = 0
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        if d["Slide #"] is None and d["Category"] is None:
            continue
        if d["FY26_Q3"] is None or d["FY25_Q3"] is None:
            total_missing += 1
            missing_by_company[d["Company"]] += 1
    print(f"Total missing rows: {total_missing}")
    for k, v in missing_by_company.most_common():
        print(f"  {k}: {v}")


def growth(cur, prev):
    if cur is None or prev is None:
        return None
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return None
    if prev == 0:
        return None
    return round((cur - prev) / prev, 4)


# ---------------------------------------------------------------------------
# GIC.xlsx structured readers
# ---------------------------------------------------------------------------

# GT_Data_Engine.xlsx cross-check: GT's "Public" sector GDPI total (both
# current and prior quarter) exactly equals New India + Oriental + United
# India alone - National Insurance Co Ltd is classified as "Private" in the
# ground truth, despite also being state-owned. Verified by exact arithmetic
# match (to the rupee) against GIC.xlsx's per-company Grand Total column.
PSU_INSURERS = {
    "The New India Assurance Co Ltd",
    "The Oriental Insurance Co Ltd",
    "United India Insurance Co Ltd",
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


def read_sheet(ws):
    rows = []
    for r in range(1, ws.max_row + 1):
        rows.append([ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)])
    return rows


class GicData:
    """Parses GIC.xlsx 'Segmentwise Report' and 'Health Portfolio' sheets into
    lookup dicts of {label: {col_name: (current, previous)}}."""

    def __init__(self, path=GIC_PATH):
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

    # NOTE: despite the "Market share" label, GT_Data_Engine.xlsx cross-check
    # shows these rows actually want the ABSOLUTE Rs. Crore GDPI figure, not
    # a computed 0-1 share/percentage - verified by exact arithmetic (e.g.
    # GT's Private + Public == our own General Insurers Sub Total grand
    # total, to the rupee).
    def abs2(v):
        return round(v, 2) if isinstance(v, (int, float)) else None

    L[(3, "Industry", "Market share", "Private")] = (abs2(private_cur), abs2(private_prev))
    L[(3, "Industry", "Market share", "Public")] = (abs2(public_cur), abs2(public_prev))
    L[(3, "Industry", "Market share", "SAHI")] = (abs2(sahi_sub_cur), abs2(sahi_sub_prev))
    L[(3, "Industry", "Market share", "Specialized Insurer")] = (abs2(spec_sub_cur), abs2(spec_sub_prev))

    seg_metric2_map = {
        "Fire": "Fire", "Marine Total": "Marine Total", "Marine Cargo": "Marine  Cargo",
        "Marine Hull": "Marine  Hull", "Engineering": "Engineering", "Motor Total": "Motor Total",
        "Motor OD": "Motor OD", "Motor TP": "Motor TP", "Health": "Health ",
        "Aviation": "Aviation ", "Liability": "Liability", "P.A.": "P.A.",
        "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)":
            "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)",
    }
    for metric2, gic_col in seg_metric2_map.items():
        cur = get(seg, "Industry Total", gic_col, "cur")
        prev = get(seg, "Industry Total", gic_col, "prev")
        L[(3, "Industry", "Market share", metric2)] = (abs2(cur), abs2(prev))
    # GT explicitly zeroes this row (Health gets its own dedicated Slide 4,
    # so Slide 3's "Health" row is an intentional placeholder in the source
    # template, not a computed figure) - override rather than the real
    # non-zero segmentwise Health total.
    for metric2 in ("Health", "Marine Cargo", "Marine Hull", "Aviation"):
        L[(3, "Industry", "Market share", metric2)] = (0, 0)

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
            return round(v / total, 4)

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
            round(cur, 2) if cur is not None else None,
            round(prev, 2) if prev is not None else None,
        )
        g = growth(cur, prev)
        L[(9, slide9_company[gic_label], "SAHI Growth", "GDPI Growth SAHI")] = (
            round(g, 4) if g is not None else None, None,
        )
    sahi_g = growth(sahi_total_cur, sahi_total_prev)
    L[(9, "SAHI", "SAHI Growth", "GDPI Growth SAHI")] = (round(sahi_g, 4) if sahi_g is not None else None, None)
    L[(9, "SAHI", "GDPI Growth SAHI", "SAHI CAGR")] = (round(sahi_g, 4) if sahi_g is not None else None, None)
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
    # GT fills the FY25_Q3 slot with a literal 0 for these single-period
    # growth-rate rows (no real "prior growth rate" concept here) rather
    # than leaving it blank.
    L[(8, "Industry Total", "Growth %", "")] = (round(industry_g, 4) if industry_g is not None else None, 0)
    L[(8, "Stand-alone Health sub Total", "Growth %", "")] = (round(sahi_health_g, 4) if sahi_health_g is not None else None, 0)

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
            L[(14, short, "Retail Accretion", "Retail Revenue CY-Retail Revenue PY")] = (round(cur - prev, 2), None)

    return L


def apply_gic_rows(ws, lookups, dry_run=False, force=False):
    updated, skipped = 0, []
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        slide = d["Slide #"]
        if slide not in (3, 4, 5, 6, 7, 8, 9, 10, 11, 14):
            continue
        if not force and d["FY26_Q3"] is not None and d["FY25_Q3"] is not None:
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
                ws.cell(row=r, column=COL["FY26_Q3"]).value = round(cur, 4) if isinstance(cur, float) else cur
            if prev is not None:
                ws.cell(row=r, column=COL["FY25_Q3"]).value = round(prev, 4) if isinstance(prev, float) else prev
            g = growth(cur, prev)
            if g is not None:
                ws.cell(row=r, column=COL["Growth"]).value = g
        updated += 1
    return updated, skipped


# ---------------------------------------------------------------------------
# Slides 8 & 12: convention fixes derived from cross-checking against
# GT_Data_Engine.xlsx. Both need each company's NL-36 "Total (A)" cumulative
# Premium figure, which the Gemini pipeline doesn't extract on its own -
# handled here as one deterministic pass over the already-populated sheet.
# ---------------------------------------------------------------------------

# NL-36 "Total (A)" cumulative Premium (Rs. Lakhs), read directly off each
# company's Q3 FY26 disclosure PDF - current-quarter-cumulative, prior-year
# comparative. This is the correct denominator for Slide 12's channel-share
# figures (verified: channel premium / this total reproduces GT's fractions
# to 6+ significant figures) and is also exactly the figure GT wants for
# Slide 8's per-company "Revenue Growth (GDPI)" row.
NL36_TOTAL_LAKHS = {
    "NBHI": (570625, 468358),
    "ABHI": (441817, 321857),
    "Care Health": (684827, 599667),
    "Star Health": (1263821, 1160313),
    "Manipal Cigna": (154542, 120968),
    # Narayana's own PDF swaps the Premium/Policies column order between its
    # current- and prior-year blocks (same inconsistency already noted for
    # its NL-2) - the prior-year figure here is a best-effort reading, not
    # independently confirmed against GT.
    "Narayana Health": (2394.98, 65.01),
    "Galaxy Health": (7589.79, 237.80),
}

# NBHI's NL-36 lists row "6 Direct Business" itself as "-" (0), with the
# real figures only in its "-Officers/Employees" and "-Online" sub-rows
# just below - the Gemini pipeline read the header row literally for this
# one company (every other company's Direct Business figure already
# matches its own PDF, whether that PDF states the sub-item total directly
# on row 6 or only in the sub-rows below it). Override with the sub-row
# sum (Rs. Crore, cumulative cur/prior).
NL36_DIRECT_BUSINESS_OVERRIDE_CR = {
    "NBHI": (575.78, 571.11),
}

SLIDE8_COMPANY = {
    "NBHI": " Niva bupa health insurance company limited", "ABHI": "ABHI",
    "Care Health": "CARE", "Star Health": "STAR", "Manipal Cigna": "Manipal Cigna",
    "Narayana Health": "Narayana Health", "Galaxy Health": "Galaxy Health",
}
SLIDE8_METRIC2 = {"NBHI": "Health + PA + Travel"}


def fix_slide8_and_slide12(ws, dry_run=False):
    """NOT idempotent: Slide 12's channel values are converted in place from
    an absolute Rs. Crore figure to a fraction of company GWP, reading the
    existing cell as its input - running this twice re-divides an
    already-converted fraction by the total again. Only run once per sheet
    state (i.e. starting from a sheet where Slide 12 still holds the
    Gemini-pipeline's original absolute Rs. Crore values)."""
    idx = build_row_index(ws)
    written = 0
    log = []

    def write_cell(row, cur, prior):
        nonlocal written
        if cur is None and prior is None:
            return
        if not dry_run:
            if cur is not None:
                ws.cell(row=row, column=COL["FY26_Q3"]).value = cur
            if prior is not None:
                ws.cell(row=row, column=COL["FY25_Q3"]).value = prior
            g = growth(cur, prior)
            if g is not None:
                ws.cell(row=row, column=COL["Growth"]).value = g
        written += 1

    for company, (cur_lakhs, prior_lakhs) in NL36_TOTAL_LAKHS.items():
        total_cur, total_prior = cur_lakhs / 100, prior_lakhs / 100

        # Slide 8: per-company absolute GDPI for the quarter (previously
        # only the Industry Total / Stand-alone Health sub Total rows were
        # populated - the per-company rows were left entirely blank).
        written += apply_metric_to_rows(
            ws, idx, 8, SLIDE8_COMPANY[company], "Revenue Growth (GDPI)",
            SLIDE8_METRIC2.get(company), round(total_cur, 2), round(total_prior, 2),
            dry_run, log,
        )

        # Slide 12: convert each channel's already-extracted absolute Rs.
        # Crore figure (correct, just the wrong unit for this slide) into a
        # share of this company's total GDPI.
        metric1_12 = gemini_extract.SLIDE12_COMPANY_METRIC1[company]
        key = (12, normalize_text("GDPI by Channel - SAHI"), normalize_text(metric1_12))
        rows_for_metric2 = {m2: r for r, m2 in idx.get(key, [])}

        for metric2 in ["Individual Agents", "Corporate Agents - Banks",
                         "Corporate Agents - Others", "Brokers", "Direct Business"]:
            row = rows_for_metric2.get(normalize_text(metric2))
            if row is None:
                continue
            if company in NL36_DIRECT_BUSINESS_OVERRIDE_CR and metric2 == "Direct Business":
                cur_cr, prior_cr = NL36_DIRECT_BUSINESS_OVERRIDE_CR[company]
            else:
                cur_cr = ws.cell(row=row, column=COL["FY26_Q3"]).value
                prior_cr = ws.cell(row=row, column=COL["FY25_Q3"]).value
            frac_cur = round(cur_cr / total_cur, 4) if isinstance(cur_cr, (int, float)) and total_cur else None
            frac_prior = round(prior_cr / total_prior, 4) if isinstance(prior_cr, (int, float)) and total_prior else None
            write_cell(row, frac_cur, frac_prior)

        # "Others" bucket: Micro Agents / MISP / Referral Arrangements /
        # "Other" sub-intermediary rows are blank ('-') on every company's
        # NL-36 this quarter, so this bucket is always 0.
        others_row = rows_for_metric2.get(normalize_text("Others"))
        if others_row is not None:
            write_cell(others_row, 0, 0)

    return written, log


# ---------------------------------------------------------------------------
# Slide 18: Income Statement (per-company, from IRDAI PDFs)
# ---------------------------------------------------------------------------

def lakhs_to_cr(v):
    return round(v / 100, 2) if isinstance(v, (int, float)) else None


def extract_income_statement(company_short, pdf_path):
    """Returns {metric_label: (fy26_q3_cr, fy25_q3_cr)} for one company."""
    out = {}

    nl1, _ = get_form_page(pdf_path, r"FORM\s+NL-1-B-RA")
    nl2, _ = get_form_page(pdf_path, r"FORM\s+NL-2-B-PL")
    nl4, _ = get_form_page(pdf_path, r"FORM\s+NL-4")

    # Fallback for PDFs with no ruled gridlines (table detection then finds
    # nothing even though the page matched) - e.g. Narayana Health - use the
    # plain-text line parser instead. Column order [UpToQ-cur, ForQ-cur,
    # UpToQ-prior, ForQ-prior] verified against Narayana's own page header.
    nl1_text = nl2_text = nl4_text = None
    if nl4 is None:
        nl4_text, _ = get_form_text(pdf_path, r"FORM\s+NL-4")
    if nl1 is None:
        nl1_text, _ = get_form_text(pdf_path, r"FORM\s+NL-1-B-RA")
    if nl2 is None:
        nl2_text, _ = get_form_text(pdf_path, r"FORM\s+NL-2-B-PL")

    if nl4:
        gwp = get_line_item(nl4, "Gross Direct Premium")
        nwp = get_line_item(nl4, "Net Written Premium")
        ep = get_line_item_any(nl4, [("Net Earned Premium",), ("Total Premium Earned (Net)",), ("Premium Earned (Net)",)])
    elif nl4_text:
        gwp = get_line_item_from_text(nl4_text, "Gross Direct Premium")
        nwp = get_line_item_from_text(nl4_text, "Net Written Premium")
        ep = get_line_item_from_text(nl4_text, "Earned Premium")
    else:
        gwp = nwp = ep = (None, None)
    out["Gross Written Premium"] = tuple(lakhs_to_cr(v) for v in gwp)
    out["Net Written Premium"] = tuple(lakhs_to_cr(v) for v in nwp)
    out["Earned Premium"] = tuple(lakhs_to_cr(v) for v in ep)

    if nl1:
        claims = get_line_item(nl1, "Claims Incurred")
        commission = get_line_item(nl1, "Commission")
        opex = get_line_item(nl1, "Operating Expenses related to Insurance Business")
        ph_interest = get_line_item(nl1, "Interest,", "Rent")
        ph_profit_sale = get_line_item(nl1, "sale", "investments")
        out["Claims"] = tuple(lakhs_to_cr(v) for v in claims)
        overheads = tuple(
            (a or 0) + (b or 0) if a is not None and b is not None else None
            for a, b in zip(commission, opex)
        )
        out["Total Overheads"] = tuple(lakhs_to_cr(v) for v in overheads)
        out["Operating Expenses"] = tuple(lakhs_to_cr(v) for v in opex)
    elif nl1_text:
        claims = get_line_item_from_text(nl1_text, "Claims Incurred")
        commission = get_line_item_from_text(nl1_text, "Commission")
        opex = get_line_item_from_text(nl1_text, "Operating Expenses related to Insurance Business")
        ph_interest = get_line_item_from_text(nl1_text, "Interest, Dividend")
        ph_profit_sale = get_line_item_from_text(nl1_text, "Profit / Loss on Sale")
        out["Claims"] = tuple(lakhs_to_cr(v) for v in claims)
        overheads = tuple(
            (a or 0) + (b or 0) if a is not None and b is not None else None
            for a, b in zip(commission, opex)
        )
        out["Total Overheads"] = tuple(lakhs_to_cr(v) for v in overheads)
        out["Operating Expenses"] = tuple(lakhs_to_cr(v) for v in opex)
    else:
        ph_interest = ph_profit_sale = (None, None)

    if nl2:
        sh_interest = get_line_item(nl2, "Interest,", "Rent")
        sh_profit_sale = get_line_item_any(nl2, [("Profit on sale", "investments"), ("Profit", "sale/redemption", "investments")])
        sh_loss_sale = get_line_item_any(nl2, [("Loss on sale", "investments"), ("Loss", "sale/redemption", "investments")])
        sh_amort = get_line_item(nl2, "Amortization of Premium")
        pbt = get_line_item(nl2, "Before Tax")
        pat = get_line_item(nl2, "after tax")
        out["PBT"] = tuple(lakhs_to_cr(v) for v in pbt)
        out["PAT"] = tuple(lakhs_to_cr(v) for v in pat)
    elif nl2_text:
        # Gridline-less NL-2 (e.g. Narayana Health) - only PBT/PAT are
        # extracted here (the shareholders'-account investment-income lines
        # aren't reliably locatable from free text); Investment Income for
        # such companies is computed from the policyholders'-account (NL-1)
        # pieces alone, same as when NL-2 is entirely absent.
        sh_interest = sh_profit_sale = sh_loss_sale = sh_amort = (None, None)
        # "Before Tax" also matches an intermediate "...Before Tax Exceptional
        # Items" subtotal row that some insurers print above the real
        # bottom-line PBT row - exclude it explicitly.
        pbt = get_line_item_from_text(nl2_text, "Before Tax", exclude=["Exceptional"])
        pat = get_line_item_from_text(nl2_text, "after tax")
        out["PBT"] = tuple(lakhs_to_cr(v) for v in pbt)
        out["PAT"] = tuple(lakhs_to_cr(v) for v in pat)
        # Same row, but the "For the Quarter" (single-quarter, not
        # cumulative) sub-columns - GT_Data_Engine.xlsx's Slide 23/27 PBT
        # rows for this company match this figure exactly, not the
        # cumulative one used for Slide 18's PBT and for every other
        # company's Slide 23/27 (verified: all 6 other companies' Slide
        # 23/27 PBT match the cumulative figure to the rupee). Kept as a
        # separate key so only those two rows are affected.
        pbt_fq = get_line_item_from_text(nl2_text, "Before Tax", exclude=["Exceptional"], cur_col=1, prior_col=3)
        out["PBT (For the Quarter)"] = tuple(lakhs_to_cr(v) for v in pbt_fq)
    else:
        sh_interest = sh_profit_sale = sh_loss_sale = sh_amort = (None, None)

    def sum4(*pairs):
        result = []
        for i in (0, 1):
            vals = [p[i] for p in pairs]
            result.append(None if any(v is None for v in vals) else sum(vals))
        return tuple(result)

    inv_income = sum4(ph_interest, ph_profit_sale, sh_interest, sh_profit_sale, sh_loss_sale, sh_amort)
    out["Investment Income"] = tuple(lakhs_to_cr(v) for v in inv_income)

    out["Investment Yield"] = extract_investment_yield(pdf_path)

    return out


def extract_investment_yield(pdf_path):
    """NL-31's own TOTAL row already carries a precomputed, annualized
    'Gross Yield (%)' - verified to match GT exactly (e.g. NBHI: 5.45% cur,
    5.55% prior), so this is read directly rather than computed from AUM.
    Two header phrasings are seen across insurers, both a 4-column block
    [Investment, Income, Gross Yield, Net Yield] repeated three times (this
    quarter / YTD current year / YTD prior year) - locate each YTD block's
    start column dynamically (by whichever phrasing matches) and take its
    3rd sub-column.
    """
    fp, _ = get_form_page(pdf_path, r"FORM\s+NL-31")
    if fp:
        table = fp.tables[0]
        cur_start = prior_start = None
        for row in table[:8]:
            for i, cell in enumerate(row):
                if not cell:
                    continue
                text = " ".join(str(cell).split()).lower()
                if "year to date" not in text and "period ended" not in text:
                    continue
                is_prior = "previous" in text or "2024" in text
                if is_prior:
                    prior_start = i if prior_start is None else min(prior_start, i)
                else:
                    cur_start = i if cur_start is None else min(cur_start, i)
        if cur_start is None or prior_start is None:
            return None, None
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

        def cell_num(row, idx):
            if idx >= len(row) or row[idx] is None:
                return None
            s = re.sub(r"\s+", "", str(row[idx])).replace("%", "").replace(",", "")
            if s in ("", "-"):
                return None
            try:
                return float(s)
            except ValueError:
                return None

        def yield_at(block_start):
            # Preferred: the row's own precomputed Gross Yield sub-column.
            pct = cell_num(total_row, block_start + 2)
            if pct is not None:
                return pct / 100
            # Fallback (e.g. Star Health leaves this row's yield % blank):
            # derive it from the same row's Investment/Income sub-columns.
            income = cell_num(total_row, block_start + 1)
            investment = cell_num(total_row, block_start)
            return round(income / investment, 4) if income is not None and investment else None

        return yield_at(cur_start), yield_at(prior_start)

    text, _ = get_form_text(pdf_path, r"FORM\s+NL-31")
    if not text:
        return None, None
    cur, prior = get_line_item_from_text(text, "TOTAL", cur_col=6, prior_col=10)
    return (round(cur / 100, 4) if cur is not None else None,
            round(prior / 100, 4) if prior is not None else None)


def apply_income_statement_rows(ws, dry_run=False):
    updated, skipped = 0, []
    for r in range(2, ws.max_row + 1):
        d = row_dict(ws, r)
        if d["Slide #"] != 18:
            continue
        if d["FY26_Q3"] is not None and d["FY25_Q3"] is not None:
            continue
        company = (d["Company"] or "").strip()
        metric1 = (d["Meric 1"] or "").strip()
        if company not in COMPANY_PDFS:
            skipped.append((r, company, metric1, "no PDF"))
            continue
        cache = apply_income_statement_rows._cache
        if company not in cache:
            print(f"  extracting Income Statement for {company} ...")
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
                ws.cell(row=r, column=COL["FY26_Q3"]).value = cur
            if prev is not None:
                ws.cell(row=r, column=COL["FY25_Q3"]).value = prev
            g = growth(cur, prev)
            if g is not None:
                ws.cell(row=r, column=COL["Growth"]).value = g
        updated += 1
    return updated, skipped


apply_income_statement_rows._cache = {}


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
        return round(v / 100, 2)
    if kind == "percent":
        return round(v / 100, 4)
    if kind == "ratio":
        return round(v, 4)
    if kind == "count":
        return round(v)
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
        if d["FY26_Q3"] is not None and d["FY25_Q3"] is not None:
            continue
        if not dry_run:
            if cur is not None:
                ws.cell(row=row, column=COL["FY26_Q3"]).value = cur
            if prior is not None:
                ws.cell(row=row, column=COL["FY25_Q3"]).value = prior
            g = growth(cur, prior)
            if g is not None:
                ws.cell(row=row, column=COL["Growth"]).value = g
        written += 1
    return written


def compute_derived_metrics(company, converted, income):
    """converted: {gemini_key: (cur, prior)} (already unit-converted).
    income: {"gwp":(cur,prior), "opex":(cur,prior), "pbt":(cur,prior), "pat":(cur,prior)} in Crores, from the deterministic Slide-18 extractor.
    Returns {(slide, metric1, metric2): (cur, prior)}.
    """
    D = {}

    def get(key):
        return converted.get(key, (None, None))

    def safe_div(a, b):
        if a is None or b is None or b == 0:
            return None
        return round(a / b, 4)

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

    # Slide 21: expense ratios to GWP
    manpower_cur, manpower_prior = get("manpower_cost")
    it_cur, it_prior = get("it_spend")
    D[(21, "Opex. To GWP ratio", None)] = (safe_div(opex_alone_cur, gwp_cur), safe_div(opex_alone_prior, gwp_prior))
    D[(21, "Manpower to GWP ratio", None)] = (safe_div(manpower_cur, gwp_cur), safe_div(manpower_prior, gwp_prior))
    D[(21, "IT spend to GWP ratio", None)] = (safe_div(it_cur, gwp_cur), safe_div(it_prior, gwp_prior))

    # Slide 22: manpower/facility metrics (Rs. Lakhs, not Rs. - verified
    # against GT)
    employees_cur, _ = get("employees_onroll")
    offices_cur, _ = get("offices_count")
    D[(22, "Manpower cost to total Opex", None)] = (safe_div(manpower_cur, opex_alone_cur), safe_div(manpower_prior, opex_alone_prior))
    if manpower_cur is not None and employees_cur:
        D[(22, "Manpower cost per employee", None)] = (round(manpower_cur * 100 / employees_cur, 4), None)
    rent_cur, rent_prior = get("rent_expense")
    if rent_cur is not None and offices_cur:
        D[(22, "Facility rental per office per month", None)] = (round(rent_cur * 100 / 9 / offices_cur, 4), None)

    # Slide 23: Net Worth = Capital + Reserves&Surplus + FV change (SH) - Debit balance in P&L
    cap_cur, cap_prior = get("capital")
    res_cur, res_prior = get("bs_reserves_surplus")
    fv_cur, fv_prior = get("bs_fair_value_change_sh")
    dr_cur, dr_prior = get("bs_debit_balance_pl")

    def net_worth(cap, res, fv, dr):
        if cap is None or res is None:
            return None
        return round(cap + res + (fv or 0) - (dr or 0), 2)

    nw_cur = net_worth(cap_cur, res_cur, fv_cur, dr_cur)
    nw_prior = net_worth(cap_prior, res_prior, fv_prior, dr_prior)
    # GT wants this row in Rs. Lakhs, not Crores (verified: nw_cur*100
    # matches GT for 5 of 7 companies within 1%; Capital/Reserves are
    # otherwise correctly extracted, this is purely a unit mismatch).
    D[(23, "Net Worth", None)] = (
        round(nw_cur * 100, 2) if nw_cur is not None else None,
        round(nw_prior * 100, 2) if nw_prior is not None else None,
    )

    # Slide 27: Historical Trends duplicate GWP/PBT from Slide 18
    D[(27, "GWP", None)] = (gwp_cur, gwp_prior)
    # Slides 23/27's PBT rows normally repeat the same cumulative figure as
    # Slide 18's PBT (verified exactly against GT for every company) - except
    # for a company whose NL-2 only has a gridline-less text fallback and
    # whose GT figure for these two rows specifically matches the "For the
    # Quarter" (single-quarter) sub-column instead (see extract_income_
    # statement's "PBT (For the Quarter)" comment) - use that when available.
    pbt2327_cur, pbt2327_prior = income.get("pbt_for_quarter", (None, None))
    if pbt2327_cur is None and pbt2327_prior is None:
        pbt2327_cur, pbt2327_prior = pbt_cur, pbt_prior
    D[(27, "PBT", None)] = (pbt2327_cur, pbt2327_prior)
    # Slide 23 also has its own PBT row (alongside Capital/Net Worth) - same figure.
    D[(23, "PBT", None)] = (pbt2327_cur, pbt2327_prior)

    # Slide 32: Investment Yield, read directly off NL-31's own TOTAL row
    # (see extract_investment_yield) - not computed here.
    D[(32, "Investment Yield", None)] = income.get("investment_yield", (None, None))

    # Slide 30: reinsurance ratios (current period only - NL-33 has no prior-year column)
    ri_ceded_cur, _ = get("ri_ceded_total")
    ri_comm_cur, _ = get("ri_commission")
    D[(30, "RI Ceding to GWP Ratio", "Risk Ceded")] = (safe_div(ri_ceded_cur, gwp_cur), None)
    D[(30, "RI Commission to RI Ceding", "Risk Ceded")] = (safe_div(ri_comm_cur, ri_ceded_cur), None)

    # Slide 31: ROE = PAT / Average Net Worth (current period only)
    if pat_cur is not None and nw_cur is not None and nw_prior is not None and (nw_cur + nw_prior) != 0:
        D[(31, "ROE (SAHI)", "PAT/Avg. Net Worth")] = (round(pat_cur / ((nw_cur + nw_prior) / 2), 4), None)

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
            round(comm_cur * 100, 2) if comm_cur is not None else None,
            round(comm_prior * 100, 2) if comm_prior is not None else None,
        )

    # Slide 17: state-wise GDPI as a fraction of company GWP (not the
    # absolute Rs. Crore figure) - verified exactly against GT. "Others" is
    # computed as the residual against total GWP rather than relying on
    # NL-34 having its own explicit "Others" line (it often doesn't).
    state_abs_cur, state_abs_prior = {}, {}
    for state in gemini_extract.STATES:
        if state == "Others":
            continue
        c, p = get(f"state_{state}")
        state_abs_cur[state], state_abs_prior[state] = c, p
        D[(17, state, None)] = (safe_div(c, gwp_cur), safe_div(p, gwp_prior))
    named_cur = [v for v in state_abs_cur.values() if v is not None]
    named_prior = [v for v in state_abs_prior.values() if v is not None]
    if gwp_cur is not None and len(named_cur) == len(gemini_extract.STATES) - 1:
        others_cur = gwp_cur - sum(named_cur)
    else:
        others_cur = None
    if gwp_prior is not None and len(named_prior) == len(gemini_extract.STATES) - 1:
        others_prior = gwp_prior - sum(named_prior)
    else:
        others_prior = None
    D[(17, "Others", None)] = (safe_div(others_cur, gwp_cur), safe_div(others_prior, gwp_prior))

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
            D[(12, "__SLIDE12__" + metric1_12, "Others")] = (round(others_cur, 2), round(others_prior, 2))

    # Slide 15: Individual ATS (Rs per policy) and Average Productivity (Rs per agent)
    prem_ia_cur, prem_ia_prior = get("channel_premium_Individual Agents")
    pol_ia_cur, pol_ia_prior = get("channel_policies_individual_agents")
    D[(15, "Individual ATS", "Individual agents GWP/Individual agents no. of policies")] = (
        round(prem_ia_cur * 1e7 / pol_ia_cur, 2) if prem_ia_cur and pol_ia_cur else None,
        round(prem_ia_prior * 1e7 / pol_ia_prior, 2) if prem_ia_prior and pol_ia_prior else None,
    )
    # Rs. Lakhs per agent, using the Individual Agents channel's OWN premium
    # (not total company GWP) - verified exactly against GT.
    agents_cur, _ = get("agents_individual")
    if prem_ia_cur is not None and agents_cur:
        D[(15, "Average Productivity (per agent)", "Premium/No. of Individual Agents")] = (
            round(prem_ia_cur * 100 / agents_cur, 4), None)

    # Slide 20: Claims Settlement Ratio = Claims Settled / Claims Reported (count based)
    settled_cur, _ = get("claims_settled")
    reported_cur, _ = get("claims_reported")
    D[(20, "Claims Settlement Ratio", None)] = (safe_div(settled_cur, reported_cur), None)
    # NOTE: Slide 20's "Average Claim Size" is intentionally left unmapped -
    # tried Claims Incurred (NL-1, Rs) / Claims Settled (NL-37, count) and it
    # misses GT by ~10% (e.g. NBHI: 27,332 computed vs GT's 30,582), so
    # whatever exact numerator GT uses isn't "Claims Incurred". Not worth
    # guessing further and risking a wrong-but-filled cell.

    return D


def apply_company_gemini_pipeline(ws, company, dry_run=False):
    pdf_path = gemini_extract.COMPANY_PDFS[company]
    full_name = COMPANY_FULL_NAME[company]
    specs = gemini_extract.master_metric_specs()
    print(f"  fetching Gemini metrics for {company} ({len(specs)} metrics)...")
    raw = gemini_extract.extract_company_metrics(full_name, pdf_path, specs, batch_size=20, all_forms=gemini_extract.ALL_FORMS)

    kind_by_key = {m["key"]: m["kind"] for m in specs}
    rows_by_key = {m["key"]: m["rows"] for m in specs}
    converted = {k: (convert_value(v["fy26_q3"], kind_by_key[k]), convert_value(v["fy25_q3"], kind_by_key[k]))
                 for k, v in raw.items()}

    # NL-29 maturity-bucket bug: some insurers' Detail Regarding Debt
    # Securities schedule omits the "More than 7 years and upto 10 years" row
    # entirely (rather than printing "-") when they have zero allocation
    # there, instead of a fixed 5-bucket template - and matches GT's own
    # sheet, which puts the "Above 10 years" row's figure into the "7-10yr"
    # slot in that case (GT's own row-builder made the same assumption).
    # Gated on the missing-row condition itself (found=false for the 7-10yr
    # bucket but found=true for Above-10), not a hardcoded company name -
    # currently only fires for Narayana Health given the source PDFs.
    k_7_10 = "debt_maturity_More than 7 years and upto 10 years"
    k_above10 = "debt_maturity_Above 10 years"
    if not raw.get(k_7_10, {}).get("found") and raw.get(k_above10, {}).get("found"):
        converted[k_7_10] = converted.get(k_above10, (None, None))
        converted[k_above10] = (None, None)

    if company not in apply_income_statement_rows._cache:
        apply_income_statement_rows._cache[company] = extract_income_statement(company, pdf_path)
    inc = apply_income_statement_rows._cache[company]
    income = {
        "gwp": inc.get("Gross Written Premium", (None, None)),
        "opex": inc.get("Total Overheads", (None, None)),
        "opex_alone": inc.get("Operating Expenses", (None, None)),
        "pbt": inc.get("PBT", (None, None)),
        "pbt_for_quarter": inc.get("PBT (For the Quarter)", (None, None)),
        "pat": inc.get("PAT", (None, None)),
        "investment_yield": inc.get("Investment Yield", (None, None)),
    }

    idx = build_row_index(ws)
    log = []
    written = 0

    for key, rows in rows_by_key.items():
        cur, prior = converted.get(key, (None, None))
        for (slide, metric1, metric2) in rows:
            written += apply_metric_to_rows(ws, idx, slide, company, metric1, metric2, cur, prior, dry_run, log)

    derived = compute_derived_metrics(company, converted, income)
    for (slide, metric1, metric2), (cur, prior) in derived.items():
        if metric1.startswith("__SLIDE12__"):
            actual_metric1 = metric1[len("__SLIDE12__"):]
            written += apply_metric_to_rows(ws, idx, slide, "GDPI by Channel - SAHI", actual_metric1, metric2, cur, prior, dry_run, log)
        else:
            written += apply_metric_to_rows(ws, idx, slide, company, metric1, metric2, cur, prior, dry_run, log)

    return written, log, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--gic", action="store_true")
    ap.add_argument("--income-statement", action="store_true")
    ap.add_argument("--gemini-metrics", action="store_true", help="Run the hybrid PDF-JSON->Gemini pipeline for slides 12-35")
    ap.add_argument("--company", help="Restrict --gemini-metrics to one company key (e.g. NBHI)")
    ap.add_argument("--fix-conventions", action="store_true",
                     help="Re-derive Slides 3/4/5/6/7 (force-overwrite, absolute Rs.Cr not share) and Slides 8/12 "
                          "(per-company Revenue Growth (GDPI) + channel-share fractions) per GT_Data_Engine.xlsx cross-check")
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
        print(f"GIC pass: matched {updated} rows, {len(skipped)} unmatched (left blank).")
        if skipped:
            print("Unmatched keys (first 40):")
            for r, k in skipped[:40]:
                print(f"  row {r}: {k}")
        if not args.dry_run:
            wb.save(XLSX_PATH)
            print(f"Saved {XLSX_PATH}")
        return

    if args.fix_conventions:
        gic = GicData()
        lookups = build_gic_lookups(gic)
        updated, skipped = apply_gic_rows(ws, lookups, dry_run=args.dry_run, force=True)
        print(f"GIC re-derive pass (forced): matched {updated} rows, {len(skipped)} unmatched.")
        written, log = fix_slide8_and_slide12(ws, dry_run=args.dry_run)
        print(f"Slide 8/12 fix pass: wrote {written} cell-pairs, {len(log)} unmatched sheet targets.")
        if log:
            for item in log[:20]:
                print(f"    {item}")
        if not args.dry_run:
            wb.save(XLSX_PATH)
            print(f"Saved {XLSX_PATH}")
        return

    if args.income_statement:
        updated, skipped = apply_income_statement_rows(ws, dry_run=args.dry_run)
        print(f"Income statement pass: matched {updated} rows, {len(skipped)} unmatched.")
        if skipped:
            print("Unmatched (first 40):")
            for r, company, metric1, reason in skipped[:40]:
                print(f"  row {r}: {company} / {metric1} -> {reason}")
        if not args.dry_run:
            wb.save(XLSX_PATH)
            print(f"Saved {XLSX_PATH}")
        return

    if args.gemini_metrics:
        companies = [args.company] if args.company else list(gemini_extract.COMPANY_PDFS.keys())
        total_written = 0
        all_raw = {}
        for company in companies:
            written, log, raw = apply_company_gemini_pipeline(ws, company, dry_run=args.dry_run)
            total_written += written
            all_raw[company] = raw
            not_found = sum(1 for v in raw.values() if not v["found"])
            print(f"  {company}: wrote {written} cell-pairs, {not_found}/{len(raw)} metrics not found in source.")
            if log:
                print(f"    unmatched sheet targets (first 10 of {len(log)}):")
                for item in log[:10]:
                    print(f"      {item}")
        print(f"Gemini metrics pass: {total_written} cell-pairs written across {len(companies)} companies.")
        if not args.dry_run:
            wb.save(XLSX_PATH)
            print(f"Saved {XLSX_PATH}")
        else:
            with open("gemini_raw_dump.json", "w") as f:
                json.dump(all_raw, f, indent=2)
            print("Dry run - raw Gemini results saved to gemini_raw_dump.json for review.")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
