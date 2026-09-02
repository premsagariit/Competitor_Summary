"""
FY26 Q3 Competition Analysis report generator (Phase 3).

Reads Data_Engine_UI.xlsx (populated by Phase 2) and produces a PowerPoint
deck matching the structure of "Competition Summary FY25 1.pdf" - same
slide numbering/categories (verified 1:1 against that reference deck), same
~6 recurring chart archetypes (report_charts.py), same visual chrome
(report_theme.py).

Known, deliberate simplifications vs. the FY25 reference deck (see the
Phase 3 plan for the reasoning):
  - "Historical Trends" (Slides 27-31) render as a 2-period (FY25_Q3 vs
    FY26_Q3) grouped-column comparison, not an 8-year line - the Data
    Engine only carries 2 periods.
  - Slide 16 (Geographic zones) is derived from Slide 17's named-state data
    via a standard state-to-zone mapping, not extracted directly - North/
    West/South are covered, East/Central fall inside an "Others
    (unclassified)" bucket since no East/Central state is individually
    broken out by current source disclosures. Slide 20's Average Claim Size
    and No. of claims to policies are best-effort estimates (not
    independently GT-verified) rather than raw extracted figures - both
    documented on their slide's footnote and in the Glossary.
  - Insight bullets are rule-based (leader/laggard + YoY direction), not
    human analyst commentary - meant as a first-pass, fully editable draft.
  - Narayana Health and Galaxy Health are included wherever the Data Engine
    has real data for them, even on slides where the FY25 reference deck
    excluded both (they were immaterial/newer at that time).
"""
import os

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE

import report_theme as theme
import report_charts as charts
import report_data as data

OUT_PATH = os.path.join("output", "Competition_Summary_FY26_Q3.pptx")

CONTENT_LEFT = Inches(0.3)
CONTENT_WIDTH = Inches(7.6)
PANEL_TOP0 = Inches(1.55)
PANEL_BOTTOM = Inches(9.5)
INSIGHT_TOP = Inches(9.6)
INSIGHT_HEIGHT = Inches(1.35)

NUMFMT = {"percent": "0.0%", "money": "#,##0", "ratio": "0.00", "count": "#,##0"}


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def new_content_slide(prs, title, page_no):
    slide = theme.new_slide(prs)
    theme.add_header(slide)
    theme.add_title_badge(slide, title)
    theme.add_footer(slide, page_no)
    return slide


def panel_slots(n, top=PANEL_TOP0, bottom=PANEL_BOTTOM, gap=Inches(0.15)):
    total_gap = gap * (n - 1) if n > 1 else 0
    h = (bottom - top - total_gap) / n
    slots = []
    y = top
    for _ in range(n):
        slots.append((y, h))
        y = y + h + gap
    return slots


def panel_label(slide, text, left, top, width):
    tb = slide.shapes.add_textbox(left, top, width, Inches(0.25))
    p = tb.text_frame.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    r.font.size = Pt(11)
    r.font.bold = True


def disp_names(keys):
    return [theme.COMPANY_DISPLAY_NAME.get(k, k) for k in keys]


def leader_laggard_bullets(keys, current, prior, kind, metric_name, higher_is_better=True, limit=3):
    """Rule-based, auto-generated observations - explicitly not a
    substitute for human analyst commentary; the output is a real .pptx so
    these are meant to be edited/replaced."""
    names = disp_names(keys)
    pairs = [(n, c, p) for n, c, p in zip(names, current, prior) if c is not None]
    if not pairs:
        return []
    scale = 100 if kind == "percent" else 1
    unit = "%" if kind == "percent" else ("x" if kind == "ratio" else "")
    bullets = []
    top = max(pairs, key=lambda x: x[1]) if higher_is_better else min(pairs, key=lambda x: x[1])
    word = "highest" if higher_is_better else "lowest"
    bullets.append(f"{top[0]} has the {word} {metric_name} at {top[1]*scale:,.1f}{unit}")
    deltas = [(n, c, p, c - p) for n, c, p in pairs if p is not None]
    if deltas:
        best = max(deltas, key=lambda x: x[3])
        worst = min(deltas, key=lambda x: x[3])
        if best[3] > 1e-9:
            bullets.append(f"{best[0]} improved the most YoY (+{best[3]*scale:,.1f}{unit})")
        if worst[3] < -1e-9 and worst[0] != best[0]:
            bullets.append(f"{worst[0]} declined the most YoY ({worst[3]*scale:,.1f}{unit})")
    return bullets[:limit]


def metric_panels_slide(prs, rows, slide_no, title, page_no, panels, footnote=None, company_data=None,
                         key_field="Company"):
    """Generic multi-panel slide: each `panels` entry is
    {"title","metric1","metric2","kind","mode":"grouped"|"single","higher_is_better"}."""
    slide = new_content_slide(prs, title, page_no)
    cdata = company_data if company_data is not None else data.by_company(rows, slide_no, theme.canonical_company, key_field)
    bottom = PANEL_BOTTOM - (Inches(0.25) if footnote else Inches(0))
    slots = panel_slots(len(panels), bottom=bottom)
    all_bullets = []
    for (top, h), panel in zip(slots, panels):
        keys, prior, current = data.metric_series(cdata, panel["metric1"], panel.get("metric2"))
        names = disp_names(keys)
        nf = NUMFMT[panel["kind"]]
        panel_label(slide, panel["title"], CONTENT_LEFT, top, CONTENT_WIDTH)
        chart_top, chart_h = top + Inches(0.28), h - Inches(0.28)
        if panel.get("mode", "grouped") == "single":
            charts.single_series_column(slide, names, current, CONTENT_LEFT, chart_top, CONTENT_WIDTH, chart_h,
                                         number_format=nf)
        else:
            charts.grouped_column(slide, names, prior, current, CONTENT_LEFT, chart_top, CONTENT_WIDTH, chart_h,
                                   number_format=nf)
        all_bullets += leader_laggard_bullets(keys, current, prior, panel["kind"], panel["title"],
                                               panel.get("higher_is_better", True))
    if footnote:
        theme.add_note(slide, footnote, CONTENT_LEFT, PANEL_BOTTOM - Inches(0.22), CONTENT_WIDTH, Inches(0.3))
    theme.add_insight_panel(slide, all_bullets[:4] or ["No FY26 Q3 data available for this metric group."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


# ---------------------------------------------------------------------------
# Title / TOC / Glossary
# ---------------------------------------------------------------------------

def title_slide(prs):
    slide = theme.new_slide(prs)
    band = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(3.0), theme.SLIDE_WIDTH, Inches(1.1))
    band.fill.solid()
    band.fill.fore_color.rgb = theme.NAVY
    band.line.fill.background()
    tf = band.text_frame
    tf.margin_left = Inches(0.3)
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = "Competition Analysis"
    r.font.size = Pt(32)
    r.font.bold = True
    r.font.color.rgb = theme.WHITE

    sub = slide.shapes.add_textbox(Inches(3.5), Inches(9.8), Inches(4.3), Inches(1))
    stf = sub.text_frame
    p1 = stf.paragraphs[0]
    p1.alignment = PP_ALIGN.RIGHT
    r1 = p1.add_run()
    r1.text = "Executive Summary"
    r1.font.size = Pt(20)
    r1.font.bold = True
    r1.font.color.rgb = theme.NAVY
    p2 = stf.add_paragraph()
    p2.alignment = PP_ALIGN.RIGHT
    r2 = p2.add_run()
    r2.text = "Period ending December 2025 (FY26 Q3)"
    r2.font.size = Pt(13)
    r2.font.color.rgb = theme.NAVY
    return slide


TOC_ENTRIES = [
    ("Overall Industry & Market share", "3-7"), ("Revenue (Segment, Channel, Geographical mix)", "8-17"),
    ("Income statement", "18"), ("Key metrics", "19-23"), ("Investment portfolio", "24-26"),
    ("Historical trends", "27-31"), ("AUM", "32-33"), ("Distribution footprints", "34-35"),
]


def toc_slide(prs):
    slide = new_content_slide(prs, "Table of Contents", 1)
    rows_n = len(TOC_ENTRIES) + 1
    gf = slide.shapes.add_table(rows_n, 2, CONTENT_LEFT, Inches(1.7), CONTENT_WIDTH, Inches(0.5) * rows_n)
    table = gf.table
    table.columns[0].width = Inches(6.0)
    table.columns[1].width = Inches(1.6)
    table.cell(0, 0).text = "Contents"
    table.cell(0, 1).text = "Slide No."
    for c in (table.cell(0, 0), table.cell(0, 1)):
        c.fill.solid()
        c.fill.fore_color.rgb = theme.BLUE
        for p in c.text_frame.paragraphs:
            for r in p.runs:
                r.font.color.rgb = theme.WHITE
                r.font.bold = True
    for i, (label, pages) in enumerate(TOC_ENTRIES, start=1):
        table.cell(i, 0).text = label
        table.cell(i, 1).text = pages
        table.cell(i, 1).text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
    return slide


def glossary_slide(prs, page_no):
    slide = new_content_slide(prs, "Glossary", page_no)
    lines = [
        "Above information is as per public disclosures available on IRDAI/company websites for the quarter ended 31 Dec 2025 (FY26 Q3), compared to the quarter ended 31 Dec 2024 (FY25 Q3).",
        "SAHI = Stand-alone Health Insurer. GDPI = Gross Direct Premium Income. GWP = Gross Written Premium. NWP = Net Written Premium.",
        "“Historical Trends” slides in this FY26 Q3 report show a 2-period (FY25 Q3 vs FY26 Q3) year-on-year comparison, not multi-year history - the underlying data pipeline currently carries only these 2 periods.",
        "Slide 16's zone split (North/West/South) is derived from Slide 17's named-state data using the standard Ministry of Home Affairs zonal convention; East and Central exposure isn't separately identifiable from current source disclosures and is included in “Others (unclassified)”.",
        "Slide 20's Average Claim Size and No. of claims to No. of policies are best-effort estimates, not independently GT-verified - Average Claim Size (Claims Incurred / Claims Settled) has previously landed ~10% off ground truth for at least one company; review before external use.",
        "Company short names: NBHI = Niva Bupa Health Insurance, STAR = Star Health & Allied Insurance, CARE = Care Health Insurance, CIGNA = ManipalCigna Health Insurance, ABHI = Aditya Birla Health Insurance, Narayana = Narayana Health Insurance, Galaxy = Galaxy Health Insurance.",
        "This report was generated automatically from Data_Engine_UI.xlsx; insight bullets are rule-based auto-generated observations and should be reviewed before external use.",
    ]
    tb = slide.shapes.add_textbox(CONTENT_LEFT, Inches(1.7), CONTENT_WIDTH, Inches(6))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        r = p.add_run()
        r.text = f"➢  {line}"
        r.font.size = Pt(11)
        p.space_after = Pt(10)
    return slide


# ---------------------------------------------------------------------------
# Slides 3-7: Overall Industry & Market share
# ---------------------------------------------------------------------------

SEGMENT_MIX_LINES = ["Fire", "Marine Total", "Engineering", "Motor Total", "Health",
                     "Aviation", "Liability", "P.A.",
                     "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)"]
SEGMENT_MIX_DISPLAY = {"Marine Total": "Marine", "Motor Total": "Motor",
                        "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)": "Others"}


def _doughnut_slide_by_metric2(prs, rows, slide_no, title, page_no, company_key, metric2_list,
                                colors_fn, unit_label="INR Crores", top_note=None):
    slide = new_content_slide(prs, title, page_no)
    slide_rows = data.for_slide(rows, slide_no)
    by_m2 = {r["Metric 2"]: (data.num(r["FY26_Q3"]), data.num(r["FY25_Q3"]))
             for r in slide_rows if r["Company"] == company_key}
    labels = [SEGMENT_MIX_DISPLAY.get(m, m) for m in metric2_list if m in by_m2]
    raw_labels = [m for m in metric2_list if m in by_m2]
    current = [by_m2[m][0] for m in raw_labels]
    prior = [by_m2[m][1] for m in raw_labels]
    colors = colors_fn(raw_labels)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(3.6))
    charts.doughnut_pair(slide, "FY25_Q3", "FY26_Q3", labels, prior, current, colors,
                          CONTENT_LEFT + Inches(0.1), PANEL_TOP0 + Inches(0.1), CONTENT_WIDTH - Inches(0.2),
                          Inches(3.4), unit_label=unit_label)
    bullets = leader_laggard_bullets(raw_labels, current, prior, "money", title)
    if top_note:
        bullets = [top_note] + bullets
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_03(prs, rows):
    slide = new_content_slide(prs, "GI Industry", 3)
    slide_rows = data.for_slide(rows, 3)
    by_m2 = {r["Metric 2"]: (data.num(r["FY26_Q3"]), data.num(r["FY25_Q3"])) for r in slide_rows}
    own_labels = ["Private", "Public", "SAHI", "Specialized Insurer"]
    own_cur = [by_m2[m][0] for m in own_labels]
    own_pri = [by_m2[m][1] for m in own_labels]
    own_colors = [theme.SEGMENT_COLORS[m] for m in own_labels]
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(3.2))
    charts.doughnut_pair(slide, "FY25_Q3", "FY26_Q3", own_labels, own_pri, own_cur, own_colors,
                          CONTENT_LEFT + Inches(0.1), PANEL_TOP0 + Inches(0.1), CONTENT_WIDTH - Inches(0.2),
                          Inches(3.0), unit_label="INR Crores")

    seg_top = PANEL_TOP0 + Inches(3.35)
    seg_present = [m for m in SEGMENT_MIX_LINES if m in by_m2 and by_m2[m][0]]
    seg_labels = [SEGMENT_MIX_DISPLAY.get(m, m) for m in seg_present]
    seg_cur = [by_m2[m][0] for m in seg_present]
    seg_pri = [by_m2[m][1] for m in seg_present]
    theme.add_panel_frame(slide, CONTENT_LEFT, seg_top, CONTENT_WIDTH, Inches(3.4))
    charts.doughnut_pair(slide, "FY25_Q3", "FY26_Q3", seg_labels, seg_pri, seg_cur,
                          theme.FALLBACK_SERIES_COLORS[:len(seg_labels)],
                          CONTENT_LEFT + Inches(0.1), seg_top + Inches(0.1), CONTENT_WIDTH - Inches(0.2),
                          Inches(3.2), unit_label="INR Crores")
    bullets = leader_laggard_bullets(own_labels, own_cur, own_pri, "money", "market share")
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_04(prs, rows):
    slide = new_content_slide(prs, "Health Industry (Inc. PA & Travel)", 4)
    slide_rows = data.for_slide(rows, 4)
    by_m2 = {r["Metric 2"]: (data.num(r["FY26_Q3"]), data.num(r["FY25_Q3"])) for r in slide_rows}
    own_labels = ["Private", "Public", "SAHI"]
    own_cur = [by_m2[m][0] for m in own_labels]
    own_pri = [by_m2[m][1] for m in own_labels]
    own_colors = [theme.SEGMENT_COLORS[m] for m in own_labels]
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(3.2))
    charts.doughnut_pair(slide, "FY25_Q3", "FY26_Q3", own_labels, own_pri, own_cur, own_colors,
                          CONTENT_LEFT + Inches(0.1), PANEL_TOP0 + Inches(0.1), CONTENT_WIDTH - Inches(0.2),
                          Inches(3.0), unit_label="INR Crores")

    mix_labels = ["Health-Retail", "Health-Group", "Health-Government schemes", "Overseas Medical", "P.A."]
    mix_cur = [by_m2[m][0] for m in mix_labels]
    mix_pri = [by_m2[m][1] for m in mix_labels]
    seg_top = PANEL_TOP0 + Inches(3.35)
    theme.add_panel_frame(slide, CONTENT_LEFT, seg_top, CONTENT_WIDTH, Inches(3.4))
    charts.doughnut_pair(slide, "FY25_Q3", "FY26_Q3", mix_labels, mix_pri, mix_cur,
                          theme.FALLBACK_SERIES_COLORS[:len(mix_labels)],
                          CONTENT_LEFT + Inches(0.1), seg_top + Inches(0.1), CONTENT_WIDTH - Inches(0.2),
                          Inches(3.2), unit_label="INR Crores")
    bullets = leader_laggard_bullets(own_labels, own_cur, own_pri, "money", "Health industry share")
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_05(prs, rows):
    slide = new_content_slide(prs, "SAHI Market", 5)
    slide_rows = data.for_slide(rows, 5)
    by_company = {}
    for r in slide_rows:
        key = theme.canonical_company(r["Metric 2"])
        if key:
            by_company[key] = (data.num(r["FY26_Q3"]), data.num(r["FY25_Q3"]))
    keys = [k for k in data.COMPANY_ORDER if k in by_company]
    cur = [by_company[k][0] for k in keys]
    pri = [by_company[k][1] for k in keys]
    colors = [theme.COMPANY_COLORS[k] for k in keys]
    names = disp_names(keys)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(3.4))
    charts.doughnut_pair(slide, "FY25_Q3", "FY26_Q3", names, pri, cur, colors,
                          CONTENT_LEFT + Inches(0.1), PANEL_TOP0 + Inches(0.1), CONTENT_WIDTH - Inches(0.2),
                          Inches(3.2), unit_label="INR Crores")

    change_top = PANEL_TOP0 + Inches(3.55)
    panel_label(slide, "Market Share Change (pp)", CONTENT_LEFT, change_top, CONTENT_WIDTH)
    theme.add_panel_frame(slide, CONTENT_LEFT, change_top + Inches(0.3), CONTENT_WIDTH, Inches(2.6))
    totals_cur = sum(v for v in cur if v)
    totals_pri = sum(v for v in pri if v)
    changes = {}
    for k, c, p in zip(keys, cur, pri):
        if c is not None and p is not None and totals_cur and totals_pri:
            changes[k] = (c / totals_cur - p / totals_pri) * 100
    charts.change_dot_chart(slide, keys, changes, CONTENT_LEFT + Inches(0.2), change_top + Inches(0.45),
                             CONTENT_WIDTH - Inches(0.4), Inches(2.3))
    bullets = leader_laggard_bullets(keys, cur, pri, "money", "SAHI GDPI")
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


SEG5 = ["Health-Retail", "Health-Group", "Health-Government schemes", "Overseas Medical", "P.A."]


def slide_06(prs, rows):
    slide = new_content_slide(prs, "Segment-wise: Health & PA", 6)
    cdata = data.metric2_by_group(rows, 6, "Company")
    groups = [g for g in ("SAHI Market", "Pvt GI", "Public GI") if g in cdata]
    series = {seg: [cdata[g].get(seg, (None, None))[0] for g in groups] for seg in SEG5}
    colors = theme.series_colors_for(SEG5)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(4.0))
    charts.stacked_100_column(slide, groups, series, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(3.5), pct100=False, number_format="#,##0")
    theme.add_note(slide, "FY26 Q3 absolute GDPI (Rs. Crore) by segment, for SAHI / Private GI / Public GI as groups.",
                    CONTENT_LEFT, PANEL_TOP0 + Inches(4.05), CONTENT_WIDTH, Inches(0.3))
    theme.add_insight_panel(slide, ["Health-Group is the largest segment for both Private and Public GI players.",
                                     "SAHI's mix skews more heavily to Health-Retail than Private/Public GI."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_07(prs, rows):
    slide = new_content_slide(prs, "Segment wise SAHI's share", 7)
    cdata = data.metric2_by_group(rows, 7, "Company", canonical_fn=theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(keys)
    series = {seg: [cdata[k].get(seg, (None, None))[0] for k in keys] for seg in SEG5}
    colors = theme.series_colors_for(SEG5)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(4.0))
    charts.stacked_100_column(slide, names, series, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(3.5), pct100=False, number_format="#,##0")
    theme.add_note(slide, "FY26 Q3 absolute GDPI (Rs. Crore) by segment, per SAHI company.",
                    CONTENT_LEFT, PANEL_TOP0 + Inches(4.05), CONTENT_WIDTH, Inches(0.3))
    theme.add_insight_panel(slide, ["Retail remains the dominant segment across most SAHI players."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


# ---------------------------------------------------------------------------
# Slides 8-17: Revenue (Segment, Channel, Geographical mix)
# ---------------------------------------------------------------------------

def slide_08(prs, rows):
    cdata = data.by_company(rows, 8, theme.canonical_company)
    keys, prior, current = data.metric_series(cdata, "Revenue Growth (GDPI)", None)
    slide = new_content_slide(prs, "Revenue Growth (GDPI)", 8)
    panel_label(slide, "Per-company GDPI (Rs. Crore)", CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH)
    charts.grouped_column(slide, disp_names(keys), prior, current, CONTENT_LEFT, PANEL_TOP0 + Inches(0.3),
                           CONTENT_WIDTH, Inches(6.0), number_format="#,##0")
    bullets = leader_laggard_bullets(keys, current, prior, "money", "GDPI")
    slide_rows = data.for_slide(rows, 8)
    for r in slide_rows:
        if r["Company"] in ("Industry Total", "Stand-alone Health sub Total") and r["Meric 1"] == "Growth %":
            v = data.num(r["FY26_Q3"])
            if v is not None:
                bullets.append(f"{r['Company']} growth %: {v*100:.1f}%")
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_09(prs, rows):
    cdata = data.by_company(rows, 9, theme.canonical_company)
    keys, _, current = data.metric_series(cdata, "SAHI Growth", "GDPI Growth SAHI")
    slide = new_content_slide(prs, "Revenue & Growth % (SAHI)", 9)
    panel_label(slide, "GDPI Growth % (YoY)", CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH)
    charts.single_series_column(slide, disp_names(keys), current, CONTENT_LEFT, PANEL_TOP0 + Inches(0.3),
                                 CONTENT_WIDTH, Inches(5.8), number_format="0.0%")
    bullets = leader_laggard_bullets(keys, current, [None] * len(keys), "percent", "GDPI growth")
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_10(prs, rows):
    # Phase 2's compute_derived_metrics only normalizes the "SAHI" row of
    # this slide into a clean 0-1 mix (its own comment: GT's figures for
    # Industry/Public GI/Pvt. GI "look internally inconsistent... left as
    # absolute Rs. Crore rather than guessed at further"). Charting all 4
    # groups on one 100%-stacked axis would mix incompatible scales, so only
    # SAHI (the one clean series) gets the mix chart; the other 3 groups are
    # noted as not reliably available in percentage form this quarter.
    cdata = data.metric2_by_group(rows, 10, "Meric 1")
    segs = ["Retail", "Group", "Govt.", "Travel", "PA"]
    slide = new_content_slide(prs, "Segment-wise GDPI mix", 10)
    sahi = cdata.get("SAHI", {})
    labels = [s for s in segs if s in sahi]
    cur = [sahi[s][0] for s in labels]
    pri = [sahi[s][1] for s in labels]
    colors = theme.FALLBACK_SERIES_COLORS[:len(labels)]
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(3.6))
    charts.doughnut_pair(slide, "FY25_Q3", "FY26_Q3", labels, pri, cur, colors,
                          CONTENT_LEFT + Inches(0.1), PANEL_TOP0 + Inches(0.1), CONTENT_WIDTH - Inches(0.2),
                          Inches(3.4), unit_label="% of SAHI GDPI")
    theme.add_note(slide, "Industry / Public GI / Pvt. GI segment mix isn't reliably derivable as a clean "
                           "percentage from current source data this quarter - SAHI's mix only.",
                    CONTENT_LEFT, PANEL_TOP0 + Inches(3.7), CONTENT_WIDTH, Inches(0.5))
    theme.add_insight_panel(slide, ["SAHI segment mix (Retail/Group/Govt./Travel/PA) as a share of SAHI's own GDPI."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_11(prs, rows):
    cdata = data.metric2_by_group(rows, 11, "Meric 1", canonical_fn=theme.canonical_company)
    ordered = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(ordered)
    segs = ["Retail", "Group", "Govt.", "Travel", "PA"]
    series = {seg: [cdata[k].get(seg, (None, None))[0] for k in ordered] for seg in segs}
    colors = theme.series_colors_for(segs)
    slide = new_content_slide(prs, "Segment-wise GDPI mix - SAHI", 11)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(6.0))
    charts.stacked_100_column(slide, names, series, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(5.5))
    theme.add_insight_panel(slide, ["Segment mix (% of own GDPI) per SAHI company."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_12(prs, rows):
    cdata = data.metric2_by_group(rows, 12, "Meric 1", canonical_fn=theme.canonical_company)
    ordered = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(ordered)
    channels = ["Individual Agents", "Corporate Agents - Banks", "Corporate Agents - Others", "Brokers",
                "Direct Business", "Others"]
    series = {ch: [cdata[k].get(ch, (None, None))[0] for k in ordered] for ch in channels}
    colors = theme.series_colors_for(channels)
    slide = new_content_slide(prs, "GDPI by Channel: SAHI's", 12)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(6.0))
    charts.stacked_100_column(slide, names, series, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(5.5), pct100=False, number_format="#,##0")
    theme.add_insight_panel(slide, ["FY26 Q3 GDPI (Rs. Crore) by distribution channel, per SAHI company."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


CHANNEL8 = ["Individual Agents", "Corporate Agents-Banks", "Corporate Agents-Others", "Brokers",
            "CSC", "IMF", "Web Aggregator", "POS"]


def slide_13(prs, rows):
    cdata = data.by_company(rows, 13, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(keys)
    series = {ch: [cdata[k].get(("Channel-wise Gross Commision % to GDPI", ch), (None, None))[0] for k in keys]
              for ch in CHANNEL8}
    colors = theme.series_colors_for(CHANNEL8)
    slide = new_content_slide(prs, "Channel-wise Commission: SAHI's", 13)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(6.0))
    charts.stacked_100_column(slide, names, series, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(5.5), pct100=False, number_format="#,##0")
    theme.add_note(slide, "Gross commission (Rs. Lakhs) by channel.", CONTENT_LEFT, PANEL_TOP0 + Inches(6.05),
                    CONTENT_WIDTH, Inches(0.3))
    theme.add_insight_panel(slide, ["Individual Agents remain the largest commission channel for most companies."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_14(prs, rows):
    panels = [
        {"title": "Retail Revenue (Rs. Crore)", "metric1": "Retail Revenue", "metric2": None, "kind": "money"},
        {"title": "Retail Accretion (Rs. Crore)", "metric1": "Retail Accretion",
         "metric2": "Retail Revenue CY-Retail Revenue PY", "kind": "money", "mode": "single"},
    ]
    return metric_panels_slide(prs, rows, 14, "Retail Revenue", 14, panels)


def slide_15(prs, rows):
    panels = [
        {"title": "Individual ATS (Rs. per policy)", "metric1": "Individual ATS",
         "metric2": "Individual agents GWP/Individual agents no. of policies", "kind": "money", "mode": "single"},
        {"title": "Average Productivity (Rs. Lakhs per agent)", "metric1": "Average Productivity (per agent)",
         "metric2": "Premium/No. of Individual Agents", "kind": "money", "mode": "single"},
    ]
    return metric_panels_slide(prs, rows, 15, "ATS", 15, panels)


STATES8 = ["Uttar Pradesh", "Maharashtra", "Karnataka", "Haryana", "Tamil Nadu", "Kerala", "Delhi", "Others"]

# Standard Indian zonal classification (Ministry of Home Affairs zonal
# council convention), applied to whichever of the 8 Slide 17 state buckets
# fall in each zone. Only these 7 named states are individually broken out
# by the source disclosures - "Others" is a residual across every
# unlisted state, which necessarily spans multiple zones (including the
# entirety of the East and Central zones) and can't be disaggregated
# further from current source data, so it's kept as its own "Others
# (unclassified)" bucket rather than guessed at.
STATE_TO_ZONE = {
    "Uttar Pradesh": "North", "Haryana": "North", "Delhi": "North",
    "Maharashtra": "West",
    "Karnataka": "South", "Tamil Nadu": "South", "Kerala": "South",
}


def slide_16(prs, rows):
    cdata = data.by_company(rows, 17, theme.canonical_company)  # Slide 17's state-share data, re-aggregated
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(keys)
    zones = ["North", "West", "South", "Others (unclassified)"]

    def zone_value(company_key, zone):
        if zone == "Others (unclassified)":
            states = [s for s in STATES8 if s not in STATE_TO_ZONE]
        else:
            states = [s for s, z in STATE_TO_ZONE.items() if z == zone]
        vals = [cdata[company_key].get((s, None), (None, None))[0] for s in states]
        vals = [v for v in vals if v is not None]
        return round(sum(vals), 4) if vals else None

    series = {z: [zone_value(k, z) for k in keys] for z in zones}
    colors = {"North": theme.SEGMENT_COLORS["Public"], "West": theme.SEGMENT_COLORS["Private"],
              "South": theme.SEGMENT_COLORS["SAHI"], "Others (unclassified)": theme.LIGHT_GREY}
    slide = new_content_slide(prs, "Geographical Distribution: Zones", 16)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(5.6))
    charts.stacked_100_column(slide, names, series, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(5.0))
    theme.add_note(slide, "North/West/South derived from Slide 17's named states (Ministry of Home Affairs zonal "
                           "convention); East and Central aren't separately broken out by current source "
                           "disclosures and fall inside 'Others (unclassified)'.",
                    CONTENT_LEFT, PANEL_TOP0 + Inches(5.65), CONTENT_WIDTH, Inches(0.45))
    theme.add_insight_panel(slide, ["Zone split is a best-effort estimate from named-state data - East/Central "
                                     "exposure isn't separately identifiable this quarter."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_17(prs, rows):
    cdata = data.by_company(rows, 17, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(keys)
    series = {st: [cdata[k].get((st, None), (None, None))[0] for k in keys] for st in STATES8}
    colors = theme.series_colors_for(STATES8)
    slide = new_content_slide(prs, "Geographical Distribution: States", 17)
    theme.add_panel_frame(slide, CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH, Inches(6.0))
    charts.stacked_100_column(slide, names, series, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(5.5))
    theme.add_insight_panel(slide, ["State-wise GDPI as a share of each company's own GWP."],
                             CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


# ---------------------------------------------------------------------------
# Slide 18: Income Statement
# ---------------------------------------------------------------------------

INCOME_ROWS = ["Gross Written Premium", "Net Written Premium", "Earned Premium", "Investment Income",
               "Claims", "Total Overheads", "PBT", "PAT"]


def slide_18(prs, rows):
    slide = new_content_slide(prs, "Income Statement", 18)
    cdata = data.by_company(rows, 18, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    cur_vals = {label: {k: cdata[k].get((label, None), (None, None))[0] for k in keys} for label in INCOME_ROWS}
    pri_vals = {label: {k: cdata[k].get((label, None), (None, None))[1] for k in keys} for label in INCOME_ROWS}

    panel_label(slide, "FY26 Q3 (Rs. Crore)", CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH)
    h1 = charts.logo_table(slide, INCOME_ROWS, keys, cur_vals, CONTENT_LEFT, PANEL_TOP0 + Inches(0.3),
                            CONTENT_WIDTH, "FY26 Q3")
    t2_top = PANEL_TOP0 + Inches(0.3) + h1 + Inches(0.35)
    panel_label(slide, "FY25 Q3 (Rs. Crore)", CONTENT_LEFT, t2_top, CONTENT_WIDTH)
    charts.logo_table(slide, INCOME_ROWS, keys, pri_vals, CONTENT_LEFT, t2_top + Inches(0.3), CONTENT_WIDTH, "FY25 Q3")

    bullets = leader_laggard_bullets(keys, [cur_vals["PBT"][k] for k in keys], [pri_vals["PBT"][k] for k in keys],
                                      "money", "PBT")
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


# ---------------------------------------------------------------------------
# Slides 19-23: Key Metrics
# ---------------------------------------------------------------------------

def slide_19(prs, rows):
    panels = [
        {"title": "Combined Ratio", "metric1": "Combined Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Expense Ratio", "metric1": "Expense Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Loss Ratio", "metric1": "Loss Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
    ]
    return metric_panels_slide(prs, rows, 19, "Key Metrics", 19, panels)


def slide_20(prs, rows):
    panels = [
        {"title": "Claims Settlement Ratio", "metric1": "Claims Settlement Ratio", "metric2": None,
         "kind": "percent", "mode": "single"},
        {"title": "Average Claim Size (Rs.)", "metric1": "Average Claim Size", "metric2": None,
         "kind": "money", "mode": "single"},
        {"title": "No. of Claims to No. of Policies", "metric1": "No. of claims to No. of policies", "metric2": None,
         "kind": "percent", "mode": "single", "higher_is_better": False},
    ]
    return metric_panels_slide(prs, rows, 20, "Key Metrics", 20, panels,
                                footnote="Average Claim Size and No. of claims to policies are best-effort estimates "
                                         "(not independently GT-verified) - see Glossary.")


def slide_21(prs, rows):
    panels = [
        {"title": "Opex. To GWP ratio", "metric1": "Opex. To GWP ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Manpower to GWP ratio", "metric1": "Manpower to GWP ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "IT spend to GWP ratio", "metric1": "IT spend to GWP ratio", "metric2": None, "kind": "percent"},
    ]
    return metric_panels_slide(prs, rows, 21, "Key Metrics", 21, panels)


def slide_22(prs, rows):
    panels = [
        {"title": "Manpower cost to total Opex", "metric1": "Manpower cost to total Opex", "metric2": None,
         "kind": "percent"},
        {"title": "Manpower cost per employee (Rs.)", "metric1": "Manpower cost per employee", "metric2": None,
         "kind": "money", "mode": "single"},
        {"title": "Facility rental per office per month (Rs. Lakhs)", "metric1": "Facility rental per office per month",
         "metric2": None, "kind": "money", "mode": "single"},
    ]
    return metric_panels_slide(prs, rows, 22, "Key Metrics", 22, panels)


def slide_23(prs, rows):
    panels = [
        {"title": "Capital (Rs. Crore)", "metric1": "Capital", "metric2": None, "kind": "money"},
        {"title": "Net Worth (Rs. Lakhs)", "metric1": "Net Worth", "metric2": None, "kind": "money"},
        {"title": "PBT (Rs. Crore)", "metric1": "PBT", "metric2": None, "kind": "money"},
    ]
    return metric_panels_slide(prs, rows, 23, "Key Metrics", 23, panels)


# ---------------------------------------------------------------------------
# Slides 24-26: Investment / Debt Portfolio
# ---------------------------------------------------------------------------

def _fractions_of_row_total(series_dict, n):
    """series_dict: {series_name: [value_per_category, ...]}. Returns the
    same shape with each category's values rescaled to fractions of that
    category's own cross-series total - for slides (e.g. Investment
    Portfolio) whose source metric is 'money' kind (absolute Rs. Cr per
    asset class), not already a pre-computed percentage like Slides 10/11/
    17/25/26 (all 'percent' kind, already fractions - left untouched)."""
    totals = [sum(v[i] or 0 for v in series_dict.values()) for i in range(n)]
    return {name: [(v[i] / totals[i]) if v[i] is not None and totals[i] else None for i in range(n)]
            for name, v in series_dict.items()}


def _two_period_stacked_slide(prs, rows, slide_no, title, page_no, series_names, note, normalize=False):
    # metric1_only, not by_company: Slide 26's Metric 2 is a descriptive
    # "Avg. Maturity (X years)" annotation, not a disambiguating key - each
    # (company, metric1/bucket) pair is already unique on these 3 slides.
    cdata = data.pivot_metric1_only(rows, slide_no, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(keys)
    colors = theme.series_colors_for(series_names)
    slide = new_content_slide(prs, title, page_no)
    cur = {s: [cdata[k].get(s, (None, None))[0] for k in keys] for s in series_names}
    pri = {s: [cdata[k].get(s, (None, None))[1] for k in keys] for s in series_names}
    if normalize:
        cur = _fractions_of_row_total(cur, len(keys))
        pri = _fractions_of_row_total(pri, len(keys))
    panel_label(slide, "FY26 Q3", CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH)
    charts.stacked_100_column(slide, names, cur, colors, CONTENT_LEFT + Inches(0.2), PANEL_TOP0 + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(3.7))
    t2_top = PANEL_TOP0 + Inches(4.15)
    panel_label(slide, "FY25 Q3", CONTENT_LEFT, t2_top, CONTENT_WIDTH)
    charts.stacked_100_column(slide, names, pri, colors, CONTENT_LEFT + Inches(0.2), t2_top + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(3.7))
    theme.add_insight_panel(slide, [note], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


def slide_24(prs, rows):
    series_names = ["Corporate Bonds/Debentures", "Govt Bonds", "Deposits", "Equity/Invits/REIT", "Mutual Funds"]
    # This metric is extracted as absolute Rs. Cr per asset class ("money"
    # kind), not a pre-computed percentage (unlike Slides 10/11/17/25/26) -
    # normalize to % of each company's own portfolio to match the reference
    # deck's framing.
    return _two_period_stacked_slide(prs, rows, 24, "Investment Portfolio", 24, series_names,
                                      "Investment mix as % of each company's own book value, FY26 Q3 vs FY25 Q3.",
                                      normalize=True)


def slide_25(prs, rows):
    series_names = ["Sovereign", "AAA rated", "AA or better", "Rated below AA but above A", "Rated below A"]
    return _two_period_stacked_slide(prs, rows, 25, "Debt Portfolio: Credit Rating", 25, series_names,
                                      "Exposure by credit rating, FY26 Q3 vs FY25 Q3.")


def slide_26(prs, rows):
    series_names = ["Up to 1 year", "More than 1 year and upto 3 years", "More than 3 years and upto 7 years",
                     "More than 7 years and upto 10 years", "Above 10 years"]
    return _two_period_stacked_slide(prs, rows, 26, "Debt Portfolio: Residual Maturity", 26, series_names,
                                      "Exposure by residual maturity, FY26 Q3 vs FY25 Q3.")


# ---------------------------------------------------------------------------
# Slides 27-31: Historical Trends (2-period comparison, not multi-year)
# ---------------------------------------------------------------------------

TRENDS_FOOTNOTE = ("Quarterly YoY comparison (FY25 Q3 vs FY26 Q3), not multi-year history - a full trend line "
                    "requires accumulating quarterly snapshots over future pipeline runs.")


def slide_27(prs, rows):
    panels = [
        {"title": "GWP (Rs. Crore)", "metric1": "GWP", "metric2": None, "kind": "money"},
        {"title": "PBT (Rs. Crore)", "metric1": "PBT", "metric2": None, "kind": "money"},
    ]
    return metric_panels_slide(prs, rows, 27, "Historical Trends", 27, panels, footnote=TRENDS_FOOTNOTE)


def slide_28(prs, rows):
    panels = [
        {"title": "Combined Ratio", "metric1": "Combined Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Loss Ratio", "metric1": "Loss Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
    ]
    return metric_panels_slide(prs, rows, 28, "Historical Trends", 28, panels, footnote=TRENDS_FOOTNOTE)


def slide_29(prs, rows):
    panels = [
        {"title": "Expense Ratio", "metric1": "Expense Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Expense of Management Ratio", "metric1": "Expense of Management Ratio", "metric2": None,
         "kind": "percent", "higher_is_better": False},
    ]
    return metric_panels_slide(prs, rows, 29, "Historical Trends", 29, panels, footnote=TRENDS_FOOTNOTE)


def slide_30(prs, rows):
    panels = [
        {"title": "RI Ceding to GWP Ratio", "metric1": "RI Ceding to GWP Ratio", "metric2": "Risk Ceded",
         "kind": "percent", "mode": "single"},
        {"title": "RI Commission to RI Ceding", "metric1": "RI Commission to RI Ceding", "metric2": "Risk Ceded",
         "kind": "percent", "mode": "single"},
    ]
    return metric_panels_slide(prs, rows, 30, "Historical Trends", 30, panels, footnote=TRENDS_FOOTNOTE)


def slide_31(prs, rows):
    panels = [
        {"title": "ROE (SAHI)", "metric1": "ROE (SAHI)", "metric2": "PAT/Avg. Net Worth", "kind": "percent",
         "mode": "single"},
        {"title": "Solvency Ratio", "metric1": "Solvency Ratios", "metric2": None, "kind": "ratio"},
    ]
    return metric_panels_slide(prs, rows, 31, "Historical Trends", 31, panels, footnote=TRENDS_FOOTNOTE)


# ---------------------------------------------------------------------------
# Slides 32-33: AUM
# ---------------------------------------------------------------------------

def slide_32(prs, rows):
    panels = [
        {"title": "AUM (Overall, Rs. Crore)", "metric1": "AUM (Overall)", "metric2": None, "kind": "money"},
        {"title": "Investment Yield", "metric1": "Investment Yield", "metric2": None, "kind": "percent"},
    ]
    return metric_panels_slide(prs, rows, 32, "Asset Under Management", 32, panels)


def slide_33(prs, rows):
    panels = [
        {"title": "AUM - Policyholders (Rs. Crore)", "metric1": "AUM -Policyholders", "metric2": None,
         "kind": "money"},
        {"title": "AUM - Shareholders (Rs. Crore)", "metric1": "AUM -Shareholders", "metric2": None, "kind": "money"},
    ]
    return metric_panels_slide(prs, rows, 33, "Asset Under Management", 33, panels)


# ---------------------------------------------------------------------------
# Slides 34-35: Distribution Footprint
# ---------------------------------------------------------------------------

def slide_34(prs, rows):
    panels = [
        {"title": "Employees (On-roll)", "metric1": "Employees", "metric2": "On-roll Employee", "kind": "count",
         "mode": "single"},
        {"title": "Individual Agents", "metric1": "Agents", "metric2": "Individual Agents", "kind": "count",
         "mode": "single"},
    ]
    return metric_panels_slide(prs, rows, 34, "Distribution Footprint", 34, panels)


INTERMEDIARY_TYPES = ["Individual Agents", "CA-Banks", "CA-Others", "Brokers", "WA", "IMF", "POS"]


def slide_35(prs, rows):
    cdata = data.by_company(rows, 35, theme.canonical_company)
    # "No. of Offices" has a descriptive (not disambiguating) Metric 2
    # ("No. of branches at the end of the period") - look it up ignoring it.
    offices = data.pivot_metric1_only(rows, 35, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    names = disp_names(keys)
    slide = new_content_slide(prs, "Distribution Footprint", 35)

    off_cur = [offices.get(k, {}).get("No. of Offices", (None, None))[0] for k in keys]
    panel_label(slide, "No. of Offices", CONTENT_LEFT, PANEL_TOP0, CONTENT_WIDTH)
    charts.single_series_column(slide, names, off_cur, CONTENT_LEFT, PANEL_TOP0 + Inches(0.3), CONTENT_WIDTH,
                                 Inches(2.6), number_format="#,##0")

    im_top = PANEL_TOP0 + Inches(3.2)
    series = {t: [cdata[k].get(("Intermediaries", t), (None, None))[0] for k in keys] for t in INTERMEDIARY_TYPES}
    colors = theme.series_colors_for(INTERMEDIARY_TYPES)
    panel_label(slide, "Intermediaries by type", CONTENT_LEFT, im_top, CONTENT_WIDTH)
    charts.stacked_100_column(slide, names, series, colors, CONTENT_LEFT + Inches(0.2), im_top + Inches(0.3),
                               CONTENT_WIDTH - Inches(0.4), Inches(3.5), pct100=False, number_format="#,##0")

    bullets = leader_laggard_bullets(keys, off_cur, [None] * len(keys), "count", "office count")
    theme.add_insight_panel(slide, bullets[:4], CONTENT_LEFT, INSIGHT_TOP, CONTENT_WIDTH, INSIGHT_HEIGHT)
    return slide


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SLIDE_FUNCS = [slide_03, slide_04, slide_05, slide_06, slide_07, slide_08, slide_09, slide_10, slide_11,
               slide_12, slide_13, slide_14, slide_15, slide_16, slide_17, slide_18, slide_19, slide_20,
               slide_21, slide_22, slide_23, slide_24, slide_25, slide_26, slide_27, slide_28, slide_29,
               slide_30, slide_31, slide_32, slide_33, slide_34, slide_35]


def build(out_path=OUT_PATH):
    rows = data.load_rows()
    prs = Presentation()
    prs.slide_width = theme.SLIDE_WIDTH
    prs.slide_height = theme.SLIDE_HEIGHT

    title_slide(prs)
    toc_slide(prs)
    for fn in SLIDE_FUNCS:
        fn(prs, rows)
    glossary_slide(prs, 36)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    prs.save(out_path)
    print(f"Saved {out_path} ({len(prs.slides)} slides)")
    return out_path


if __name__ == "__main__":
    build()
