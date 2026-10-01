"""
Round-half-up, used everywhere a value is rounded for storage or display.

Python's own round() and f-string formatting round a tie to the nearest EVEN
digit (round(2.5) == 2, f"{0.5:.0f}" == "0"), and work on the float's binary
value, so a figure that prints as 2.675 can round down to 2.67. Reports are
read against hand arithmetic, where a half always goes up: these helpers
round the value as written (its shortest repr), with ties away from zero.
"""
import math
import numbers
from decimal import Decimal, ROUND_HALF_UP


def round_half_up(v, ndigits=None):
    """Drop-in for round(v, ndigits) with ties going up (away from zero):
    round_half_up(2.5) == 3, round_half_up(0.125, 2) == 0.13. Like round(),
    returns an int when ndigits is omitted, else a float. Non-finite values
    are returned unchanged."""
    if isinstance(v, numbers.Integral):
        d = Decimal(int(v))
    else:
        # float() first: numpy scalars (what matplotlib/pandas hand back) repr
        # as "np.float64(...)" on numpy 2, which Decimal can't parse.
        f = float(v)
        if not math.isfinite(f):
            return v
        d = Decimal(repr(f))
    exp = Decimal(1).scaleb(-(ndigits or 0))
    r = d.quantize(exp, rounding=ROUND_HALF_UP)
    # + 0.0 turns -0.0 (e.g. -0.04 rounded to 1 place) into 0.0, so it
    # never displays as "-0.0".
    return int(r) if ndigits is None else float(r) + 0.0


def fmt_fixed(v, ndigits=0, grouping=False):
    """f"{v:.{ndigits}f}" (or ",.{ndigits}f" with grouping) with round-half-up."""
    return f"{round_half_up(v, ndigits):{',' if grouping else ''}.{ndigits}f}"
