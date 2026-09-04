"""
Pydantic schemas for Phase 2 Stage 3 (Gemini) structured extraction.

One model per IRDAI NL-form Gemini is asked to read from, each a flat set of
`ExtractedValue` leaf fields - the metric registry `gemini.master_metric_specs()`
already defines exactly this field set per form (grouped by its `forms` entry);
these models give it a typed, validated shape instead of the ad-hoc JSON-schema
dict `gemini.build_schema()` builds today.

The templated forms (NL-6, NL-29, NL-34, NL-36, NL-41's intermediary rows) are
built with `create_model()` directly off the SAME label lists
(`CHANNELS_36`, `DEBT_RATINGS`, `MATURITY_BUCKETS`, `STATES`, `INTERMEDIARIES`)
`master_metric_specs()` itself loops over - not a hand-copied duplicate of
them - so the field set can't silently drift out of sync with the registry.
Each field's alias is the exact metric key `master_metric_specs()` produces
(e.g. "commission_ch_Corporate Agents - Banks"); the Python attribute name is
a sanitized, valid-identifier form of the same label for downstream code to
use. `populate_by_name=True` lets a model be built from either.

Not wired into gemini.py yet - this is groundwork only (see forms.py/
table_markdown.py commits for the same incremental approach applied to the
Stage 1 side of this refactor).
"""
import re

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from competitor_analysis.extraction.gemini import (
    CHANNELS_36, DEBT_RATINGS, MATURITY_BUCKETS, STATES, INTERMEDIARIES,
)

_CONFIG = ConfigDict(populate_by_name=True, extra="forbid")


class ExtractedValue(BaseModel):
    """One leaf metric's extraction result, mirroring the audit-trail shape
    `gemini._extract_metrics_via_gemini_uncached` already returns per key.

    `current`/`prior` replace the existing dict's period-literal
    `fy26_q3`/`fy25_q3` names - the pipeline already supports arbitrary
    FY/quarter via `config.set_period()`, so a period-agnostic name here
    avoids baking one hard-coded quarter into a schema meant to outlive it.
    """
    model_config = _CONFIG

    found: bool
    current: float | None = None
    prior: float | None = None
    source_form: str | None = None
    page_number: int | None = None
    evidence: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _found_matches_values(self) -> "ExtractedValue":
        # Encodes the prompt's own contract (gemini.PROMPT_TEMPLATE: "If a
        # metric is genuinely not present ... set found=false and both values
        # to null - do not guess, estimate, or compute a value that isn't
        # directly shown") as a structural check instead of a convention the
        # model can silently violate. A response that fails this is malformed
        # structured output, not a business fact - callers should retry once
        # then mark the metric unresolved rather than accept it.
        if self.found and self.current is None:
            raise ValueError("found=True but current is null")
        if not self.found and (self.current is not None or self.prior is not None):
            raise ValueError("found=False but current/prior is non-null - never invent values")
        return self


def _sanitize(label: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", label).strip("_").lower()


def _value_fields(prefix: str, labels: list[str]) -> dict:
    """{python_name: (ExtractedValue, Field(alias=<exact master_metric_specs() key>))}
    for one templated group of metric-key suffixes."""
    return {
        f"{prefix}_{_sanitize(label)}": (ExtractedValue, Field(alias=f"{prefix}_{label}"))
        for label in labels
    }


CHANNEL_LABELS = [metric2 for _, metric2 in CHANNELS_36]
DEBT_RATING_LABELS = [metric1 for metric1, _ in DEBT_RATINGS]
INTERMEDIARY_LABELS = [metric2 for _, metric2 in INTERMEDIARIES]


class NL3(BaseModel):
    """Balance Sheet (NL-3-B-BS): Capital, Net Worth inputs."""
    model_config = _CONFIG

    capital: ExtractedValue
    bs_reserves_surplus: ExtractedValue
    bs_fair_value_change_sh: ExtractedValue
    bs_debit_balance_pl: ExtractedValue


NL6 = create_model(
    "NL6",
    __doc__="Commission Schedule (NL-6): channel-wise commission + reinsurance commission.",
    __config__=_CONFIG,
    **_value_fields("commission_ch", CHANNEL_LABELS),
    ri_commission=(ExtractedValue, ...),
)


class NL7(BaseModel):
    """Operating Expenses Schedule (NL-7): manpower, IT spend, rent."""
    model_config = _CONFIG

    manpower_cost: ExtractedValue
    it_spend: ExtractedValue
    rent_expense: ExtractedValue


class NL12(BaseModel):
    """Investment Schedule (NL-12 & 12A): portfolio breakdown + AUM."""
    model_config = _CONFIG

    inv_govt_bonds: ExtractedValue
    inv_corporate_bonds: ExtractedValue
    inv_deposits: ExtractedValue
    inv_equity: ExtractedValue
    inv_mutual_funds: ExtractedValue
    aum_total: ExtractedValue
    aum_shareholders: ExtractedValue
    aum_policyholders: ExtractedValue


class NL20(BaseModel):
    """Analytical Ratios Schedule (NL-20)."""
    model_config = _CONFIG

    combined_ratio: ExtractedValue
    loss_ratio: ExtractedValue
    expense_ratio_nwp: ExtractedValue
    eom_ratio_gdp: ExtractedValue
    solvency_ratio: ExtractedValue


NL29 = create_model(
    "NL29",
    __doc__="Detail Regarding Debt Securities (NL-29): rating mix + maturity mix, both '% of total for this class'.",
    __config__=_CONFIG,
    **_value_fields("debt_rating", DEBT_RATING_LABELS),
    **_value_fields("debt_maturity", MATURITY_BUCKETS),
)


class NL33(BaseModel):
    """Reinsurance/Retrocession Risk Concentration (NL-33): total premium ceded."""
    model_config = _CONFIG

    ri_ceded_total: ExtractedValue


NL34 = create_model(
    "NL34",
    __doc__="Geographical Distribution of Business (NL-34): per-state GDPI.",
    __config__=_CONFIG,
    **_value_fields("state", STATES),
)


NL36 = create_model(
    "NL36",
    __doc__="Business - Channels Wise (NL-36): premium and policy count per channel.",
    __config__=_CONFIG,
    **_value_fields("channel_premium", CHANNEL_LABELS),
    **_value_fields("channel_policies", CHANNEL_LABELS),
)


class NL37(BaseModel):
    """Claims Data (NL-37): claim counts, not amounts."""
    model_config = _CONFIG

    claims_os_start: ExtractedValue
    claims_reported: ExtractedValue
    claims_settled: ExtractedValue


NL41 = create_model(
    "NL41",
    __doc__="Offices Information (NL-41): point-in-time snapshot, no prior-year comparative.",
    __config__=_CONFIG,
    employees_onroll=(ExtractedValue, ...),
    agents_individual=(ExtractedValue, ...),
    offices_count=(ExtractedValue, ...),
    **_value_fields("intermediary", INTERMEDIARY_LABELS),
)


FORM_SCHEMAS = {
    "NL-3": NL3, "NL-6": NL6, "NL-7": NL7, "NL-12": NL12, "NL-20": NL20,
    "NL-29": NL29, "NL-33": NL33, "NL-34": NL34, "NL-36": NL36,
    "NL-37": NL37, "NL-41": NL41,
}
