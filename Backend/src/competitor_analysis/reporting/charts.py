"""
Chart-drawing functions for the matplotlib/PDF report. Each function draws
onto axes the caller already created (via a GridSpec cell) and returns
True/False for whether it actually plotted anything - callers use this to
skip a panel entirely (no empty box, no "not available" placeholder) when
the underlying data is absent, per the report's "only show what we have"
rule.
"""
import math
import textwrap
from types import SimpleNamespace

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import PercentFormatter

from competitor_analysis.reporting import theme
from competitor_analysis import config as cfg
from competitor_analysis.rounding import fmt_fixed, round_half_up


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
        return fmt_fixed(v, 2, grouping=True)
    return fmt_fixed(v, 0, grouping=True)


GROWTH_UP_COLOR = "#2E7D32"
GROWTH_DOWN_COLOR = "#C00000"


def _growth(cur, prior):
    """YoY growth fraction, or None when there's no meaningful base (a
    missing or non-positive prior value)."""
    if cur is None or prior is None or prior <= 0:
        return None
    return cur / prior - 1


def _growth_label(g):
    return f"{'+' if g >= 0 else ''}{fmt_fixed(g * 100, 0)}%"


def _growth_color(g):
    return GROWTH_UP_COLOR if g >= 0 else GROWTH_DOWN_COLOR


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


def _style_bar_axes(ax):
    # No y-axis - report-wide style choice (bar labels already carry every
    # value, so ticks/spine would be redundant).
    ax.tick_params(axis="y", left=False, labelleft=False)
    ax.spines["left"].set_visible(False)
    ax.spines[["top", "right"]].set_visible(False)


def _make_broken_axes(fig, subplot_spec, top_ratio=0.32):
    # hspace=0: the two axes sit flush against each other, so a bar spanning
    # the break reads as one continuous bar (just visually cut by the
    # diagonal break marks) rather than two blocks with a gap between them.
    inner = gridspec.GridSpecFromSubplotSpec(2, 1, subplot_spec=subplot_spec,
                                              height_ratios=[top_ratio, 1], hspace=0)
    ax_top = fig.add_subplot(inner[0])
    ax_bot = fig.add_subplot(inner[1])
    return ax_top, ax_bot


def _finish_broken_axes(ax_top, ax_bot, bottom_max, top_max, lo=0.0):
    ax_bot.set_ylim(lo, bottom_max * 1.28)
    ax_top.set_ylim(bottom_max * 1.28, top_max * 1.15)
    for ax in (ax_top, ax_bot):
        _style_bar_axes(ax)
    ax_top.spines["bottom"].set_visible(False)
    ax_bot.spines["top"].set_visible(False)
    ax_top.tick_params(bottom=False, labelbottom=False)


def panel_box(fig, subplot_spec, title=None, unit_label=None, pad_x=0.014, pad_bottom=0.06, pad_top=0.045):
    """Dashed rounded box wrapping subplot_spec's area, with an optional
    centered title and a top-right unit_label (e.g. "INR Crores") both
    inside the box, on their own row above the chart content. The caller
    still uses `subplot_spec` itself (via fig.add_subplot) for its chart
    axes - this only draws the frame/labels around it. `pad_bottom` defaults
    generously so a legend drawn just below the axes (bbox_to_anchor y<0, as
    stacked_bar's own legend is) still lands inside the box instead of
    poking out under it."""
    bbox = subplot_spec.get_position(fig)
    box = FancyBboxPatch((bbox.x0 - pad_x, bbox.y0 - pad_bottom),
                          (bbox.x1 - bbox.x0) + 2 * pad_x, (bbox.y1 - bbox.y0) + pad_bottom + pad_top,
                          transform=fig.transFigure, boxstyle="round,pad=0,rounding_size=0.012",
                          linewidth=1, edgecolor=theme.INSIGHT_BORDER, facecolor="none",
                          linestyle=(0, (5, 3)), clip_on=False)
    fig.add_artist(box)
    row_y = bbox.y1 + pad_top / 2
    if title:
        fig.text((bbox.x0 + bbox.x1) / 2, row_y, title, fontsize=11, fontweight="bold", color=theme.DARK_TEXT,
                  ha="center", va="center", transform=fig.transFigure)
    if unit_label:
        fig.text(bbox.x1 - pad_x - 0.004, row_y, unit_label, fontsize=7.5, style="italic",
                  color=theme.GREY_TEXT, ha="right", va="center", transform=fig.transFigure)


def _indian_grouping(n):
    """'307666' -> '3,07,666': last 3 digits, then pairs going left - the
    lakh/crore grouping the reference deck uses throughout for Rs. figures,
    not Western 3-digit grouping (which agrees with it below 1,00,000 but
    diverges above)."""
    sign = "-" if n < 0 else ""
    s = str(abs(round_half_up(n)))
    if len(s) <= 3:
        return sign + s
    last3, rest = s[-3:], s[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return sign + ",".join(groups) + "," + last3


def doughnut_pair(fig, subplot_spec, prior_period_label, current_period_label, labels, prior_values,
                   current_values, colors, group_label=None, unit_label=None, value_fmt=None, title=None):
    """Two side-by-side doughnuts (prior/current), reference-deck style: each
    slice's share is called out just outside the ring (name + %), its
    absolute value sits inside the ring in bold white, and the ring's own
    total plus group/period label sit stacked in the doughnut hole - no
    legend. The pair is framed in a dashed rounded box with `unit_label`
    (e.g. "INR Crores") pinned inside its top-right corner.

    `value_fmt`, if given, formats both the inside-wedge and center-total
    numbers (default: Indian lakh/crore grouping) - pass e.g.
    `lambda v: f"{v * 100:.1f}%"` for a doughnut whose values are already
    fractions of a whole (slide 10) rather than absolute Rs. Crore.

    Returns False (draws nothing) if both periods are fully empty."""
    value_fmt = value_fmt or _indian_grouping
    pairs_prior = [(l, v, c) for l, v, c in zip(labels, prior_values, colors) if v is not None]
    pairs_cur = [(l, v, c) for l, v, c in zip(labels, current_values, colors) if v is not None]
    if not pairs_prior and not pairs_cur:
        return False
    inner = gridspec.GridSpecFromSubplotSpec(1, 2, subplot_spec=subplot_spec, wspace=0.25)
    # The dashed box's inner edges (same geometry as drawn at the end of this
    # function) - outside callout labels are kept within it.
    panel_bbox = subplot_spec.get_position(fig)
    panel = SimpleNamespace(x0=panel_bbox.x0 - 0.014, x1=panel_bbox.x1 + 0.014,
                            y0=panel_bbox.y0 - 0.018, y1=panel_bbox.y1)
    for i, (period_label, pairs) in enumerate([(prior_period_label, pairs_prior), (current_period_label, pairs_cur)]):
        ax = fig.add_subplot(inner[i])
        if not pairs:
            ax.axis("off")
            ax.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=9, color=theme.GREY_TEXT)
            continue
        vals = [v for _, v, _ in pairs]
        labs = [l for l, _, _ in pairs]
        cols = [c for _, _, c in pairs]
        total = sum(vals)

        def _autopct(pct):
            # matplotlib hands autopct the slice's share (0-100), computed
            # from the same `vals` this closure already has - reconstructing
            # the absolute value from it (v = pct/100 * total) is exact,
            # since that's precisely how matplotlib derived pct in the first
            # place, and avoids a second, potentially misaligned pass over
            # `vals` by wedge index.
            v = pct / 100 * total
            return value_fmt(v) if pct >= 3 else ""

        # The ring is drawn at radius R inside fixed axis limits (XL, YL), so
        # the outside name/% callouts always have room within the axes - and
        # so within the panel's dashed box - instead of growing past it.
        R = 1.0
        wedges, _, autotexts = ax.pie(
            vals, colors=cols, autopct=_autopct, pctdistance=0.73, radius=R,
            wedgeprops=dict(width=0.55 * R, edgecolor="white"), startangle=90)
        for t in autotexts:
            t.set_fontsize(8)
            t.set_fontweight("bold")
            t.set_color("white")

        # A thin wedge's value can be wider than the wedge itself - measuring
        # that needs a real layout pass (text extent isn't known until
        # something has been drawn), so force one before checking. Anything
        # that doesn't fit is slid along the ring - same radius, angled off
        # its own wedge's center toward whichever neighbor has more room -
        # rather than pushed radially out past the ring: it stays on the
        # colored band (so it keeps its white color) and only borrows a
        # little of the wider neighbor's arc, the way a hand-built deck
        # nudges a label that doesn't fit its own slice. If even borrowing
        # the most we're willing to (45% of that neighbor's own span) still
        # isn't enough, the wedge is just too small for this value to be
        # shown at all - drop it rather than force an illegible overlap.
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        n = len(wedges)
        spans = [w.theta2 - w.theta1 for w in wedges]
        ring = 0.73 * R
        for wi, (w, t) in enumerate(zip(wedges, autotexts)):
            if not t.get_text():
                continue
            text_w = t.get_window_extent(renderer=renderer).width
            own_span = math.radians(spans[wi])
            chord = 2 * ring * math.sin(own_span / 2)
            p0, p1 = ax.transData.transform((0, 0)), ax.transData.transform((chord, 0))
            chord_px = abs(p1[0] - p0[0])
            if text_w <= chord_px * 0.92:
                continue
            prev_span, next_span = spans[(wi - 1) % n], spans[(wi + 1) % n]
            toward_next = next_span >= prev_span
            neighbor_span = math.radians(next_span if toward_next else prev_span)
            scale = (chord_px / chord) if chord else 1
            needed_rad = ((text_w - chord_px) / scale) / ring if scale else 0
            if needed_rad > neighbor_span * 0.45:
                t.set_text("")
                continue
            bisector = math.radians((w.theta1 + w.theta2) / 2)
            ang = bisector + (needed_rad if toward_next else -needed_rad)
            t.set_position((ring * math.cos(ang), ring * math.sin(ang)))

        # Outside callout labels (name + share), anchored by the edge facing
        # away from the ring so they grow outward. Then, per side: labels of
        # adjacent small slices are spread apart vertically so they never
        # overlap (a thin leader line ties each back to its slice), and any
        # label that would cross the axes edge is pulled back inside.
        px_per_unit = (ax.transData.transform((1, 1)) - ax.transData.transform((0, 0)))
        callouts = []
        for w, name, v in zip(wedges, labs, vals):
            ang = math.radians((w.theta1 + w.theta2) / 2)
            x, y = math.cos(ang), math.sin(ang)
            pct = (v / total * 100) if total else 0
            wrapped = "\n".join(textwrap.wrap(str(name), 16)) or str(name)
            t = ax.text(0, 0, f"{wrapped}\n{fmt_fixed(pct, 1)}%", fontsize=7.5, color=theme.GREY_TEXT,
                        ha="left" if x >= 0 else "right", va="center")
            ext = t.get_window_extent(renderer=renderer)
            callouts.append({"text": t, "anchor": (R * x, R * y), "side": 1 if x >= 0 else -1,
                             "x": 1.22 * R * x, "y": 1.15 * R * y,
                             "w": ext.width / px_per_unit[0], "h": ext.height / px_per_unit[1]})
        # Where a label may go, in this axes' data units: inside the panel's
        # dashed box, and on this doughnut's own half of the gap between the
        # two doughnuts (so neither pair's labels can run into the other's).
        cell_l, cell_r = inner[0].get_position(fig), inner[1].get_position(fig)
        mid = (cell_l.x1 + cell_r.x0) / 2
        margin = 0.006
        fx0 = (panel.x0 + margin) if i == 0 else (mid + margin)
        fx1 = (mid - margin) if i == 0 else (panel.x1 - margin)
        to_data = lambda fx, fy: ax.transData.inverted().transform(fig.transFigure.transform((fx, fy)))
        lo_x, lo_y = to_data(fx0, panel.y0 + margin)
        hi_x, hi_y = to_data(fx1, panel.y1 - margin)

        gap = 0.03
        for side in (1, -1):
            group = sorted((c for c in callouts if c["side"] == side), key=lambda c: -c["y"])
            for prev, cur in zip(group, group[1:]):
                limit = prev["y"] - (prev["h"] + cur["h"]) / 2 - gap
                if cur["y"] > limit:
                    cur["y"] = limit
            if group:
                low = group[-1]["y"] - group[-1]["h"] / 2
                if low < lo_y:
                    for c in group:
                        c["y"] += lo_y - low
                high = group[0]["y"] + group[0]["h"] / 2
                if high > hi_y:
                    for c in group:
                        c["y"] -= high - hi_y
            for c in group:
                # Start the label clear of the ring at its own height (the
                # vertical spreading above can move it level with the ring)...
                top, bottom = c["y"] + c["h"] / 2, c["y"] - c["h"] / 2
                nearest = 0.0 if bottom <= 0 <= top else min(abs(top), abs(bottom))
                if nearest < R:
                    clear = math.sqrt(R * R - nearest * nearest) + 0.06
                    c["x"] = side * max(abs(c["x"]), clear)
                # ...then pull back anything that would still cross its
                # allowed area - staying inside the box wins over clearance.
                if side > 0:
                    c["x"] = min(c["x"], hi_x - c["w"])
                else:
                    c["x"] = max(c["x"], lo_x + c["w"])
        for c in callouts:
            c["text"].set_position((c["x"], c["y"]))
            ax.annotate("", xy=c["anchor"], xytext=(c["x"], c["y"]),
                        arrowprops=dict(arrowstyle="-", color=theme.GRID_COLOR, linewidth=0.6,
                                        shrinkA=1, shrinkB=1))

        lines = ([(group_label, 6.5, theme.DARK_TEXT)] if group_label else []) + [
            (period_label, 6.5, theme.DARK_TEXT),
            (value_fmt(total), 9, theme.DARK_TEXT),
        ]
        step = 0.11
        y0 = (len(lines) - 1) / 2 * step
        for j, (text, fontsize, color) in enumerate(lines):
            ax.text(0, y0 - j * step, text, ha="center", va="center", fontsize=fontsize,
                    fontweight="bold", color=color)

    bbox = subplot_spec.get_position(fig)
    pad_x, pad_bottom, pad_top = 0.014, 0.018, 0.045
    box = FancyBboxPatch((bbox.x0 - pad_x, bbox.y0 - pad_bottom),
                          (bbox.x1 - bbox.x0) + 2 * pad_x, (bbox.y1 - bbox.y0) + pad_bottom + pad_top,
                          transform=fig.transFigure, boxstyle="round,pad=0,rounding_size=0.012",
                          linewidth=1, edgecolor=theme.INSIGHT_BORDER, facecolor="none",
                          linestyle=(0, (5, 3)), clip_on=False)
    fig.add_artist(box)
    # Title + unit_label share one row inside the box's reserved top strip
    # (mirrors panel_box's layout) rather than sitting above the box, which
    # otherwise clips into whatever's drawn just above this panel.
    row_y = bbox.y1 + pad_top / 2
    if title:
        fig.text((bbox.x0 + bbox.x1) / 2, row_y, title, fontsize=11, fontweight="bold", color=theme.DARK_TEXT,
                  ha="center", va="center", transform=fig.transFigure)
    if unit_label:
        fig.text(bbox.x1 - pad_x - 0.004, row_y, unit_label, fontsize=7.5, style="italic",
                  color=theme.GREY_TEXT, ha="right", va="center", transform=fig.transFigure)
    return True


def _annotate_pair_growth(ax, x, pri, cur, raw_pairs, only_if=None):
    """Writes each category's YoY growth (current vs prior) centered above
    its bar pair, clear of the pair's own value labels. `only_if(top)`, if
    given, restricts labelling to pairs whose taller bar belongs on `ax`
    (the broken-axis case, so a pair isn't labelled in both panels)."""
    for xi, p, c, (_, raw_p, raw_c) in zip(x, pri, cur, raw_pairs):
        g = _growth(raw_c, raw_p)
        if g is None:
            continue
        top = max(p, c, 0)
        if only_if is not None and not only_if(top):
            continue
        ax.annotate(_growth_label(g), (xi, top), textcoords="offset points", xytext=(0, 11),
                    ha="center", va="bottom", fontsize=7.5, fontweight="bold", color=_growth_color(g))


def grouped_bar(fig, subplot_spec, categories, prior_values, current_values, prior_label=None,
                 current_label=None, is_percent=False, higher_is_better=True, show_growth=False):
    """`show_growth`, if set, labels each category with its current-vs-prior
    YoY growth % above its pair of bars (money-type metrics - for a ratio
    metric a change in percentage points is the meaningful figure instead)."""
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
    # Bar centres sit `off` either side of the category - a little more than
    # half a bar apart - so the two value labels have room side by side.
    off = 0.21
    fmt = (lambda v: f"{fmt_fixed(v * 100, 1)}%") if is_percent else _num_fmt

    def _draw(ax):
        b1 = ax.bar([i - off for i in x], pri, width=w, label=prior_label, color=theme.PRIOR_COLOR)
        b2 = ax.bar([i + off for i in x], cur, width=w, label=current_label, color=theme.CURRENT_COLOR)
        ax.set_xticks(x)
        return b1, b2

    brk = _outlier_break(pri + cur)
    if brk is None:
        ax = fig.add_subplot(subplot_spec)
        b1, b2 = _draw(ax)
        for bars, vals in ((b1, pri), (b2, cur)):
            ax.bar_label(bars, labels=[fmt(v) for v in vals], fontsize=7.5, padding=1)
        if show_growth:
            _annotate_pair_growth(ax, x, pri, cur, pairs)
        # Extra headroom (vs. the 0.14 default) so the legend - anchored at
        # the very top of the axes - has clear air above the tallest bar's
        # value label (and growth label, when shown) instead of sitting on it.
        _add_headroom(ax, pri + cur, frac=0.40 if show_growth else 0.30)
        ax.set_xticklabels(cats, fontsize=8)
        _style_bar_axes(ax)
        ax.legend(fontsize=7, frameon=False, loc="upper right", bbox_to_anchor=(1.0, 1.02))
        return True

    bottom_max, top_max = brk
    ax_top, ax_bot = _make_broken_axes(fig, subplot_spec)
    # Draw each bar's REAL height on both axes and let each axis's own ylim
    # (set below, with 28%/15% headroom on the bottom/top clusters
    # respectively) clip away whatever doesn't belong to it. This is what
    # makes a tall bar read as ONE bar interrupted by the break instead of a
    # short one floating in ax_top with nothing connecting it down to zero -
    # which is what zeroing out each axis's "other" copy (the previous
    # approach here) produces, since ax_bot's copy of a tall bar was 0.
    # Headroom keeps every bar's true top comfortably clear of its own
    # axis's ylim, so clipping a bar at the OTHER axis's boundary happens
    # mid-bar, not at a top edge, avoiding the antialiasing sliver a
    # boundary-hugging clip could otherwise leave.
    b1t = ax_top.bar([i - off for i in x], pri, width=w, label=prior_label, color=theme.PRIOR_COLOR)
    b2t = ax_top.bar([i + off for i in x], cur, width=w, label=current_label, color=theme.CURRENT_COLOR)
    ax_top.set_xticks(x)
    b1b = ax_bot.bar([i - off for i in x], pri, width=w, label=prior_label, color=theme.PRIOR_COLOR)
    b2b = ax_bot.bar([i + off for i in x], cur, width=w, label=current_label, color=theme.CURRENT_COLOR)
    # Label only the axis a bar's true value actually falls in, so a tall
    # bar isn't labeled twice (once in each panel).
    ax_top.bar_label(b1t, labels=[fmt(v) if v > bottom_max else "" for v in pri], fontsize=7.5, padding=1)
    ax_top.bar_label(b2t, labels=[fmt(v) if v > bottom_max else "" for v in cur], fontsize=7.5, padding=1)
    ax_bot.bar_label(b1b, labels=[fmt(v) if v <= bottom_max else "" for v in pri], fontsize=7.5, padding=1)
    ax_bot.bar_label(b2b, labels=[fmt(v) if v <= bottom_max else "" for v in cur], fontsize=7.5, padding=1)
    if show_growth:
        _annotate_pair_growth(ax_top, x, pri, cur, pairs, only_if=lambda top: top > bottom_max)
        _annotate_pair_growth(ax_bot, x, pri, cur, pairs, only_if=lambda top: top <= bottom_max)
    lo = min(0, min(pri + cur))
    _finish_broken_axes(ax_top, ax_bot, bottom_max, top_max, lo=lo)
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
    fmt = (lambda v: f"{fmt_fixed(v * 100, 1)}%") if is_percent else _num_fmt

    brk = _outlier_break(vals)
    if brk is None:
        ax = fig.add_subplot(subplot_spec)
        bars = ax.bar(cats, vals, color=color or theme.BLUE)
        ax.bar_label(bars, labels=[fmt(v) for v in vals], fontsize=8, padding=1)
        _add_headroom(ax, vals)
        ax.tick_params(axis="x", labelsize=8)
        _style_bar_axes(ax)
        return True

    bottom_max, top_max = brk
    ax_top, ax_bot = _make_broken_axes(fig, subplot_spec)
    # Draw each bar's REAL height on both axes and let each axis's own ylim
    # clip away whatever doesn't belong to it, so a tall bar reads as ONE
    # bar interrupted by the break instead of a short one floating in
    # ax_top with nothing connecting it down to zero - see grouped_bar's
    # longer version of this comment for why (same fix, same reason).
    bars_top = ax_top.bar(cats, vals, color=color or theme.BLUE)
    bars_bot = ax_bot.bar(cats, vals, color=color or theme.BLUE)
    ax_top.bar_label(bars_top, labels=[fmt(v) if v > bottom_max else "" for v in vals], fontsize=8, padding=1)
    ax_bot.bar_label(bars_bot, labels=[fmt(v) if v <= bottom_max else "" for v in vals], fontsize=8, padding=1)
    lo = min(0, min(vals))
    _finish_broken_axes(ax_top, ax_bot, bottom_max, top_max, lo=lo)
    ax_bot.tick_params(axis="x", labelsize=8)
    return True


def stacked_bar(ax, categories, series_dict, colors, pct100=True, value_labels=True, show_totals=False,
                 show_yaxis=False, raw_value_labels=False, prior_series=None, display_totals=None,
                 segment_pcts=None):
    """series_dict: {series_name: [value_per_category, ...]}.

    `segment_pcts`, if given (same shape as series_dict), is what each
    segment is labelled with, as a fraction shown as a whole percent
    ("12%"), instead of its share of the bar - e.g. slide 13, whose segments
    are sized by the channel commission rates themselves (normalized to a
    full bar) and labelled with the rates as-is. A None entry leaves that
    segment unlabelled.

    `display_totals`, if given (aligned to `categories`), is what
    `show_totals` prints above each bar instead of the bar's own summed
    total - for a bar whose series are already mix fractions (they sum to 1),
    whose real absolute total has to come from elsewhere. A None entry gets
    no total label.

    `prior_series`, if given (same shape as series_dict, aligned to the same
    `categories`, for the last-year counterpart period), adds each bar's YoY
    growth % - its absolute total vs. the prior period's total for the same
    category - on the line beneath the total when `show_totals` is on, or
    alone above the bar otherwise. A category with no positive prior total
    gets no growth label.

    `show_totals`, if set, annotates each bar's own absolute total (the
    per-category sum across all series, before any pct100 normalization -
    already computed as `totals` below regardless of that flag) just above
    the bar, in Indian lakh/crore grouping - for a pct100 chart, this is how
    a reader sees both the mix (%, inside each segment) and the underlying
    scale (the absolute total, above the bar) at once.

    `raw_value_labels`, with pct100=True, keeps the normalized-height bars
    (every category the same total height, so a category whose own total is
    tiny next to another's isn't squashed to invisible) but labels each
    segment with its real absolute value instead of a %-of-bar share - for a
    metric where the categories' totals differ by orders of magnitude (e.g.
    Individual Agents vs. Web Aggregators) and the reader needs the actual
    counts, not just each company's proportion within that one category.

    `show_yaxis=False` drops the y tick labels/ticks and the left spine -
    for a pct100 chart with `value_labels` on, the % already printed inside
    each segment makes the axis redundant."""
    n = len(categories)
    # A category where every series is None/0 (e.g. no SAHI company writes
    # any Govt.-scheme business) has nothing to show - drop it rather than
    # rendering an empty bar with a fabricated total.
    # Likewise a category whose shown total (`display_totals`) is exactly 0:
    # its mix fractions describe nothing - e.g. Narayana's FY25 geography,
    # stored as 100% Karnataka against a GDPI of 0 - so it isn't drawn.
    present = [i for i in range(n)
               if any((v[i] is not None and v[i] != 0) for v in series_dict.values())
               and not (display_totals is not None and display_totals[i] == 0)]
    if not present:
        return False
    cats = [categories[i] for i in present]
    raw = {name: [vals[i] if vals[i] is not None else 0 for i in present] for name, vals in series_dict.items()}
    totals = [sum(raw[name][j] for name in raw) or 1 for j in range(len(cats))]
    if pct100:
        data = {name: [raw[name][j] / totals[j] for j in range(len(cats))] for name in raw}
    else:
        data = raw
    bottoms = [0.0] * len(cats)
    for name, vals in data.items():
        bars = ax.bar(cats, vals, bottom=bottoms, label=name, color=colors.get(name, theme.ORANGE))
        if value_labels:
            # Suppress the label for a segment too small (<4% of its bar's
            # own total) to fit legibly - avoids overlapping text for
            # near-zero segments, which otherwise render as an illegible
            # cluster. In pct100 mode `v` is already that share (0-1) - `data`
            # was normalized by `totals` above - so dividing by `tot` (the
            # category's ABSOLUTE total) a second time here would compare a
            # fraction against a Rs.-Crore-sized number and suppress every
            # label unconditionally; only the raw-value branch needs the
            # division to turn `v` into a share at all.
            labels = []
            for j, (v, tot, rv) in enumerate(zip(vals, totals, raw[name])):
                share = v if pct100 else (v / tot if tot else 0)
                if not v or share < 0.04:
                    labels.append("")
                elif segment_pcts is not None:
                    pv = (segment_pcts.get(name) or [None] * n)[present[j]]
                    labels.append(f"{fmt_fixed(pv * 100, 0)}%" if pv is not None else "")
                elif raw_value_labels:
                    labels.append(_num_fmt(rv))
                else:
                    labels.append(f"{fmt_fixed(v * 100, 0)}%" if pct100 else _num_fmt(v))
            ax.bar_label(bars, labels=labels, label_type="center", fontsize=7, color="white")
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    growths = [None] * len(cats)
    if prior_series:
        for j, i in enumerate(present):
            pvals = [vals[i] for vals in prior_series.values() if vals[i] is not None]
            growths[j] = _growth(sum(raw[name][j] for name in raw), sum(pvals) if pvals else None)
    show_growth = any(g is not None for g in growths)
    if display_totals is not None:
        totals = [display_totals[i] for i in present]
    if show_totals or show_growth:
        headroom = 0.03 if pct100 else max(bottoms) * 0.03
        for x, (top, tot, g) in enumerate(zip(bottoms, totals, growths)):
            if show_totals and tot is not None:
                ax.text(x, top + headroom, _indian_grouping(tot), ha="center", va="bottom", fontsize=8.5,
                        fontweight="bold", color=theme.DARK_TEXT)
            if g is not None:
                # Stacked just above the total (offset in points, so it
                # clears the total's own text height at any axis scale).
                ax.annotate(_growth_label(g), (x, top + headroom), textcoords="offset points",
                            xytext=(0, 11 if show_totals else 0), ha="center", va="bottom", fontsize=7.5,
                            fontweight="bold", color=_growth_color(g))
    ax.tick_params(axis="x", labelsize=7, rotation=0)
    # Extra room above the bars for the growth line, on top of what the
    # total alone needs.
    growth_room = (0.12 if show_totals else 0.10) if show_growth else 0.0
    if pct100:
        ax.set_ylim(0, (1.16 if show_totals else 1.05) + growth_room)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    elif show_totals or show_growth:
        ax.set_ylim(0, max(bottoms) * (1.12 + growth_room))
    if show_yaxis:
        ax.tick_params(axis="y", labelsize=7)
    else:
        ax.tick_params(axis="y", left=False, labelleft=False)
        ax.spines["left"].set_visible(False)
    ax.legend(fontsize=6, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=min(len(data), 5))
    ax.spines[["top", "right"]].set_visible(False)
    return True


def change_bar(ax, changes, unit="%"):
    """Horizontal diverging bar - replaces the reference deck's dot/bubble
    'Market Share Change' mini-chart with a simpler, equally-informative
    static chart. `changes`: {company_key: signed_change_value}.

    Each bar keeps its company's own colour (matching the doughnut above it
    on the same page) whatever the sign - direction is already carried by
    the bar's side of the zero line and the +/- label."""
    pairs = [(k, v) for k, v in changes.items() if v is not None]
    if not pairs:
        return False
    labels = [theme.COMPANY_DISPLAY_NAME.get(k, k) for k, _ in pairs]
    vals = [v for _, v in pairs]
    colors = [theme.COMPANY_COLORS.get(k, theme.ORANGE) for k, _ in pairs]
    y = range(len(labels))
    bars = ax.barh(list(y), vals, color=colors)
    ax.bar_label(bars, labels=[f"{'+' if v >= 0 else ''}{fmt_fixed(v, 1)}{unit}" for v in vals], fontsize=8, padding=3)
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = (hi - lo) or (abs(hi) or 1)
    ax.set_xlim(lo - span * 0.18 if lo < 0 else lo, hi + span * 0.18 if hi > 0 else hi)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.tick_params(axis="x", labelsize=7)
    ax.spines[["top", "right"]].set_visible(False)
    return True


def trend_lines(fig, subplot_spec, years, company_keys, company_values, title=None, unit_label="INR Crore",
                 is_percent=False, value_fmt=None, gap=0.14, pad=0.12, stretch=0.4, colors=None):
    """One horizontal LANE per company (stacked top-to-bottom in
    `company_keys` order), rather than every company sharing one y-axis. A
    shared axis flattens whichever companies are far smaller than the
    period's leader - e.g. a fast young insurer's 3x growth is invisible
    next to STAR's absolute scale - so within its own lane each series is
    independently min-max normalized ((v - min) / (max - min), padded by
    `pad` on both ends for label clearance), making every company's OWN
    shape equally readable regardless of its neighbors' scale. The plotted
    position is this normalized value (compressed toward the lane's
    vertical center by `stretch` - see below); every point is still
    labeled with its REAL value via `fmt`. `gap` (in lane-height units)
    separates lanes so no lane's line/labels can reach into its neighbor's.
    Companies are identified via a shared legend below the axes (report-wide
    convention), not per-lane labels.

    Plain min-max normalization always stretches a lane's min to its very
    bottom and max to its very top, regardless of how large that range
    actually is in real terms - a metric that only moves ~20-30%
    peak-to-trough over 9 years (e.g. Agent Productivity) ends up looking
    just as dramatic a zigzag as one that triples. `stretch` (0-1) pulls the
    normalized value in toward the lane's center by that factor before
    plotting, damping the visual amplitude uniformly; the REAL values in the
    labels are unaffected - only the line's shape is calmed down.

    `company_values`: {company_key: [value_or_None, ...]} aligned to `years`.
    `value_fmt`, if given, overrides the default label formatter (Indian
    lakh/crore grouping, or a whole-number percent when `is_percent`) - e.g.
    a metric whose values are small decimals (Rs. Lakhs per agent, ~1.3)
    needs 2 decimal places, which lakh/crore grouping's round-to-int would
    otherwise flatten to "1" for every year.
    `colors`, if given, overrides theme.COMPANY_COLORS per key - for a
    non-company series (e.g. SAHI/Industry aggregates), which would
    otherwise all fall back to the same ORANGE default and be indistinguishable.
    Returns False (draws nothing) if every company's series is empty."""
    fmt = value_fmt or ((lambda v: f"{fmt_fixed(v * 100, 0)}%") if is_percent else _indian_grouping)
    pairs = [(k, company_values.get(k)) for k in company_keys
             if company_values.get(k) and any(v is not None for v in company_values[k])]
    if not pairs:
        return False
    panel_box(fig, subplot_spec, title=title, unit_label=unit_label)
    ax = fig.add_subplot(subplot_spec)
    x = list(range(len(years)))
    n = len(pairs)
    lane_h = 1.0
    step = lane_h + gap
    for i, (k, vals) in enumerate(pairs):
        pts = [(xi, v) for xi, v in zip(x, vals) if v is not None]
        if not pts:
            continue
        color = (colors or {}).get(k) or theme.COMPANY_COLORS.get(k, theme.ORANGE)
        vs = [v for _, v in pts]
        lo, hi = min(vs), max(vs)
        span = (hi - lo) or 1.0
        y0 = (n - 1 - i) * step  # index 0 -> topmost lane

        def norm(v, lo=lo, span=span, y0=y0):
            frac = 0.5 if hi == lo else (v - lo) / span
            frac = 0.5 + (frac - 0.5) * stretch
            return y0 + pad + frac * (lane_h - 2 * pad)

        label = theme.COMPANY_DISPLAY_NAME.get(k, k)
        ax.plot([xi for xi, _ in pts], [norm(v) for _, v in pts], marker="o", markersize=3.5,
                linewidth=1.6, color=color, label=label)
        for xi, v in pts:
            ax.annotate(fmt(v), (xi, norm(v)), textcoords="offset points", xytext=(0, 6),
                        ha="center", fontsize=7.5, color=theme.DARK_TEXT)
    ax.set_xticks(x)
    ax.set_xticklabels(years, fontsize=8)
    ax.margins(x=0.05)
    ax.set_ylim(-gap * 0.3, n * step - gap * 0.7)
    ax.set_yticks([])
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.legend(fontsize=7, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=min(len(pairs), 5))
    return True


def income_table(ax, row_labels, company_keys, values_dict, title, percent_rows=None):
    """values_dict: {row_label: {company_key: value}}. Row labels are the
    table's own first column (not matplotlib's separate `rowLabels`, which
    is positioned outside the table's axes and gets clipped by the page
    edge for a table this close to the left margin).

    `percent_rows`, if given, is the set of row_labels (e.g. "Combined
    Ratio") whose values are fractions to format as a whole-number percent
    ("103.8%") instead of plain comma-grouped money ("1,822") - both shapes
    can appear in the same table (e.g. a segment P&L with money rows above
    ratio rows), so this is a per-row choice, not a whole-table one."""
    percent_rows = percent_rows or set()
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
            if not isinstance(v, (int, float)):
                row.append("-")
            elif r in percent_rows:
                row.append(f"{fmt_fixed(v * 100, 1)}%")
            else:
                row.append(fmt_fixed(v, 0, grouping=True))
        cell_text.append(row)
    n_cols = len(col_labels)
    col_widths = [0.28] + [0.72 / (n_cols - 1)] * (n_cols - 1)
    table = ax.table(cellText=cell_text, colLabels=col_labels, cellLoc="center", colWidths=col_widths,
                      loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    table.scale(1, 1.5)
    for (r, c), cell in table.get_celld().items():
        if r == 0:
            cell.set_facecolor(theme.NAVY)
            cell.set_text_props(color="white", fontweight="bold")
        elif c == 0:
            cell.set_text_props(ha="left", fontweight="bold" if row_labels[r - 1] in
                                ("PBT", "PAT", "UW Profit/(Loss)") else "normal")
            cell._loc = "left"
        cell.set_edgecolor(theme.GRID_COLOR)
    ax.set_title(title, fontsize=10, fontweight="bold", loc="left", pad=6)
    return True
