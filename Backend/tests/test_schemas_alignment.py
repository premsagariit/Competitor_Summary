"""schemas.py's Pydantic models are built off gemini.py's own label lists
(CHANNELS_36, DEBT_RATINGS, ...), not a hand-copied duplicate - but that
guards against typos, not against the two sides drifting apart over time
(e.g. a new metric added to master_metric_specs() without a matching schema
field). This asserts the field sets are exactly equal, per form, so such a
drift fails a test instead of silently producing an incomplete schema."""
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
