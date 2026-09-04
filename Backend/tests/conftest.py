"""Shared test configuration.

`pythonpath = ["src"]` in pyproject.toml puts the package on the path, so tests
import `competitor_analysis.*` the same way production code does - no
sys.path juggling in individual test modules.
"""
import pytest

from competitor_analysis import config as cfg

# The period the on-disk fixtures (data/downloads/...) belong to. Tests that
# read real filings resolve their paths through this; nothing asserts on the
# specific dates it maps to.
FIXTURE_FY = "FY25-26"
FIXTURE_QUARTER = "Q3"

# Set at import time, not just in the fixture below: pytest loads conftest
# before the test modules, and several modules derive constants (column
# labels, per-period paths) at import - importing them with no period
# configured raises. The fixture then re-asserts it per test.
cfg.set_period(FIXTURE_FY, FIXTURE_QUARTER)


@pytest.fixture(autouse=True)
def _reporting_period():
    """Every test runs with a period configured.

    Several modules derive path constants and column labels from
    config.FY/QUARTER, and leaving them unset raises. Restoring the values
    afterwards keeps a test that deliberately switches period (to check
    period-agnosticism) from leaking into its neighbours."""
    previous = (cfg.FY, cfg.QUARTER)
    cfg.set_period(FIXTURE_FY, FIXTURE_QUARTER)
    yield
    if previous[0] is not None:
        cfg.set_period(*previous)
