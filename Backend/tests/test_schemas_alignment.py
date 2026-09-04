"""schemas.py's per-form Pydantic models are built off its own label lists
(CHANNELS_36, DEBT_RATINGS, ...), which gemini.master_metric_specs() loops
over too - not a hand-copied duplicate, but that guards against typos, not
against the two sides drifting apart over time (e.g. a new metric added to
master_metric_specs() without a matching schema field). This asserts the
field sets are exactly equal, per form, so such a drift fails a test instead
of silently producing an incomplete schema.

Also covers gemini.py's wire-level schema: build_schema()'s per-value JSON
Schema fragment is a separate, hand-shaped literal (kept that way to avoid an
untested shape against Gemini's response_json_schema - see gemini._VALUE_SCHEMA's
comment), so it needs its own drift guard against ExtractedValue's fields."""
import collections

from competitor_analysis.extraction import gemini
from competitor_analysis.extraction import schemas


def _keys_by_form():
    by_form = collections.defaultdict(set)
    for spec in gemini.master_metric_specs():
        for form in spec["forms"]:
            by_form[form].add(spec["key"])
    return by_form


def test_every_in_scope_form_has_a_schema():
    in_scope = set(schemas.FORM_SCHEMAS)
    specced = set(_keys_by_form())
    # NL-1/2/4 are Stage 1/2 deterministic forms, not part of this schema set.
    stage3_only = specced - {"NL-1", "NL-2", "NL-4"}
    assert in_scope == stage3_only


def test_schema_fields_match_metric_registry_exactly():
    by_form = _keys_by_form()
    for form, model in schemas.FORM_SCHEMAS.items():
        aliases = {f.alias or name for name, f in model.model_fields.items()}
        assert aliases == by_form[form], (
            f"{form}: schema aliases and master_metric_specs() keys differ - "
            f"only in registry: {by_form[form] - aliases}, "
            f"only in schema: {aliases - by_form[form]}"
        )


def test_no_form_exceeds_twenty_fields():
    for form, model in schemas.FORM_SCHEMAS.items():
        assert len(model.model_fields) <= 20, f"{form} has {len(model.model_fields)} fields"


def test_wire_schema_matches_extracted_value_fields():
    """gemini.build_schema()'s per-value fragment must ask for exactly the
    properties ExtractedValue.model_validate() will accept as aliases -
    otherwise a field either can't be populated from the response, or is
    required by ExtractedValue but never asked for on the wire."""
    aliases = {f.alias or name for name, f in schemas.ExtractedValue.model_fields.items()}
    wire_props = set(gemini._VALUE_SCHEMA["properties"])
    assert wire_props == aliases
