"""Turn a form's resolved tables into clean markdown for the Gemini prompt.

Sources cached tables from pdf_cache.py (via forms.get_form_page), classifies
every table's columns along TWO axes - forms.classify_period_columns() (which
reporting period) and forms.classify_group_columns() (which named category -
a line-of-business segment, a fund, a premium-vs-policy-count split) - and
composes them at this call site into one column identity per resolved value.
Neither classifier is touched: classify_period_columns backs get_line_item()
(Slide 18) and is proven byte-identical; this module only calls it.

Never guesses which physical column holds a value. A column is only emitted
once its full identity is known - which period (when the table has one at
all), and which group (when the table has more than one). A period-bearing
column that can't be traced to a group in an otherwise multi-group table, or
any other identity gap, marks the WHOLE FORM unresolved - the caller must not
send it to the LLM; it surfaces at the awaiting_review gate instead. This is
what replaces the "rightmost column is the value" guess that produced a
plausible-looking but wrong number for NL-41's Employees/Agents table, and
the column collision that scrambled NL-34's per-state, per-segment figures.

Emits one markdown block PER TABLE, never merged across tables sharing a
page - a "Total" row in one block must never be confusable with a "Total"
row in another (NL-41's Office Information block vs. its Employees/Agents
block is the concrete case this guards against).

Human-readable period labels ALWAYS come from the run's own configured
period (config.cur_period_ending_str() etc.), never from table text - a
genuine period caption sometimes sits as free page text outside any
pdfplumber-detected table (confirmed for NL-41: "Statement as on <date>" is
a page-level caption between tables). When a table has no internal period
marker at all but genuine group-labeled data (NL-41's reconciliation
tables), the run's own period is borrowed for the label - a period-LABEL
fallback only, never a column-identity guess; which physical column holds
which group's value still comes from classify_group_columns, resolved the
same way as everywhere else in this module.

Decorative, page-wide rows (a form's title line, a bare section divider like
"STATES") are stripped before classification (forms.strip_decorative_rows) -
left in, one forward-fills as a single spurious header spanning every
column, indistinguishable from a genuine one, which is exactly what
mis-scoped NL-34's first table.
"""
import re

from competitor_analysis.extraction import forms
from competitor_analysis.extraction import pdf_cache
from competitor_analysis import config as cfg

_KIND_PRIORITY = ("cumulative", "snapshot", "quarter")

# Every IRDAI schedule this pipeline reads uses one of these spellings for its
# serial-number column - a project-wide convention already relied on
# elsewhere (forms.extract_nl36's first_label() skips it the same way). It is
# never a value, regardless of how numeric its own contents look.
_SERIAL_NO_RE = re.compile(r"^\s*s[lr]?\.?\s*no\.?\s*$", re.I)


def _period_label(kind, which):
    """Human-readable header label for `which` ('current'/'prior'), sourced
    from the run's own configured period - never from table text."""
    end = cfg.cur_period_ending_str() if which == "current" else cfg.prior_period_ending_str()
    if kind == "cumulative":
        return f"Upto {end}"
    if kind == "quarter":
        return f"Quarter {end}"
    if kind == "snapshot":
        return f"As at {end}"
    return end


def _pick_pair(events):
    """From classify_period_columns() events restricted to ONE group's (or
    the whole table's, if it has no group axis) columns, pick the single
    best (current_col, prior_col, kind): cumulative first if present, else
    snapshot, else quarter - matching the pipeline's own stated preference
    (gemini.PROMPT_TEMPLATE always wants the cumulative figure over the
    single-quarter one). The LAST matching event per (period, kind) wins,
    the same nearest-preceding-row convention _col_for already uses
    elsewhere in this package, so a stacked-block layout resolves to its
    final, most specific header."""
    by_key = {}
    for e in events:
        if e["period"] == "unknown":
            continue
        period, kind = e["period"].split("_", 1)
        by_key[(period, kind)] = e["col"]
    for kind in _KIND_PRIORITY:
        cur = by_key.get(("current", kind))
        if cur is not None:
            return cur, by_key.get(("prior", kind)), kind
    return None, None, None


def _group_for_column(group_events, col, before_row):
    """Nearest-preceding (row <= before_row) group label recorded for this
    exact column index, or None."""
    best, best_row = None, -1
    for e in group_events:
        if e["col"] == col and e["row"] <= before_row and e["row"] > best_row:
            best, best_row = e["label"], e["row"]
    return best


def _is_numeric_column(table, col_idx, header_rows=3):
    """True if most of a column's data-row cells (skipping the header rows)
    parse as a number. Distinguishes a genuine value column from a text/
    label column when no period marker exists to do it instead (NL-41's
    reconciliation tables: 'Office Information'/'Particulars' are label
    columns despite having their own header text, exactly like 'Number'/
    'Employees' do)."""
    numeric = total = 0
    for row in table[header_rows:]:
        if col_idx >= len(row) or not row[col_idx]:
            continue
        cell = str(row[col_idx]).strip()
        if cell in ("", "-"):
            continue
        total += 1
        if forms.parse_num(cell) is not None:
            numeric += 1
    return total > 0 and numeric / total >= 0.6


def _header_row_count(table, max_check=8):
    """How many leading rows are header rows, for a table with no period
    marker to anchor "nearest preceding header" on. A row counts as header
    until the first one where most of its non-empty cells parse as a
    number - i.e. the first genuine data row. No fixed row count: NL-41's
    two reconciliation tables have exactly one real header row each, not a
    conventional multi-row stack, and guessing a fixed depth (3, say) reads
    straight into their first data row."""
    for i, row in enumerate(table[:max_check]):
        cells = [c for c in row if c and str(c).strip() not in ("", "-")]
        if not cells:
            continue
        if sum(1 for c in cells if forms.parse_num(str(c)) is not None) / len(cells) >= 0.5:
            return i
    return max_check


def _resolve_columns(table):
    """Returns (columns, label_upto, header_end_row, reason). `columns` is
    a list of {"group": label or None, "kind": "cumulative"|"quarter"|
    "snapshot", "current_col": idx, "prior_col": idx or None}, one per
    resolved value column, ordered by current_col - filtered to columns
    whose data cells are actually mostly numeric, so a caption's incidental
    text (no period/group axis worth resolving at all, just prose) can't
    surface as an empty-but-present phantom column. `label_upto` is the
    leftmost column index belonging to ANY resolved column's group -
    including a group's own DISCARDED sibling (e.g. a "Health" group's
    quarter column, dropped in favor of its cumulative one) - so a losing
    sibling's value never leaks into the row-label text just because it
    wasn't the one chosen. `header_end_row` is the last row index that is
    itself header, not data - the render loop must not walk it as a value
    row. `reason`, if set, means a data-bearing column could not be safely
    named - the caller must treat the whole form as unresolved."""
    period_events = forms.classify_period_columns(table)
    group_events = forms.classify_group_columns(table)
    named_period_events = [e for e in period_events if e["period"] != "unknown"]
    distinct_groups = {e["label"] for e in group_events if not _SERIAL_NO_RE.match(e["label"])}
    has_group_axis = len(distinct_groups) >= 2

    if not named_period_events:
        # No period event anywhere to anchor "nearest preceding header" the
        # way the group-axis branch below does - so, lacking that anchor, a
        # group label is only trusted from the table's actual header rows
        # (_header_row_count). Without this, a data row's own cell value
        # (e.g. "212") reads as a column label too, since it doesn't match
        # a period phrase either.
        n_header = _header_row_count(table)
        header_group_events = [e for e in group_events if e["row"] < n_header]
        if not header_group_events:
            return [], None, 0, None  # no recognizable structure - e.g. a pure caption block
        cols_by_group = {}
        for e in header_group_events:
            if _SERIAL_NO_RE.match(e["label"]):
                continue
            cols_by_group.setdefault(e["label"], e["col"])
        kept = {label: col for label, col in cols_by_group.items() if _is_numeric_column(table, col, n_header)}
        columns = [{"group": label if has_group_axis else None, "kind": "snapshot",
                    "current_col": col, "prior_col": None} for label, col in kept.items()]
        label_upto = min(kept.values()) if kept else None
        return columns, label_upto, n_header - 1, None

    if not has_group_axis:
        cur_col, prior_col, kind = _pick_pair(named_period_events)
        if cur_col is None:
            return [], None, 0, "period phrasing found but no year fragment matched"
        if not _is_numeric_column(table, cur_col, max(e["row"] for e in named_period_events) + 1):
            return [], None, 0, None  # header text only, no real numeric data under it
        label_upto = min(e["col"] for e in named_period_events)
        header_end_row = max(e["row"] for e in named_period_events)
        return [{"group": None, "kind": kind, "current_col": cur_col,
                 "prior_col": prior_col}], label_upto, header_end_row, None

    by_group = {}
    for e in named_period_events:
        label = _group_for_column(group_events, e["col"], e["row"])
        if label is None or _SERIAL_NO_RE.match(label):
            return [], None, 0, (f"column {e['col']} resolves a period ({e['period']}) but no group, "
                                 f"in a table that otherwise has a group axis ({sorted(distinct_groups)})")
        by_group.setdefault(label, []).append(e)

    columns, kept_cols, header_end_row = [], [], 0
    for label, events in by_group.items():
        cur_col, prior_col, kind = _pick_pair(events)
        if cur_col is not None and _is_numeric_column(table, cur_col, max(e["row"] for e in events) + 1):
            columns.append({"group": label, "kind": kind, "current_col": cur_col, "prior_col": prior_col})
            kept_cols.extend(e["col"] for e in events)  # include this group's discarded siblings too
            header_end_row = max(header_end_row, max(e["row"] for e in events))
    if not columns:
        # Every candidate group either had no current value or turned out to
        # be prose, not data (e.g. a caption line coincidentally containing
        # "as on" text) - harmless, since nothing gets emitted either way,
        # never a resolution failure worth blocking the form over.
        return [], None, 0, None
    columns.sort(key=lambda c: c["current_col"])
    return columns, min(kept_cols), header_end_row, None


def render_table(table, heading):
    """One table -> one markdown block, or (None, reason) if this table's
    data columns could not be safely named."""
    table = forms.strip_decorative_rows(table)
    columns, label_upto, header_end_row, reason = _resolve_columns(table)
    if reason:
        return None, reason
    if not columns:
        return f"### {heading}\n\n*(no data columns found)*", None

    headers = ["Line Item"]
    for c in columns:
        cur_h = f"{c['group']} ({_period_label(c['kind'], 'current')})" if c["group"] else _period_label(c["kind"], "current")
        headers.append(cur_h)
        if c["prior_col"] is not None:
            prior_h = f"{c['group']} ({_period_label(c['kind'], 'prior')})" if c["group"] else _period_label(c["kind"], "prior")
        else:
            prior_h = f"{c['group']} (Prior)" if c["group"] else "Prior"
        headers.append(prior_h)

    lines = [f"### {heading}", "", "| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]

    for row in table[header_end_row + 1:]:
        label = " ".join(str(c).strip() for c in row[:label_upto] if c and str(c).strip())
        if not label:
            continue
        cells, any_value = [label], False
        for c in columns:
            cur_raw = row[c["current_col"]] if c["current_col"] < len(row) else None
            cur_val = str(cur_raw).strip() if cur_raw else ""
            if cur_val:
                any_value = True
            cells.append(cur_val)
            if c["prior_col"] is not None:
                prior_raw = row[c["prior_col"]] if c["prior_col"] < len(row) else None
                cells.append(str(prior_raw).strip() if prior_raw else "-")
            else:
                cells.append("*(not available in this filing)*")
        if not any_value:
            continue
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines), None


def build_markdown(pdf_path, form_tag):
    """All of a form's tables -> one markdown document, one block per table.

    Returns (markdown, unresolved). `unresolved` lists one reason string per
    table whose columns could not be safely named - if non-empty, the
    caller must NOT use `markdown`; the form surfaces at the
    awaiting_review gate instead of being sent to the LLM."""
    display_name, pattern = pdf_cache.FORM_PATTERNS[form_tag]
    fp, page_idxs = forms.get_form_page(pdf_path, pattern, all_matches=True)
    if fp is None or not fp.tables:
        return "", [f"{form_tag}: no table found"]

    blocks, unresolved = [], []
    multi = len(fp.tables) > 1
    for i, table in enumerate(fp.tables):
        heading = f"{form_tag} - {display_name}" + (f" (table {i + 1} of {len(fp.tables)})" if multi else "")
        md, reason = render_table(table, heading)
        if reason:
            unresolved.append(f"table {i + 1}: {reason}")
            continue
        blocks.append(md)
    return "\n\n".join(blocks), unresolved
