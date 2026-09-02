"""
Flat data-access layer over Data_Engine_UI.xlsx for report generation.
Reuses phase2_extraction's loader/row reader rather than reimplementing
sheet access.
"""
from collections import defaultdict

import phase2_extraction as p2

# Canonical rendering order for the 7 pipeline-key companies, reused across
# every "standard shape" slide (13-35).
COMPANY_ORDER = ["NBHI", "STAR", "CARE", "CIGNA", "ABHI", "NARAYANA", "GALAXY"]


def load_rows(path="Data_Engine_UI.xlsx"):
    """Every populated Data Engine row as a plain dict with keys matching
    p2.HEADERS, restricted to rows that belong to a real slide."""
    wb, ws = p2.load_engine(path)
    rows = []
    for r in range(2, ws.max_row + 1):
        d = p2.row_dict(ws, r)
        if not isinstance(d["Slide #"], int):
            continue
        rows.append(d)
    return rows


def for_slide(rows, slide_no):
    return [r for r in rows if r["Slide #"] == slide_no]


def num(v):
    return v if isinstance(v, (int, float)) else None


def pivot_metric1_only(rows, slide_no, canonical_fn=None, key_field="Company"):
    """{key_field value (optionally resolved via canonical_fn): {metric1:
    (fy26_q3, fy25_q3)}} - for slides where Metric 2 is either always None
    or a purely descriptive annotation (e.g. Slide 26's "Avg. Maturity (X
    years)" suffix, Slide 35's "No. of branches..." caption) that isn't
    needed to disambiguate the row - each (key_field, metric1) pair maps to
    exactly one row on these slides."""
    out = defaultdict(dict)
    for r in for_slide(rows, slide_no):
        raw_key = r[key_field]
        key = canonical_fn(raw_key) if canonical_fn else raw_key
        if canonical_fn and key is None:
            continue
        out[key][r["Meric 1"]] = (num(r["FY26_Q3"]), num(r["FY25_Q3"]))
    return out


def metric2_by_group(rows, slide_no, group_field, canonical_fn=None):
    """{group_field value (optionally resolved via canonical_fn): {metric2:
    (fy26_q3, fy25_q3)}} - for slides where Meric 1 is either a constant
    (e.g. "Market share") or duplicates the group field itself, and Metric 2
    alone is the varying leaf label (a segment/channel/state name)."""
    out = defaultdict(dict)
    for r in for_slide(rows, slide_no):
        raw_key = r[group_field]
        key = canonical_fn(raw_key) if canonical_fn else raw_key
        if canonical_fn and key is None:
            continue
        out[key][r["Metric 2"]] = (num(r["FY26_Q3"]), num(r["FY25_Q3"]))
    return out


def by_company(rows, slide_no, canonical_fn, key_field="Company"):
    """{canonical company key: {(metric1, metric2): (fy26_q3, fy25_q3)}} -
    resolves whichever spelling scheme this slide's `key_field` uses (Company
    or Meric 1) down to the 7 canonical keys via `canonical_fn`
    (report_theme.canonical_company), dropping rows that aren't an
    individual company (aggregate/group labels)."""
    out = defaultdict(dict)
    for r in for_slide(rows, slide_no):
        key = canonical_fn(r[key_field])
        if key is None:
            continue
        out[key][(r["Meric 1"], r["Metric 2"])] = (num(r["FY26_Q3"]), num(r["FY25_Q3"]))
    return out


def metric_series(company_data, metric1, metric2=None, companies=COMPANY_ORDER):
    """Given by_company()'s output, returns (companies_present, prior_values,
    current_values) for one (metric1, metric2) pair, in COMPANY_ORDER,
    skipping companies with no data for it at all."""
    keys, prior, current = [], [], []
    for c in companies:
        pair = company_data.get(c, {}).get((metric1, metric2))
        if pair is None:
            continue
        cur, pr = pair
        if cur is None and pr is None:
            continue
        keys.append(c)
        current.append(cur)
        prior.append(pr)
    return keys, prior, current
