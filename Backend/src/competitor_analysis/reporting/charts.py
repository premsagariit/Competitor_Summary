"""
Chart-drawing functions for the matplotlib/PDF report. Each function draws
onto axes the caller already created (via a GridSpec cell) and returns
True/False for whether it actually plotted anything - callers use this to
skip a panel entirely (no empty box, no "not available" placeholder) when
the underlying data is absent, per the report's "only show what we have"
rule.
"""
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.ticker import PercentFormatter

from competitor_analysis.reporting import theme
from competitor_analysis import config as cfg


def _clean(ax):
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)


def _add_headroom(ax, values, frac=0.14):
    """Expand the y-axis by `frac` beyond the data range so a bar's value
    label (drawn just outside the bar) doesn't get clipped by the axes
    boundary when a bar is close to the auto-computed max/min."""
    vals = [v for v in values if v is not None]
    if not vals:
        return
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = (hi - lo) or (abs(hi) or 1)
    ax.set_ylim(lo - span * frac if lo < 0 else lo, hi + span * frac if hi > 0 else hi)


def _num_fmt(v):
    """Adaptive precision: a plain '{:,.0f}' rounds every small-magnitude
    value (e.g. 'Rs. Lakhs per agent' figures around 0.3-1.3) down to 0 or 1,
    destroying all the information a chart like that exists to show."""
    if abs(v) < 10:
        return f"{v:,.2f}"
    return f"{v:,.0f}"


def _outlier_break(values, ratio_threshold=5):
    """Classifies values into an 'outlier' cluster (anything more than
    ratio_threshold times the MEDIAN positive value) and a 'normal'
    cluster, returning (bottom_cluster_max, overall_max) marking where a
    broken y-axis should split the chart, or None if nothing is dominant
    enough to need one.

    The median is used as the reference point (not the single largest
    adjacent-value gap) specifically because a "staircase" of several
    moderately-spaced outliers can collectively dwarf the normal cluster
    without any ONE adjacent pair crossing a gap threshold - e.g. two very
    new/small insurers at 2145% and 913% are only ~2.3x apart from each
    other, so a gap-based check misses that both are enormous relative to
    a normal ~100-135% cluster; comparing every value against the robust
    median catches this instead, and naturally groups any number of
    simultaneous outliers into one shared 'top' panel."""
    positive = sorted({v for v in values if v is not None and v > 0})
    if len(positive) < 2:
        return None
    n = len(positive)
    median = positive[n // 2] if n % 2 else (positive[n // 2 - 1] + positive[n // 2]) / 2
    if median <= 0:
        return None
    cutoff = median * ratio_threshold
    bottom = [v for v in positive if v <= cutoff]
    top = [v for v in positive if v > cutoff]
    if not bottom or not top:
        return None
    return max(bottom), max(top)


def _style_bar_axes(ax, is_percent):
    ax.tick_params(axis="y", labelsize=7)
    if is_percent:
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=theme.GRID_COLOR, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)


def _make_broken_axes(fig, subplot_spec, top_ratio=0.32):
    inner = gridspec.GridSpecFromSubplotSpec(2, 1, subplot_spec=subplot_spec,
                                              height_ratios=[top_ratio, 1], hspace=0.08)
    ax_top = fig.add_subplot(inner[0])
    ax_bot = fig.add_subplot(inner[1])
    return ax_top, ax_bot


def _finish_broken_axes(ax_top, ax_bot, bottom_max, top_max, is_percent, lo=0.0):
    ax_bot.set_ylim(lo, bottom_max * 1.28)
    ax_top.set_ylim(bottom_max * 1.28, top_max * 1.15)
    for ax in (ax_top, ax_bot):
        _style_bar_axes(ax, is_percent)
    ax_top.spines["bottom"].set_visible(False)
    ax_bot.spines["top"].set_visible(False)
    ax_top.tick_params(bottom=False, labelbottom=False)
    d = 0.012
    kwargs = dict(transform=ax_top.transAxes, color="black", clip_on=False, linewidth=0.9)
    ax_top.plot((-d, d), (-2 * d, 2 * d), **kwargs)
    ax_top.plot((1 - d, 1 + d), (-2 * d, 2 * d), **kwargs)
    kwargs["transform"] = ax_bot.transAxes
    ax_bot.plot((-d, d), (1 - d, 1 + d), **kwargs)
    ax_bot.plot((1 - d, 1 + d), (1 - d, 1 + d), **kwargs)


def doughnut_pair(fig, subplot_spec, prior_title, current_title, labels, prior_values, current_values,
                   colors, unit_label=None):
    """Two side-by-side doughnuts (prior/current) with each ring's total in
    the center. Returns False (draws nothing) if both periods are fully empty."""
    pairs_prior = [(l, v, c) for l, v, c in zip(labels, prior_values, colors) if v is not None]
    pairs_cur = [(l, v, c) for l, v, c in zip(labels, current_values, colors) if v is not None]
    if not pairs_prior and not pairs_cur:
        return False
    inner = gridspec.GridSpecFromSubplotSpec(1, 2, subplot_spec=subplot_spec, wspace=0.4)
    for i, (title, pairs) in enumerate([(prior_title, pairs_prior), (current_title, pairs_cur)]):
        ax = fig.add_subplot(inner[i])
        if not pairs:
            ax.axis("off")
            ax.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=9, color=theme.GREY_TEXT)
            continue
        vals = [v for _, v, _ in pairs]
        labs = [l for l, _, _ in pairs]
        cols = [c for _, _, c in pairs]

        def _autopct(pct):
            # Suppress the label for slices too thin (<3%) to fit legible
            # text - avoids overlapping labels crowding around tiny wedges.
            return f"{pct:.1f}%" if pct >= 3 else ""

        wedges, _, autotexts = ax.pie(vals, colors=cols, autopct=_autopct, pctdistance=0.8,
                                       wedgeprops=dict(width=0.45, edgecolor="white"),
                                       textprops={"fontsize": 7})
        for t in autotexts:
            t.set_fontsize(7)
        total = sum(vals)
        ax.text(0, 0, f"{total:,.0f}", ha="center", va="center", fontsize=10, fontweight="bold")
        ax.set_title(title, fontsize=10, fontweight="bold", pad=2)
        ax.legend(wedges, labs, loc="upper center", bbox_to_anchor=(0.5, -0.02), fontsize=6.5,
                   ncol=2, frameon=False)
    if unit_label:
        bbox = subplot_spec.get_position(fig)
        fig.text(bbox.x1, bbox.y1 + 0.002, unit_label, fontsize=7.5, style="italic", color=theme.GREY_TEXT,
                  ha="right", transform=fig.transFigure)
    return True


def grouped_bar(fig, subplot_spec, categories, prior_values, current_values, prior_label=None,
                 current_label=None, is_percent=False, higher_is_better=True):
    prior_label = prior_label or cfg.prior_period_label()
    current_label = current_label or cfg.cur_period_label()
    pairs = [(c, p, cu) for c, p, cu in zip(categories, prior_values, current_values)
             if p is not None or cu is not None]
    if not pairs:
        return False
    cats = [p[0] for p in pairs]
    pri = [p[1] if p[1] is not None else 0 for p in pairs]
    cur = [p[2] if p[2] is not None else 0 for p in pairs]
    x = list(range(len(cats)))
    w = 0.36
    fmt = (lambda v: f"{v * 100:.1f}%") if is_percent else _num_fmt

    def _draw(ax):
        b1 = ax.bar([i - w / 2 for i in x], pri, width=w, label=prior_label, color=theme.PRIOR_COLOR)
        b2 = ax.bar([i + w / 2 for i in x], cur, width=w, label=current_label, color=theme.CURRENT_COLOR)
        ax.set_xticks(x)
        return b1, b2

    brk = _outlier_break(pri + cur)
    if brk is None:
        ax = fig.add_subplot(subplot_spec)
        b1, b2 = _draw(ax)
        for bars, vals in ((b1, pri), (b2, cur)):
            ax.bar_label(bars, labels=[fmt(v) for v in vals], fontsize=6.5, padding=1)
        _add_headroom(ax, pri + cur)
        ax.set_xticklabels(cats, fontsize=8)
        _style_bar_axes(ax, is_percent)
        ax.legend(fontsize=7, frameon=False, loc="upper right")
        return True

    bottom_max, top_max = brk
    ax_top, ax_bot = _make_broken_axes(fig, subplot_spec)
    # Zero out each axis's copy of a bar that doesn't belong to its cluster
    # (rather than drawing the real height everywhere and relying on ylim to
    # clip the rest away) - clipping a real bar right at the axis boundary
    # can leave a stray sliver of its top edge visible from anti-aliasing.
    pri_top = [v if v > bottom_max else 0 for v in pri]
    cur_top = [v if v > bottom_max else 0 for v in cur]
    pri_bot = [v if v <= bottom_max else 0 for v in pri]
    cur_bot = [v if v <= bottom_max else 0 for v in cur]
    b1t, b2t = ax_top.bar([i - w / 2 for i in x], pri_top, width=w, label=prior_label, color=theme.PRIOR_COLOR), \
        ax_top.bar([i + w / 2 for i in x], cur_top, width=w, label=current_label, color=theme.CURRENT_COLOR)
    ax_top.set_xticks(x)
    b1b = ax_bot.bar([i - w / 2 for i in x], pri_bot, width=w, label=prior_label, color=theme.PRIOR_COLOR)
    b2b = ax_bot.bar([i + w / 2 for i in x], cur_bot, width=w, label=current_label, color=theme.CURRENT_COLOR)
    ax_top.bar_label(b1t, labels=[fmt(v) if v else "" for v in pri_top], fontsize=6.5, padding=1)
    ax_top.bar_label(b2t, labels=[fmt(v) if v else "" for v in cur_top], fontsize=6.5, padding=1)
    ax_bot.bar_label(b1b, labels=[fmt(v) if v else "" for v in pri_bot], fontsize=6.5, padding=1)
    ax_bot.bar_label(b2b, labels=[fmt(v) if v else "" for v in cur_bot], fontsize=6.5, padding=1)
    lo = min(0, min(pri + cur))
    _finish_broken_axes(ax_top, ax_bot, bottom_max, top_max, is_percent, lo=lo)
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels(cats, fontsize=8)
    # The outlier company/companies (tall bars) are typically on the right
    # (Narayana/Galaxy are last in COMPANY_ORDER) in both panels, so anchor
    # the legend top-left in the (mostly empty) top panel instead of
    # upper-right in the bottom panel, where it would tend to collide with
    # whichever bar is tallest there.
    ax_top.legend(fontsize=6.5, frameon=False, loc="upper left")
    return True


def single_bar(fig, subplot_spec, categories, values, is_percent=False, color=None):
    pairs = [(c, v) for c, v in zip(categories, values) if v is not None]
    if not pairs:
        return False
    cats = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]
    fmt = (lambda v: f"{v * 100:.1f}%") if is_percent else _num_fmt

    brk = _outlier_break(vals)
    if brk is None:
        ax = fig.add_subplot(subplot_spec)
        bars = ax.bar(cats, vals, color=color or theme.BLUE)
        ax.bar_label(bars, labels=[fmt(v) for v in vals], fontsize=7, padding=1)
        _add_headroom(ax, vals)
        ax.tick_params(axis="x", labelsize=8)
        _style_bar_axes(ax, is_percent)
        return True

    bottom_max, top_max = brk
    ax_top, ax_bot = _make_broken_axes(fig, subplot_spec)
    # Zero out each axis's copy of a bar that doesn't belong to its cluster
    # (rather than drawing the real height everywhere and relying on ylim to
    # clip the rest away) - clipping a real bar right at the axis boundary
    # can leave a stray sliver of its top edge visible from anti-aliasing.
    vals_top = [v if v > bottom_max else 0 for v in vals]
    vals_bot = [v if v <= bottom_max else 0 for v in vals]
    bars_top = ax_top.bar(cats, vals_top, color=color or theme.BLUE)
    bars_bot = ax_bot.bar(cats, vals_bot, color=color or theme.BLUE)
    ax_top.bar_label(bars_top, labels=[fmt(v) if v else "" for v in vals_top], fontsize=7, padding=1)
    ax_bot.bar_label(bars_bot, labels=[fmt(v) if v else "" for v in vals_bot], fontsize=7, padding=1)
    lo = min(0, min(vals))
    _finish_broken_axes(ax_top, ax_bot, bottom_max, top_max, is_percent, lo=lo)
    ax_bot.tick_params(axis="x", labelsize=8)
    return True


def stacked_bar(ax, categories, series_dict, colors, pct100=True, value_labels=True):
    """series_dict: {series_name: [value_per_category, ...]}."""
    n = len(categories)
    present = [i for i in range(n) if any((v[i] is not None) for v in series_dict.values())]
    if not present:
        return False
    cats = [categories[i] for i in present]
    raw = {name: [vals[i] if vals[i] is not None else 0 for i in present] for name, vals in series_dict.items()}
    if pct100:
        totals = [sum(raw[name][j] for name in raw) or 1 for j in range(len(cats))]
        data = {name: [raw[name][j] / totals[j] for j in range(len(cats))] for name in raw}
    else:
        data = raw
        totals = [sum(raw[name][j] for name in raw) or 1 for j in range(len(cats))]
    bottoms = [0.0] * len(cats)
    for name, vals in data.items():
        bars = ax.bar(cats, vals, bottom=bottoms, label=name, color=colors.get(name, theme.ORANGE))
        if value_labels:
            # Suppress the label for a segment too small (<4% of its bar's
            # own total) to fit legibly - avoids overlapping text for
            # near-zero segments, which otherwise render as an illegible cluster.
            labels = []
            for v, tot in zip(vals, totals):
                if not v or v / tot < 0.04:
                    labels.append("")
                else:
                    labels.append(f"{v * 100:.0f}%" if pct100 else _num_fmt(v))
            ax.bar_label(bars, labels=labels, label_type="center", fontsize=6, color="white")
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax.tick_params(axis="x", labelsize=7, rotation=0)
    ax.tick_params(axis="y", labelsize=7)
    if pct100:
        ax.set_ylim(0, 1.05)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.legend(fontsize=6, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=min(len(data), 5))
    ax.spines[["top", "right"]].set_visible(False)
    return True


def change_bar(ax, changes, unit="pp"):
    """Horizontal diverging bar - replaces the reference deck's dot/bubble
    'Market Share Change' mini-chart with a simpler, equally-informative
    static chart. `changes`: {company_key: signed_change_value}."""
    pairs = [(k, v) for k, v in changes.items() if v is not None]
    if not pairs:
        return False
    labels = [theme.COMPANY_DISPLAY_NAME.get(k, k) for k, _ in pairs]
    vals = [v for _, v in pairs]
    colors = [theme.COMPANY_COLORS.get(k, theme.ORANGE) if v >= 0 else "#C00000" for (k, _), v in zip(pairs, vals)]
    y = range(len(labels))
    bars = ax.barh(list(y), vals, color=colors)
    ax.bar_label(bars, labels=[f"{'+' if v >= 0 else ''}{v:.1f}{unit}" for v in vals], fontsize=7, padding=3)
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = (hi - lo) or (abs(hi) or 1)
    ax.set_xlim(lo - span * 0.18 if lo < 0 else lo, hi + span * 0.18 if hi > 0 else hi)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.tick_params(axis="x", labelsize=7)
    ax.spines[["top", "right"]].set_visible(False)
    return True


def income_table(ax, row_labels, company_keys, values_dict, title):
    """values_dict: {row_label: {company_key: value}}. Row labels are the
    table's own first column (not matplotlib's separate `rowLabels`, which
    is positioned outside the table's axes and gets clipped by the page
    edge for a table this close to the left margin)."""
    present_cols = [k for k in company_keys if any(values_dict.get(r, {}).get(k) is not None for r in row_labels)]
    if not present_cols:
        return False
    ax.axis("off")
    col_labels = ["Metric"] + [theme.COMPANY_DISPLAY_NAME.get(k, k) for k in present_cols]
    cell_text = []
    for r in row_labels:
        row = [r]
        for k in present_cols:
            v = values_dict.get(r, {}).get(k)
            row.append(f"{v:,.0f}" if isinstance(v, (int, float)) else "-")
        cell_text.append(row)
    n_cols = len(col_labels)
    col_widths = [0.28] + [0.72 / (n_cols - 1)] * (n_cols - 1)
    table = ax.table(cellText=cell_text, colLabels=col_labels, cellLoc="center", colWidths=col_widths,
                      loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(7.5)
    table.scale(1, 1.5)
    for (r, c), cell in table.get_celld().items():
        if r == 0:
            cell.set_facecolor(theme.NAVY)
            cell.set_text_props(color="white", fontweight="bold")
        elif c == 0:
            cell.set_text_props(ha="left", fontweight="bold" if row_labels[r - 1] in ("PBT", "PAT") else "normal")
            cell._loc = "left"
        cell.set_edgecolor(theme.GRID_COLOR)
    ax.set_title(title, fontsize=10, fontweight="bold", loc="left", pad=6)
    return True
