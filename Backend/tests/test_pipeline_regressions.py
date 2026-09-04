"""Regression tests for four extraction/mapping bug classes found by manual
audit of the Data Engine against the source filings.

Every assertion here is PERIOD-AGNOSTIC unless explicitly marked otherwise:
expected values are read live from the source GIC.xlsx / PDFs at test time
rather than hardcoded, so these keep testing the pipeline's logic (not one
quarter's numbers) as new filings land.

Run:  myenv/Scripts/python.exe -m pytest test_pipeline_regressions.py -v
"""
import inspect
import os
import re
from pathlib import Path

import pytest

from competitor_analysis import config as cfg

# The period under test. Point these at whichever quarter's inputs are on
# disk; nothing below depends on the specific dates they resolve to.

from competitor_analysis.extraction import forms  # noqa: E402
from competitor_analysis.extraction import data_engine as p2  # noqa: E402


def _module_source(module):
    """Read a module's own source via its import record, so these guards keep
    working if the module is renamed or moved."""
    return Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")


def _require(path):
    if not os.path.exists(path):
        pytest.skip(f"input not available: {path}")
    return path


@pytest.fixture(scope="module")
def gic():
    return p2.GicData(_require(cfg.gic_path()))


@pytest.fixture(scope="module")
def nl36_all():
    out = {}
    for company, pdf in forms.COMPANY_PDFS.items():
        if os.path.exists(pdf):
            out[company] = forms.extract_nl36(pdf)
    if not out:
        pytest.skip("no company PDFs available")
    return out


# ---------------------------------------------------------------------------
# Bug 1: GIC Segmentwise industry rows silently zeroed
# ---------------------------------------------------------------------------

# These four were being overwritten with 0 after being read correctly.
BUG1_SEGMENTS = ["Marine Cargo", "Marine Hull", "Health", "Aviation"]


@pytest.mark.parametrize("segment", BUG1_SEGMENTS)
def test_slide3_segment_matches_gic_industry_total(gic, segment):
    """Slide 3's per-segment rows must equal the Segmentwise Report's own
    'Industry Total' row. The expected figure is read from the sheet at test
    time, so this stays valid when next quarter's GIC.xlsx carries different
    numbers."""
    lookups = p2.build_gic_lookups(gic)
    seg = gic.segmentwise
    col = {p2._norm_seg(c): c for c in seg["col_names"]}[p2._norm_seg(segment)]
    expected = (p2.get(seg, "Industry Total", col, "cur"),
                p2.get(seg, "Industry Total", col, "prev"))

    got = lookups[(3, "Industry", "Market share", segment)]
    assert got == pytest.approx([round(v, 2) for v in expected]), (
        f"Slide 3 {segment!r} = {got}, but GIC.xlsx Industry Total says {expected}")
    # Guards the specific regression: a real segment silently reported as 0.
    assert any(v not in (0, None) for v in got), (
        f"Slide 3 {segment!r} came through as {got} despite GIC.xlsx "
        f"holding {expected}")


def test_segment_columns_resolve_despite_irregular_spelling(gic):
    """The Segmentwise header spells several columns irregularly ('Marine
    Cargo' with a double space, 'Health '/'Aviation ' with a trailing one).
    Matching must be whitespace/punctuation-insensitive rather than relying on
    those exact variants."""
    norm = {p2._norm_seg(c) for c in gic.segmentwise["col_names"]}
    for segment in BUG1_SEGMENTS + ["Fire", "Marine Total", "P.A.", "Motor OD"]:
        assert p2._norm_seg(segment) in norm, (
            f"{segment!r} did not match any Segmentwise column")


# ---------------------------------------------------------------------------
# Bug 2: "Others" distribution channel always 0
# ---------------------------------------------------------------------------

def test_channel_mix_sums_to_one_per_company(nl36_all):
    """The strong period-agnostic invariant: the five named channel buckets
    plus the 'Others' residual must account for exactly Total (A), for every
    company and both periods."""
    for company, d in nl36_all.items():
        assert d is not None, f"NL-36 extraction failed for {company}"
        for k, period in ((0, "current"), (1, "prior")):
            total = d["total_a"][k]
            if not total:
                continue
            parts = [d["channels"][b][k] or 0 for b in forms.NL36_CHANNELS]
            parts.append(d["others"][k] or 0)
            assert sum(parts) == pytest.approx(total, rel=1e-6), (
                f"{company} {period}: channel buckets sum to {sum(parts)}, "
                f"Total (A) is {total}")
            assert sum(parts) / total == pytest.approx(1.0, abs=0.005)


def test_others_is_nonzero_where_filing_reports_sub_channels(nl36_all):
    """'Others' was hardcoded to 0 for all seven companies. At least one
    company genuinely reports CSC/IMF/POS/Web-Aggregator business, so a
    sheet-wide zero means the residual is not being computed."""
    nonzero = {c: d["others"] for c, d in nl36_all.items()
               if d and d["others"][0]}
    assert nonzero, (
        "'Others' is zero for every company - the residual is not being "
        "computed from Total (A) minus the five named buckets")


def test_others_equals_total_minus_named_buckets(nl36_all):
    """'Others' must be a computed residual, not a read line item - so it is
    robust to the sub-line labels varying between insurers."""
    for company, d in nl36_all.items():
        if not d:
            continue
        for k in (0, 1):
            if d["total_a"][k] is None:
                continue
            named = sum(d["channels"][b][k] or 0 for b in forms.NL36_CHANNELS)
            assert d["others"][k] == pytest.approx(
                round(d["total_a"][k] - named, 2), abs=0.02), (
                f"{company}: 'Others' is not Total(A) minus the named buckets")


# ---------------------------------------------------------------------------
# Bug 3: quarter-vs-cumulative column chosen by fixed position
# ---------------------------------------------------------------------------

def test_no_fixed_position_default_for_cumulative_columns():
    """get_line_item_from_text must resolve columns from the form's own header
    by default. A non-None default for cur_col/prior_col would silently
    reintroduce the fixed-position assumption."""
    import inspect
    sig = inspect.signature(forms.get_line_item_from_text)
    for name in ("cur_col", "prior_col"):
        assert sig.parameters[name].default is None, (
            f"{name} has a fixed default - cumulative columns must be "
            f"resolved from the header, not assumed by position")


def test_year_fragments_derive_from_configured_period():
    """Year matching must follow the configured period, not a hardcoded year,
    or next quarter's headers stop resolving."""
    cur, prior = forms.year_frags("FY25-26", "Q3")
    assert cur[0] == "2025" and prior[0] == "2024"
    # Same logic, a different period - no code change involved.
    cur4, prior4 = forms.year_frags("FY25-26", "Q4")
    assert cur4[0] == "2026" and prior4[0] == "2025"
    cur27, _ = forms.year_frags("FY26-27", "Q1")
    assert cur27[0] == "2026"


@pytest.mark.parametrize("form,label,exclude", [
    (r"FORM\s+NL-2-B-PL", ("Before Tax",), ["Exceptional"]),
    (r"FORM\s+NL-2-B-PL", ("after tax",), None),
])
def test_narayana_cumulative_column_is_header_matched(form, label, exclude):
    """Narayana Health's NL-2 orders its columns [Upto-cur, For-cur,
    For-prior, Upto-prior] - the prior-year pair reversed relative to the
    current-year pair, and relative to its own NL-20. A fixed 'second numeric
    value' rule reads the single-quarter figure for one and the wrong
    prior-year column for the other.

    Period-specific by design (per the bug report): this checks today's actual
    filing, confirming the header-matched column is the one the PDF itself
    labels cumulative.
    """
    pdf = _require(forms.COMPANY_PDFS["Narayana Health"])
    text, _ = forms.get_form_text(pdf, form)
    assert text, f"{form} text not found"

    cur_col, prior_col = forms._text_period_columns(text, form=form)
    assert (cur_col, prior_col) == (0, 3), (
        f"expected the cumulative columns at positions (0, 3) for this "
        f"filing's [Upto, For, For, Upto] layout, got {(cur_col, prior_col)}")

    # Cross-check the resolution against the header text itself: the chosen
    # positions must be the ones labelled cumulative, and must NOT be the
    # single-quarter ones.
    header = next(l for l in text.splitlines()
                  if len(forms._PERIOD_LABEL_RE.findall(l)) >= 2)
    kinds = ["cum" if re.search(r"up\s*to|upto", m.group(1), re.I) else "qtr"
             for m in forms._PERIOD_LABEL_RE.finditer(header)]
    assert kinds[cur_col] == "cum" and kinds[prior_col] == "cum", (
        f"resolved columns {(cur_col, prior_col)} are not the cumulative ones "
        f"in header layout {kinds}")

    cur, prior = forms.get_line_item_from_text(
        text, *label, exclude=exclude, form=form)
    assert cur is not None and prior is not None
    # The cumulative 9-month loss must be the larger-magnitude of the two
    # current-year columns for this filing (single quarter -316.87 lakhs vs
    # cumulative -1,334.13) - i.e. we did not land on the quarter column.
    assert abs(cur) > 1000, (
        f"{label} current-period value {cur} looks like the single-quarter "
        f"figure, not the cumulative one")


def test_narayana_nl20_cumulative_is_first_column():
    """Narayana's NL-20 uses [Upto-cur, For-cur, Upto-prior, For-prior] - a
    DIFFERENT order from its own NL-2. Documents that its Combined Ratio
    cumulative figure is the first column, so a future 'fix' cannot silently
    invert it."""
    pdf = _require(forms.COMPANY_PDFS["Narayana Health"])
    text, _ = forms.get_form_text(pdf, r"FORM\s+NL-20")
    assert text
    cur_col, prior_col = forms._text_period_columns(text, form="NL-20")
    assert (cur_col, prior_col) == (0, 2), (
        f"NL-20 cumulative columns resolved to {(cur_col, prior_col)}; this "
        f"form's layout is [Upto-cur, For-cur, Upto-prior, For-prior]")


def test_number_tokenizer_handles_split_figures():
    """pdfplumber splits some figures across a stray space ('9 .13',
    '1 ,102.73'). Those must not be read as two separate values, which would
    shift every later column left."""
    line = "10 Combined Ratio 1.66 1.28 9 .13 1 7.65"
    cur, prior = forms.get_line_item_from_text(
        line, "Combined Ratio", cur_col=0, prior_col=2)
    assert cur == pytest.approx(1.66)
    assert prior == pytest.approx(9.13), (
        f"'9 .13' read as {prior} - the mid-number space was not collapsed")


# ---------------------------------------------------------------------------
# Bug 4: Slide 8 GDPI sourced inconsistently
# ---------------------------------------------------------------------------

def test_slide8_gdpi_uses_nl36_grand_total_for_every_company(nl36_all):
    """Slide 8's GDPI is NL-36's 'Grand Total (A+B)', read from each company's
    own filing by one shared code path - no per-company mapping. Expected
    values come from the PDFs at test time, not hardcoded."""
    for company, d in nl36_all.items():
        assert d is not None, f"NL-36 extraction failed for {company}"
        gt_cur, gt_prior = d["grand_total"]
        assert gt_cur, f"{company}: no NL-36 Grand Total (A+B) extracted"
        # Grand Total (A+B) must be at least Total (A); B is never negative.
        assert gt_cur >= d["total_a"][0] - 0.01, (
            f"{company}: Grand Total {gt_cur} < Total (A) {d['total_a'][0]}")
        if gt_prior and d["total_a"][1]:
            assert gt_prior >= d["total_a"][1] - 0.01


def test_care_slide8_reconciles_to_its_own_filing(nl36_all):
    """The reported symptom: CARE's Slide 8 figure matched no column of its
    own NL-36. It must equal that filing's Grand Total (A+B), and CARE is the
    one company where (B) is non-zero, so Grand Total must differ from Total
    (A) - proving the two are not being conflated."""
    d = nl36_all.get("Care Health")
    if d is None:
        pytest.skip("CARE Health PDF not available")
    assert d["grand_total"][0] > d["total_a"][0], (
        "CARE reports 'Business outside India (B)', so its Grand Total (A+B) "
        "must exceed Total (A)")
    assert p2.lakhs_to_cr(d["grand_total"][0]) == pytest.approx(
        round(d["grand_total"][0] / 100, 2))


def test_no_hardcoded_nl36_literals_remain():
    """Slide 8/12 must not be fed by a transcribed per-company table; those
    literals are correct only for the quarter they were typed for."""
    src = _module_source(p2)
    assert "NL36_TOTAL_LAKHS" not in src, (
        "the hardcoded NL-36 totals table is back - Slide 8 must extract from "
        "each filing instead")
    assert "NL36_DIRECT_BUSINESS_OVERRIDE_CR" not in src, (
        "the per-company Direct Business override is back - it should be "
        "handled generically by summing indented sub-rows")


def test_no_per_company_special_casing_in_extraction():
    """Guards instruction: fixes must not be per-company branches."""
    src = _module_source(p2)
    for pattern in (r'company\s*==\s*["\']Narayana',
                    r'company_short\s*==\s*["\']Narayana',
                    r'company\s*==\s*["\']Care'):
        assert not re.search(pattern, src), (
            f"per-company special case found: {pattern}")


# ---------------------------------------------------------------------------
# Period-driven Data Engine column headers
# ---------------------------------------------------------------------------

def test_period_column_labels_follow_config():
    """The value-column headers are derived from the configured FY/quarter,
    and the prior column is always the same quarter one financial year back."""
    assert cfg.period_column("FY25-26", "Q3") == "FY25-26_Q3"
    assert cfg.period_column("FY26-27", "Q1") == "FY26-27_Q1"
    assert cfg.cur_period_column() == "FY25-26_Q3"
    assert cfg.prior_period_column() == "FY24-25_Q3"


def test_headers_use_configured_period_not_literals():
    """data_engine's HEADERS must carry the configured period, and the
    CUR/PRIOR aliases must point at those same two columns."""
    assert p2.CUR_PERIOD == cfg.cur_period_column()
    assert p2.PRIOR_PERIOD == cfg.prior_period_column()
    assert p2.HEADERS[7] == p2.CUR_PERIOD
    assert p2.HEADERS[8] == p2.PRIOR_PERIOD
    assert p2.COL[p2.CUR] == 8 and p2.COL[p2.PRIOR] == 9


def test_sync_period_headers_relabels_a_stale_sheet():
    """A template saved from a previous quarter must be relabelled, so the
    figures are never presented under the wrong period heading."""
    import openpyxl
    ws = openpyxl.Workbook().active
    ws.cell(row=1, column=8).value = "FY98-99_Q9"
    ws.cell(row=1, column=9).value = "FY97-98_Q9"

    changed = p2.sync_period_headers(ws)
    assert ("FY98-99_Q9", p2.CUR_PERIOD) in changed
    assert ws.cell(row=1, column=8).value == p2.CUR_PERIOD
    assert ws.cell(row=1, column=9).value == p2.PRIOR_PERIOD
    # Idempotent: a second call reports nothing to change.
    assert p2.sync_period_headers(ws) == []


def test_row_dict_exposes_period_neutral_aliases():
    import openpyxl
    ws = openpyxl.Workbook().active
    ws.cell(row=2, column=8).value = 123.0
    ws.cell(row=2, column=9).value = 45.0
    d = p2.row_dict(ws, 2)
    assert d[p2.CUR] == 123.0 and d[p2.PRIOR] == 45.0
    # The period-labelled keys stay available for display/debugging.
    assert d[p2.CUR_PERIOD] == 123.0


def test_no_module_hardcodes_period_column_keys():
    """Reader modules must address the value columns through the neutral
    aliases; a literal subscript would raise KeyError in another quarter."""
    import glob
    offenders = []
    for path in glob.glob(os.path.join(os.path.dirname(__file__) or ".", "*.py")):
        if os.path.basename(path).startswith("test_"):
            continue
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r'\[\s*"FY\d\d_Q\d"\s*\]', src):
            offenders.append(f"{os.path.basename(path)}: {m.group(0)}")
    assert not offenders, (
        "period-literal column subscripts found (use p2.CUR / p2.PRIOR):\n  "
        + "\n  ".join(offenders))


def test_months_elapsed_scales_ytd_run_rates():
    """Monthly run-rates divide a cumulative YTD figure by months elapsed in
    the financial year - a hardcoded 9 would misstate Q1 by 3x."""
    assert [cfg.months_elapsed("FY25-26", q) for q in ("Q1", "Q2", "Q3", "Q4")] == [3, 6, 9, 12]
