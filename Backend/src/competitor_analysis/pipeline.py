"""
End-to-end Phase 2 pipeline runner.

Assumes the source documents are already present locally (GIC.xlsx and the
7 insurers' disclosure PDFs under downloads/{FY}/{Quarter}/) - does NOT call
scraper.py / Phase 1 at all. Fills Data_Engine_UI.xlsx from scratch (run this
against a sheet with FY26_Q3/FY25_Q3/Growth already cleared), then compares
Fills the Data Engine workbook from the sources on disk.

run_phase2() is the reusable core (stages 1-4, no load/save/GT-compare) -
cli.py calls it directly with an existence-filtered company
list and run_gic= based on whether GIC.xlsx was found for the target period.
This script's main() is the standalone dev/calibration entrypoint, defaulting
to the FY26 Q3 period this pipeline was calibrated against.

Usage:
    python -m competitor_analysis.cli --fy FY26 --quarter Q3 --stage build
"""
import time
from collections import defaultdict

from competitor_analysis import config as cfg

if cfg.FY is None:
    cfg.set_period("FY25-26", "Q3")

from competitor_analysis import logging_setup
from competitor_analysis.logging_setup import phase
from competitor_analysis.extraction import gemini as gemini_extract
from competitor_analysis.extraction import data_engine as p

log = logging_setup.get_logger(__name__)


def run_phase2(ws, companies=None, run_gic=True, on_progress=None, should_cancel=None):
    """Runs Phase 2's 4 stages against an already-loaded worksheet. Does NOT
    load/save the workbook - callers own that.

    `on_progress(key, percent, status)`, if given, is called as each unit of
    per-company work lands, so a caller driving a UI can show real movement
    instead of everything jumping to 100% at the end. `key` is a company key
    or "GIC". The three milestones are weighted by how long each actually
    takes: the PDF parse dominates, so it carries most of the bar.

    `should_cancel()`, if given, is checked between companies in every stage
    that has one (the PDF-parse and Gemini prefetches also check it
    themselves, between whichever of their concurrent units finish first) -
    raises PipelineCancelled the moment it returns True, so a run being
    stopped doesn't have to wait for every remaining company to finish."""
    from competitor_analysis.cancellation import PipelineCancelled

    def _check_cancel():
        if should_cancel is not None and should_cancel():
            raise PipelineCancelled()

    t_start = time.time()
    _check_cancel()

    def progress(key, percent, status=None):
        if on_progress is not None:
            on_progress(key, percent, status)
    per_company = defaultdict(dict)  # company -> {"income_statement": s, "gemini": s}

    # ---- Stage -1: reset process-lifetime state from any earlier run ----
    # apply_income_statement_rows._cache and apply_segment_income_statement_rows
    # ._cache are company-keyed memoization dicts, module-level so Stage 2's
    # write and Stage 3's derived-metrics read can share one PDF parse within
    # THIS run without re-parsing - but with no period in the key, a
    # long-lived process (the API server) that has already extracted a
    # company for one period leaves that period's numbers cached forever,
    # silently reused - under the new period's label - for every later run
    # of a DIFFERENT period. A fresh CLI process never shows this (the module
    # re-imports every invocation), which is exactly why it only ever
    # surfaced on the deployed server. Clearing here keeps the intra-run
    # memoization intact while making it impossible for it to outlive the
    # run it was built for.
    p.apply_income_statement_rows._cache.clear()
    p.apply_segment_income_statement_rows._cache.clear()
    # NL-36 (Slide 12's channel mix) has the same company-only key: missing
    # it here made the server's FY26-27 Q1 run draw every company's FY25-26
    # Q1 channel mix, left over from the FY25-26 Q1 run before it.
    p._NL36_CACHE.clear()
    # Prefetched Gemini answers are popped as each company is written, but a
    # run cancelled or failed between prefetch and write leaves the rest
    # behind - which the next run (any period) would pop and write as its own.
    p.apply_company_gemini_pipeline._raw_cache.clear()
    # Same class of staleness, lower stakes (nothing reads it back into the
    # workbook, but a long-lived process would otherwise print every earlier
    # run's column-resolution fallbacks mixed into this run's own).
    p.RESOLUTION_LOG.clear()

    # ---- Stage 0: label the value columns for the period being run ----
    with phase("Phase 2 / Headers"):
        relabelled = p.sync_period_headers(ws)
        for old, new in relabelled:
            log.info("column header %r -> %r", old, new)
        if not relabelled:
            log.info("already labelled %s / %s.", p.CUR_PERIOD, p.PRIOR_PERIOD)

    # ---- Stage 1: GIC.xlsx-sourced rows (Slides 3-11, 14) ----
    # `gic` is kept around past this block (not just used here): Stage 2's
    # extract_income_statement uses it as GWP's authoritative GDPI base
    # (set_gic_gdpi_lookup below), and Stage 4 uses it for Slide 8.
    gic = None
    with phase("Phase 2 / GIC"):
        if run_gic:
            t0 = time.time()
            gic = p.GicData()
            lookups = p.build_gic_lookups(gic)
            gic_updated, gic_skipped = p.apply_gic_rows(ws, lookups)
            t_gic = time.time() - t0
            log.info("%d rows written, %d unmatched  -  %.1fs", gic_updated, len(gic_skipped), t_gic)
            progress("GIC", 100, "done")
        else:
            log.warning("skipped - not found for this period.")
            progress("GIC", 0, "skipped")
    p.set_gic_gdpi_lookup(p.gic_gdpi_lookup(gic))

    if companies is None:
        companies = list(gemini_extract.COMPANY_PDFS.keys())

    # ---- Stage 2: per-company deterministic income statement (Slide 18) ----
    # Extracted for all companies concurrently (independent, PDF-bound work);
    # the sheet writes below stay sequential.
    with phase("Phase 2 / Income Statement"):
        t0 = time.time()
        log.info("extracting %d companies concurrently ...", len(companies))
        mem_skipped = p.prefetch_income_statements(
            companies, on_company_done=lambda c: progress(c, 60, "extracting"),
            should_cancel=should_cancel)
        t_inc_extract = time.time() - t0
        if mem_skipped:
            # Not fatal by design: the container's memory budget was reached
            # while parsing these filings, so they were abandoned to keep the
            # process alive. Say so loudly - their rows will read as
            # not-found, which otherwise looks like missing source data.
            log.warning("%d filing(s) abandoned to stay within the memory budget: %s",
                        len(mem_skipped), ", ".join(sorted(mem_skipped)))
            for company, reason in sorted(mem_skipped.items()):
                log.warning("  %s: %s", company, reason)
        for company in companies:
            per_company[company]["income_statement"] = t_inc_extract / max(len(companies), 1)
        log.info("extraction done  -  %.1fs total", t_inc_extract)
        t0 = time.time()
        inc_updated, inc_skipped = p.apply_income_statement_rows(ws)
        t_income_write = time.time() - t0
        log.info("%d rows written, %d unmatched  -  extract: %.1fs, write: %.1fs",
                  inc_updated, len(inc_skipped),
                  sum(v["income_statement"] for v in per_company.values()), t_income_write)

    # ---- Stage 2b: per-company, per-segment income statement (Slides 19-21) ----
    # Reuses the same on-disk PDF-JSON cache Stage 2 already warmed (both go
    # through forms.get_form_page -> pdf_cache.get_company_json), so this is
    # just re-scanning already-parsed tables, not re-parsing PDFs.
    with phase("Phase 2 / Segment P&L"):
        t0 = time.time()
        seg_total_updated, seg_total_skipped = 0, 0
        for slide_no, segment in ((19, "Health"), (20, "Personal Accident"), (21, "Travel")):
            seg_updated, seg_skipped = p.apply_segment_income_statement_rows(ws, slide_no, segment)
            seg_total_updated += seg_updated
            seg_total_skipped += len(seg_skipped)
        t_segment_write = time.time() - t0
        log.info("%d rows written, %d unmatched  -  write: %.1fs",
                  seg_total_updated, seg_total_skipped, t_segment_write)

    # ---- Stage 3: Gemini-based metrics (Slides 12-35) ----
    # All companies' model calls are issued up front under one shared
    # concurrency gate; the per-company loop below then only converts and
    # writes, which is fast and must stay serial (openpyxl isn't thread-safe).
    with phase("Phase 2 / Gemini"):
        t0 = time.time()
        p.prefetch_gemini_metrics(
            companies, on_company_done=lambda c: progress(c, 85, "extracting"),
            should_cancel=should_cancel)
        t_fetch = time.time() - t0
        hits, misses = gemini_extract.CACHE_STATS["hit"], gemini_extract.CACHE_STATS["miss"]
        log.info("extraction done  -  %.1fs total (%d cached, %d live call%s)",
                  t_fetch, hits, misses, "s" if misses != 1 else "")

        failed = {}
        for company in companies:
            _check_cancel()
            t0 = time.time()
            try:
                written, apply_log, raw = p.apply_company_gemini_pipeline(ws, company)
            except Exception as e:
                # One company must not sink the run. The prefetch above already
                # isolates per company, but this loop re-fetches any company the
                # prefetch got nothing for - and that call was unprotected, so a
                # single transient 503 on the last company discarded a completed
                # 21-minute run. Its rows stay unfilled, which is the honest
                # outcome, and the run finishes with the other six intact.
                per_company[company]["gemini"] = time.time() - t0
                failed[company] = f"{type(e).__name__}: {e}"
                progress(company, 100, "failed")
                log.error("%s: extraction failed, leaving its rows unfilled - %s", company, e)
                continue
            per_company[company]["gemini"] = time.time() - t0
            progress(company, 100, "done")
            not_found = sum(1 for v in raw.values() if not v["found"])
            log.info("%s: wrote %d cell-pairs, %d/%d not found  -  %.1fs",
                      company, written, not_found, len(raw), per_company[company]["gemini"])

        if failed:
            log.error("%d of %d companies failed extraction entirely: %s",
                      len(failed), len(companies), ", ".join(sorted(failed)))

    # ---- Stage 4: Slide 8/12 convention fixes (needs Stage 3's Slide 12 values already written) ----
    with phase("Phase 2 / Fix Slide 8-12"):
        t0 = time.time()
        fix_written, fix_log = p.fix_slide8_and_slide12(ws, gic)
        t_fix = time.time() - t0
        log.info("%d cell-pairs written  -  %.1fs", fix_written, t_fix)

    # ---- Stage 5: post-run invariants ----
    # Period-agnostic self-check: it references no date and no expected
    # figure, so it keeps catching a dropped/double-counted channel bucket in
    # future quarters.
    with phase("Phase 2 / Invariants"):
        failures = p.assert_channel_mix_sums(ws)
        if failures:
            log.error("Slide 12 channel shares must sum to 1.0:")
            for company, period, total, n in failures:
                log.error("  %s %s: sum=%s across %d buckets", company, period, total, n)
        else:
            log.info("Slide 12 channel mix sums to 1.0 for every company, both periods.")

        # Any column/period that could not be resolved from a form's own header
        # is reported explicitly rather than passing silently.
        if p.RESOLUTION_LOG:
            log.warning("Column-resolution fallbacks (%d):", len(p.RESOLUTION_LOG))
            for msg in dict.fromkeys(p.RESOLUTION_LOG):
                log.warning("  %s", msg)

    # ---- Stage 6: backfill prior-period gaps from last year's own filing ----
    # Slides 16/17 (zone/state mix) and 23/25 (claims settlement schedules,
    # offices/employee counts) can have a real PRIOR-period gap even in a
    # clean run - some source schedules never carry a prior-year comparative
    # at all. Last year's own run captured that same quarter's number back
    # when it was "current" - reuse it here instead of leaving those charts
    # current-period-only forever. No-ops (and says so) until a same-quarter
    # run from last year actually exists on disk.
    with phase("Phase 2 / Prior Backfill"):
        t0 = time.time()
        backfill_written, backfill_log = p.backfill_prior_from_last_year(ws)
        t_backfill = time.time() - t0
        if backfill_written:
            log.info("%d prior-period cell(s) filled from last year's own filing (%s %s)  -  %.1fs",
                      backfill_written, cfg.prior_fy(), cfg.QUARTER, t_backfill)
        else:
            log.info("no %s %s Data Engine archive found on disk - skipped.",
                      cfg.prior_fy(), cfg.QUARTER)

    t_total = time.time() - t_start
    log.info("Phase 2 pipeline time: %.1fs", t_total)

    # ---- Per-company/per-document timing summary ----
    summary_lines = [f"{'Company':<16}{'Income Stmt':<14}{'Gemini':<10}{'Total':<10}"]
    for c in companies:
        t_inc = per_company[c].get("income_statement", 0.0)
        t_gem = per_company[c].get("gemini", 0.0)
        summary_lines.append(f"{c:<16}{t_inc:<14.1f}{t_gem:<10.1f}{t_inc + t_gem:<10.1f}")
    log.info("Per-company (PDF) timing, seconds:\n%s", "\n".join(summary_lines))

    return per_company


def main():
    logging_setup.configure()
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the on-disk Gemini response cache and re-issue "
                         "every model call (use after changing a metric prompt)")
    ap.add_argument("--concurrency", type=int, default=None,
                    help=f"max in-flight Gemini calls "
                         f"(default {gemini_extract.MAX_CONCURRENT_GEMINI})")
    args = ap.parse_args()
    if args.no_cache:
        gemini_extract.USE_GEMINI_CACHE = False
        log.info("Gemini response cache disabled for this run.")
    if args.concurrency:
        gemini_extract.MAX_CONCURRENT_GEMINI = args.concurrency

    wb, ws = p.load_engine()
    run_phase2(ws)
    wb.save(p.XLSX_PATH)
    log.info("Saved %s.", p.XLSX_PATH)


if __name__ == "__main__":
    main()
