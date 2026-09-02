"""
FY26 Q3 Competition Analysis report generator - PDF (matplotlib), rebuilt
from generate_report.py (the earlier PowerPoint version) after switching
formats: PDF lets Claude render each page to an image (via pdfplumber) and
visually verify it directly, which a .pptx file does not in this
environment (no PowerPoint/LibreOffice available to render it).

Hard rule throughout: if a metric group has no FY26 Q3 data at all, it is
skipped entirely - no placeholder box, no "not available" note, no empty
chart frame. Every chart-drawing function in pdf_charts.py returns False
when there's nothing to plot, and every section function here checks that
before deciding whether to allocate a panel for it.

Known, deliberate simplifications vs. the FY25 reference deck:
  - "Historical Trends" sections render as a 2-period (FY25_Q3 vs FY26_Q3)
    comparison, not 8-year history - the Data Engine only carries 2 periods.
  - The reference deck's dot/bubble "Market Share Change" mini-chart is
    replaced by a horizontal diverging bar chart (simpler, equally
    informative, easier to verify correct in a static image).
  - Company logos are not embedded (matplotlib table cells don't host
    per-cell images cleanly) - company columns use plain short names instead.
  - Insight bullets are rule-based (leader/laggard + YoY direction), not
    human analyst commentary - a first-pass draft only.
"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch
from matplotlib.lines import Line2D

import pdf_theme as theme
import pdf_charts as charts
import report_data as data

OUT_PATH = os.path.join("output", "Competition_Summary_FY26_Q3.pdf")

NUMFMT_PCT = {"percent"}


def _header_footer(fig, title, page_no):
    fig.text(0.06, 0.965, "Competition Analysis - FY26 Q3", fontsize=9, color=theme.GREY_TEXT)
    fig.text(0.94, 0.965, f"Page {page_no}", fontsize=9, color=theme.GREY_TEXT, ha="right")
    fig.add_artist(Line2D([0.06, 0.94], [0.955, 0.955], transform=fig.transFigure, color=theme.NAVY, linewidth=1.2))
    fig.text(0.06, 0.925, title, fontsize=17, fontweight="bold", color=theme.NAVY)
    fig.text(0.5, 0.02, str(page_no), fontsize=8, color=theme.GREY_TEXT, ha="center")


def new_page(title, page_no, n_panels, height_ratios=None, want_insights=False):
    """Returns (fig, list_of_subplot_specs_for_panels, subplot_spec_for_insights_or_None)."""
    fig = plt.figure(figsize=theme.PAGE_SIZE, dpi=theme.DPI)
    _header_footer(fig, title, page_no)
    total_rows = n_panels + (1 if want_insights else 0)
    ratios = list(height_ratios) if height_ratios else [1] * n_panels
    if want_insights:
        ratios = ratios + [0.32]
    gs = fig.add_gridspec(total_rows, 1, left=0.09, right=0.94, top=0.88, bottom=0.06, hspace=0.32,
                           height_ratios=ratios)
    panel_specs = [gs[i] for i in range(n_panels)]
    insight_spec = gs[n_panels] if want_insights else None
    return fig, panel_specs, insight_spec


def draw_insights(fig, subplot_spec, bullets):
    if not bullets:
        return
    ax = fig.add_subplot(subplot_spec)
    ax.axis("off")
    box = FancyBboxPatch((0, 0), 1, 1, transform=ax.transAxes, boxstyle="round,pad=0.02,rounding_size=0.03",
                          linewidth=1, edgecolor=theme.INSIGHT_BORDER, facecolor=theme.INSIGHT_BG,
                          linestyle=(0, (5, 3)), clip_on=False)
    ax.add_patch(box)
    text = "\n".join(f"➔  {b}" for b in bullets[:4])
    ax.text(0.03, 0.5, text, fontsize=8.5, color=theme.DARK_TEXT, va="center", ha="left", transform=ax.transAxes,
            wrap=True)


def panel_title(fig, subplot_spec, text):
    """A small bold label above a chart panel, without consuming its own
    gridspec row (placed via the panel's own axes title instead is simpler,
    but some panels are multi-axes (doughnut pairs) so a figure-level label
    is used there instead)."""
    bbox = subplot_spec.get_position(fig)
    fig.text((bbox.x0 + bbox.x1) / 2, bbox.y1 + 0.002, text, fontsize=10.5, fontweight="bold", ha="center")


def disp_names(keys):
    return [theme.COMPANY_DISPLAY_NAME.get(k, k) for k in keys]


def leader_laggard_bullets(keys, current, prior, kind, metric_name, higher_is_better=True, limit=3):
    names = disp_names(keys)
    pairs = [(n, c, p) for n, c, p in zip(names, current, prior) if c is not None]
    if not pairs:
        return []
    scale = 100 if kind == "percent" else 1
    unit = "%" if kind == "percent" else ("x" if kind == "ratio" else "")
    bullets = []
    top = max(pairs, key=lambda x: x[1]) if higher_is_better else min(pairs, key=lambda x: x[1])
    word = "highest" if higher_is_better else "lowest"
    bullets.append(f"{top[0]} has the {word} {metric_name} at {top[1] * scale:,.1f}{unit}")
    deltas = [(n, c, p, c - p) for n, c, p in pairs if p is not None]
    if deltas:
        best = max(deltas, key=lambda x: x[3])
        worst = min(deltas, key=lambda x: x[3])
        if best[3] > 1e-9:
            bullets.append(f"{best[0]} improved the most YoY (+{best[3] * scale:,.1f}{unit})")
        if worst[3] < -1e-9 and worst[0] != best[0]:
            bullets.append(f"{worst[0]} declined the most YoY ({worst[3] * scale:,.1f}{unit})")
    return bullets[:limit]


# ---------------------------------------------------------------------------
# Cover / TOC / Glossary
# ---------------------------------------------------------------------------

def cover_page(pdf):
    fig = plt.figure(figsize=theme.PAGE_SIZE, dpi=theme.DPI)
    fig.patch.set_facecolor("white")
    band = plt.Rectangle((0, 0.62), 1, 0.09, transform=fig.transFigure, facecolor=theme.NAVY, clip_on=False)
    fig.add_artist(band)
    fig.text(0.08, 0.665, "Competition Analysis", fontsize=30, fontweight="bold", color="white")
    fig.text(0.92, 0.15, "Executive Summary", fontsize=18, fontweight="bold", color=theme.NAVY, ha="right")
    fig.text(0.92, 0.11, "Period ending December 2025 (FY26 Q3)", fontsize=12, color=theme.NAVY, ha="right")
    pdf.savefig(fig)
    plt.close(fig)


TOC_ENTRIES = [
    ("Overall Industry & Market share", "3-7"), ("Revenue (Segment, Channel, Geographical mix)", "8-17"),
    ("Income statement", "18"), ("Key metrics", "19-23"), ("Investment portfolio", "24-26"),
    ("Historical trends", "27-31"), ("AUM", "32-33"), ("Distribution footprints", "34-35"),
]


def toc_page(pdf, page_no):
    fig = plt.figure(figsize=theme.PAGE_SIZE, dpi=theme.DPI)
    _header_footer(fig, "Table of Contents", page_no)
    ax = fig.add_axes([0.09, 0.15, 0.85, 0.68])
    ax.axis("off")
    rows = [[label, pages] for label, pages in TOC_ENTRIES]
    table = ax.table(cellText=rows, colLabels=["Contents", "Slide No."], cellLoc="left", loc="center",
                      colWidths=[0.8, 0.2])
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 2.2)
    for (r, c), cell in table.get_celld().items():
        if r == 0:
            cell.set_facecolor(theme.BLUE)
            cell.set_text_props(color="white", fontweight="bold")
        cell.set_edgecolor(theme.GRID_COLOR)
    pdf.savefig(fig)
    plt.close(fig)


def glossary_page(pdf, page_no):
    fig = plt.figure(figsize=theme.PAGE_SIZE, dpi=theme.DPI)
    _header_footer(fig, "Glossary", page_no)
    lines = [
        "Above information is as per public disclosures available on IRDAI/company websites for the quarter ended 31 Dec 2025 (FY26 Q3), compared to the quarter ended 31 Dec 2024 (FY25 Q3).",
        "SAHI = Stand-alone Health Insurer. GDPI = Gross Direct Premium Income. GWP = Gross Written Premium. NWP = Net Written Premium.",
        "“Historical Trends” sections show a 2-period (FY25 Q3 vs FY26 Q3) year-on-year comparison, not multi-year history - the underlying data pipeline currently carries only these 2 periods.",
        "Geographic zone split (North/West/South) is derived from named-state data using the standard Ministry of Home Affairs zonal convention; East and Central aren't separately identifiable from current source disclosures.",
        "Average Claim Size and No. of claims to No. of policies are best-effort estimates, not independently ground-truth-verified.",
        "Sections with no FY26 Q3 data for any company are omitted from this report entirely, rather than shown as an empty or placeholder chart.",
        "Company short names: NBHI = Niva Bupa Health Insurance, STAR = Star Health & Allied Insurance, CARE = Care Health Insurance, CIGNA = ManipalCigna Health Insurance, ABHI = Aditya Birla Health Insurance, Narayana = Narayana Health Insurance, Galaxy = Galaxy Health Insurance.",
        "This report was generated automatically from Data_Engine_UI.xlsx; insight bullets are rule-based auto-generated observations and should be reviewed before external use.",
    ]
    y = 0.82
    for line in lines:
        fig.text(0.08, y, f"➔  {line}", fontsize=10, wrap=True, va="top")
        y -= 0.09
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Slides 3-7: Overall Industry & Market share
# ---------------------------------------------------------------------------

SEGMENT_MIX_LINES = ["Fire", "Marine Total", "Engineering", "Motor Total", "Health",
                     "Aviation", "Liability", "P.A.",
                     "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)"]
SEGMENT_MIX_DISPLAY = {"Marine Total": "Marine", "Motor Total": "Motor",
                        "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)": "Others"}


def industry_share_page(pdf, rows, title, page_no, slide_no, company_key, own_labels, mix_labels, mix_colors_fn):
    slide_rows = data.for_slide(rows, slide_no)
    by_m2 = {r["Metric 2"]: (data.num(r["FY26_Q3"]), data.num(r["FY25_Q3"]))
             for r in slide_rows if r["Company"] == company_key}
    own_cur = [by_m2.get(m, (None, None))[0] for m in own_labels]
    own_pri = [by_m2.get(m, (None, None))[1] for m in own_labels]
    own_colors = [theme.SEGMENT_COLORS.get(m, theme.ORANGE) for m in own_labels]

    mix_present = [m for m in mix_labels if by_m2.get(m, (None, None))[0]]
    mix_disp = [SEGMENT_MIX_DISPLAY.get(m, m) for m in mix_present]
    mix_cur = [by_m2[m][0] for m in mix_present]
    mix_pri = [by_m2[m][1] for m in mix_present]

    has_own = any(v is not None for v in own_cur + own_pri)
    has_mix = bool(mix_present)
    if not has_own and not has_mix:
        return
    n_panels = int(has_own) + int(has_mix)
    bullets = leader_laggard_bullets(own_labels, own_cur, own_pri, "money", "market share") if has_own else []
    fig, panels, ins = new_page(title, page_no, n_panels, want_insights=bool(bullets))
    idx = 0
    if has_own:
        panel_title(fig, panels[idx], "Market Share")
        charts.doughnut_pair(fig, panels[idx], "FY25 Q3", "FY26 Q3", own_labels, own_pri, own_cur, own_colors,
                              unit_label="INR Crores")
        idx += 1
    if has_mix:
        panel_title(fig, panels[idx], "Segment Mix")
        charts.doughnut_pair(fig, panels[idx], "FY25 Q3", "FY26 Q3", mix_disp, mix_pri, mix_cur,
                              theme.FALLBACK_SERIES_COLORS[:len(mix_disp)], unit_label="INR Crores")
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_03(pdf, rows):
    industry_share_page(pdf, rows, "GI Industry", 3, 3, "Industry",
                         ["Private", "Public", "SAHI", "Specialized Insurer"], SEGMENT_MIX_LINES, None)


def slide_04(pdf, rows):
    industry_share_page(pdf, rows, "Health Industry (Inc. PA & Travel)", 4, 4, "Health Industry (Inc. PA and Travel)",
                         ["Private", "Public", "SAHI"],
                         ["Health-Retail", "Health-Group", "Health-Government schemes", "Overseas Medical", "P.A."],
                         None)


def slide_05(pdf, rows):
    slide_rows = data.for_slide(rows, 5)
    by_company = {}
    for r in slide_rows:
        key = theme.canonical_company(r["Metric 2"])
        if key:
            by_company[key] = (data.num(r["FY26_Q3"]), data.num(r["FY25_Q3"]))
    keys = [k for k in data.COMPANY_ORDER if k in by_company]
    if not keys:
        return
    cur = [by_company[k][0] for k in keys]
    pri = [by_company[k][1] for k in keys]
    colors = [theme.COMPANY_COLORS[k] for k in keys]
    names = disp_names(keys)

    totals_cur = sum(v for v in cur if v)
    totals_pri = sum(v for v in pri if v)
    changes = {}
    for k, c, p in zip(keys, cur, pri):
        if c is not None and p is not None and totals_cur and totals_pri:
            changes[k] = (c / totals_cur - p / totals_pri) * 100
    has_change = bool(changes)

    bullets = leader_laggard_bullets(keys, cur, pri, "money", "SAHI GDPI")
    n_panels = 1 + int(has_change)
    fig, panels, ins = new_page("SAHI Market", 5, n_panels, height_ratios=[1.3, 1][:n_panels],
                                 want_insights=bool(bullets))
    panel_title(fig, panels[0], "Market Share")
    charts.doughnut_pair(fig, panels[0], "FY25 Q3", "FY26 Q3", names, pri, cur, colors, unit_label="INR Crores")
    if has_change:
        panel_title(fig, panels[1], "Market Share Change (pp)")
        ax = fig.add_subplot(panels[1])
        charts.change_bar(ax, changes)
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


SEG5 = ["Health-Retail", "Health-Group", "Health-Government schemes", "Overseas Medical", "P.A."]


def slide_06(pdf, rows):
    cdata = data.metric2_by_group(rows, 6, "Company")
    groups = [g for g in ("SAHI Market", "Pvt GI", "Public GI") if g in cdata]
    if not groups:
        return
    series = {seg: [cdata[g].get(seg, (None, None))[0] for g in groups] for seg in SEG5}
    if not any(any(v is not None for v in vals) for vals in series.values()):
        return
    colors = theme.series_colors_for(SEG5)
    fig, panels, ins = new_page("Segment-wise: Health & PA", 6, 1, want_insights=True)
    ax = fig.add_subplot(panels[0])
    charts.stacked_bar(ax, groups, series, colors, pct100=False)
    draw_insights(fig, ins, ["Health-Group is the largest segment for both Private and Public GI players.",
                             "SAHI's mix skews more heavily to Health-Retail than Private/Public GI.",
                             "FY26 Q3 absolute GDPI (Rs. Crore) by segment."])
    pdf.savefig(fig)
    plt.close(fig)


def slide_07(pdf, rows):
    cdata = data.metric2_by_group(rows, 7, "Company", canonical_fn=theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    series = {seg: [cdata[k].get(seg, (None, None))[0] for k in keys] for seg in SEG5}
    colors = theme.series_colors_for(SEG5)
    fig, panels, ins = new_page("Segment wise SAHI's share", 7, 1, want_insights=True)
    ax = fig.add_subplot(panels[0])
    charts.stacked_bar(ax, names, series, colors, pct100=False)
    draw_insights(fig, ins, ["Retail remains the dominant segment across most SAHI players.",
                             "FY26 Q3 absolute GDPI (Rs. Crore) by segment, per SAHI company."])
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Slides 8-17: Revenue (Segment, Channel, Geographical mix)
# ---------------------------------------------------------------------------

def slide_08(pdf, rows):
    cdata = data.by_company(rows, 8, theme.canonical_company)
    keys, prior, current = data.metric_series(cdata, "Revenue Growth (GDPI)", None)
    if not keys:
        return
    bullets = leader_laggard_bullets(keys, current, prior, "money", "GDPI")
    slide_rows = data.for_slide(rows, 8)
    for r in slide_rows:
        if r["Company"] in ("Industry Total", "Stand-alone Health sub Total") and r["Meric 1"] == "Growth %":
            v = data.num(r["FY26_Q3"])
            if v is not None:
                bullets.append(f"{r['Company']} growth %: {v * 100:.1f}%")
    fig, panels, ins = new_page("Revenue Growth (GDPI)", 8, 1, want_insights=bool(bullets))
    panel_title(fig, panels[0], "Per-company GDPI (Rs. Crore)")
    charts.grouped_bar(fig, panels[0], disp_names(keys), prior, current)
    draw_insights(fig, ins, bullets[:4])
    pdf.savefig(fig)
    plt.close(fig)


def slide_09(pdf, rows):
    cdata = data.by_company(rows, 9, theme.canonical_company)
    keys, _, current = data.metric_series(cdata, "SAHI Growth", "GDPI Growth SAHI")
    if not keys:
        return
    bullets = leader_laggard_bullets(keys, current, [None] * len(keys), "percent", "GDPI growth")
    fig, panels, ins = new_page("Revenue & Growth % (SAHI)", 9, 1, want_insights=bool(bullets))
    panel_title(fig, panels[0], "GDPI Growth % (YoY)")
    charts.single_bar(fig, panels[0], disp_names(keys), current, is_percent=True)
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_10(pdf, rows):
    # Only "SAHI" has a clean 0-1 mix in the Data Engine for this slide -
    # Industry/Public GI/Pvt. GI are absolute Rs. Crore per Phase 2's own
    # finding that GT's figures for those don't fit a percentage convention.
    cdata = data.metric2_by_group(rows, 10, "Meric 1")
    sahi = cdata.get("SAHI", {})
    segs = ["Retail", "Group", "Govt.", "Travel", "PA"]
    labels = [s for s in segs if s in sahi]
    if not labels:
        return
    cur = [sahi[s][0] for s in labels]
    pri = [sahi[s][1] for s in labels]
    colors = theme.FALLBACK_SERIES_COLORS[:len(labels)]
    fig, panels, ins = new_page("Segment-wise GDPI mix", 10, 1, want_insights=True)
    charts.doughnut_pair(fig, panels[0], "FY25 Q3", "FY26 Q3", labels, pri, cur, colors, unit_label="% of SAHI GDPI")
    draw_insights(fig, ins, ["SAHI segment mix (Retail/Group/Govt./Travel/PA) as a share of SAHI's own GDPI.",
                             "Industry/Public GI/Pvt. GI segment mix isn't reliably derivable as a percentage this quarter - SAHI only."])
    pdf.savefig(fig)
    plt.close(fig)


def slide_11(pdf, rows):
    cdata = data.metric2_by_group(rows, 11, "Meric 1", canonical_fn=theme.canonical_company)
    ordered = [k for k in data.COMPANY_ORDER if k in cdata]
    if not ordered:
        return
    names = disp_names(ordered)
    segs = ["Retail", "Group", "Govt.", "Travel", "PA"]
    series = {seg: [cdata[k].get(seg, (None, None))[0] for k in ordered] for seg in segs}
    colors = theme.series_colors_for(segs)
    fig, panels, ins = new_page("Segment-wise GDPI mix - SAHI", 11, 1, want_insights=True)
    ax = fig.add_subplot(panels[0])
    charts.stacked_bar(ax, names, series, colors, pct100=True)
    draw_insights(fig, ins, ["Segment mix (% of own GDPI) per SAHI company."])
    pdf.savefig(fig)
    plt.close(fig)


def slide_12(pdf, rows):
    cdata = data.metric2_by_group(rows, 12, "Meric 1", canonical_fn=theme.canonical_company)
    ordered = [k for k in data.COMPANY_ORDER if k in cdata]
    if not ordered:
        return
    names = disp_names(ordered)
    channels = ["Individual Agents", "Corporate Agents - Banks", "Corporate Agents - Others", "Brokers",
                "Direct Business", "Others"]
    series = {ch: [cdata[k].get(ch, (None, None))[0] for k in ordered] for ch in channels}
    colors = theme.series_colors_for(channels)
    fig, panels, ins = new_page("GDPI by Channel: SAHI's", 12, 1, want_insights=True)
    ax = fig.add_subplot(panels[0])
    # This slide's Data Engine values are already a fraction of each
    # company's own GWP (Phase 2's fix_slide8_and_slide12 converts them from
    # the originally-extracted absolute Rs. Crore in a late pipeline stage) -
    # not absolute Rs. Crore, despite Slide 6/7/13's similar-looking channel
    # breakdowns being absolute. pct100=True re-normalizes (a near no-op
    # since they already sum to ~1) and gets the axis/labels right.
    charts.stacked_bar(ax, names, series, colors, pct100=True)
    draw_insights(fig, ins, ["Channel mix as % of each SAHI company's own GDPI (FY26 Q3)."])
    pdf.savefig(fig)
    plt.close(fig)


CHANNEL8 = ["Individual Agents", "Corporate Agents-Banks", "Corporate Agents-Others", "Brokers",
            "CSC", "IMF", "Web Aggregator", "POS"]


def slide_13(pdf, rows):
    cdata = data.by_company(rows, 13, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    series = {ch: [cdata[k].get(("Channel-wise Gross Commision % to GDPI", ch), (None, None))[0] for k in keys]
              for ch in CHANNEL8}
    colors = theme.series_colors_for(CHANNEL8)
    fig, panels, ins = new_page("Channel-wise Commission: SAHI's", 13, 1, want_insights=True)
    ax = fig.add_subplot(panels[0])
    charts.stacked_bar(ax, names, series, colors, pct100=False)
    draw_insights(fig, ins, ["Individual Agents remain the largest commission channel for most companies.",
                             "Gross commission (Rs. Lakhs) by channel."])
    pdf.savefig(fig)
    plt.close(fig)


def metric_panels_page(pdf, rows, slide_no, title, page_no, panels_def, footnote=None, key_field="Company"):
    cdata = data.by_company(rows, slide_no, theme.canonical_company, key_field)
    resolved = []
    for pdef in panels_def:
        keys, prior, current = data.metric_series(cdata, pdef["metric1"], pdef.get("metric2"))
        if not keys:
            continue
        resolved.append((pdef, keys, prior, current))
    if not resolved:
        return
    bullets = []
    for pdef, keys, prior, current in resolved:
        bullets += leader_laggard_bullets(keys, current, prior, pdef["kind"], pdef["title"],
                                           pdef.get("higher_is_better", True))
    fig, panels, ins = new_page(title, page_no, len(resolved), want_insights=bool(bullets or footnote))
    for (pdef, keys, prior, current), spec in zip(resolved, panels):
        panel_title(fig, spec, pdef["title"])
        is_pct = pdef["kind"] == "percent"
        if pdef.get("mode", "grouped") == "single":
            charts.single_bar(fig, spec, disp_names(keys), current, is_percent=is_pct)
        else:
            charts.grouped_bar(fig, spec, disp_names(keys), prior, current, is_percent=is_pct)
    # draw_insights only ever shows the first 4 lines - reserve a slot for
    # the footnote up front rather than appending it and having it silently
    # truncated away when there are already 4 leader/laggard bullets.
    ins_bullets = (bullets[:3] + [footnote]) if footnote else bullets[:4]
    draw_insights(fig, ins, ins_bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_14(pdf, rows):
    panels = [
        {"title": "Retail Revenue (Rs. Crore)", "metric1": "Retail Revenue", "metric2": None, "kind": "money"},
        {"title": "Retail Accretion (Rs. Crore)", "metric1": "Retail Accretion",
         "metric2": "Retail Revenue CY-Retail Revenue PY", "kind": "money", "mode": "single"},
    ]
    metric_panels_page(pdf, rows, 14, "Retail Revenue", 14, panels)


def slide_15(pdf, rows):
    panels = [
        {"title": "Individual ATS (Rs. per policy)", "metric1": "Individual ATS",
         "metric2": "Individual agents GWP/Individual agents no. of policies", "kind": "money", "mode": "single"},
        {"title": "Average Productivity (Rs. Lakhs per agent)", "metric1": "Average Productivity (per agent)",
         "metric2": "Premium/No. of Individual Agents", "kind": "money", "mode": "single"},
    ]
    metric_panels_page(pdf, rows, 15, "ATS", 15, panels)


STATES8 = ["Uttar Pradesh", "Maharashtra", "Karnataka", "Haryana", "Tamil Nadu", "Kerala", "Delhi", "Others"]
STATE_TO_ZONE = {
    "Uttar Pradesh": "North", "Haryana": "North", "Delhi": "North",
    "Maharashtra": "West",
    "Karnataka": "South", "Tamil Nadu": "South", "Kerala": "South",
}


def slide_16(pdf, rows):
    # Derived from Slide 17's named-state data (standard MHA zonal
    # convention) rather than extracted directly - see Glossary.
    cdata = data.by_company(rows, 17, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    zones = ["North", "West", "South", "Others (unclassified)"]

    def zone_value(company_key, zone):
        states = [s for s in STATES8 if s not in STATE_TO_ZONE] if zone == "Others (unclassified)" \
            else [s for s, z in STATE_TO_ZONE.items() if z == zone]
        vals = [cdata[company_key].get((s, None), (None, None))[0] for s in states]
        vals = [v for v in vals if v is not None]
        return round(sum(vals), 4) if vals else None

    series = {z: [zone_value(k, z) for k in keys] for z in zones}
    if not any(any(v is not None for v in vals) for vals in series.values()):
        return
    colors = {"North": theme.SEGMENT_COLORS["Public"], "West": theme.SEGMENT_COLORS["Private"],
              "South": theme.SEGMENT_COLORS["SAHI"], "Others (unclassified)": "#BFBFBF"}
    fig, panels, ins = new_page("Geographical Distribution: Zones", 16, 1, want_insights=True)
    ax = fig.add_subplot(panels[0])
    charts.stacked_bar(ax, names, series, colors, pct100=True)
    draw_insights(fig, ins, ["Zone split is a best-effort estimate from named-state data - East/Central "
                             "exposure isn't separately identifiable this quarter (see Glossary)."])
    pdf.savefig(fig)
    plt.close(fig)


def slide_17(pdf, rows):
    cdata = data.by_company(rows, 17, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    series = {st: [cdata[k].get((st, None), (None, None))[0] for k in keys] for st in STATES8}
    colors = theme.series_colors_for(STATES8)
    fig, panels, ins = new_page("Geographical Distribution: States", 17, 1, want_insights=True)
    ax = fig.add_subplot(panels[0])
    charts.stacked_bar(ax, names, series, colors, pct100=True)
    draw_insights(fig, ins, ["State-wise GDPI as a share of each company's own GWP."])
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Slide 18: Income Statement
# ---------------------------------------------------------------------------

INCOME_ROWS = ["Gross Written Premium", "Net Written Premium", "Earned Premium", "Investment Income",
               "Claims", "Total Overheads", "PBT", "PAT"]


def slide_18(pdf, rows):
    cdata = data.by_company(rows, 18, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    cur_vals = {label: {k: cdata[k].get((label, None), (None, None))[0] for k in keys} for label in INCOME_ROWS}
    pri_vals = {label: {k: cdata[k].get((label, None), (None, None))[1] for k in keys} for label in INCOME_ROWS}
    bullets = leader_laggard_bullets(keys, [cur_vals["PBT"][k] for k in keys], [pri_vals["PBT"][k] for k in keys],
                                      "money", "PBT")
    fig, panels, ins = new_page("Income Statement", 18, 2, want_insights=bool(bullets))
    ax1 = fig.add_subplot(panels[0])
    charts.income_table(ax1, INCOME_ROWS, keys, cur_vals, "FY26 Q3 (Rs. Crore)")
    ax2 = fig.add_subplot(panels[1])
    charts.income_table(ax2, INCOME_ROWS, keys, pri_vals, "FY25 Q3 (Rs. Crore)")
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Slides 19-23: Key Metrics
# ---------------------------------------------------------------------------

def slide_19(pdf, rows):
    panels = [
        {"title": "Combined Ratio", "metric1": "Combined Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Expense Ratio", "metric1": "Expense Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Loss Ratio", "metric1": "Loss Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
    ]
    metric_panels_page(pdf, rows, 19, "Key Metrics", 19, panels)


def slide_20(pdf, rows):
    panels = [
        {"title": "Claims Settlement Ratio", "metric1": "Claims Settlement Ratio", "metric2": None,
         "kind": "percent", "mode": "single"},
        {"title": "Average Claim Size (Rs.)", "metric1": "Average Claim Size", "metric2": None,
         "kind": "money", "mode": "single"},
        {"title": "No. of Claims to No. of Policies", "metric1": "No. of claims to No. of policies", "metric2": None,
         "kind": "percent", "mode": "single", "higher_is_better": False},
    ]
    metric_panels_page(pdf, rows, 20, "Key Metrics", 20, panels,
                        footnote="Average Claim Size and No. of claims to policies are best-effort estimates.")


def slide_21(pdf, rows):
    panels = [
        {"title": "Opex. To GWP ratio", "metric1": "Opex. To GWP ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Manpower to GWP ratio", "metric1": "Manpower to GWP ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "IT spend to GWP ratio", "metric1": "IT spend to GWP ratio", "metric2": None, "kind": "percent"},
    ]
    metric_panels_page(pdf, rows, 21, "Key Metrics", 21, panels)


def slide_22(pdf, rows):
    panels = [
        {"title": "Manpower cost to total Opex", "metric1": "Manpower cost to total Opex", "metric2": None,
         "kind": "percent"},
        {"title": "Manpower cost per employee (Rs.)", "metric1": "Manpower cost per employee", "metric2": None,
         "kind": "money", "mode": "single"},
        {"title": "Facility rental per office per month (Rs. Lakhs)", "metric1": "Facility rental per office per month",
         "metric2": None, "kind": "money", "mode": "single"},
    ]
    metric_panels_page(pdf, rows, 22, "Key Metrics", 22, panels)


def slide_23(pdf, rows):
    panels = [
        {"title": "Capital (Rs. Crore)", "metric1": "Capital", "metric2": None, "kind": "money"},
        {"title": "Net Worth (Rs. Lakhs)", "metric1": "Net Worth", "metric2": None, "kind": "money"},
        {"title": "PBT (Rs. Crore)", "metric1": "PBT", "metric2": None, "kind": "money"},
    ]
    metric_panels_page(pdf, rows, 23, "Key Metrics", 23, panels)


# ---------------------------------------------------------------------------
# Slides 24-26: Investment / Debt Portfolio
# ---------------------------------------------------------------------------

def _fractions_of_row_total(series_dict, n):
    totals = [sum(v[i] or 0 for v in series_dict.values()) for i in range(n)]
    return {name: [(v[i] / totals[i]) if v[i] is not None and totals[i] else None for i in range(n)]
            for name, v in series_dict.items()}


def two_period_stacked_page(pdf, rows, slide_no, title, page_no, series_names, note, normalize=False):
    cdata = data.pivot_metric1_only(rows, slide_no, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    colors = theme.series_colors_for(series_names)
    cur = {s: [cdata[k].get(s, (None, None))[0] for k in keys] for s in series_names}
    pri = {s: [cdata[k].get(s, (None, None))[1] for k in keys] for s in series_names}
    has_cur = any(any(v is not None for v in vals) for vals in cur.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri.values())
    if not has_cur and not has_pri:
        return
    if normalize:
        if has_cur:
            cur = _fractions_of_row_total(cur, len(keys))
        if has_pri:
            pri = _fractions_of_row_total(pri, len(keys))
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page(title, page_no, n_panels, want_insights=True)
    idx = 0
    if has_cur:
        panel_title(fig, panels[idx], "FY26 Q3")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, cur, colors, pct100=True)
        idx += 1
    if has_pri:
        panel_title(fig, panels[idx], "FY25 Q3")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, pri, colors, pct100=True)
    draw_insights(fig, ins, [note])
    pdf.savefig(fig)
    plt.close(fig)


def slide_24(pdf, rows):
    series_names = ["Corporate Bonds/Debentures", "Govt Bonds", "Deposits", "Equity/Invits/REIT", "Mutual Funds"]
    two_period_stacked_page(pdf, rows, 24, "Investment Portfolio", 24, series_names,
                             "Investment mix as % of each company's own book value.", normalize=True)


def slide_25(pdf, rows):
    series_names = ["Sovereign", "AAA rated", "AA or better", "Rated below AA but above A", "Rated below A"]
    two_period_stacked_page(pdf, rows, 25, "Debt Portfolio: Credit Rating", 25, series_names,
                             "Exposure by credit rating.")


def slide_26(pdf, rows):
    series_names = ["Up to 1 year", "More than 1 year and upto 3 years", "More than 3 years and upto 7 years",
                     "More than 7 years and upto 10 years", "Above 10 years"]
    two_period_stacked_page(pdf, rows, 26, "Debt Portfolio: Residual Maturity", 26, series_names,
                             "Exposure by residual maturity.")


# ---------------------------------------------------------------------------
# Slides 27-31: Historical Trends (2-period comparison)
# ---------------------------------------------------------------------------

TRENDS_NOTE = "Quarterly YoY comparison (FY25 Q3 vs FY26 Q3), not multi-year history."


def slide_27(pdf, rows):
    panels = [
        {"title": "GWP (Rs. Crore)", "metric1": "GWP", "metric2": None, "kind": "money"},
        {"title": "PBT (Rs. Crore)", "metric1": "PBT", "metric2": None, "kind": "money"},
    ]
    metric_panels_page(pdf, rows, 27, "Historical Trends", 27, panels, footnote=TRENDS_NOTE)


def slide_28(pdf, rows):
    panels = [
        {"title": "Combined Ratio", "metric1": "Combined Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Loss Ratio", "metric1": "Loss Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
    ]
    metric_panels_page(pdf, rows, 28, "Historical Trends", 28, panels, footnote=TRENDS_NOTE)


def slide_29(pdf, rows):
    panels = [
        {"title": "Expense Ratio", "metric1": "Expense Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Expense of Management Ratio", "metric1": "Expense of Management Ratio", "metric2": None,
         "kind": "percent", "higher_is_better": False},
    ]
    metric_panels_page(pdf, rows, 29, "Historical Trends", 29, panels, footnote=TRENDS_NOTE)


def slide_30(pdf, rows):
    panels = [
        {"title": "RI Ceding to GWP Ratio", "metric1": "RI Ceding to GWP Ratio", "metric2": "Risk Ceded",
         "kind": "percent", "mode": "single"},
        {"title": "RI Commission to RI Ceding", "metric1": "RI Commission to RI Ceding", "metric2": "Risk Ceded",
         "kind": "percent", "mode": "single"},
    ]
    metric_panels_page(pdf, rows, 30, "Historical Trends", 30, panels, footnote=TRENDS_NOTE)


def slide_31(pdf, rows):
    panels = [
        {"title": "ROE (SAHI)", "metric1": "ROE (SAHI)", "metric2": "PAT/Avg. Net Worth", "kind": "percent",
         "mode": "single"},
        {"title": "Solvency Ratio", "metric1": "Solvency Ratios", "metric2": None, "kind": "ratio"},
    ]
    metric_panels_page(pdf, rows, 31, "Historical Trends", 31, panels, footnote=TRENDS_NOTE)


# ---------------------------------------------------------------------------
# Slides 32-33: AUM
# ---------------------------------------------------------------------------

def slide_32(pdf, rows):
    panels = [
        {"title": "AUM (Overall, Rs. Crore)", "metric1": "AUM (Overall)", "metric2": None, "kind": "money"},
        {"title": "Investment Yield", "metric1": "Investment Yield", "metric2": None, "kind": "percent"},
    ]
    metric_panels_page(pdf, rows, 32, "Asset Under Management", 32, panels)


def slide_33(pdf, rows):
    panels = [
        {"title": "AUM - Policyholders (Rs. Crore)", "metric1": "AUM -Policyholders", "metric2": None,
         "kind": "money"},
        {"title": "AUM - Shareholders (Rs. Crore)", "metric1": "AUM -Shareholders", "metric2": None, "kind": "money"},
    ]
    metric_panels_page(pdf, rows, 33, "Asset Under Management", 33, panels)


# ---------------------------------------------------------------------------
# Slides 34-35: Distribution Footprint
# ---------------------------------------------------------------------------

def slide_34(pdf, rows):
    panels = [
        {"title": "Employees (On-roll)", "metric1": "Employees", "metric2": "On-roll Employee", "kind": "count",
         "mode": "single"},
        {"title": "Individual Agents", "metric1": "Agents", "metric2": "Individual Agents", "kind": "count",
         "mode": "single"},
    ]
    metric_panels_page(pdf, rows, 34, "Distribution Footprint", 34, panels)


INTERMEDIARY_TYPES = ["Individual Agents", "CA-Banks", "CA-Others", "Brokers", "WA", "IMF", "POS"]


def slide_35(pdf, rows):
    cdata = data.by_company(rows, 35, theme.canonical_company)
    offices = data.pivot_metric1_only(rows, 35, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    off_cur = [offices.get(k, {}).get("No. of Offices", (None, None))[0] for k in keys]
    series = {t: [cdata[k].get(("Intermediaries", t), (None, None))[0] for k in keys] for t in INTERMEDIARY_TYPES}
    has_off = any(v is not None for v in off_cur)
    has_int = any(any(v is not None for v in vals) for vals in series.values())
    if not has_off and not has_int:
        return
    n_panels = int(has_off) + int(has_int)
    bullets = leader_laggard_bullets(keys, off_cur, [None] * len(keys), "count", "office count") if has_off else []
    fig, panels, ins = new_page("Distribution Footprint", 35, n_panels, want_insights=bool(bullets))
    idx = 0
    if has_off:
        panel_title(fig, panels[idx], "No. of Offices")
        charts.single_bar(fig, panels[idx], names, off_cur)
        idx += 1
    if has_int:
        panel_title(fig, panels[idx], "Intermediaries by type")
        colors = theme.series_colors_for(INTERMEDIARY_TYPES)
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, series, colors, pct100=False)
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SECTION_FUNCS = [slide_03, slide_04, slide_05, slide_06, slide_07, slide_08, slide_09, slide_10, slide_11,
                 slide_12, slide_13, slide_14, slide_15, slide_16, slide_17, slide_18, slide_19, slide_20,
                 slide_21, slide_22, slide_23, slide_24, slide_25, slide_26, slide_27, slide_28, slide_29,
                 slide_30, slide_31, slide_32, slide_33, slide_34, slide_35]


def build(out_path=OUT_PATH):
    rows = data.load_rows()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with PdfPages(out_path) as pdf:
        cover_page(pdf)
        toc_page(pdf, 1)
        for fn in SECTION_FUNCS:
            fn(pdf, rows)
        glossary_page(pdf, 36)
    print(f"Saved {out_path}")
    return out_path


if __name__ == "__main__":
    build()
