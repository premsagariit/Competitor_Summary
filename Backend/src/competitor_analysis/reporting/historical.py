"""
Multi-year trend tables for the report's "Historical Trends" slide(s).

Source is data/historical/Historical_Trends.xlsx (paths.HISTORICAL_TRENDS_WORKBOOK)
- a single sheet containing several stacked tables, one per metric, each
shaped like:

    Retail Health:
    Particulars (Rs in crs)   FY18   FY19   ...   FY26
    NBHI                       634    737   ...   5,748
    STAR                      3,629  4,678  ...  17,743
    ...
    <blank row>

Unlike every other slide in report.py (which reads Data_Engine_UI.xlsx's
2-column CUR/PRIOR shape via reporting.data), this file's tables carry a
full year-by-year series - a different shape entirely, hence a separate
loader rather than an extension of data.py.

Maintained by hand, once a year, and placed at that path directly (no
upload endpoint) - see storage/s3.py's restore_all(), which pulls it down
on a fresh container instance the same way as every other data/ input.
"""
import ast
import copy
import re
import shutil
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string, get_column_letter

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis.reporting import theme

_YEAR_RE = re.compile(r"(\d{2,4})")

# Non-company rows worth keeping under their own key (e.g. slide_08's SAHI
# vs Industry growth panel) - everything else that doesn't resolve via
# theme.canonical_company() (state names, "Total", other aggregate labels)
# is silently skipped, same as before.
_AGGREGATE_ALIASES = {"sahi's": "SAHI", "sahi": "SAHI", "industry": "Industry"}


def _year_sort_key(label):
    m = _YEAR_RE.search(str(label))
    return int(m.group(1)) if m else 0


def _is_header_row(row):
    first = row[0] if row else None
    return isinstance(first, str) and first.strip().lower().startswith("particulars")


def sorted_years(table, cap_to_period=True):
    """table: {company_key: {year_label: value}} (one entry of
    load_historical_tables()'s return). Returns every year label seen across
    all companies, chronologically ordered.

    `cap_to_period` (default True) drops any year the active reporting
    period hasn't completed: beyond its FY-end year (config.fy_end_year())
    for a Q4 report, and the FY-end year itself too for Q1-Q3 (a Q1 FY26
    report ends its full years at FY25 - its own year is still in progress,
    shown as the "Q1 FY26" point instead, see report._trend_series). The workbook is
    updated by hand once a year, so it can run ahead of a quarterly report
    mid-FY (its latest column would be a not-yet-real future year the report
    period hasn't reached) or behind it (the workbook simply hasn't been
    updated for the current FY yet). Capping at the period's own FY-end year
    handles the first case; since that cap can only ever drop columns, not
    add ones the workbook doesn't have, whatever's left is naturally always
    the most recent year actually present - handling the second case too,
    with no special-casing needed."""
    years = {y for comp in table.values() for y in comp}
    if cap_to_period:
        cap = cfg.fy_end_year() % 100 - (0 if cfg.QUARTER == "Q4" else 1)
        years = {y for y in years if _year_sort_key(y) <= cap}
    return sorted(years, key=_year_sort_key)


def cagr(table, cap_to_period=True):
    """{row_key: CAGR fraction} spanning the table's earliest year through
    its most recent one (capped to the active period the same way
    sorted_years() is, so the end year moves with the reporting period
    rather than being hardcoded). A key is omitted if either endpoint is
    missing or the start value isn't positive (CAGR is undefined for a
    zero/negative base)."""
    years = sorted_years(table, cap_to_period=cap_to_period)
    if len(years) < 2:
        return {}
    start_year, end_year = years[0], years[-1]
    n = _year_sort_key(end_year) - _year_sort_key(start_year)
    if n <= 0:
        return {}
    out = {}
    for key, series in table.items():
        sv, ev = series.get(start_year), series.get(end_year)
        if sv is None or ev is None or sv <= 0:
            continue
        out[key] = (ev / sv) ** (1 / n) - 1
    return out


_REF_RE = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)")
_SUM_RE = re.compile(r"SUM\(\s*\$?([A-Z]{1,3})\$?(\d+)\s*:\s*\$?([A-Z]{1,3})\$?(\d+)\s*\)", re.IGNORECASE)
_ARITH_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
                ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)


def _safe_arith(expr, names):
    """Evaluate + - * / arithmetic over numbers and the given names only."""
    tree = ast.parse(expr, mode="eval")
    if not all(isinstance(n, _ARITH_NODES) for n in ast.walk(tree)):
        raise ValueError(f"unsupported formula: {expr}")
    return eval(compile(tree, "<formula>", "eval"), {"__builtins__": {}}, names)  # noqa: S307 - vetted AST


class _SheetValues:
    """Cell values of the workbook's first sheet: the value Excel saved for
    a formula cell when there is one, otherwise the formula worked out here.

    openpyxl can't recalculate, and a workbook it saves carries no saved
    results for ANY formula - so after append_year_from_data_engine writes
    the file, every formula cell (GDPI Growth, Retail Accretion, the
    Employees/Agents totals) would read as empty until re-saved in Excel.
    The workbook only uses cell references, + - * / and SUM(range), which
    are evaluated here instead."""

    def __init__(self, path):
        self.formulas = openpyxl.load_workbook(path).worksheets[0]
        self.values = openpyxl.load_workbook(path, data_only=True).worksheets[0]
        self._memo = {}

    def get(self, row, col):
        key = (row, col)
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = None  # guards a circular reference
        saved = self.values.cell(row, col).value
        raw = self.formulas.cell(row, col).value
        is_formula = isinstance(raw, str) and raw.startswith("=")
        if saved is not None:
            value = saved
        elif is_formula:
            value = self._evaluate(raw[1:])
        else:
            value = raw
        self._memo[key] = value
        return value

    def _evaluate(self, expr):
        def sum_range(m):
            c0, r0 = column_index_from_string(m[1].upper()), int(m[2])
            c1, r1 = column_index_from_string(m[3].upper()), int(m[4])
            total = 0.0
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    v = self.get(r, c)
                    if isinstance(v, (int, float)):
                        total += v
            return repr(total)

        expr = _SUM_RE.sub(sum_range, expr)
        names = {}

        def ref(m):
            name = f"_v{len(names)}"
            names[name] = self.get(int(m[2]), column_index_from_string(m[1]))
            return name

        expr = _REF_RE.sub(ref, expr).strip()
        if expr in names:  # a bare reference, e.g. a header "=D4" -> "FY19"
            return names[expr]
        if any(not isinstance(v, (int, float)) for v in names.values()):
            return None
        try:
            return _safe_arith(expr, names)
        except (ValueError, SyntaxError, ZeroDivisionError, TypeError):
            return None


def load_historical_tables(path=None):
    """Scans the sheet top-to-bottom for title -> header -> company/aggregate
    -row blocks, each terminated by a blank first cell or EOF (see
    _is_header_row for what makes a row a header).

    Returns {metric_title: {row_key: {year_label: value}}}. A title row is
    whatever non-empty text sits directly above the header row - trailing
    ':' is stripped. A row's key is theme.canonical_company() for a company
    row (any of the sheet's spelling variants resolve the same way they do
    everywhere else in the report), or its _AGGREGATE_ALIASES entry for a
    handful of recognized non-company rows (SAHI, Industry); anything else
    (state names, "Total", other aggregates) is silently skipped.

    Returns {} if the workbook doesn't exist yet (this data is optional -
    the slide that reads it just skips, per the report's usual
    no-data-no-placeholder rule) or is otherwise unreadable.
    """
    path = path or paths.HISTORICAL_TRENDS_WORKBOOK
    if not Path(path).is_file():
        return {}
    sheet = _SheetValues(path)
    ws = sheet.formulas
    # min_row/min_col anchored to the sheet's own used range (not hardcoded to
    # column A) - the real workbook's tables start at column B, with column A
    # entirely unused. Values via _SheetValues, so a formula cell with no
    # saved result (e.g. after append_year_from_data_engine) still reads.
    rows = [[sheet.get(r, c) for c in range(ws.min_column, ws.max_column + 1)]
            for r in range(ws.min_row, ws.max_row + 1)]

    tables = {}
    pending_title = None
    i, n = 0, len(rows)
    while i < n:
        row = rows[i]
        if _is_header_row(row):
            year_cols = []
            for ci in range(1, len(row)):
                v = row[ci]
                if v is None or not str(v).strip():
                    break
                year_cols.append((ci, str(v).strip()))
            title = (pending_title or "Untitled").strip().rstrip(":").strip()
            table = {}
            i += 1
            while i < n:
                r = rows[i]
                label = r[0] if r else None
                if label is None or not str(label).strip():
                    break
                key = theme.canonical_company(label) or _AGGREGATE_ALIASES.get(str(label).strip().lower())
                # An unrecognized label (e.g. "Total Health & PA") ends this
                # block rather than being silently skipped - otherwise the
                # loop reads straight through it into the NEXT table's title
                # and header rows, corrupting both (this table absorbs the
                # next one's data rows, and the next table's own header is
                # never seen by the outer loop, so it never gets recognized
                # at all). The outer loop's pending_title tracking already
                # handles skipping through non-header junk correctly.
                if key is None:
                    break
                table[key] = {yl: (r[ci] if ci < len(r) else None) for ci, yl in year_cols}
                i += 1
            tables[title] = table
            pending_title = None
            continue
        first = row[0] if row else None
        if first is None or not str(first).strip():
            pending_title = None
        else:
            pending_title = str(first).strip()
        i += 1
    return tables


def get_tables(titles, path=None):
    """Loads the workbook once and returns {requested_title: table_or_None}
    for each of `titles`, matched case/colon-insensitively against whatever
    title each block actually had in the sheet."""
    lookup = {t.lower(): tbl for t, tbl in load_historical_tables(path).items()}
    return {title: lookup.get(title.strip().rstrip(":").lower()) for title in titles}


def get_table(title, path=None):
    return get_tables([title], path)[title]


# ---------------------------------------------------------------------------
# The reporting period's value for each table, from a Data Engine
# ---------------------------------------------------------------------------

# Data Engine row carrying each table's value for the reporting period -
# verified: for FY25-26 Q4 every one of these equals this workbook's own FY26
# column. (slide, Meric 1), read from the CUR column.
HISTORICAL_DE_SOURCE = {
    "GDPI": (8, "Revenue Growth (GDPI)"),
    "Retail Health": (14, "Retail Revenue"),
    "Retail Health Accretion": (14, "Retail Accretion"),
    "ATS": (15, "Individual ATS"),
    "Average Productivity (per agent)": (15, "Average Productivity (per agent)"),
    "GWP": (31, "GWP"), "PBT": (31, "PBT"),
    "Combined Ratio": (32, "Combined Ratio"), "Loss Ratio": (32, "Loss Ratio"),
    "Expense Ratio": (33, "Expense Ratio"), "Expense of Management Ratio": (33, "Expense of Management Ratio"),
    "RI Ceding Ratio": (34, "RI Ceding to GWP Ratio"),
    "RI Commission to Ceding Ratio": (34, "RI Commission to RI Ceding"),
    "ROE": (35, "ROE (SAHI)"), "Solvency Ratio": (35, "Solvency Ratios"),
    "AUM": (36, "AUM (Overall)"), "Investment Yield": (36, "Investment Yield"),
    "AUM Shareholders": (37, "AUM -Shareholders"), "AUM Policyholders": (37, "AUM -Policyholders"),
    "Employees": (38, "Employees"), "Agents": (38, "Agents"),
    "Offices": (39, "No. of Offices"),
}


# Flow metrics whose Q1-Q3 point is annualized on the trend chart: per-agent
# productivity, and NL-31's gross yield (year-to-date income / investment,
# not annualized by the filers - Q1 prints ~1.8% against ~7.3% for a year).
ANNUALIZED_TABLES = {"Average Productivity (per agent)", "Investment Yield"}


def period_values(rows, title):
    """{row key: value} for one table, from a Data Engine's rows (current
    period, as-is - a Q1-Q3 figure is year to date). Keys follow
    load_historical_tables (company keys, "SAHI", "Industry").

    GDPI Growth is derived: each company's GDPI current/prior - 1, plus the
    SAHI and Industry "Growth %" rows. GDPI's own SAHI row is the sum of
    every SAHI company's GDPI. Tables in ANNUALIZED_TABLES are scaled to a
    full-year run rate (x 12 / months elapsed - Q1 x4) so the quarter point
    sits on the same scale as the full years before it, matching the
    reference deck. {} for a table with no Data Engine source."""
    from competitor_analysis.reporting import data
    out = {}
    if title == "GDPI Growth":
        for r in data.for_slide(rows, 8):
            cur, prior = data.num(r[data.CUR]), data.num(r[data.PRIOR])
            if r["Meric 1"] == "Growth %":
                key = {"Stand-alone Health sub Total": "SAHI", "Industry Total": "Industry"}.get(r["Company"])
                if key and cur is not None:
                    out[key] = cur
            elif r["Meric 1"] == "Revenue Growth (GDPI)":
                key = theme.canonical_company(r["Company"])
                if key and cur is not None and prior:
                    out[key] = cur / prior - 1
        return out
    source = HISTORICAL_DE_SOURCE.get(title)
    if source is None:
        return out
    slide, metric1 = source
    for r in data.for_slide(rows, slide):
        key = theme.canonical_company(r["Company"])
        if key and r["Meric 1"] == metric1 and data.num(r[data.CUR]) is not None:
            out.setdefault(key, data.num(r[data.CUR]))
    if title == "GDPI" and out:
        out["SAHI"] = sum(out.values())
    if title in ANNUALIZED_TABLES:
        scale = 12 / cfg.months_elapsed()
        out = {k: v * scale for k, v in out.items()}
    return out


def _industry_health_pa_total(rows):
    """Health + PA GDPI for the whole industry (GDPI table's "Total Health &
    PA" row) - the sum of Slide 10's Industry segments."""
    from competitor_analysis.reporting import data
    vals = [data.num(r[data.CUR]) for r in data.for_slide(rows, 10)
            if r["Meric 1"] == "Industry" and data.num(r[data.CUR]) is not None]
    return sum(vals) if vals else None


# ---------------------------------------------------------------------------
# Appending a completed year to the workbook (explicit command, Q4 only)
# ---------------------------------------------------------------------------

def append_year_from_data_engine(data_engine_path, path=None, dry_run=False, log=print):
    """Add the reporting year's column to every table in the workbook that
    doesn't have it yet, from a Q4 Data Engine. The active period
    (config.set_period) must be that Data Engine's Q4.

    Append-only: a table that already shows the year is left alone, and a
    table whose target column isn't empty is skipped - nothing already in
    the workbook is ever overwritten. Per row of a table being extended:
      - previous year's cell is a formula -> the same formula, shifted one
        column right (the GDPI Growth, Retail Accretion and total rows keep
        working as formulas);
      - otherwise -> the Data Engine value for that row.
    A table with any row that can't be filled is skipped whole, so a later
    run can still add it. Styles are copied from the previous year's column.

    Writes a timestamped backup under artifacts/output/ first. Returns
    {"added": [...titles], "skipped": {title: reason}, "backup": path}."""
    from competitor_analysis.reporting import data
    if cfg.QUARTER != "Q4":
        raise ValueError(f"only a Q4 Data Engine completes a year - the period is {cfg.FY} {cfg.QUARTER}")
    path = Path(path or paths.HISTORICAL_TRENDS_WORKBOOK)
    year = f"FY{cfg.fy_end_year() % 100:02d}"
    rows = data.load_rows(data_engine_path)
    sheet = _SheetValues(path)
    ws = sheet.formulas
    first_col = ws.min_column
    industry_total = _industry_health_pa_total(rows)

    added, skipped, writes = [], {}, []
    title = None
    r = ws.min_row
    while r <= ws.max_row:
        label = sheet.get(r, first_col)
        if not (isinstance(label, str) and label.strip().lower().startswith("particulars")):
            if label is not None and str(label).strip():
                title = str(label).strip().rstrip(":").strip()
            r += 1
            continue
        header_row, name = r, title or "Untitled"
        # This table's year columns: contiguous non-empty header cells.
        year_cols = []
        c = first_col + 1
        while c <= ws.max_column + 1 and sheet.get(header_row, c) not in (None, ""):
            year_cols.append(c)
            c += 1
        body = []
        r += 1
        # A table ends at a blank row, or at a row with nothing in its year
        # columns - the next table's title can follow with no blank line
        # between (GDPI runs straight into the "GDPI Growth" title).
        while (r <= ws.max_row and sheet.get(r, first_col) not in (None, "")
               and any(ws.cell(r, c).value not in (None, "") for c in year_cols)):
            body.append(r)
            r += 1
        labels = [str(sheet.get(header_row, c)).strip() for c in year_cols]
        if not year_cols:
            skipped[name] = "no year columns"
            continue
        if year in labels:
            continue  # already has the year - never touched
        last, target = year_cols[-1], year_cols[-1] + 1
        occupied = [rr for rr in [header_row] + body if ws.cell(rr, target).value not in (None, "")]
        if occupied:
            skipped[name] = f"column {get_column_letter(target)} not empty (rows {occupied})"
            continue
        values = period_values(rows, name)
        planned, missing, left_blank = [], [], []
        for rr in [header_row] + body:
            prev = ws.cell(rr, last).value
            dest = f"{get_column_letter(target)}{rr}"
            if isinstance(prev, str) and prev.startswith("="):
                planned.append((rr, Translator(prev, origin=f"{get_column_letter(last)}{rr}").translate_formula(dest)))
            elif rr == header_row:
                planned.append((rr, year))
            else:
                row_label = str(sheet.get(rr, first_col)).strip()
                key = theme.canonical_company(row_label) or _AGGREGATE_ALIASES.get(row_label.lower())
                value = values.get(key) if key else (
                    industry_total if name == "GDPI" and row_label.lower().startswith("total health") else None)
                if value is not None:
                    planned.append((rr, value))
                elif theme.canonical_company(row_label):
                    missing.append(row_label)  # a charted company row - required
                else:
                    left_blank.append(row_label)  # aggregate row with no Data Engine source
        if missing:
            skipped[name] = f"no {year} value for {missing}"
            continue
        if left_blank:
            log(f"  {name}: {year} left blank for {left_blank} (no Data Engine source - fill in by hand)")
        added.append(name)
        writes += [(rr, target, last, v) for rr, v in planned]
        for rr, v in planned:
            log(f"  {name}: {get_column_letter(target)}{rr} = {v!r}")

    backup = None
    if writes and not dry_run:
        backup = paths.OUTPUT_DIR / f"Historical_Trends_backup_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        paths.ensure_parent(backup)
        shutil.copy2(path, backup)
        for rr, target, last, v in writes:
            cell = ws.cell(rr, target)
            cell.value = v
            src = ws.cell(rr, last)
            if src.has_style:
                cell._style = copy.copy(src._style)
        ws.parent.save(path)
    return {"added": added, "skipped": skipped, "backup": backup, "year": year, "cells": len(writes)}
