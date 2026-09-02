"""
Chart-archetype builders for the FY26 Q3 Competition Analysis deck. Each
function takes plain Python data (labels/series/colors) plus a bounding box
and mutates the given slide - report_specs.py decides WHAT data goes where,
these functions only know HOW to draw one archetype.

Archetypes, matching Backend/Competition Summary FY25 1.pdf:
  - doughnut_pair        Slides 3-7 (prior-year vs current-year doughnuts,
                          center total label, per-category/company colors)
  - grouped_column        Most "Key Metrics"/ratio slides, and (reused,
                          2-period-only) "Historical Trends" slides
  - stacked_100_column     Slide 24 Investment Portfolio mix
  - logo_table             Slide 18 Income Statement (two stacked tables,
                          company logos as column headers)
  - change_dot_chart       Slide 6's "Market Share Change" mini-chart
"""
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt

import report_theme as theme

PRIOR_COLOR = RGBColor(0x2E, 0x54, 0x95)
CURRENT_COLOR = RGBColor(0xA6, 0xA6, 0xA6)


def _fmt_num(v, decimals=1):
    if v is None:
        return None
    return round(v, decimals)


def doughnut_pair(slide, prior_title, current_title, labels, prior_values, current_values,
                   colors, left, top, width, height, unit_label=None, pct_of_total=True):
    """Two side-by-side doughnut charts (prior period, current period), each
    with its total printed in the center. `colors` is a list matching
    `labels`' order. Values are absolute (Rs. Cr) - the chart itself always
    shows each slice's % share (pct_of_total=True) with the raw absolute
    value as a data label suffix isn't attempted (python-pptx doesn't support
    multi-line per-point labels cleanly) - the percentage is what matters
    visually, matching the reference deck's own emphasis."""
    half_w = (width - Inches(0.2)) / 2
    for i, (title, values) in enumerate([(prior_title, prior_values), (current_title, current_values)]):
        x = left + i * (half_w + Inches(0.2))
        chart_top = top + Inches(0.35)
        chart_h = height - Inches(0.35)

        title_tb = slide.shapes.add_textbox(x, top, half_w, Inches(0.3))
        tp = title_tb.text_frame.paragraphs[0]
        tp.alignment = PP_ALIGN.CENTER
        tr = tp.add_run()
        tr.text = title
        tr.font.size = Pt(11)
        tr.font.bold = True

        present = [(lab, v, c) for lab, v, c in zip(labels, values, colors) if v is not None]
        if not present:
            theme.add_placeholder_panel(slide, "Not available", x, chart_top, half_w, chart_h)
            continue

        cd = CategoryChartData()
        cd.categories = [lab for lab, _, _ in present]
        cd.add_series("share", tuple(v for _, v, _ in present))
        gf = slide.shapes.add_chart(XL_CHART_TYPE.DOUGHNUT, x, chart_top, half_w, chart_h, cd)
        chart = gf.chart
        chart.has_legend = False
        chart.has_title = False
        plot = chart.plots[0]
        plot.has_data_labels = True
        dl = plot.data_labels
        # Slice values are absolute Rs. Cr (so slice angles are correct) -
        # show_percentage renders each label as % of the ring's own total,
        # computed natively by PowerPoint, rather than formatting the raw
        # absolute value as if it were already a fraction.
        dl.show_value = not pct_of_total
        dl.show_percentage = pct_of_total
        dl.number_format = "0.0%"
        dl.number_format_is_linked = False
        dl.font.size = Pt(8)
        try:
            plot.vary_by_categories = True
        except Exception:
            pass
        series = plot.series[0]
        for pt, (_, _, color) in zip(series.points, present):
            pt.format.fill.solid()
            pt.format.fill.fore_color.rgb = color

        total = sum(v for _, v, _ in present)
        center_tb = slide.shapes.add_textbox(x, chart_top + chart_h / 2 - Inches(0.35), half_w, Inches(0.7))
        ctf = center_tb.text_frame
        ctf.word_wrap = True
        ctf.vertical_anchor = MSO_ANCHOR.MIDDLE
        cp = ctf.paragraphs[0]
        cp.alignment = PP_ALIGN.CENTER
        cr = cp.add_run()
        cr.text = f"{total:,.0f}"
        cr.font.size = Pt(12)
        cr.font.bold = True

    if unit_label:
        lbl = slide.shapes.add_textbox(left + width - Inches(1.3), top - Inches(0.05), Inches(1.3), Inches(0.25))
        lp = lbl.text_frame.paragraphs[0]
        lp.alignment = PP_ALIGN.RIGHT
        lr = lp.add_run()
        lr.text = unit_label
        lr.font.size = Pt(9)
        lr.font.italic = True


def grouped_column(slide, categories, prior_values, current_values, left, top, width, height,
                    prior_label="FY25_Q3", current_label="FY26_Q3", number_format="0%"):
    """2-series grouped column chart - Key Metrics ratio slides, and (with a
    caller-supplied footnote) the Historical Trends slides, which only have
    2 periods of data rather than the reference deck's 8-year history."""
    present = [(c, p, cu) for c, p, cu in zip(categories, prior_values, current_values)
               if p is not None or cu is not None]
    if not present:
        theme.add_placeholder_panel(slide, "Not available for FY26 Q3", left, top, width, height)
        return
    cd = CategoryChartData()
    cd.categories = [c for c, _, _ in present]
    cd.add_series(prior_label, tuple(p if p is not None else 0 for _, p, _ in present))
    cd.add_series(current_label, tuple(cu if cu is not None else 0 for _, _, cu in present))
    gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, left, top, width, height, cd)
    chart = gf.chart
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    chart.legend.font.size = Pt(9)
    chart.has_title = False
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.number_format = number_format
    plot.data_labels.number_format_is_linked = False
    plot.data_labels.font.size = Pt(8)
    plot.series[0].format.fill.solid()
    plot.series[0].format.fill.fore_color.rgb = PRIOR_COLOR
    plot.series[1].format.fill.solid()
    plot.series[1].format.fill.fore_color.rgb = CURRENT_COLOR
    chart.category_axis.tick_labels.font.size = Pt(8)
    chart.value_axis.tick_labels.font.size = Pt(8)


def single_series_column(slide, categories, values, left, top, width, height,
                          number_format="0%", color=None, series_label="FY26_Q3"):
    """1-series column chart, for current-period-only metrics (no prior-year
    comparative in the source, e.g. NL-41 point-in-time schedules)."""
    present = [(c, v) for c, v in zip(categories, values) if v is not None]
    if not present:
        theme.add_placeholder_panel(slide, "Not available for FY26 Q3", left, top, width, height)
        return
    cd = CategoryChartData()
    cd.categories = [c for c, _ in present]
    cd.add_series(series_label, tuple(v for _, v in present))
    gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, left, top, width, height, cd)
    chart = gf.chart
    chart.has_legend = False
    chart.has_title = False
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.number_format = number_format
    plot.data_labels.number_format_is_linked = False
    plot.data_labels.font.size = Pt(8)
    plot.series[0].format.fill.solid()
    plot.series[0].format.fill.fore_color.rgb = color or theme.BLUE
    chart.category_axis.tick_labels.font.size = Pt(8)
    chart.value_axis.tick_labels.font.size = Pt(8)


def stacked_100_column(slide, categories, series_dict, series_colors, left, top, width, height,
                        number_format="0%", pct100=True):
    """`series_dict`: {series_name: [value_per_category, ...]}, one entry per
    stack segment (e.g. asset class or business line); values are fractions
    summing to ~1 per category when pct100=True, or absolute Rs. Cr amounts
    (plain stacked, not normalized) when pct100=False. `series_colors`:
    {series_name: RGBColor}."""
    company_has_any = [any(vals[i] is not None for vals in series_dict.values()) for i in range(len(categories))]
    if not any(company_has_any):
        theme.add_placeholder_panel(slide, "Not available for FY26 Q3", left, top, width, height)
        return
    cd = CategoryChartData()
    cd.categories = categories
    for name, vals in series_dict.items():
        cd.add_series(name, tuple(v if v is not None else 0 for v in vals))
    chart_type = XL_CHART_TYPE.COLUMN_STACKED_100 if pct100 else XL_CHART_TYPE.COLUMN_STACKED
    gf = slide.shapes.add_chart(chart_type, left, top, width, height, cd)
    chart = gf.chart
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    chart.legend.font.size = Pt(8)
    chart.has_title = False
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.number_format = number_format
    plot.data_labels.number_format_is_linked = False
    plot.data_labels.font.size = Pt(7)
    for series in plot.series:
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = series_colors.get(series.name, theme.ORANGE)
    chart.category_axis.tick_labels.font.size = Pt(8)
    chart.value_axis.visible = False


def logo_table(slide, row_labels, company_keys, current_values, left, top, width, section_title,
               row_height=Inches(0.34)):
    """One table: header row shows each company's logo (or a colored text
    badge if it has none), one data row per metric label. `current_values`:
    {row_label: {company_key: value}}."""
    n_cols = len(company_keys) + 1
    n_rows = len(row_labels) + 1
    height = row_height * n_rows
    gf = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
    table = gf.table
    col0_w = Inches(1.9)
    other_w = int((width - col0_w) / max(1, len(company_keys)))
    table.columns[0].width = col0_w
    for i in range(1, n_cols):
        table.columns[i].width = other_w

    hdr = table.rows[0]
    hdr.height = Inches(0.55)
    hdr.cells[0].text = section_title
    hdr.cells[0].fill.solid()
    hdr.cells[0].fill.fore_color.rgb = theme.NAVY
    for p in hdr.cells[0].text_frame.paragraphs:
        for r in p.runs:
            r.font.color.rgb = theme.WHITE
            r.font.bold = True
            r.font.size = Pt(11)

    for i, key in enumerate(company_keys, start=1):
        cell = hdr.cells[i]
        cell.text = ""
        cell.fill.solid()
        cell.fill.fore_color.rgb = theme.LIGHT_GREY
        logo = theme.logo_path(key)
        if logo:
            # Overlay the logo picture on top of the (blank) header cell,
            # since table cells can't natively embed a picture.
            cell_left = left + col0_w + (i - 1) * other_w
            slide.shapes.add_picture(logo, cell_left + Inches(0.08), top + Inches(0.06),
                                      height=hdr.height - Inches(0.12))
        else:
            cell.text = theme.COMPANY_DISPLAY_NAME.get(key, key)
            cell.fill.fore_color.rgb = theme.COMPANY_COLORS.get(key, theme.LIGHT_GREY)
            for p in cell.text_frame.paragraphs:
                p.alignment = PP_ALIGN.CENTER
                for r in p.runs:
                    r.font.bold = True
                    r.font.size = Pt(10)
                    r.font.color.rgb = theme.WHITE

    for ri, label in enumerate(row_labels, start=1):
        row = table.rows[ri]
        row.height = row_height
        c0 = row.cells[0]
        c0.text = label
        for p in c0.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(10)
                r.font.bold = label in ("PBT", "PAT")
        for ci, key in enumerate(company_keys, start=1):
            v = current_values.get(label, {}).get(key)
            cell = row.cells[ci]
            cell.text = f"{v:,.0f}" if isinstance(v, (int, float)) else "-"
            for p in cell.text_frame.paragraphs:
                p.alignment = PP_ALIGN.CENTER
                for r in p.runs:
                    r.font.size = Pt(10)
                    r.font.bold = label in ("PBT", "PAT")
    return height


def change_dot_chart(slide, company_keys, changes, left, top, width, height, unit="pp"):
    """Slide 6's 'Market Share Change' mini-chart: one colored circle per
    company, positioned above a midline if it gained and below if it lost,
    with its %/pp change labeled above the circle and its name below.
    `changes`: {company_key: signed_change_value}."""
    present = {k: v for k, v in changes.items() if v is not None}
    if not present:
        theme.add_placeholder_panel(slide, "Not available for FY26 Q3", left, top, width, height)
        return
    mid_y = top + height / 2
    line = slide.shapes.add_connector(1, left, mid_y, left + width, mid_y)
    line.line.color.rgb = RGBColor(0xBF, 0xBF, 0xBF)
    line.line.width = Pt(0.75)

    n = len(present)
    slot_w = width / n
    dot_d = Inches(0.55)
    for i, (key, val) in enumerate(present.items()):
        cx = left + slot_w * i + slot_w / 2 - dot_d / 2
        gained = val >= 0
        cy = (mid_y - Inches(0.9)) if gained else (mid_y + Inches(0.35))

        dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, cx, cy, dot_d, dot_d)
        dot.fill.solid()
        dot.fill.fore_color.rgb = theme.COMPANY_COLORS.get(key, theme.ORANGE)
        dot.line.fill.background()

        label_tb = slide.shapes.add_textbox(left + slot_w * i, cy - Inches(0.55), slot_w, Inches(0.5))
        ltf = label_tb.text_frame
        ltf.word_wrap = True
        lp = ltf.paragraphs[0]
        lp.alignment = PP_ALIGN.CENTER
        lr = lp.add_run()
        sign = "+" if val >= 0 else ""
        lr.text = f"{sign}{val:.1f}{unit}"
        lr.font.size = Pt(10)
        lr.font.bold = True
        name_p = ltf.add_paragraph()
        name_p.alignment = PP_ALIGN.CENTER
        nr = name_p.add_run()
        nr.text = theme.COMPANY_DISPLAY_NAME.get(key, key)
        nr.font.size = Pt(9)
