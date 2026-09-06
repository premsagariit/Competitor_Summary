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

from competitor_analysis.extraction import gemini as gemini_extract
from competitor_analysis.extraction import data_engine as p


def run_phase2(ws, companies=None, run_gic=True):
    """Runs Phase 2's 4 stages against an already-loaded worksheet. Does NOT
    load/save the workbook - callers own that."""
    t_start = time.time()
    per_company = defaultdict(dict)  # company -> {"income_statement": s, "gemini": s}

    # ---- Stage 0: label the value columns for the period being run ----
    relabelled = p.sync_period_headers(ws)
    for old, new in relabelled:
        print(f"[Headers]         column header {old!r} -> {new!r}")
    if not relabelled:
        print(f"[Headers]         already labelled {p.CUR_PERIOD} / {p.PRIOR_PERIOD}.")

    # ---- Stage 1: GIC.xlsx-sourced rows (Slides 3-11, 14) ----
    if run_gic:
        t0 = time.time()
        gic = p.GicData()
        lookups = p.build_gic_lookups(gic)
        gic_updated, gic_skipped = p.apply_gic_rows(ws, lookups)
        t_gic = time.time() - t0
        print(f"[GIC.xlsx]        {gic_updated} rows written, {len(gic_skipped)} unmatched  -  {t_gic:.1f}s")
    else:
        print("[GIC.xlsx]        skipped - not found for this period.")

    if companies is None:
        companies = list(gemini_extract.COMPANY_PDFS.keys())

    # ---- Stage 2: per-company deterministic income statement (Slide 18) ----
    # Extracted for all companies concurrently (independent, PDF-bound work);
    # the sheet writes below stay sequential.
    t0 = time.time()
    print(f"[Income Statement] extracting {len(companies)} companies concurrently ...")
    mem_skipped = p.prefetch_income_statements(companies)
    t_inc_extract = time.time() - t0
    if mem_skipped:
        # Not fatal by design: the container's memory budget was reached
        # while parsing these filings, so they were abandoned to keep the
        # process alive. Say so loudly - their rows will read as
        # not-found, which otherwise looks like missing source data.
        print(f"[Memory]          {len(mem_skipped)} filing(s) abandoned to stay within the "
              f"memory budget: {', '.join(sorted(mem_skipped))}")
        for company, reason in sorted(mem_skipped.items()):
            print(f"[Memory]            {company}: {reason}")
    for company in companies:
        per_company[company]["income_statement"] = t_inc_extract / max(len(companies), 1)
    print(f"[Income Statement] extraction done  -  {t_inc_extract:.1f}s total")
    t0 = time.time()
    inc_updated, inc_skipped = p.apply_income_statement_rows(ws)
    t_income_write = time.time() - t0
    print(f"[Income Statement] {inc_updated} rows written, {len(inc_skipped)} unmatched  -  "
          f"extract: {sum(v['income_statement'] for v in per_company.values()):.1f}s, write: {t_income_write:.1f}s")

    # ---- Stage 3: Gemini-based metrics (Slides 12-35) ----
    # All companies' model calls are issued up front under one shared
    # concurrency gate; the per-company loop below then only converts and
    # writes, which is fast and must stay serial (openpyxl isn't thread-safe).
    t0 = time.time()
    p.prefetch_gemini_metrics(companies)
    t_fetch = time.time() - t0
    hits, misses = gemini_extract.CACHE_STATS["hit"], gemini_extract.CACHE_STATS["miss"]
    print(f"[Gemini]          extraction done  -  {t_fetch:.1f}s total "
          f"({hits} cached, {misses} live call{'s' if misses != 1 else ''})")

    for company in companies:
        t0 = time.time()
        written, log, raw = p.apply_company_gemini_pipeline(ws, company)
        per_company[company]["gemini"] = time.time() - t0
        not_found = sum(1 for v in raw.values() if not v["found"])
        print(f"[Gemini]          {company}: wrote {written} cell-pairs, {not_found}/{len(raw)} not found  -  "
              f"{per_company[company]['gemini']:.1f}s")

    # ---- Stage 4: Slide 8/12 convention fixes (needs Stage 3's Slide 12 values already written) ----
    t0 = time.time()
    fix_written, fix_log = p.fix_slide8_and_slide12(ws)
    t_fix = time.time() - t0
    print(f"[Fix Slide 8/12]  {fix_written} cell-pairs written  -  {t_fix:.1f}s")

    # ---- Stage 5: post-run invariants ----
    # Period-agnostic self-check: it references no date and no expected
    # figure, so it keeps catching a dropped/double-counted channel bucket in
    # future quarters.
    failures = p.assert_channel_mix_sums(ws)
    if failures:
        print("\n[INVARIANT FAILED] Slide 12 channel shares must sum to 1.0:")
        for company, period, total, n in failures:
            print(f"   - {company} {period}: sum={total} across {n} buckets")
    else:
        print("[Invariant]       Slide 12 channel mix sums to 1.0 for every "
              "company, both periods.")

    # Any column/period that could not be resolved from a form's own header
    # is reported explicitly rather than passing silently.
    if p.RESOLUTION_LOG:
        print(f"\n=== Column-resolution fallbacks ({len(p.RESOLUTION_LOG)}) ===")
        for msg in dict.fromkeys(p.RESOLUTION_LOG):
            print(f"   * {msg}")

    t_total = time.time() - t_start
    print(f"\nPhase 2 pipeline time: {t_total:.1f}s")

    # ---- Per-company/per-document timing summary ----
    print("\n=== Per-company (PDF) timing, seconds ===")
    print(f"{'Company':<16}{'Income Stmt':<14}{'Gemini':<10}{'Total':<10}")
    for c in companies:
        t_inc = per_company[c].get("income_statement", 0.0)
        t_gem = per_company[c].get("gemini", 0.0)
        print(f"{c:<16}{t_inc:<14.1f}{t_gem:<10.1f}{t_inc + t_gem:<10.1f}")

    return per_company


def main():
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
        print("Gemini response cache disabled for this run.")
    if args.concurrency:
        gemini_extract.MAX_CONCURRENT_GEMINI = args.concurrency

    wb, ws = p.load_engine()
    run_phase2(ws)
    wb.save(p.XLSX_PATH)
    print(f"\nSaved {p.XLSX_PATH}.")


if __name__ == "__main__":
    main()
