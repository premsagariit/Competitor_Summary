# Known issues — logged, deliberately not fixed

Two **open** findings (sections 2 and 3) and one **fixed** finding (section
1, kept for its evidence and reasoning). Sections 1-2 came out of
refactoring Phase 2 Stage 3 extraction to Pydantic schemas (commits
`2b4ff61`..`e391238`); section 3 came out of checking, against a real Q4
filing, whether the pipeline handles a future quarter. None was *introduced*
by that work — all are pre-existing pipeline behavior — but all were
diagnosed and confirmed against real data before being acted on.

An appendix below covers four *closed* findings - already investigated and
explained, not open follow-ups. Keep the three categories separate:
section 1 is "here's a bug, here's what fixed it"; sections 2-3 are "here's
a bug, here's the fix shape, not yet applied"; the appendix is "here's why
this isn't a bug," kept only so it doesn't get mistaken for a new problem
or re-investigated later.

Two appendix entries have now had to be corrected after being written up as
explained-and-not-a-bug (Care Health / NL-29, Narayana / NL-6). Treat an
explanation here as a lead, not a verdict.

---

## 1. [FIXED in `aa93b6e`] `FORM_PATTERNS["NL-4"]` also matched every NL-41 page

**Where:** `src/competitor_analysis/extraction/pdf_cache.py:59`

```python
"NL-4": ("Premium Schedule (NL-4)", r"FORM\s+NL-4"),   # before aa93b6e
"NL-4": ("Premium Schedule (NL-4)", r"FORM\s+NL-4(?!\d)"),   # after
```

The pattern was unanchored, and detection uses `re.search` against page text
(`pdf_cache.py:96-99`), so `FORM\s+NL-4` matched inside `FORM NL-41`. Every
page headed "FORM NL-41" was therefore tagged as **both** NL-4 and NL-41.

The evidence, reasoning, and the rejected-alternative trap below are kept
as-found (pre-fix) for reference - this is what made the bug real and what
ruled out the obvious fix, not a live description of current behavior.

**Confirmed (pre-fix)** across all 7 companies, FY25-26 Q3 — every company's
NL-41 page reported `forms_detected=['NL-4', 'NL-41']`:

| Company | NL-41 page |
|---|---|
| NBHI | 42 |
| ABHI | 44 |
| Care Health | 47 |
| Star Health | 47 |
| Manipal Cigna | 41 |
| Narayana Health | 42 |
| Galaxy Health | 45 |

**Why it wasn't (yet) producing wrong numbers:** the only NL-4 consumer,
`data_engine.py:774`, calls `get_form_page(pdf_path, r"FORM\s+NL-4")` with
the default `all_matches=False`. `pages_for_pattern` returns pages in document
order and `get_form_page` takes `pages[:1]`, so the genuine NL-4 (Premium
Schedule) page wins because it precedes NL-41 (Offices) in every
ascending-form-number filing checked.

**What would have made it bite:** any filing that orders forms differently,
or any future NL-4 call passing `all_matches=True`, would have silently
merged NL-41's Offices tables into NL-4's payload. NL-4 feeds Gross Direct
Premium / Net Written Premium / Net Earned Premium into the Slide 18 income
statement (`data_engine.py:793-799`), so the failure mode would have been
corrupted **headline premium figures**, with no error raised.

**Fix applied — and a trap in the obvious alternative.** Used a negative
lookahead, not a word boundary:

```python
r"FORM\s+NL-4(?!\d)"     # applied in aa93b6e
r"FORM\s+NL-4\b"         # considered and REJECTED - see below
```

Both stop NL-4 matching `FORM NL-41`. But `\b` applied registry-wide would
have broken NL-12: its schedule is "NL-12 & 12A", so `FORM NL-12A` **must**
keep matching NL-12's pattern, and `r"FORM\s+NL-12\b"` would no longer have.
`(?!\d)` preserves it. Verified before choosing:

| Pattern | vs `FORM NL-41` | vs `FORM NL-12A` |
|---|---|---|
| before (unanchored) | matches (bug) | matches (wanted) |
| `+ \b` (rejected) | no match (fixed) | **no match (breaks NL-12)** |
| `+ (?!\d)` (applied) | no match (fixed) | matches (wanted) |

**Scope of the audit:** within this registry, NL-4 was the *only* colliding
pattern — NL-1/2/3 carry full suffixes (`NL-3-B-BS`), and no other registry
key numerically extends another (`NL-5`, `NL-6`, `NL-7`, `NL-12`, `NL-20`,
`NL-29`, `NL-31`, `NL-33`, `NL-34`, `NL-36`, `NL-37`, `NL-41` have no
same-prefix sibling here). Note that "safe within the registry" is not "safe
against the document": these patterns are unanchored against arbitrary filing
text, so a future audit should check against the forms that actually appear
in IRDAI filings, not just the keys listed here.

**Not the fix:** reordering `FORM_PATTERNS`, or relying on document order.
Both would have left the silent-corruption path open.

**Status:** fixed in `aa93b6e`. Re-verified against all 7 companies' real
PDFs post-fix (force-refreshing `pdf_cache`'s on-disk JSON cache, which does
not itself invalidate on a code/regex change, only on the source PDF's
mtime+size): every company's NL-41 page now tags as `['NL-41']` only, NL-4's
own page still tags as `['NL-4']`, and the NL-12/NL-12A cases above were
re-checked against the live post-fix regex strings and still hold. 158/158
tests pass. Was Stage 1 (`pdf_cache.py`), unrelated to the Stage 3 Pydantic
extraction refactor - its own standalone commit.

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

> **CORRECTION (2026-09-06): these two values are almost certainly CORRECT,
> not errors.** They were originally recorded here as "implausible" on the
> reasoning that no real insurer pays out 9–20× premium. That reasoning
> applied mature-insurer intuition to two companies that are not mature.
> Checked against the filings' own registration lines: **Narayana Health
> registered with IRDAI 03-Jan-2024** and **Galaxy Health 20-Mar-2024** —
> both are startups, and every other insurer here dates from 2006-2016.
> Their prior-year comparative (nine months to Dec 2024) therefore covers
> their first months of operation. Prior-year GWP: Narayana **₹0.65 Cr**,
> Galaxy **₹2.38 Cr**, against ₹4,684 Cr (NBHI) and ₹11,603 Cr (Star Health).
> Startup fixed costs over a near-zero premium base produce ratios of exactly
> this magnitude arithmetically. Narayana's `1.66 / 9.13` bare-decimal pair
> also means the prompt's ×100 normalization did the *right* thing.
>
> This makes the case against a naive magnitude gate **stronger, not weaker**:
> a threshold tuned to reject 913% would have discarded correct data from two
> real insurers. §2's original reasoning anticipated this ("young insurers
> genuinely do post combined ratios above 200%"); there is now hard evidence
> for it. The structural gap below is still real - nothing checks magnitude -
> but it currently has **no known instance of an actually-wrong value**, which
> should lower its priority accordingly.

**The two values arise differently — this distinction was expensive to
establish, so don't re-litigate it as one thing:**

- **Narayana Health** — the source cell is a bare decimal pair, `1.66` /
  `9.13`, with no `%` sign. `PROMPT_TEMPLATE`'s own normalization rule ("no %
  sign and a bare decimal roughly 0–20 → multiply by 100") fires *correctly
  per its instruction* and yields 166% / 913%. The model's own `notes` field
  says so verbatim: *"Multiplied bare decimal multiples by 100 per
  instructions."* So this is the **prompt heuristic**, not a model
  hallucination.
- **Galaxy Health** — the source cell already prints `190.01%` and
  `2144.74%` with explicit `%` signs, so no multiplication heuristic was
  applied at all; the model read what was printed. The open question here was
  whether that printed figure was genuine or a table-extraction artifact -
  **resolved: genuine.** Galaxy registered 20-Mar-2024 and its prior-year GWP
  was ₹2.38 Cr, so a 21× combined ratio is what its own first-months numbers
  actually produce.

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

## 3. A form spilling onto a third page is silently truncated

**Where:** `gemini.py:92`, `build_company_payload(pdf_path, form_keys,
max_pages_per_form=2)` → `pdf_cache.pages_for_form(doc, key, max_pages=2)`,
which slices with no warning.

**Not currently losing anything** — no form in FY25-26 Q3 exceeds two pages.
The concern is forward-looking: twelve company/form combinations already sit
at *exactly* the cap, so one added row in a future filing crosses it.

| form | companies at 2 pages (FY25-26 Q3) |
|---|---|
| NL-20 | NBHI, ABHI, Care Health, Star Health, Galaxy Health |
| NL-34 | ABHI, Star Health, Galaxy Health |
| NL-7  | Star Health, Galaxy Health |
| NL-37 | ABHI |
| NL-1  | Galaxy Health |

**Why it would be silent.** Continuation pages *do* repeat their form header
(verified on all twelve — e.g. Star Health's NL-20 pages 25 and 26 both open
`FORM NL-20-ANALYTICAL RATIOS SCHEUDLE`), so a third page would be detected
correctly and *then* dropped by the slice. The metrics on it come back
`found=False`, which is indistinguishable from data the filing genuinely
does not contain — the same confusion that made Care Health's NL-29 gap
(appendix) look explained for as long as it did.

**Why deliberately not fixed here:** the options trade off against each
other and the choice is not obvious. Warning on truncation is free and
makes the failure visible but still loses the data. Raising the cap costs
tokens on every affected call and enlarges the response schema, which
`DEFAULT_BATCH_SIZE`'s comment records as already near an API limit.
Routing an over-cap form to the `awaiting_review` gate is the most
consistent with how `table_markdown.py` treats unresolved columns, but
blocks a run on something that may be harmless.

**Minimum worth doing:** whichever option is chosen, `build_company_payload`
should not drop pages without saying so.

---

## Appendix — closed findings, not open follow-ups

These four are **pre-existing pipeline behavior noticed while verifying
the Pydantic refactor** - distinct from sections 1-3 above, which are open
or fixed defects. As of 2026-09-06:

- **Care Health / NL-29** was initially recorded here as an explained
  page-detection gap. Re-diagnosed later and found to be a real, fixable
  bug - now fixed (`2806ae7`). Kept for the diagnosis and the regression
  trap in the obvious fix.
- **Narayana Health / NL-6** was recorded here as a model-reasoning limit.
  That was a misdiagnosis, corrected 2026-09-06: the channel break-up
  section is simply absent from the filing, so the extraction is already
  correct. Second entry in this appendix to have been written up wrong.
- **NL-41 prior-year** remains explained, with no fix pending: a genuinely
  missing input document.
- **NL-29 maturity-bucket swap** documents a working mechanism, not a gap.

Worth noting the Care Health entry's history as a caution: it was recorded
here as "investigated, explained, not-a-bug" and turned out to be wrong as
soon as someone actually grepped the page text for the form number. An
explanation recorded here is not proof; re-check before relying on one to
rule out a fix.

**Care Health / NL-29 — zero pages detected. [FIXED in `2806ae7`]** `pdf_cache`
found *no* page matching the NL-29 pattern in this company's PDF, so the
payload was empty before Gemini saw it, and all 10 `debt_rating_*` /
`debt_maturity_*` fields returned `found=False`. Diagnosed 2026-09-06: the
form was **present all along** on page 37, headed `NL-29 DETAILS REGARDING
DEBT SECURITIES` — no `FORM` prefix, which every registry pattern required.
Fixed by making `FORM` optional *and line-anchored*
(`r"(?m)^\s*(?:FORM\s+)?NL-29(?!\d)"`); the anchor matters, because an
unanchored optional-`FORM` pattern also matches contents pages that list
schedules mid-line (Star Health's page 2 lists 42 of them as
`30 NL-29-DEBT SECURITIES ...`), and that page precedes its real page 35, so
`get_form_page`'s `pages[:1]` would have read the index instead. Post-fix,
all 7 companies detect 16/16 forms and Care Health returns 10/10 NL-29
fields matching the source page's Book Value "% of total" column exactly.
Audited at the same time: this was the **only** company/form combination in
the registry with zero detected pages.

**Narayana Health / NL-6 — the channel break-up is not in the filing.
[CORRECTED 2026-09-06 — the earlier entry here was a misdiagnosis.]**

This was previously recorded as "page found, channels unparsed", blaming
Gemini's reasoning over raw `page.extract_tables()` output and suggesting
`table_markdown.py`'s column classification as the fix shape. That was
wrong, and it is the same failure the Care Health caution above warns
about: an explanation was written down without anyone reading the page.

Narayana's NL-6 schedule ends at `Net Commission`. It has **no
"Break-up of the expenses (Gross) incurred to procure business" section at
all** — no header, and zero channel labels anywhere on the page. Verified
in both FY25-26 Q3 (page 7 of 46) and FY25-26 Q4 (page 7 of 55), so it is a
consistent characteristic of this filer, not a one-quarter anomaly.

All 6 other insurers do carry the section, and all 6 return 9/9
`commission_ch_*` found. Gemini's own cached note for Narayana reads
*"Channel breakdown not explicitly present in NL-6 commission schedule"*,
and `ri_commission` succeeds at 6.67 from the same page — so the model read
the page correctly and reported an absence. `found=False` x9 is the right
answer, and there is nothing to fix.

**It will self-heal if Narayana starts filing the section.** Nothing in the
extraction path branches per company, the Gemini cache key hashes the
payload and the period, and `pdf_cache` invalidates on the PDF's mtime+size
— so a filing that adds the break-up produces a cache miss and a fresh call.

Unrelated but worth recording, since the old entry pointed at it:
`table_markdown.py` has **no caller and no test** anywhere in the repo. It
is not a path that could have been "wired in" to fix this; it is dead code.

**NL-41 FY25_Q3 is structurally unfillable from current inputs.** NL-41 is a
point-in-time snapshot with no prior-year comparative column; the current
filing's only backward-looking column is "beginning of the quarter", i.e.
prior *quarter*, not prior *year*. `GT_Data_Engine.xlsx`'s populated NL-41
prior-year values were keyed by an analyst from **last year's filing** — a
document this pipeline never downloads. This is a missing input document, not
a bug: `gemini.py`'s prompt and the NL-41 annotations are correct as written.
Recorded because this one already cost one investigation cycle after being
mistaken for pipeline output.

Re-confirmed on FY25-26 Q4 (Narayana, page 47): still a single `Number`
column, plus an Employees/Intermediaries movement table whose backward
column is again "beginning of the quarter". So this is a property of the
form in every quarter, not of one filing.

If it is ever built, the shape is: run the existing scraper against
`cfg.prior_fy(FY)` with the same quarter into `downloads/{prior_fy}/{Q}/`,
then source NL-41's prior-year fields from that second document.
`scraper.main()` is already FY-parameterised and `cfg.prior_fy()` /
`prior_period_*()` already exist, so the work is a second download set plus
an extraction path that reads two periods — a Phase 1 scope expansion, not
a bug fix. One partial shortcut worth knowing: in a **Q4** filing, row 1
("No. of offices at the beginning of the year") is the prior FY's closing
position, so the offices count alone is recoverable from the current
document. That does not extend to the employee or intermediary counts.

**NL-29 maturity-bucket swap — a working mechanism, not a gap.**
`data_engine.py:1341-1359`, inside `apply_company_gemini_pipeline`. Predates
every commit in this refactor and this session's other work - introduced in
`cc8003c`, well before `2b4ff61`. Not part of the Pydantic schema work and
not related to `schemas.py`'s retry-isolate mechanism, even though the two
compose (see below).

Some insurers' Detail Regarding Debt Securities schedule omits the "More than
7 years and upto 10 years" maturity row entirely (rather than printing "-")
when they have zero allocation there, instead of the fixed 5-bucket template
every other field assumes - and GT's own row-builder made the same
assumption, folding that allocation into the "Above 10 years" figure. The
shim's condition, read directly off the extraction's raw (pre-conversion)
dict:

```python
k_7_10 = "debt_maturity_More than 7 years and upto 10 years"
k_above10 = "debt_maturity_Above 10 years"
if not raw.get(k_7_10, {}).get("found") and raw.get(k_above10, {}).get("found"):
    raw_for_derivation[k_7_10] = raw[k_above10]
    raw_for_derivation[k_above10] = {"fy26_q3": None, "fy25_q3": None, "found": False, ...}
```

When the 7-10yr bucket is unresolved but Above-10yr is found, Above-10yr's
value is copied into the 7-10yr slot for derivation, and Above-10yr's own
slot is zeroed out - both changes applied to a copy (`raw_for_derivation`),
so `write_extraction_audit`'s audit trail still shows the model's original,
unswapped answer for both fields.

**Confirmed firing for real**, Narayana Health, FY25-26 Q3 live validation
run (run `851b622f1f26`, 2026-09-05): the audit shows `debt_maturity_Above
10 years` found=true (67.0/50.0, evidence quoting "above 10 years: ... 67%
... 50%") and `debt_maturity_More than 7 years and upto 10 years` found=false
("validation failed after retry"). The downloaded workbook's Slide 26 shows
"More than 7 years and upto 10 years" = 0.67/0.5 (Above-10yr's converted
value) and "Above 10 years" = None/None - re-verified against every Slide-26
row for all 7 companies, unfiltered, to rule out a row-misalignment reading
rather than a real swap.

This is the first confirmed case of the shim composing with `schemas.py`'s
retry-isolate mechanism: the shim only reads the raw dict's `found` flag,
indifferent to *why* a key ended up not-found (a genuinely missing row vs.
this refactor's validation-retry-then-isolate outcome), so the two combine
correctly without either needing to know about the other.
