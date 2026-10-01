"""round_half_up / fmt_fixed: a half always rounds up (away from zero), unlike
Python's round() and f-string formatting, which round a tie to even."""
import math

import pytest

from competitor_analysis.rounding import fmt_fixed, round_half_up


@pytest.mark.parametrize("value,ndigits,expected", [
    (0.5, None, 1),
    (1.5, None, 2),
    (2.5, None, 3),      # round(2.5) == 2
    (-2.5, None, -3),
    (0.125, 2, 0.13),    # round(0.125, 2) == 0.12
    (2.675, 2, 2.68),    # round(2.675, 2) == 2.67 (binary representation)
    (12.25, 1, 12.3),
    (0.12345, 4, 0.1235),
    (7, None, 7),
])
def test_round_half_up(value, ndigits, expected):
    assert round_half_up(value, ndigits) == expected


def test_return_types_match_round():
    assert isinstance(round_half_up(2.5), int)
    assert isinstance(round_half_up(2.5, 1), float)


def test_non_finite_passthrough():
    assert math.isnan(round_half_up(float("nan"), 2))
    assert round_half_up(float("inf")) == float("inf")


@pytest.mark.parametrize("value,ndigits,grouping,expected", [
    (0.5, 0, False, "1"),                   # f"{0.5:.0f}" == "0"
    (12.25, 1, False, "12.3"),              # f"{12.25:.1f}" == "12.2"
    (1234567.5, 0, True, "1,234,568"),
    (-0.04, 1, False, "0.0"),               # never "-0.0"
])
def test_fmt_fixed(value, ndigits, grouping, expected):
    assert fmt_fixed(value, ndigits, grouping=grouping) == expected
