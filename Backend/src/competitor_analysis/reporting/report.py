"""
FY26 Q3 Competition Analysis report generator - PDF (matplotlib), rebuilt
from an earlier python-pptx implementation (since removed) after switching
formats: PDF lets Claude render each page to an image (via pdfplumber) and
visually verify it directly, which a .pptx file does not in this
environment (no PowerPoint/LibreOffice available to render it).

Hard rule throughout: if a metric group has no FY26 Q3 data at all, it is
skipped entirely - no placeholder box, no "not available" note, no empty
chart frame. Every chart-drawing function in pdf_charts.py returns False
when there's nothing to plot, and every section function here checks that
before deciding whether to allocate a panel for it.

Known, deliberate simplifications vs. the FY25 reference deck:
  - Multi-year trend slides (Retail Revenue, ATS, Historical Trends (Key
    Ratios), RI Ceded, ROE & Solvency, Asset Under Management, Distribution
    Footprint) read from reporting.historical / data/historical/
    Historical_Trends.xlsx - a separate, hand-maintained workbook updated
    once a year - not the quarterly Data Engine every other slide reads.
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
from matplotlib.patches import FancyBboxPatch, Polygon, Rectangle
from matplotlib.lines import Line2D

from competitor_analysis.reporting import theme
from competitor_analysis.reporting import charts
from competitor_analysis.reporting import data
from competitor_analysis.reporting import historical
from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis.rounding import fmt_fixed
from competitor_analysis import logging_setup

log = logging_setup.get_logger(__name__)

NUMFMT_PCT = {"percent"}

# Shown in the footer band of every page - see the reference deck screenshot.
VISION_TAGLINE = "Our Vision is, “To become India's most admired Health Insurance Company”"

# Left margin every header/footer element aligns to, clearing _left_border's strip.
_MARGIN_L = 0.075
_MARGIN_R = 0.94


def _left_border(fig):
    """A thin accent-colored strip down the full left edge of the page -
    page chrome, not content, so it's drawn directly on the figure rather
    than through a gridspec panel."""
    fig.add_artist(Rectangle((0, 0), 0.014, 1.0, transform=fig.transFigure,
                              facecolor=theme.BORDER_ACCENT, edgecolor="none", clip_on=False))


def _footer(fig, page_no):
    """Full-width vision-tagline band plus a page-number pennant tucked into
    its top-left corner (a pentagon: rectangle body + a triangular point on
    the right, like a ribbon/flag)."""
    band_y0, band_h = 0.0, 0.032
    fig.add_artist(Rectangle((_MARGIN_L - 0.055, band_y0), (_MARGIN_R + 0.04) - (_MARGIN_L - 0.055), band_h,
                              transform=fig.transFigure, facecolor=theme.BLUE, edgecolor="none", clip_on=False))
    fig.text((_MARGIN_L + _MARGIN_R) / 2, band_y0 + band_h / 2, VISION_TAGLINE, fontsize=8.5, color="white",
              style="italic", ha="center", va="center", transform=fig.transFigure)

    rx0, rw, rtip = _MARGIN_L - 0.055, 0.15, 0.02
    ry0, rh = band_h - 0.006, 0.03
    pennant = [(rx0, ry0), (rx0, ry0 + rh), (rx0 + rw, ry0 + rh), (rx0 + rw + rtip, ry0 + rh / 2), (rx0 + rw, ry0)]
    fig.add_artist(Polygon(pennant, closed=True, transform=fig.transFigure, facecolor=theme.RIBBON_BLUE,
                            edgecolor="none", clip_on=False))
    fig.text(rx0 + 0.016, ry0 + rh / 2, "Page", fontsize=8, color=theme.GREY_TEXT, ha="left", va="center",
              transform=fig.transFigure)
    fig.text(rx0 + 0.066, ry0 + rh / 2, "|", fontsize=9, color=theme.GREY_TEXT, ha="center", va="center",
              transform=fig.transFigure)
    fig.text(rx0 + 0.08, ry0 + rh / 2, str(page_no), fontsize=9.5, fontweight="bold", color=theme.NAVY,
              ha="left", va="center", transform=fig.transFigure)


def _header_footer(fig, title, page_no):
    _left_border(fig)
    # cur_period_label() itself omits the quarter for Q4 (config.period_label).
    fig.text(_MARGIN_L, 0.965, f"Competition Analysis {cfg.cur_period_label()}", fontsize=12.5,
              fontweight="bold", color=theme.NAVY)
    fig.add_artist(Line2D([_MARGIN_L, _MARGIN_R], [0.955, 0.955], transform=fig.transFigure, color=theme.BLUE,
                          linewidth=1.5))
    fig.text(_MARGIN_L, 0.925, title, fontsize=17, fontweight="bold", color=theme.NAVY)
    _footer(fig, page_no)


def new_page(title, page_no, n_panels, height_ratios=None, want_insights=False, hspace=0.32,
             n_insight_lines=None, bottom=0.09):
    """Returns (fig, list_of_subplot_specs_for_panels, subplot_spec_for_insights_or_None).

    The insights row defaults to a fixed 0.32 height ratio (sized for
    draw_insights' 4-line cap) - pass `n_insight_lines` (the real bullet
    count, capped at 4 same as draw_insights itself) to size it to what's
    actually there instead, so 2 short bullets don't sit in a box built for 4.

    `bottom` (default 0.09) is normally enough clearance above the footer,
    because want_insights=True's insights box is the last thing on the page
    and buffers the actual chart panels from it. A page with no insights row
    (e.g. historical_trend_page) has its last chart panel sitting directly
    on `bottom` instead - and panel_box's own pad_bottom (reserved for a
    chart's legend) eats further into that margin - so such callers should
    pass a larger `bottom` to keep the panel's box/legend clear of the
    page-number pennant in the footer."""
    fig = plt.figure(figsize=theme.PAGE_SIZE, dpi=theme.DPI)
    printed_page = page_no + SLIDE_TO_PAGE_OFFSET
    _header_footer(fig, title, printed_page)
    hardcoded = []
    if HARDCODED_INSIGHTS:
        # The page's insights box holds PAGE_INSIGHTS' text (or nothing), and
        # the caller's own bullets are ignored - see draw_insights.
        hardcoded = _wrap_insight_lines(fig, PAGE_INSIGHTS.get(printed_page, []))
        if hardcoded and not want_insights:
            # A page laid out without an insights row (e.g. the trend pages)
            # gets shorter panels once one is added; the gap between them was
            # sized for the taller ones, so widen it or a panel's legend runs
            # into the next panel's title.
            hspace = max(hspace, 0.62)
        want_insights = bool(hardcoded)
    total_rows = n_panels + (1 if want_insights else 0)
    ratios = list(height_ratios) if height_ratios else [1] * n_panels
    if want_insights:
        if hardcoded:
            n = len(hardcoded)
        else:
            n = 4 if n_insight_lines is None else max(1, min(n_insight_lines, 4))
        ratios = ratios + [0.10 + 0.055 * n]
    gs = fig.add_gridspec(total_rows, 1, left=0.09, right=0.94, top=0.85, bottom=bottom, hspace=hspace,
                           height_ratios=ratios)
    panel_specs = [gs[i] for i in range(n_panels)]
    insight_spec = gs[n_panels] if want_insights else None
    if hardcoded:
        _insight_box(fig, insight_spec, "\n".join(hardcoded))
    return fig, panel_specs, insight_spec


# While True, every content page's insights box shows PAGE_INSIGHTS' text for
# that printed page (none if it has no entry) instead of the rule-based
# bullets each slide function computes - set False to restore those.
HARDCODED_INSIGHTS = True

# Keyed by PRINTED page number (Data Engine slide + SLIDE_TO_PAGE_OFFSET).
# Hardcoded for now (Q1'FY27 reference deck) - not derived from the data, so
# it does NOT follow the reporting period.
PAGE_INSIGHTS = {
    4: [  # GI Industry
        "General Insurance Q1’FY27 growth at 10.9% (Q1’FY26 growth at 8.8%)",
        "Segment wise growth % : Health & PA: 22.4% (Q1’FY26: 10.3%), Motor: 13.9% (Q1’FY26: 8.7%), "
        "Fire & Eng: -22.2% (Q1’FY26: 17.6%) & Others: 4.7% (Q1’FY26: -8.4%)",
        "Insurer wise growth%: Pvt: 11.3%, PSU: 3.6%, SAHI: 32.9%, Specialised Insurers: -30.6% & Overall: 10.9%",
        "Without 1/n impact: Pvt: 11.4%, PSU: 3.6%, SAHI: 29.5%, Specialised Insurers: -30.6% & Overall: 10.8%",
    ],
    5: [  # Health Industry
        "Health & PA growth Q1’FY27 : 22.4% (SAHI : 32.9%, Pvt. GI : 31.6%, PSU GI: 7.7%)",
        "Segment wise growth % : Retail: 31.6%, Group: 14.0%, Govt.: 15.2%, Travel: 6.7% & PA: 47.4%",
        "Retail Health & PA are the only segments with increase in market share from 30.9% to 33.2% and "
        "9.0% to 10.8% and for Q1’FY27",
    ],
    6: ["NBHI & STAR are the only SAHI’s with decrease in market share in Q1’FY27"],  # SAHI Market
    7: [  # Segment-wise: Health & PA
        "Retail & Group Health growth at 31.6% & 14.0% in Q1’FY27 (Q1’FY26 Growth: Retail: 9.4% & Group: 9.7%)",
        "Govt. Health growth at 15% primarily on account of Bajaj (2899%), Reliance GI (8%) & Oriental (53%)",
    ],
    8: ["ABHI & CARE have collectively written 72.5% of SAHI PA business & 85.8% of SAHI Travel business "
        "in Q1’FY27"],  # Segment wise SAHI's share
    10: [  # Revenue & Growth % (SAHI)
        "All SAHI’s recorded 20%+ growth (CAGR) over FY18-FY26 ; ABHI CAGR highest at 50%",
        "ABHI recorded highest growth amongst SAHI’s in Q1’FY27 followed by CARE, NBHI & CIGNA",
    ],
    11: ["Group business contributes major proportion for Pvt. & PSU GI players"],  # Segment-wise GDPI: Health
    12: [  # Segment-wise GDPI Mix - SAHI's
        "ABHI, CARE & STAR contribution for retail health segment has increased by 5.3%, 2.8% & 2.7% in "
        "Q1’FY27 as against Q1’FY26",
        "STAR & NBHI with highest contribution from retail segment amongst all SAHI players",
    ],
    13: [  # GDPI by Channel: SAHI's
        "Agency business growth: NBHI at 43%, STAR at 19%, CARE at 44%, CIGNA at 50% & ABHI at 58%",
        "Banca business growth: NBHI at 32%, STAR at -24%, CARE at 21%, CIGNA at 8% & ABHI at 21%",
        "Proprietary channel contribution: NBHI at 45%, STAR at 92%, CARE at 45%, CIGNA at 28% & ABHI at 15%",
    ],
    15: ["STAR with highest retail accretion market share across industry at 22.1% for Q1’FY27"],  # Retail Revenue
    16: [  # ATS
        "Average realisation for all SAHI’s increased over Q1’FY26",
        "SAHI agency ATS realisation is highest for CIGNA followed by NBHI and CARE",
        "All SAHI’s average productivity increased in Q1’FY27 except STAR & ABHI",
    ],
    17: ["Highest contribution: NBHI from North Zone, STAR from South Zone and CARE, CIGNA & ABHI from "
         "West Zone"],  # Geographical Distribution: Zones
    29: [  # Investment Portfolio
        "CIGNA & NBHI with highest exposure in Corporate Bonds followed CARE",
        "NBHI & CARE reduced exposure in Corporate Bonds/Debentures and shifted to Equity / Invits / REIT",
    ],
    30: [  # Debt Portfolio: Credit Rating
        "STAR with highest exposure to AA securities followed by NBHI, CARE & ABHI",
        "All SAHI’s except CIGNA reduced exposure in Sovereign securities & shifted to AAA rated securities",
    ],
}


def _wrap_insight_lines(fig, bullets, fontsize=8.5):
    """The bullets as display lines ("➔" on each bullet's first line), each
    wrapped to the insights box's width by rendered text width."""
    if not bullets:
        return []
    renderer = fig.canvas.get_renderer()
    max_w = (0.94 - 0.09) * 0.93 * fig.get_figwidth() * fig.dpi

    def fits(s):
        t = fig.text(0, 0, s, fontsize=fontsize)
        w = t.get_window_extent(renderer=renderer).width
        t.remove()
        return w <= max_w

    lines = []
    for bullet in bullets:
        out, cur = [], ""
        for word in bullet.split():
            trial = f"{cur} {word}".strip()
            prefix = "➔  " if not out else "     "
            if cur and not fits(prefix + trial):
                out.append(cur)
                cur = word
            else:
                cur = trial
        out.append(cur)
        lines += ["➔  " + out[0]] + ["     " + w for w in out[1:]]
    return lines


def _insight_box(fig, subplot_spec, text):
    ax = fig.add_subplot(subplot_spec)
    ax.axis("off")
    box = FancyBboxPatch((0, 0), 1, 1, transform=ax.transAxes, boxstyle="round,pad=0.02,rounding_size=0.03",
                          linewidth=1, edgecolor=theme.INSIGHT_BORDER, facecolor=theme.INSIGHT_BG,
                          linestyle=(0, (5, 3)), clip_on=False)
    ax.add_patch(box)
    ax.text(0.03, 0.5, text, fontsize=8.5, color=theme.DARK_TEXT, va="center", ha="left", transform=ax.transAxes)


def draw_insights(fig, subplot_spec, bullets):
    """The rule-based insights box. A no-op while HARDCODED_INSIGHTS is on:
    new_page has already drawn that page's hardcoded text (or no box)."""
    if HARDCODED_INSIGHTS or not bullets:
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
    bullets.append(f"{top[0]} has the {word} {metric_name} at {fmt_fixed(top[1] * scale, 1, grouping=True)}{unit}")
    deltas = [(n, c, p, c - p) for n, c, p in pairs if p is not None]
    if deltas:
        best = max(deltas, key=lambda x: x[3])
        worst = min(deltas, key=lambda x: x[3])
        if best[3] > 1e-9:
            bullets.append(f"{best[0]} improved the most YoY (+{fmt_fixed(best[3] * scale, 1, grouping=True)}{unit})")
        if worst[3] < -1e-9 and worst[0] != best[0]:
            bullets.append(f"{worst[0]} declined the most YoY ({fmt_fixed(worst[3] * scale, 1, grouping=True)}{unit})")
    return bullets[:limit]


# ---------------------------------------------------------------------------
# Cover / TOC / Glossary
# ---------------------------------------------------------------------------

def cover_page(pdf):
    fig = plt.figure(figsize=theme.PAGE_SIZE, dpi=theme.DPI)
    fig.patch.set_facecolor("white")
    _left_border(fig)
    band = plt.Rectangle((0, 0.62), 1, 0.09, transform=fig.transFigure, facecolor=theme.NAVY, clip_on=False)
    fig.add_artist(band)
    fig.text(0.08, 0.665, "Competition Analysis", fontsize=30, fontweight="bold", color="white")
    fig.text(0.92, 0.15, "Executive Summary", fontsize=18, fontweight="bold", color=theme.NAVY, ha="right")
    fig.text(0.92, 0.11, f"Period ending {cfg.cur_period_ending_str()} ({cfg.cur_period_label()})",
             fontsize=12, color=theme.NAVY, ha="right")
    pdf.savefig(fig)
    plt.close(fig)


TOC_ENTRIES = [
    ("Key Highlights", "3"),
    ("Overall Industry & Market share", "4-8"), ("Revenue (Segment, Channel, Geographical mix)", "9-18"),
    ("Income statement", "19"), ("Segment performance (Health, PA, Travel)", "20-22"),
    ("Key metrics", "23-28"), ("Investment portfolio", "29-31"),
    ("Historical trends", "32-36"), ("AUM", "37-38"),
    ("Distribution footprints", "39-40"),
]

# The Key Highlights page (page 3) has no Data Engine slide of its own, so
# every content page prints one past its Data Engine slide number. slide_NN
# functions keep passing their Data Engine slide number; new_page adds this.
SLIDE_TO_PAGE_OFFSET = 1


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
        f"Above information is as per public disclosures available on IRDAI/company websites for the quarter ended "
        f"{cfg.cur_period_ending_str()} ({cfg.cur_period_label()}), compared to the quarter ended "
        f"{cfg.prior_period_ending_str()} ({cfg.prior_period_label()}).",
        "SAHI = Stand-alone Health Insurer. GDPI = Gross Direct Premium Income. GWP = Gross Written Premium. NWP = Net Written Premium.",
        "Multi-year trend slides (Retail Revenue, ATS, Historical Trends (Key Ratios), RI Ceded, ROE & Solvency, "
        "Asset Under Management, Distribution Footprint) are sourced from a separate, hand-maintained multi-year "
        "workbook, not the quarterly Data Engine - updated once a year, so their most recent year may lag the "
        "current quarter's own period.",
        "Geographic zone split (North/West/South) is derived from named-state data using the standard Ministry of Home Affairs zonal convention; East and Central aren't separately identifiable from current source disclosures.",
        "Average Claim Size and No. of claims to No. of policies are best-effort estimates, not independently ground-truth-verified.",
        f"Sections with no {cfg.cur_period_label()} data for any company are omitted from this report entirely, rather than shown as an empty or placeholder chart.",
        "Company short names: NBHI = Niva Bupa Health Insurance, STAR = Star Health & Allied Insurance, CARE = Care Health Insurance, CIGNA = ManipalCigna Health Insurance, ABHI = Aditya Birla Health Insurance, Narayana = Narayana Health Insurance, Galaxy = Galaxy Health Insurance.",
        ("This report was generated automatically from Data_Engine_UI.xlsx; insight bullets are analyst-written "
         "commentary entered separately and should be reviewed before external use."
         if HARDCODED_INSIGHTS else
         "This report was generated automatically from Data_Engine_UI.xlsx; insight bullets are rule-based "
         "auto-generated observations and should be reviewed before external use."),
    ]
    y = 0.82
    for line in lines:
        fig.text(0.08, y, f"➔  {line}", fontsize=10, wrap=True, va="top")
        y -= 0.09
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Key Highlights (page 3)
# ---------------------------------------------------------------------------

# Hardcoded for now (Q1'FY27 reference deck) - not derived from the Data
# Engine, so it does NOT follow the reporting period. Each entry: (fill,
# border, bullets, footnote-or-None), drawn top to bottom.
KEY_HIGHLIGHTS = [
    ("#E2EFDA", "#8EA9C1", [
        "General Insurance Q1’FY27 growth at 10.9% against 8.8% in Q1’FY26 (with 1/n impact) and "
        "Q1’FY27 growth for without 1/n impact at 10.8%",
        "Health & PA growth Q1’FY27: 22.4% (SAHI: 32.9%, Pvt. GI: 31.6%, PSU GI: 7.7%)",
    ], None),
    ("#DDE3F3", "#8EA9C1", [
        "Health & PA is the largest segment in GI Industry with Q1’FY27 market share at 49.5% "
        "(Q1’FY26 at 44.8%)",
        "NBHI Retail Health market share for Q1’FY27 at 11.1% (Q1’FY26 at 10.0%)",
    ], None),
    ("#FFF2CC", "#8EA9C1", [
        "NBHI & STAR maintained healthy solvency of 2.25 & 2.09 in Q1’FY27 (highest among SAHI)",
        "ABHI & CARE solvency lowest among SAHI for Q1’FY27 at 1.58",
    ], None),
    ("#FBE5D6", "#8EA9C1", [
        "All SAHI’s profitability improved in Q1’FY27",
        "NBHI recorded highest PAT growth amongst SAHI at 93%",
        "STAR reported the lowest CISR amongst SAHI at 97.1% for Q1’FY27 (-1.6% vs Q1’FY26)",
        "NBHI & STAR were only SAHI’s with improved loss ratio over Q1’FY26",
    ], "Above comments are based on Ind AS Financials for SAHI excluding CIGNA"),
    ("#DDEBF7", "#8EA9C1", [
        "CARE reported outside India business of Rs 6 crs in Q1’FY27 in line with Q1’FY26",
        "ABHI has the highest Claim Settlement Ratio (Number of claims) among SAHI’s at 97.5%",
    ], None),
]


def key_highlights_page(pdf, page_no):
    """Rounded, colour-filled boxes of bullet text (KEY_HIGHLIGHTS), stacked
    down the page and sized to their own wrapped line count, with the
    report's usual header/footer."""
    fig = plt.figure(figsize=theme.PAGE_SIZE, dpi=theme.DPI)
    _header_footer(fig, "Key Highlights", page_no)

    x0, width = 0.09, 0.85
    fontsize, line_h = 11, 0.022
    pad_y, gap, foot_h = 0.02, 0.03, 0.022
    text_x = x0 + 0.025
    max_w = (x0 + width - 0.02 - text_x) * fig.get_figwidth() * fig.dpi  # usable width, pixels
    renderer = fig.canvas.get_renderer()

    def fits(s):
        t = fig.text(0, 0, s, fontsize=fontsize)
        w = t.get_window_extent(renderer=renderer).width
        t.remove()
        return w <= max_w

    def wrap(bullet):
        # Longest-fitting greedy wrap by rendered width, so a line never
        # runs past the box edge whatever the font's character widths.
        out, cur = [], ""
        for word in bullet.split():
            trial = f"{cur} {word}".strip()
            prefix = "▪   " if not out else "     "
            if cur and not fits(prefix + trial):
                out.append(cur)
                cur = word
            else:
                cur = trial
        out.append(cur)
        return ["▪   " + out[0]] + ["     " + w for w in out[1:]]

    boxes = []
    for fill, border, bullets, foot in KEY_HIGHLIGHTS:
        lines = [line for b in bullets for line in wrap(b)]
        boxes.append((fill, border, lines, foot))

    used = sum(len(l) * line_h + 2 * pad_y + (foot_h if f else 0) for _, _, l, f in boxes)
    top, bottom = 0.87, 0.08
    gap = max(gap, (top - bottom - used) / max(len(boxes) - 1, 1)) if len(boxes) > 1 else 0
    gap = min(gap, 0.06)
    y = top
    for fill, border, lines, foot in boxes:
        h = len(lines) * line_h + 2 * pad_y
        fig.add_artist(FancyBboxPatch((x0, y - h), width, h, transform=fig.transFigure,
                                      boxstyle="round,pad=0,rounding_size=0.015", linewidth=1.2,
                                      edgecolor=border, facecolor=fill, clip_on=False))
        for i, line in enumerate(lines):
            fig.text(text_x, y - pad_y - (i + 0.5) * line_h, line, fontsize=fontsize,
                     color=theme.DARK_TEXT, va="center", ha="left", transform=fig.transFigure)
        y -= h
        if foot:
            fig.text(x0 + 0.005, y - foot_h / 2, foot, fontsize=7.5, color=theme.DARK_TEXT, va="center",
                     ha="left", transform=fig.transFigure)
            y -= foot_h
        y -= gap
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Slides 3-7: Overall Industry & Market share
# ---------------------------------------------------------------------------

# Slide 3's "Segment Mix" doughnut clubs the sheet's 9 raw GI-industry
# segment lines into the 4 buckets the reference deck shows; "Others" is
# exactly the deck's own footnote ("Marine, Aviation, Liability, Crop Credit
# and other misc" - the last of those three is already folded into "All
# Other Misc" upstream). Colors are theme.GI_SEGMENT_COLORS, keyed the same way.
GI_SEGMENT_GROUPS = {
    "Fire & Engineering": ["Fire", "Engineering"],
    "Motor": ["Motor Total"],
    "Health, Travel & PA": ["Health", "P.A."],
    "Others": ["Marine Total", "Aviation", "Liability",
               "All Other Misc (Crop Insurance + Credit Guarantee+All other misc)"],
}


def _grouped_metric2(by_m2, names):
    """Sums by_m2's (cur, prior) pairs across `names`, treating the group as
    absent only if none of its members have a value at all."""
    cur_vals = [by_m2.get(n, (None, None))[0] for n in names]
    pri_vals = [by_m2.get(n, (None, None))[1] for n in names]
    cur = sum(v for v in cur_vals if v is not None) if any(v is not None for v in cur_vals) else None
    pri = sum(v for v in pri_vals if v is not None) if any(v is not None for v in pri_vals) else None
    return cur, pri


def industry_share_page(pdf, rows, title, page_no, slide_no, company_key, own_labels, group_label,
                         mix_labels=None, mix_group_map=None, mix_colors=None):
    slide_rows = data.for_slide(rows, slide_no)
    by_m2 = {r["Metric 2"]: (data.num(r[data.CUR]), data.num(r[data.PRIOR]))
             for r in slide_rows if r["Company"] == company_key}
    own_cur = [by_m2.get(m, (None, None))[0] for m in own_labels]
    own_pri = [by_m2.get(m, (None, None))[1] for m in own_labels]
    own_colors = [theme.SEGMENT_COLORS.get(m, theme.ORANGE) for m in own_labels]

    if mix_group_map:
        mix_present = [g for g in mix_group_map if _grouped_metric2(by_m2, mix_group_map[g])[0] is not None]
        mix_disp = mix_present
        mix_cur = [_grouped_metric2(by_m2, mix_group_map[g])[0] for g in mix_present]
        mix_pri = [_grouped_metric2(by_m2, mix_group_map[g])[1] for g in mix_present]
        mix_colors_resolved = [mix_colors.get(g, theme.ORANGE) for g in mix_present]
    else:
        mix_present = [m for m in mix_labels if by_m2.get(m, (None, None))[0]]
        mix_disp = [SEG5_DISPLAY.get(m, m) for m in mix_present]
        mix_cur = [by_m2[m][0] for m in mix_present]
        mix_pri = [by_m2[m][1] for m in mix_present]
        mix_colors_resolved = theme.FALLBACK_SERIES_COLORS[:len(mix_disp)]

    has_own = any(v is not None for v in own_cur + own_pri)
    has_mix = bool(mix_present)
    if not has_own and not has_mix:
        return
    n_panels = int(has_own) + int(has_mix)
    bullets = leader_laggard_bullets(own_labels, own_cur, own_pri, "money", "market share") if has_own else []
    fig, panels, ins = new_page(title, page_no, n_panels, want_insights=bool(bullets), n_insight_lines=len(bullets))
    idx = 0
    if has_own:
        charts.doughnut_pair(fig, panels[idx], cfg.prior_period_label(), cfg.cur_period_label(), own_labels, own_pri,
                              own_cur, own_colors, group_label=group_label, unit_label="INR Crores",
                              title="Market Share")
        idx += 1
    if has_mix:
        charts.doughnut_pair(fig, panels[idx], cfg.prior_period_label(), cfg.cur_period_label(), mix_disp, mix_pri,
                              mix_cur, mix_colors_resolved, group_label=group_label, unit_label="INR Crores",
                              title="Segment Mix")
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_03(pdf, rows):
    industry_share_page(pdf, rows, "GI Industry", 3, 3, "Industry",
                         ["Private", "Public", "SAHI", "Specialized Insurer"], "GI Industry",
                         mix_group_map=GI_SEGMENT_GROUPS, mix_colors=theme.GI_SEGMENT_COLORS)


def slide_04(pdf, rows):
    industry_share_page(pdf, rows, "Health Industry (Inc. PA & Travel)", 4, 4, "Health Industry (Inc. PA and Travel)",
                         ["Private", "Public", "SAHI"], "Health Industry",
                         mix_labels=["Health-Retail", "Health-Group", "Health-Government schemes",
                                     "Overseas Medical", "P.A."])


def slide_05(pdf, rows):
    slide_rows = data.for_slide(rows, 5)
    by_company = {}
    for r in slide_rows:
        key = theme.canonical_company(r["Metric 2"])
        if key:
            by_company[key] = (data.num(r[data.CUR]), data.num(r[data.PRIOR]))
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
                                 want_insights=bool(bullets), n_insight_lines=len(bullets))
    charts.doughnut_pair(fig, panels[0], cfg.prior_period_label(), cfg.cur_period_label(), names, pri, cur, colors,
                          group_label="SAHI Market", unit_label="INR Crores", title="Market Share")
    if has_change:
        panel_title(fig, panels[1], "Market Share Change (%)")
        ax = fig.add_subplot(panels[1])
        charts.change_bar(ax, changes)
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


SEG5 = ["Health-Retail", "Health-Group", "Health-Government schemes", "Overseas Medical", "P.A."]

# Slide 6's x-axis (segments) and stack (player-type group) display names -
# "Travel" matches slides 10/11/12's own short name for the same
# Overseas Medical line, not a different underlying column.
SEG5_DISPLAY = {"Health-Retail": "Retail", "Health-Group": "Group", "Health-Government schemes": "Govt",
                "Overseas Medical": "Travel", "P.A.": "PA"}
SEG6_GROUPS = ["SAHI Market", "Pvt GI", "Public GI"]
SEG6_GROUP_DISPLAY = {"SAHI Market": "SAHI", "Pvt GI": "Pvt GI players", "Public GI": "Public GI players"}
SEG6_GROUP_COLORS = {"SAHI Market": theme.SEGMENT_COLORS["SAHI"], "Pvt GI": theme.SEGMENT_COLORS["Private"],
                      "Public GI": theme.SEGMENT_COLORS["Public"]}


def slide_06(pdf, rows):
    cdata = data.metric2_by_group(rows, 6, "Company")
    groups = [g for g in SEG6_GROUPS if g in cdata]
    if not groups:
        return
    seg_names = [SEG5_DISPLAY[s] for s in SEG5]
    colors = {SEG6_GROUP_DISPLAY[g]: SEG6_GROUP_COLORS[g] for g in groups}
    cur_series = {SEG6_GROUP_DISPLAY[g]: [cdata[g].get(seg, (None, None))[0] for seg in SEG5] for g in groups}
    pri_series = {SEG6_GROUP_DISPLAY[g]: [cdata[g].get(seg, (None, None))[1] for seg in SEG5] for g in groups}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return

    bullets = ["Health-Group is the largest segment for both Private and Public GI players.",
               "SAHI's mix skews more heavily to Health-Retail than Private/Public GI.",
               "Bars show each segment's own mix (%); above a bar is its total GDPI and YoY growth vs last year."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("Segment-wise: Health & PA", 6, n_panels, want_insights=True, hspace=0.75,
                                 n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"Industry {cfg.cur_period_label()}", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, seg_names, cur_series, colors, pct100=True, show_totals=True, show_yaxis=False,
                           prior_series=pri_series)
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=f"Industry {cfg.prior_period_label()}", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, seg_names, pri_series, colors, pct100=True, show_totals=True, show_yaxis=False)
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_07(pdf, rows):
    cdata = data.metric2_by_group(rows, 7, "Company", canonical_fn=theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    seg_names = [SEG5_DISPLAY[s] for s in SEG5]
    colors = {disp: theme.COMPANY_COLORS[k] for k, disp in zip(keys, names)}
    cur_series = {disp: [cdata[k].get(seg, (None, None))[0] for seg in SEG5] for k, disp in zip(keys, names)}
    pri_series = {disp: [cdata[k].get(seg, (None, None))[1] for seg in SEG5] for k, disp in zip(keys, names)}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return

    bullets = ["Retail remains the dominant segment across most SAHI players.",
               "Bars show each segment's mix (%) across SAHI companies; above a bar is its total GDPI and YoY growth."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("Segment wise SAHI's share", 7, n_panels, want_insights=True, hspace=0.75,
                                 n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"Segment wise SAHI's share ({cfg.cur_period_label()})",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, seg_names, cur_series, colors, pct100=True, show_totals=True, show_yaxis=False,
                           prior_series=pri_series)
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=f"Segment wise SAHI's share ({cfg.prior_period_label()})",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, seg_names, pri_series, colors, pct100=True, show_totals=True, show_yaxis=False)
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Slides 8-17: Revenue (Segment, Channel, Geographical mix)
# ---------------------------------------------------------------------------

def slide_08(pdf, rows):
    """Per-company GDPI panel is unchanged (Data Engine, 2-period). Adds a
    second panel - SAHI vs Health Industry GDPI growth, multi-year - sourced
    from reporting.historical's "GDPI Growth" table's SAHI/Industry
    aggregate rows (not company rows, so it can't come from `data.by_company`,
    which resolves everything through theme.canonical_company)."""
    cdata = data.by_company(rows, 8, theme.canonical_company)
    keys, prior, current = data.metric_series(cdata, "Revenue Growth (GDPI)", None)
    bullets = []
    if keys:
        bullets = leader_laggard_bullets(keys, current, prior, "money", "GDPI")
        slide_rows = data.for_slide(rows, 8)
        for r in slide_rows:
            if r["Company"] in ("Industry Total", "Stand-alone Health sub Total") and r["Meric 1"] == "Growth %":
                v = data.num(r[data.CUR])
                if v is not None:
                    bullets.append(f"{r['Company']} growth %: {fmt_fixed(v * 100, 1)}%")

    growth_table = historical.get_table("GDPI Growth")
    growth_keys = [k for k in ("SAHI", "Industry") if growth_table and k in growth_table]
    growth_years = growth_values = None
    if growth_keys:
        growth_years, growth_values = _trend_series(rows, "GDPI Growth", growth_table, growth_keys)

    if not keys and not growth_keys:
        return
    n_panels = int(bool(keys)) + int(bool(growth_keys))
    fig, panels, ins = new_page("Revenue & Growth %", 8, n_panels, want_insights=bool(bullets),
                                 hspace=0.5, n_insight_lines=len(bullets[:4]))
    idx = 0
    if keys:
        panel_title(fig, panels[idx], "Revenue Growth (GDPI)")
        charts.grouped_bar(fig, panels[idx], disp_names(keys), prior, current, show_growth=True)
        idx += 1
    if growth_keys:
        charts.trend_lines(fig, panels[idx], growth_years, growth_keys, growth_values,
                            title="SAHI vs Health Industry (Growth)", is_percent=True, unit_label=None,
                            colors=theme.SEGMENT_COLORS)
    draw_insights(fig, ins, bullets[:4])
    pdf.savefig(fig)
    plt.close(fig)


def slide_09(pdf, rows):
    """Both panels sourced from reporting.historical, not the Data Engine
    `rows` (kept as a parameter only so SECTION_FUNCS can call every
    slide_NN(pdf, rows) uniformly): GDPI Growth's company rows (not its
    SAHI/Industry aggregate rows - those are slide_08's panel) as a
    multi-year trend, plus each company's GDPI CAGR over the same table's
    full span (FY18 through the most recent year the active period allows -
    historical.cagr) as a single bar chart, since a CAGR is one number per
    company, not a year-by-year series."""
    growth_table = historical.get_table("GDPI Growth")
    growth_keys = [k for k in data.COMPANY_ORDER if growth_table and k in growth_table]
    growth_years = growth_values = None
    if growth_keys:
        growth_years, growth_values = _trend_series(rows, "GDPI Growth", growth_table, growth_keys)

    gdpi_table = historical.get_table("GDPI")
    gdpi_years = historical.sorted_years(gdpi_table) if gdpi_table else []
    cagr_map = historical.cagr(gdpi_table) if gdpi_table else {}
    # The table's SAHI aggregate row gets its own CAGR bar after the companies.
    cagr_keys = [k for k in data.COMPANY_ORDER + ["SAHI"] if k in cagr_map]
    cagr_values = [cagr_map[k] for k in cagr_keys]

    if not growth_keys and not cagr_keys:
        return
    n_panels = int(bool(growth_keys)) + int(bool(cagr_keys))
    fig, panels, _ins = new_page("Revenue & Growth % (SAHI)", 9, n_panels, hspace=0.5, bottom=0.13)
    idx = 0
    if growth_keys:
        charts.trend_lines(fig, panels[idx], growth_years, growth_keys, growth_values,
                            title="GDPI Growth % (YoY)", is_percent=True, unit_label=None)
        idx += 1
    if cagr_keys:
        charts.panel_box(fig, panels[idx], title=f"GDPI CAGR ({gdpi_years[0]}-{gdpi_years[-1]})")
        charts.single_bar(fig, panels[idx], disp_names(cagr_keys), cagr_values, is_percent=True)
    pdf.savefig(fig)
    plt.close(fig)


def _segment_abs_total(by_seg, i):
    """A group's/company's absolute Health+PA GDPI (Rs. Crore) for period
    index i (0=cur, 1=prior): the sum of its SEG5 values on Slide 6/7, which
    carry absolute figures. Slides 10/11 store only the resulting mix
    fractions (verified: each Slide 10/11 share equals that segment's
    Slide 6/7 value over this sum), so their bar totals come from here.
    None when no segment has a value."""
    vals = [by_seg.get(s, (None, None))[i] for s in SEG5]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _slide8_gdpi_totals(rows, keys, i):
    """Each company's GDPI (Rs. Crore, from GIC via Slide 8) for period
    index i (0=cur, 1=prior), aligned to `keys` - the bar total shown on
    Slides 12/16/17, whose stored values are only mix fractions. Used rather
    than each slide's own denominator (NL-36 Total (A) / GWP) so a company
    shows one total across every revenue-mix slide."""
    gdpi = data.by_company(rows, 8, theme.canonical_company)
    out = []
    for k in keys:
        pair = next((v for (m1, _), v in gdpi.get(k, {}).items() if m1 == "Revenue Growth (GDPI)"), None)
        out.append(pair[i] if pair else None)
    return out


def slide_10(pdf, rows):
    # SAHI's own values are a clean 0-1 mix; Industry/Public GI/Pvt. GI are
    # absolute Rs. Crore (Phase 2's own finding - GT's figures for those
    # don't fit a percentage convention as an ABSOLUTE value). That doesn't
    # block a %-mix stacked bar though: pct100 normalizes each bar by its
    # own column total, which is scale-invariant - correct whether that
    # bar's inputs were already fractions or absolute money.
    cdata = data.metric2_by_group(rows, 10, "Meric 1")
    groups = [g for g in ("Industry", "Pvt. GI", "Public GI", "SAHI") if g in cdata]
    if not groups:
        return
    segs = ["Retail", "Group", "Govt.", "Travel", "PA"]
    labels = [s for s in segs if any(s in cdata[g] for g in groups)]
    if not labels:
        return
    colors = theme.series_colors_for(labels)
    cur_series = {s: [cdata[g].get(s, (None, None))[0] for g in groups] for s in labels}
    pri_series = {s: [cdata[g].get(s, (None, None))[1] for g in groups] for s in labels}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return

    sahi_abs = data.metric2_by_group(rows, 6, "Company").get("SAHI Market", {})

    def _totals(vals_by_seg, i):
        return [_segment_abs_total(sahi_abs, i) if g == "SAHI"
                else sum(v for v in (vals_by_seg[s][j] for s in labels) if v is not None)
                for j, g in enumerate(groups)]

    bullets = ["Segment mix (Retail/Group/Govt./Travel/PA) as a share of each group's own GDPI.",
               "SAHI's mix skews more heavily to Retail than Industry/Pvt. GI/Public GI."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("Segment-wise GDPI: Health", 10, n_panels, want_insights=True, hspace=0.75,
                                 n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"Segment-wise GDPI Mix ({cfg.cur_period_label()})",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, groups, cur_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_totals(cur_series, 0))
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=f"Segment-wise GDPI Mix ({cfg.prior_period_label()})",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, groups, pri_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_totals(pri_series, 1))
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_11(pdf, rows):
    cdata = data.metric2_by_group(rows, 11, "Meric 1", canonical_fn=theme.canonical_company)
    ordered = [k for k in data.COMPANY_ORDER if k in cdata]
    if not ordered:
        return
    names = disp_names(ordered)
    segs = ["Retail", "Group", "Govt.", "Travel", "PA"]
    colors = theme.series_colors_for(segs)
    cur_series = {seg: [cdata[k].get(seg, (None, None))[0] for k in ordered] for seg in segs}
    pri_series = {seg: [cdata[k].get(seg, (None, None))[1] for k in ordered] for seg in segs}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return

    company_abs = data.metric2_by_group(rows, 7, "Company", canonical_fn=theme.canonical_company)
    cur_totals = [_segment_abs_total(company_abs.get(k, {}), 0) for k in ordered]
    pri_totals = [_segment_abs_total(company_abs.get(k, {}), 1) for k in ordered]

    bullets = ["Segment mix (% of own GDPI) per SAHI company."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("", 11, n_panels, want_insights=True, hspace=0.75, n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"Segment-wise GDPI Mix - SAHI's ({cfg.cur_period_label()})",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, cur_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=cur_totals)
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=f"Segment-wise GDPI Mix - SAHI's ({cfg.prior_period_label()})",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, pri_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=pri_totals)
    draw_insights(fig, ins, bullets)
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
    colors = theme.series_colors_for(channels)
    # This slide's Data Engine values are already a fraction of each
    # company's own GWP (Phase 2's fix_slide8_and_slide12 converts them from
    # the originally-extracted absolute Rs. Crore in a late pipeline stage) -
    # not absolute Rs. Crore, despite Slide 6/7/13's similar-looking channel
    # breakdowns being absolute. pct100=True re-normalizes (a near no-op
    # since they already sum to ~1) and gets the axis/labels right.
    cur_series = {ch: [cdata[k].get(ch, (None, None))[0] for k in ordered] for ch in channels}
    pri_series = {ch: [cdata[k].get(ch, (None, None))[1] for k in ordered] for ch in channels}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return

    bullets = ["Channel mix as % of each SAHI company's own GDPI."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("GDPI by Channel: SAHI's", 12, n_panels, want_insights=True, hspace=0.75,
                                 n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"SAHI's ({cfg.cur_period_label()})", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, cur_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_slide8_gdpi_totals(rows, ordered, 0))
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=f"SAHI's ({cfg.prior_period_label()})", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, pri_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_slide8_gdpi_totals(rows, ordered, 1))
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


# Must match data_engine.SLIDE13_RATE_METRIC1 (the template's Meric 1 text).
COMMISSION_RATE_METRIC1 = "Channel-wise Commission % to Channel Premium"

CHANNEL8 = ["Individual Agents", "Corporate Agents-Banks", "Corporate Agents-Others", "Brokers",
            "CSC", "IMF", "Web Aggregator", "POS"]


def slide_13(pdf, rows):
    cdata = data.by_company(rows, 13, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    colors = theme.series_colors_for(CHANNEL8)
    cur_series = {ch: [cdata[k].get(("Channel-wise Gross Commision % to GDPI", ch), (None, None))[0] for k in keys]
                  for ch in CHANNEL8}
    pri_series = {ch: [cdata[k].get(("Channel-wise Gross Commision % to GDPI", ch), (None, None))[1] for k in keys]
                  for ch in CHANNEL8}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return
    # Data Engine cell stays Rs. Lakhs (GT-verified) - rescale to Rs. Crore
    # for this chart's display/totals only, same convention as Slide 23.
    cur_series = {ch: [v * 0.01 if v is not None else None for v in vals] for ch, vals in cur_series.items()}
    pri_series = {ch: [v * 0.01 if v is not None else None for v in vals] for ch, vals in pri_series.items()}
    # Each channel's commission as % of that channel's own premium (NL-6 /
    # NL-36, computed in extraction). Matching the reference deck, segments
    # are SIZED by these rates (normalized to a full bar) and labelled with
    # them; the total above the bar is the company's gross commission.
    cur_rates = {ch: [cdata[k].get((COMMISSION_RATE_METRIC1, ch), (None, None))[0] for k in keys] for ch in CHANNEL8}
    pri_rates = {ch: [cdata[k].get((COMMISSION_RATE_METRIC1, ch), (None, None))[1] for k in keys] for ch in CHANNEL8}
    if not any(v is not None for vals in cur_rates.values() for v in vals):
        # A workbook built before these rows existed: leave segments
        # unlabelled rather than fall back to share-of-commission %s, which
        # read as the commission rate and aren't.
        log.warning("Slide 13: no '%s' rows in this Data Engine - rebuild it to label channel "
                    "commission rates.", COMMISSION_RATE_METRIC1)

    def rate_bars(amounts, rates):
        """(series, segment labels, totals above bars): rate-sized segments
        when the rates exist, else (older workbook) commission-share
        segments with no labels."""
        totals = [sum(v for v in (amounts[ch][j] for ch in CHANNEL8) if v is not None) or None
                  for j in range(len(keys))]
        if not any(v is not None for vals in rates.values() for v in vals):
            return amounts, rates, totals
        # A zero/negative rate (a commission reversal on a tiny channel, e.g.
        # STAR POS in Q1 FY27) gets no segment - it can't be a bar height.
        rates = {ch: [v if v is not None and v > 0 else None for v in vals] for ch, vals in rates.items()}
        return rates, rates, totals

    cur_heights, cur_labels, cur_totals = rate_bars(cur_series, cur_rates)
    pri_heights, pri_labels, pri_totals = rate_bars(pri_series, pri_rates)

    bullets = ["Segment: channel commission as % of that channel's own premium (NL-6 / NL-36).",
               "Total above the bar: company's gross commission (INR Crores)."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("Channel-wise Commission: SAHI's", 13, n_panels, want_insights=True, hspace=0.75,
                                 n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"Channel-wise Gross Commission % to GDPI {cfg.cur_period_label()}",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, cur_heights, colors, pct100=True, show_yaxis=False, show_totals=True,
                           segment_pcts=cur_labels, display_totals=cur_totals)
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx],
                          title=f"Channel-wise Gross Commission % to GDPI {cfg.prior_period_label()}",
                          unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, pri_heights, colors, pct100=True, show_yaxis=False, show_totals=True,
                           segment_pcts=pri_labels, display_totals=pri_totals)
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


def metric_panels_page(pdf, rows, slide_no, title, page_no, panels_def, footnote=None, key_field="Company"):
    cdata = data.by_company(rows, slide_no, theme.canonical_company, key_field)
    resolved = []
    for pdef in panels_def:
        keys, prior, current = data.metric_series(cdata, pdef["metric1"], pdef.get("metric2"))
        if not keys:
            continue
        scale = pdef.get("scale")
        if scale is not None:
            # Display-only unit rescale (e.g. Lakhs -> Crore) - the
            # underlying Data Engine cell keeps its GT-verified unit;
            # only this chart's numbers/insight bullets change.
            current = [v * scale if v is not None else None for v in current]
            prior = [v * scale if v is not None else None for v in prior]
        resolved.append((pdef, keys, prior, current))
    if not resolved:
        return
    bullets = []
    for pdef, keys, prior, current in resolved:
        bullets += leader_laggard_bullets(keys, current, prior, pdef["kind"], pdef["title"],
                                           pdef.get("higher_is_better", True))
    # draw_insights only ever shows the first 4 lines - reserve a slot for
    # the footnote up front rather than appending it and having it silently
    # truncated away when there are already 4 leader/laggard bullets.
    # Computed before new_page() so its box can be sized to how many lines
    # are actually here, not a fixed 4-line guess.
    ins_bullets = (bullets[:3] + [footnote]) if footnote else bullets[:4]
    fig, panels, ins = new_page(title, page_no, len(resolved), want_insights=bool(ins_bullets),
                                 hspace=0.75, n_insight_lines=len(ins_bullets))
    for (pdef, keys, prior, current), spec in zip(resolved, panels):
        charts.panel_box(fig, spec, title=pdef["title"])
        is_pct = pdef["kind"] == "percent"
        # Grouped (cur+prior) whenever any prior value actually exists for
        # this metric - some schedules (NL-37 claims count, NL-41
        # offices/employees) never carry a prior-year comparative, so a
        # single current-only bar is all there is until data_engine's
        # backfill_prior_from_last_year() fills a gap from last year's own
        # filing. Auto-detected from the data rather than a static per-panel
        # flag, so a metric that gains prior-year coverage (via that
        # backfill, or the source schedule itself) switches to grouped
        # automatically with no report.py change needed.
        if any(v is not None for v in prior):
            charts.grouped_bar(fig, spec, disp_names(keys), prior, current, is_percent=is_pct)
        else:
            charts.single_bar(fig, spec, disp_names(keys), current, is_percent=is_pct)
    draw_insights(fig, ins, ins_bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_14(pdf, rows):
    """Multi-year trend, sourced from reporting.historical - replaces this
    slide's earlier 2-period cur/prior bars. `rows` unused, kept only so
    SECTION_FUNCS can call every slide_NN(pdf, rows) uniformly."""
    historical_trend_page(pdf, rows, "Retail Revenue", 14, [
        {"title": "Retail Health"},
        {"title": "Retail Health Accretion"},
    ])


def slide_15(pdf, rows):
    """Multi-year trend, sourced from reporting.historical - replaces this
    slide's earlier 2-period cur/prior bars (Average Productivity in
    particular had no real prior-period value at all: its agent-count input
    comes from NL-41, a point-in-time snapshot form - see
    KNOWN_ISSUES.md - so a single-period bar was always a weak picture
    here). `rows` (the Data Engine's 2-period rows) goes unused, kept only
    so SECTION_FUNCS can call every slide_NN(pdf, rows) uniformly."""
    historical_trend_page(pdf, rows, "ATS", 15, [
        {"title": "ATS", "unit_label": "Rs. per policy"},
        {"title": "Average Productivity (per agent)", "panel_title": "Average Productivity",
         "unit_label": "Rs. Lakhs per agent", "value_fmt": lambda v: fmt_fixed(v, 2, grouping=True)},
    ])


STATES8 = ["Uttar Pradesh", "Maharashtra", "Karnataka", "Haryana", "Tamil Nadu", "Kerala", "Delhi", "Others"]


def slide_16(pdf, rows):
    # Zone rows are already computed and written into the Data Engine during
    # Phase 2 (extraction/data_engine.py's STATE_TO_ZONE derivation) - this
    # just reads them, like every other slide, rather than re-deriving zones
    # from Slide 17's state data at render time.
    cdata = data.by_company(rows, 16, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    zones = ["North", "South", "East", "West", "Central"]
    # "East" collided with SAHI's own green (theme.SEGMENT_COLORS["SAHI"],
    # already used for "South") in the pre-merge version of this dict -
    # picked ORANGE instead so all 5 zones stay visually distinct.
    colors = {"North": theme.SEGMENT_COLORS["Public"], "South": theme.SEGMENT_COLORS["SAHI"],
              "East": theme.ORANGE, "West": theme.SEGMENT_COLORS["Private"], "Central": "#7C3AED"}
    cur_series = {z: [cdata[k].get((z, None), (None, None))[0] for k in keys] for z in zones}
    pri_series = {z: [cdata[k].get((z, None), (None, None))[1] for k in keys] for z in zones}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return

    bullets = ["Zone split is derived from named-state data (standard MHA zonal convention)."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("Geographical Distribution: Zones", 16, n_panels, want_insights=True, hspace=0.75,
                                 n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"Revenue Mix ({cfg.cur_period_label()})", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, cur_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_slide8_gdpi_totals(rows, keys, 0))
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=f"Revenue Mix ({cfg.prior_period_label()})", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, pri_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_slide8_gdpi_totals(rows, keys, 1))
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


def slide_17(pdf, rows):
    cdata = data.by_company(rows, 17, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    names = disp_names(keys)
    colors = theme.series_colors_for(STATES8)
    cur_series = {st: [cdata[k].get((st, None), (None, None))[0] for k in keys] for st in STATES8}
    pri_series = {st: [cdata[k].get((st, None), (None, None))[1] for k in keys] for st in STATES8}
    has_cur = any(any(v is not None for v in vals) for vals in cur_series.values())
    has_pri = any(any(v is not None for v in vals) for vals in pri_series.values())
    if not has_cur and not has_pri:
        return

    bullets = ["State-wise GDPI as a share of each company's own GWP."]
    n_panels = int(has_cur) + int(has_pri)
    fig, panels, ins = new_page("Geographical Distribution: States", 17, n_panels, want_insights=True, hspace=0.75,
                                 n_insight_lines=len(bullets))
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=f"Geographical Distribution ({cfg.cur_period_label()})", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, cur_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_slide8_gdpi_totals(rows, keys, 0))
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=f"Geographical Distribution ({cfg.prior_period_label()})", unit_label="INR Crores")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, pri_series, colors, pct100=True, show_yaxis=False, show_totals=True,
                           display_totals=_slide8_gdpi_totals(rows, keys, 1))
    draw_insights(fig, ins, bullets)
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
    fig, panels, ins = new_page("Income Statement", 18, 2, want_insights=bool(bullets),
                                 n_insight_lines=len(bullets[:4]))
    ax1 = fig.add_subplot(panels[0])
    charts.income_table(ax1, INCOME_ROWS, keys, cur_vals, f"{cfg.cur_period_label()} (Rs. Crore)")
    ax2 = fig.add_subplot(panels[1])
    charts.income_table(ax2, INCOME_ROWS, keys, pri_vals, f"{cfg.prior_period_label()} (Rs. Crore)")
    draw_insights(fig, ins, bullets)
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Slides 19-21: Segment-wise Performance (Health / Personal Accident / Travel)
# Deterministic NL-4/5/6/7 extraction (extract_segment_income_statement),
# same "two income_table panels, cur then prior" shape as slide 18 - unlike
# slide 18 there is no NL-1/NL-2 company-wide summary to read the net lines
# from at this granularity, so every raw figure comes from its own segmented
# schedule (see data_engine.extract_segment_income_statement's docstring).
# ---------------------------------------------------------------------------

SEGMENT_ROWS = ["Gross Written Premium", "Net Written Premium", "Earned Premium", "Claims",
                "Gross Commission", "RI Commission", "Net Commission", "Operating Expenses",
                "UW Profit/(Loss)", "Combined Ratio", "Loss Ratio", "Expense Ratio"]
SEGMENT_PERCENT_ROWS = {"Combined Ratio", "Loss Ratio", "Expense Ratio"}


def segment_income_statement_page(pdf, rows, title, page_no, slide_no):
    cdata = data.by_company(rows, slide_no, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    if not keys:
        return
    cur_vals = {label: {k: cdata[k].get((label, None), (None, None))[0] for k in keys} for label in SEGMENT_ROWS}
    pri_vals = {label: {k: cdata[k].get((label, None), (None, None))[1] for k in keys} for label in SEGMENT_ROWS}
    fig, panels, _ins = new_page(title, page_no, 2)
    ax1 = fig.add_subplot(panels[0])
    charts.income_table(ax1, SEGMENT_ROWS, keys, cur_vals, f"{cfg.cur_period_label()} (Rs. Crore)",
                         percent_rows=SEGMENT_PERCENT_ROWS)
    ax2 = fig.add_subplot(panels[1])
    charts.income_table(ax2, SEGMENT_ROWS, keys, pri_vals, f"{cfg.prior_period_label()} (Rs. Crore)",
                         percent_rows=SEGMENT_PERCENT_ROWS)
    pdf.savefig(fig)
    plt.close(fig)


def slide_19(pdf, rows):
    segment_income_statement_page(pdf, rows, "Segment Performance - Health", 19, 19)


def slide_20(pdf, rows):
    segment_income_statement_page(pdf, rows, "Segment Performance - Personal Accident", 20, 20)


def slide_21(pdf, rows):
    segment_income_statement_page(pdf, rows, "Segment Performance - Travel", 21, 21)


# ---------------------------------------------------------------------------
# Slides 22-26: Key Metrics
# ---------------------------------------------------------------------------

def slide_22(pdf, rows):
    panels = [
        {"title": "Combined Ratio", "metric1": "Combined Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Expense Ratio", "metric1": "Expense Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Loss Ratio", "metric1": "Loss Ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
    ]
    metric_panels_page(pdf, rows, 22, "Key Metrics", 22, panels)


def slide_23(pdf, rows):
    panels = [
        {"title": "Claims Settlement Ratio", "metric1": "Claims Settlement Ratio", "metric2": None,
         "kind": "percent"},
        # Data Engine cell stays plain Rs. (claims Rs. / claims_settled count) -
        # "scale" converts only this chart's display to Rs. Lakhs.
        {"title": "Average Claim Size (Rs. Lakhs)", "metric1": "Average Claim Size", "metric2": None,
         "kind": "money", "scale": 1e-5},
        {"title": "No. of Claims to No. of Policies", "metric1": "No. of claims to No. of policies", "metric2": None,
         "kind": "percent", "higher_is_better": False},
    ]
    metric_panels_page(pdf, rows, 23, "Key Metrics", 23, panels,
                        footnote="Average Claim Size and No. of claims to policies are best-effort estimates.")


def slide_24(pdf, rows):
    panels = [
        {"title": "Opex. To GWP ratio", "metric1": "Opex. To GWP ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "Manpower to GWP ratio", "metric1": "Manpower to GWP ratio", "metric2": None, "kind": "percent",
         "higher_is_better": False},
        {"title": "IT spend to GWP ratio", "metric1": "IT spend to GWP ratio", "metric2": None, "kind": "percent"},
    ]
    metric_panels_page(pdf, rows, 24, "Key Metrics", 24, panels)


def slide_25(pdf, rows):
    panels = [
        {"title": "Manpower cost to total Opex", "metric1": "Manpower cost to total Opex", "metric2": None,
         "kind": "percent"},
        {"title": "Manpower cost per employee (INR Lakhs)", "metric1": "Manpower cost per employee", "metric2": None,
         "kind": "money"},
        {"title": "Facility rental per office per month (Rs. Lakhs)", "metric1": "Facility rental per office per month",
         "metric2": None, "kind": "money"},
    ]
    metric_panels_page(pdf, rows, 25, "Key Metrics", 25, panels)


def slide_26(pdf, rows):
    panels = [
        {"title": "Capital (Rs. Crore)", "metric1": "Capital", "metric2": None, "kind": "money"},
        # Data Engine cell stays Rs. Lakhs (GT-verified) - "scale" converts
        # only this chart's display (and its insight bullet) to Rs. Crore.
        {"title": "Net Worth (Rs. Crore)", "metric1": "Net Worth", "metric2": None, "kind": "money", "scale": 0.01},
        {"title": "PBT (Rs. Crore)", "metric1": "PBT", "metric2": None, "kind": "money"},
    ]
    metric_panels_page(pdf, rows, 26, "Key Metrics", 26, panels)


# ---------------------------------------------------------------------------
# Slide 27: Claims & Grievances - follows the Key Metrics pages (22-26) under
# the same title. NL-37's amount block and NL-45 items 6/7, current period
# read from the filing, prior backfilled from last year's Data Engine (see
# data_engine.PRIOR_BACKFILL_TARGETS).
# ---------------------------------------------------------------------------

def slide_27(pdf, rows):
    panels = [
        {"title": "Claim Settlement Ratio (Amount)", "metric1": "Claim Settlement Ratio (Amount)", "metric2": None,
         "kind": "percent"},
        {"title": "Claim Complaints per 10,000 claims", "metric1": "Claim Complaints per 10,000 claims",
         "metric2": None, "kind": "number", "higher_is_better": False},
        {"title": "Policy Complaints per 10,000 policies", "metric1": "Policy Complaints per 10,000 policies",
         "metric2": None, "kind": "number", "higher_is_better": False},
    ]
    metric_panels_page(pdf, rows, 27, "Key Metrics", 27, panels)


# ---------------------------------------------------------------------------
# Slides 28-30: Investment / Debt Portfolio
# ---------------------------------------------------------------------------

def _fractions_of_row_total(series_dict, n):
    totals = [sum(v[i] or 0 for v in series_dict.values()) for i in range(n)]
    return {name: [(v[i] / totals[i]) if v[i] is not None and totals[i] else None for i in range(n)]
            for name, v in series_dict.items()}


def two_period_stacked_page(pdf, rows, slide_no, title, page_no, series_names, note, normalize=False,
                             show_totals=False, unit_label=None):
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
    fig, panels, ins = new_page(title, page_no, n_panels, want_insights=True, hspace=0.75, n_insight_lines=1)
    idx = 0
    if has_cur:
        charts.panel_box(fig, panels[idx], title=cfg.cur_period_label(), unit_label=unit_label)
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, cur, colors, pct100=True, show_totals=show_totals)
        idx += 1
    if has_pri:
        charts.panel_box(fig, panels[idx], title=cfg.prior_period_label(), unit_label=unit_label)
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, names, pri, colors, pct100=True, show_totals=show_totals)
    draw_insights(fig, ins, [note])
    pdf.savefig(fig)
    plt.close(fig)


def slide_28(pdf, rows):
    series_names = ["Corporate Bonds/Debentures", "Govt Bonds", "Deposits", "Equity/Invits/REIT", "Mutual Funds"]
    # Not normalize=True: these bucket values are already absolute Rs. Crore
    # (extract_investment_portfolio), so stacked_bar's own pct100 does the
    # %-mix conversion. No totals above the bars (removed on request).
    two_period_stacked_page(pdf, rows, 28, "Investment Portfolio", 28, series_names,
                             "Investment mix as % of each company's own book value.")


def slide_29(pdf, rows):
    series_names = ["Sovereign", "AAA rated", "AA or better", "Rated below AA but above A", "Rated below A"]
    two_period_stacked_page(pdf, rows, 29, "Debt Portfolio: Credit Rating", 29, series_names,
                             "Exposure by credit rating.")


def slide_30(pdf, rows):
    series_names = ["Up to 1 year", "More than 1 year and upto 3 years", "More than 3 years and upto 7 years",
                     "More than 7 years and upto 10 years", "Above 10 years"]
    two_period_stacked_page(pdf, rows, 30, "Debt Portfolio: Residual Maturity", 30, series_names,
                             "Exposure by residual maturity.")


# ---------------------------------------------------------------------------
# Slides 31-37: Historical Trends (Key Ratios) / Asset Under Management -
# multi-year, from data/historical/Historical_Trends.xlsx. Replaces this
# range's earlier 2-period cur/prior bars (which carried a footnote
# apologizing for not having real multi-year history - now they do).
# ---------------------------------------------------------------------------

def slide_31(pdf, rows):
    historical_trend_page(pdf, rows, "Historical Trends (Key Ratios)", 31, [
        {"title": "GWP"},
        {"title": "PBT"},
    ])


def slide_32(pdf, rows):
    historical_trend_page(pdf, rows, "Historical Trends (Key Ratios)", 32, [
        {"title": "Combined Ratio", "is_percent": True, "unit_label": None},
        {"title": "Loss Ratio", "is_percent": True, "unit_label": None},
    ])


def slide_33(pdf, rows):
    historical_trend_page(pdf, rows, "Historical Trends (Key Ratios)", 33, [
        {"title": "Expense Ratio", "is_percent": True, "unit_label": None},
        {"title": "Expense of Management Ratio", "is_percent": True, "unit_label": None},
    ])


def slide_34(pdf, rows):
    historical_trend_page(pdf, rows, "RI Ceded", 34, [
        {"title": "RI Ceding Ratio", "is_percent": True, "unit_label": None},
        {"title": "RI Commission to Ceding Ratio", "is_percent": True, "unit_label": None},
    ])


def slide_35(pdf, rows):
    historical_trend_page(pdf, rows, "ROE & Solvency", 35, [
        {"title": "ROE", "is_percent": True, "unit_label": None},
        {"title": "Solvency Ratio", "unit_label": None, "value_fmt": lambda v: f"{fmt_fixed(v, 2)}x"},
    ])


def slide_36(pdf, rows):
    historical_trend_page(pdf, rows, "Asset Under Management", 36, [
        {"title": "AUM"},
        {"title": "Investment Yield", "is_percent": True, "unit_label": None,
         "value_fmt": lambda v: f"{fmt_fixed(v * 100, 1)}%"},
    ])


def slide_37(pdf, rows):
    historical_trend_page(pdf, rows, "Asset Under Management", 37, [
        {"title": "AUM Shareholders"},
        {"title": "AUM Policyholders"},
    ])


# ---------------------------------------------------------------------------
# Slides 38-39: Distribution Footprint
# ---------------------------------------------------------------------------

def slide_38(pdf, rows):
    """Multi-year trend, sourced from reporting.historical - replaces this
    slide's earlier current-period-only bars. `rows` unused, kept only so
    SECTION_FUNCS can call every slide_NN(pdf, rows) uniformly."""
    historical_trend_page(pdf, rows, "Distribution Footprint", 38, [
        {"title": "Employees", "panel_title": "Employees (On-roll)", "unit_label": "Count"},
        {"title": "Agents", "panel_title": "Individual Agents", "unit_label": "Count"},
    ])


INTERMEDIARY_TYPES = ["Individual Agents", "CA-Banks", "CA-Others", "Brokers", "WA", "IMF", "POS"]


def slide_39(pdf, rows):
    """No. of Offices is now a multi-year trend from reporting.historical;
    Intermediaries by type has no multi-year workbook table, so it stays on
    the Data Engine's current-period `rows`, same as before - this slide mixes
    both sources rather than being purely one or the other."""
    off_table = historical.get_table("Offices")
    off_years = off_keys = off_values = None
    has_off = bool(off_table)
    if has_off:
        off_keys = [k for k in data.COMPANY_ORDER if k in off_table]
        has_off = bool(off_keys)
    if has_off:
        off_years, off_values = _trend_series(rows, "Offices", off_table, off_keys)

    cdata = data.by_company(rows, 39, theme.canonical_company)
    keys = [k for k in data.COMPANY_ORDER if k in cdata]
    disp = disp_names(keys)
    # Agent type on the x-axis, one stacked segment per company (not the
    # other way around) - counts span orders of magnitude across types
    # (Individual Agents in the lakhs, WA/IMF in the tens), so each bar is
    # normalized to the same height (pct100) and labelled with the real
    # count per segment instead of a %, letting a reader see both each
    # type's company mix and its actual scale.
    series = {d: [cdata[k].get(("Intermediaries", t), (None, None))[0] for t in INTERMEDIARY_TYPES]
              for k, d in zip(keys, disp)}
    colors = {d: theme.COMPANY_COLORS.get(k, theme.ORANGE) for k, d in zip(keys, disp)}
    has_int = bool(keys) and any(any(v is not None for v in vals) for vals in series.values())

    if not has_off and not has_int:
        return
    n_panels = int(has_off) + int(has_int)
    # No insights row (the office trend replaces the single leader/laggard
    # bullet this slide used to compute from a single period) - bottom=0.13
    # keeps the last panel's legend clear of the footer, same as
    # historical_trend_page's pages.
    fig, panels, _ins = new_page("Distribution Footprint", 39, n_panels, hspace=0.5, bottom=0.13)
    idx = 0
    if has_off:
        charts.trend_lines(fig, panels[idx], off_years, off_keys, off_values, title="No. of Offices",
                            unit_label="Count")
        idx += 1
    if has_int:
        charts.panel_box(fig, panels[idx], title="Intermediaries by type")
        ax = fig.add_subplot(panels[idx])
        charts.stacked_bar(ax, INTERMEDIARY_TYPES, series, colors, pct100=True, raw_value_labels=True)
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Historical trend slides (multi-year, from data/historical/Historical_Trends.xlsx)
# ---------------------------------------------------------------------------

def period_trend_label():
    """The x-axis label for the reporting period's own point: "Q1 FY27" for
    Q1-Q3, or the plain year "FY26" for Q4 (a Q4 Data Engine is the full
    year)."""
    yy = f"FY{cfg.fy_end_year() % 100:02d}"
    return yy if cfg.QUARTER == "Q4" else f"{cfg.QUARTER} {yy}"


def _trend_series(rows, title, table, keys):
    """(years, {key: values}) for one trend chart: the workbook's completed
    years (capped to the reporting period - see historical.sorted_years),
    plus this period's Data Engine point appended as the last entry ("Q1
    FY26" after FY25) - unless the workbook already has that label (a Q4
    whose year was already added)."""
    years = historical.sorted_years(table)
    values = {k: [table[k].get(y) for y in years] for k in keys}
    label = period_trend_label()
    if label in years:
        return years, values
    point = historical.period_values(rows, title)
    if not any(point.get(k) is not None for k in keys):
        return years, values
    return years + [label], {k: v + [point.get(k)] for k, v in values.items()}


def historical_trend_page(pdf, rows, page_title, page_no, metrics, hspace=0.42):
    """Shared by every slide whose panels come from reporting.historical
    instead of the Data Engine's 2-period rows (slide 15, slide 36, ...) -
    fetches each of `metrics`' workbook tables, lays out one trend_lines
    panel per table actually present (same no-data-no-placeholder rule as
    the rest of the report), and draws the page.

    `metrics`: list of dicts, each with:
      title        - the table's title in Historical_Trends.xlsx (required)
      panel_title  - this panel's heading (defaults to `title`)
      unit_label   - top-right unit annotation (defaults to "INR Crore")
      is_percent / value_fmt - forwarded to charts.trend_lines

    `hspace` below ~0.34 (for 2 panels) puts adjacent panels' dashed
    panel_box frames closer together than their own pad_bottom + pad_top
    reserves, so the two boxes visually overlap/touch instead of leaving a
    gap - keep it above that unless panel_box's own padding shrinks too.
    A title with no matching table in the workbook is skipped."""
    tables = historical.get_tables([m["title"] for m in metrics])
    panels_data = []
    for m in metrics:
        table = tables.get(m["title"])
        if not table:
            continue
        keys = [k for k in data.COMPANY_ORDER if k in table]
        if not keys:
            continue
        years, values = _trend_series(rows, m["title"], table, keys)
        panels_data.append((m, years, keys, values))
    if not panels_data:
        return
    # bottom=0.13 (vs new_page's normal 0.09): this page has no insights box
    # to buffer the last panel from the footer, and trend_lines' own legend
    # sits inside panel_box's pad_bottom reserve just above that margin - see
    # new_page's bottom= docstring.
    fig, panels, _ins = new_page(page_title, page_no, len(panels_data), hspace=hspace, bottom=0.13)
    for spec, (m, years, keys, values) in zip(panels, panels_data):
        charts.trend_lines(fig, spec, years, keys, values, title=m.get("panel_title", m["title"]),
                            unit_label=m.get("unit_label", "INR Crore"), is_percent=m.get("is_percent", False),
                            value_fmt=m.get("value_fmt"))
    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SECTION_FUNCS = [slide_03, slide_04, slide_05, slide_06, slide_07, slide_08, slide_09, slide_10, slide_11,
                 slide_12, slide_13, slide_14, slide_15, slide_16, slide_17, slide_18, slide_19, slide_20,
                 slide_21, slide_22, slide_23, slide_24, slide_25, slide_26, slide_27, slide_28, slide_29,
                 slide_30, slide_31, slide_32, slide_33, slide_34, slide_35, slide_36, slide_37, slide_38,
                 slide_39]


def build(out_path=None, data_engine_path=None):
    # Every caption in this module (cur_period_label()/prior_period_label(),
    # the TOC, every slide title) reads the live cfg.FY/cfg.QUARTER globals -
    # never data_engine_path itself. If a caller renders a Data Engine file
    # without cfg pointed at THAT file's own period (a stale reference held
    # across a pause-for-review, a re-render, anything that skipped
    # re-confirming the period first), the PDF comes out with correct
    # numbers under the wrong period's labels and nothing raises an error.
    # Self-correct from the file's own name whenever it's parseable, rather
    # than trusting the caller.
    parsed_period = cfg.parse_period_from_data_engine_path(data_engine_path)
    if parsed_period and parsed_period != (cfg.FY, cfg.QUARTER):
        log.warning("cfg was set to %s %s, but %r is a %s %s file - correcting before rendering.",
                    cfg.FY, cfg.QUARTER, data_engine_path, *parsed_period)
        cfg.set_period(*parsed_period)
    out_path = out_path or cfg.output_pdf_path()
    rows = data.load_rows(data_engine_path)
    paths.ensure_parent(out_path)
    with PdfPages(out_path) as pdf:
        cover_page(pdf)
        toc_page(pdf, 2)
        key_highlights_page(pdf, 3)
        for fn in SECTION_FUNCS:
            fn(pdf, rows)
        glossary_page(pdf, 41)
    log.info("Saved %s", out_path)
    return out_path


if __name__ == "__main__":
    logging_setup.configure()
    build()
