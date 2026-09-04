# Known issues — logged, deliberately not fixed

Findings surfaced while refactoring Phase 2 Stage 3 extraction to Pydantic
schemas (commits `2b4ff61`..`e391238`). Each was diagnosed, confirmed against
real data, and then **deliberately left alone** as out of that refactor's
scope — not overlooked. Recorded here with the evidence and the distinguishing
facts that were expensive to establish, so none of them needs re-deriving.

---

## 1. `FORM_PATTERNS["NL-4"]` also matches every NL-41 page

**Where:** `src/competitor_analysis/extraction/pdf_cache.py:59`

```python
"NL-4": ("Premium Schedule (NL-4)", r"FORM\s+NL-4"),
```

The pattern is unanchored, and detection uses `re.search` against page text
(`pdf_cache.py:96-99`), so `FORM\s+NL-4` matches inside `FORM NL-41`. Every
page headed "FORM NL-41" is therefore tagged as **both** NL-4 and NL-41.

**Confirmed** across all 7 companies, FY25-26 Q3 — every company's NL-41 page
reports `forms_detected=['NL-4', 'NL-41']`:

| Company | NL-41 page |
|---|---|
| NBHI | 42 |
| ABHI | 44 |
| Care Health | 47 |
| Star Health | 47 |
| Manipal Cigna | 41 |
| Narayana Health | 42 |
| Galaxy Health | 45 |

**Why it isn't currently producing wrong numbers:** the only NL-4 consumer,
`data_engine.py:774`, calls `get_form_page(pdf_path, r"FORM\s+NL-4")` with
the default `all_matches=False`. `pages_for_pattern` returns pages in document
order and `get_form_page` takes `pages[:1]`, so the genuine NL-4 (Premium
Schedule) page wins because it precedes NL-41 (Offices) in every
ascending-form-number filing checked.

**What would make it bite:** any filing that orders forms differently, or any
future NL-4 call passing `all_matches=True`, silently merges NL-41's Offices
tables into NL-4's payload. NL-4 feeds Gross Direct Premium / Net Written
Premium / Net Earned Premium into the Slide 18 income statement
(`data_engine.py:793-799`), so the failure mode is corrupted **headline
premium figures**, with no error raised.

**Fix shape — and a trap in the obvious version.** Use a negative lookahead,
not a word boundary:

```python
r"FORM\s+NL-4(?!\d)"     # correct
r"FORM\s+NL-4\b"         # WRONG - see below
```

Both stop NL-4 matching `FORM NL-41`. But `\b` applied registry-wide breaks
NL-12: its schedule is "NL-12 & 12A", so `FORM NL-12A` **must** keep matching
NL-12's pattern, and `r"FORM\s+NL-12\b"` no longer does. `(?!\d)` preserves it.
Verified:

| Pattern | vs `FORM NL-41` | vs `FORM NL-12A` |
|---|---|---|
| current (unanchored) | matches (bug) | matches (wanted) |
| `+ \b` | no match (fixed) | **no match (breaks NL-12)** |
| `+ (?!\d)` | no match (fixed) | matches (wanted) |

**Scope of the audit:** within this registry, NL-4 is the *only* colliding
pattern — NL-1/2/3 carry full suffixes (`NL-3-B-BS`), and no other registry
key numerically extends another (`NL-5`, `NL-6`, `NL-7`, `NL-12`, `NL-20`,
`NL-29`, `NL-31`, `NL-33`, `NL-34`, `NL-36`, `NL-37`, `NL-41` have no
same-prefix sibling here). Note that "safe within the registry" is not "safe
against the document": these patterns are unanchored against arbitrary filing
text, so the audit should be against the forms that actually appear in IRDAI
filings, not just the keys listed here.

**Not the fix:** reordering `FORM_PATTERNS`, or relying on document order.
Both leave the silent-corruption path open.

**Why deferred:** `pdf_cache.py` / `data_engine.py` premium extraction is
Stage 1/2, explicitly outside the Stage 3 Pydantic refactor.

---

## 2. No plausibility gate before values reach derived formulas

**Where:** the path `gemini.py` →
`data_engine._converted_value()` (`data_engine.py:1074`) →
`compute_derived_metrics()` (`data_engine.py:1084`).

`ExtractedValue`'s validator (`schemas.py:97`) checks **structure only** —
`found=False` ⟺ both values null, and `found=True` requires a current value.
Nothing checks **magnitude**. An implausible extracted number passes
validation cleanly and propagates straight into derived formulas and onto the
sheet.

**Confirmed**, FY25-26 Q3 live run — prior-year `combined_ratio`:

| Company | Prior-year value | Current-year value |
|---|---|---|
| Narayana Health | **913.0** | 166.0 |
| Galaxy Health | **2144.74** | 190.01 |

A combined ratio that high implies paying out 9–20× premium in claims plus
expenses. Both passed validation without complaint; no retry was triggered.

**The two causes are different — this distinction was expensive to establish,
so don't re-litigate it as one bug:**

- **Narayana Health** — the source cell is a bare decimal pair, `1.66` /
  `9.13`, with no `%` sign. `PROMPT_TEMPLATE`'s own normalization rule ("no %
  sign and a bare decimal roughly 0–20 → multiply by 100") fires *correctly
  per its instruction* and yields 166% / 913%. The model's own `notes` field
  says so verbatim: *"Multiplied bare decimal multiples by 100 per
  instructions."* So this is the **prompt heuristic**, not a model
  hallucination.
- **Galaxy Health** — the source cell already prints `190.01%` and
  `2144.74%` with explicit `%` signs, so no multiplication heuristic was
  applied at all; the model read what was extracted. Distinguishing a genuine
  extreme figure from a table-extraction artifact requires checking the
  actual PDF page (NL-20, page 24).

**Consumer:** `combined_ratio` (`gemini.py:200`) targets Slides 19 and 28.

**Why deliberately not fixed:** a magnitude gate is new domain logic, and a
wrong threshold is actively harmful in both directions — young insurers
genuinely do post combined ratios above 200%, so a tight band suppresses real
data, while a loose band gives false confidence.

**Where it would go if built:** after `_converted_value()` and before the
formulas consume the result — most naturally a per-`kind` sanity band that
routes out-of-band values to the audit trail / a review gate rather than
silently dropping or "correcting" them. The evidence/notes audit trail
(`write_extraction_audit`) already carries what a reviewer needs.

---

## Appendix — related data-completeness gaps

Added beyond the two items above because they were also explicit
"flag, don't touch" decisions in the same session and would otherwise need
rediscovering. Strike if not wanted.

**Care Health / NL-29 — zero pages detected.** `pdf_cache` finds *no* page
matching the NL-29 pattern in this company's PDF, so the payload is empty
before Gemini sees it, and all 10 `debt_rating_*` / `debt_maturity_*` fields
correctly return `found=False`. Stage 1 page-detection question, not an
extraction fault.

**Narayana Health / NL-6 — page found, channels unparsed.** The NL-6 page
*is* found and has a table, but Gemini's own reasoning over the raw
`page.extract_tables()` output can't map it to per-channel commissions (all 9
`commission_ch_*` return `found=False`; `ri_commission` succeeds). Note that
`gemini.py`'s payload passes raw nested lists — none of `table_markdown.py`'s
period/group column classification touches Gemini's input, that is a separate
deterministic path. Consistent with this insurer's already-documented
non-standard layout (its NL-2/NL-20 column ordering is flagged as swapped
relative to every other filer, see `forms._text_period_columns`).

**NL-41 FY25_Q3 is structurally unfillable from current inputs.** NL-41 is a
point-in-time snapshot with no prior-year comparative column; the current
filing's only backward-looking column is "beginning of the quarter", i.e.
prior *quarter*, not prior *year*. `GT_Data_Engine.xlsx`'s populated NL-41
prior-year values were keyed by an analyst from **last year's filing** — a
document this pipeline never downloads. This is a missing input document, not
a bug: `gemini.py`'s prompt and the NL-41 annotations are correct as written.
Recorded because this one already cost one investigation cycle after being
mistaken for pipeline output.
