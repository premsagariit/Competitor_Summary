"""
Pydantic schemas for Phase 2 Stage 3 (Gemini) structured extraction.

One model per IRDAI NL-form Gemini is asked to read from, each a flat set of
`ExtractedValue` leaf fields - the metric registry `gemini.master_metric_specs()`
already defines exactly this field set per form (grouped by its `forms` entry);
these models give it a typed, validated shape instead of the ad-hoc JSON-schema
dict `gemini.build_schema()` builds today.

The templated forms (NL-6, NL-29, NL-34, NL-36, NL-41's intermediary rows) are
built with `create_model()` directly off the label lists below
(`CHANNELS_36`, `DEBT_RATINGS`, `MATURITY_BUCKETS`, `STATES`, `INTERMEDIARIES`)
- these live HERE, not in gemini.py, and gemini.py imports them from this
module (gemini.py already needs ExtractedValue for the wiring below, and
schemas.py can't import back from a module that imports it). This makes
schemas.py the canonical source `master_metric_specs()` itself loops over,
rather than a duplicate of lists defined there - so the field set can't
silently drift out of sync with the registry either way.

Each field's alias is the exact metric key `master_metric_specs()` produces
(e.g. "commission_ch_Corporate Agents - Banks"); the Python attribute name is
a sanitized, valid-identifier form of the same label for downstream code to
use. `populate_by_name=True` lets a model be built from either.

Wired into gemini.py's extraction call (build_schema/_extract_metrics_via_
gemini_uncached/_call_with_retry) - see that module for the batching and
retry-on-validation-failure design.

regroup_by_form()/KEY_TO_FIELD wire this into data_engine.py's derived-metrics
and row-mapping layers: they turn the flat per-key dict
extract_company_metrics_async() returns back into one validated FORM_SCHEMAS
instance per form, and back-map a metric key to (form, python field name).
"""
import collections
import re

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

_CONFIG = ConfigDict(populate_by_name=True, extra="forbid")

CHANNELS_36 = [
    ("Individual Agents", "Individual Agents"),
    ("Corporate Agents - Banks", "Corporate Agents - Banks"),
    ("Corporate Agents - Others", "Corporate Agents - Others"),
    ("Brokers", "Brokers"),
    ("Direct Business", "Direct Business"),
    ("Common Service Centers / CSC", "CSC"),
    ("Insurance Marketing Firm / IMF", "IMF"),
    ("Web Aggregators", "Web Aggregator"),
    ("Point of Sales person / POS", "POS"),
]
STATES = ["Uttar Pradesh", "Maharashtra", "Karnataka", "Haryana", "Tamil Nadu", "Kerala", "Delhi", "Others"]
DEBT_RATINGS = [
    ("Sovereign", "Any other (Sovereign)"), ("AAA rated", "AAA rated"),
    ("AA or better", "AA or better"),
    ("Rated below AA but above A", "Rated below AA but above A"),
    ("Rated below A", "Rated below A but above B / Rated Below B combined"),
]
MATURITY_BUCKETS = [
    "Up to 1 year", "More than 1 year and upto 3 years",
    "More than 3 years and upto 7 years", "More than 7 years and upto 10 years",
    "Above 10 years",
]
INTERMEDIARIES = [
    ("Individual Agents", "Individual Agents"), ("Corporate Agents-Banks", "CA-Banks"),
    ("Corporate Agents-Others", "CA-Others"), ("Insurance Brokers", "Brokers"),
    ("Web Aggregators", "WA"), ("Insurance Marketing Firm", "IMF"),
    ("Point of Sales persons", "POS"),
]


class ExtractedValue(BaseModel):
    """One leaf metric's extraction result, mirroring the audit-trail shape
    `gemini._extract_metrics_via_gemini_uncached` already returns per key.

    `current`/`prior` are the period-agnostic Python names - the pipeline
    already supports arbitrary FY/quarter via `config.set_period()`, so
    downstream code (derived metrics, mapping) shouldn't read a hard-coded
    quarter off an attribute name. On the wire they keep their existing
    `fy26_q3_value`/`fy25_q3_value` aliases: `gemini.PROMPT_TEMPLATE`'s prose
    instructs the model using those exact literal names, and data_engine.py
    reads the resulting dict's `fy26_q3`/`fy25_q3` keys - changing either
    without touching that Stage-2 code (out of scope here) would silently
    break both.
    """
    model_config = _CONFIG

    found: bool
    current: float | None = Field(default=None, alias="fy26_q3_value")
    prior: float | None = Field(default=None, alias="fy25_q3_value")
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

# metric key -> (form, python field name), e.g. "commission_ch_Corporate
# Agents - Banks" -> ("NL-6", "commission_ch_corporate_agents_banks"). Built
# from FORM_SCHEMAS itself, not hand-copied, so it can't drift from it either.
KEY_TO_FIELD = {
    f.alias or name: (form, name)
    for form, model in FORM_SCHEMAS.items()
    for name, f in model.model_fields.items()
}


def regroup_by_form(raw, specs):
    """Turns extract_company_metrics_async()'s flat return shape
    ({key: {"fy26_q3":.., "fy25_q3":.., "found":.., "source_form":..,
    "page_number":.., "evidence":.., "notes":..}}) back into one validated
    FORM_SCHEMAS instance per form present in `specs` (master_metric_specs()
    or a filtered subset of it).

    A key `specs` lists but `raw` has no entry for (that form's pages were
    never in the payload at all) is treated as not-found, the same "don't
    invent a value" contract ExtractedValue enforces everywhere else - not
    a KeyError.
    """
    by_form = collections.defaultdict(dict)
    for m in specs:
        key = m["key"]
        v = raw.get(key) or {"found": False}
        fields = {
            "fy26_q3_value": v.get("fy26_q3"), "fy25_q3_value": v.get("fy25_q3"),
            "found": v.get("found", False), "source_form": v.get("source_form"),
            "page_number": v.get("page_number"), "evidence": v.get("evidence"),
            "notes": v.get("notes"),
        }
        for form in m["forms"]:
            if form in FORM_SCHEMAS:
                by_form[form][key] = fields
    return {form: FORM_SCHEMAS[form].model_validate(fields) for form, fields in by_form.items()}
