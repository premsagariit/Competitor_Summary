"""Offline coverage for schemas.regroup_by_form/KEY_TO_FIELD and data_engine's
Pydantic-driven derived-metrics wiring (data_engine._converted_value,
compute_derived_metrics's regrouped/kind_by_key signature).

The byte-identical check against real 7-company extraction results (does the
refactored path produce the exact same (slide, metric1, metric2) -> (cur,
prior) output as the pre-schemas version) was run manually against live data
and isn't repeated here - this locks in the wiring's own correctness with
synthetic inputs instead."""
from competitor_analysis.extraction import schemas
from competitor_analysis.extraction import data_engine as de
from competitor_analysis.extraction import gemini


SPECS = [
    {"key": "manpower_cost", "description": "d", "forms": ["NL-7"], "kind": "money", "rows": []},
    {"key": "it_spend", "description": "d", "forms": ["NL-7"], "kind": "money", "rows": []},
    {"key": "rent_expense", "description": "d", "forms": ["NL-7"], "kind": "money", "rows": []},
]


def test_regroup_by_form_builds_typed_objects_and_treats_a_missing_key_as_not_found():
    raw = {
        "manpower_cost": {"fy26_q3": 500.0, "fy25_q3": 400.0, "found": True,
                          "source_form": "NL-7", "page_number": 3, "evidence": "e", "notes": None},
        "it_spend": {"fy26_q3": 100.0, "fy25_q3": 80.0, "found": True,
                    "source_form": "NL-7", "page_number": 3, "evidence": "e", "notes": None},
        # rent_expense intentionally absent - its form's pages were never in
        # the payload at all, distinct from Gemini itself reporting found=False.
    }
    regrouped = schemas.regroup_by_form(raw, SPECS)
    nl7 = regrouped["NL-7"]
    assert nl7.manpower_cost.current == 500.0
    assert nl7.manpower_cost.prior == 400.0
    assert nl7.rent_expense.found is False
    assert nl7.rent_expense.current is None
    assert nl7.rent_expense.prior is None


def test_converted_value_applies_the_same_kind_conversion_as_before():
    raw = {"manpower_cost": {"fy26_q3": 500.0, "fy25_q3": 400.0, "found": True,
                             "source_form": None, "page_number": None, "evidence": None, "notes": None}}
    regrouped = schemas.regroup_by_form(raw, SPECS)
    kind_by_key = {m["key"]: m["kind"] for m in SPECS}
    assert de._converted_value(regrouped, kind_by_key, "manpower_cost") == (5.0, 4.0)  # money: /100


def test_key_to_field_covers_every_in_scope_metric():
    for m in gemini.master_metric_specs():
        for form in m["forms"]:
            if form in schemas.FORM_SCHEMAS:
                assert m["key"] in schemas.KEY_TO_FIELD, m["key"]


def test_compute_derived_metrics_still_returns_slide_tuple_keys():
    """Signature changed (regrouped, kind_by_key instead of converted) - the
    return shape and at least one real formula's behavior must not have.

    Uses the REAL, full master_metric_specs() (not a truncated subset):
    compute_derived_metrics calls get() unconditionally for keys spanning all
    11 forms, so `regrouped` must have every form present - regroup_by_form
    gives every key regrouped doesn't explicitly set a found=False/null
    ExtractedValue automatically, so an otherwise-empty `raw` is enough."""
    specs = gemini.master_metric_specs()
    raw = {"manpower_cost": {"fy26_q3": 500.0, "fy25_q3": 400.0, "found": True,
                             "source_form": None, "page_number": None, "evidence": None, "notes": None}}
    regrouped = schemas.regroup_by_form(raw, specs)
    kind_by_key = {m["key"]: m["kind"] for m in specs}
    income = {"gwp": (1000.0, 800.0)}
    derived = de.compute_derived_metrics("ACME", regrouped, kind_by_key, income)
    assert derived[(21, "Manpower to GWP ratio", None)] == (round(5.0 / 1000.0, 4), round(4.0 / 800.0, 4))
