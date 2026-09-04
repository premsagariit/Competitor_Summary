"""
Pipeline driver: Phase 1 (download) -> Phase 2 (extraction into a fresh Data
Engine file) -> Phase 3 (PDF report generation), for a caller-chosen
FY/Quarter.

The two halves are separately invokable so a caller can pause between them
and let a human fix up the source files (the Streamlit UI does exactly this:
it runs `--stage download`, shows which insurers came through and which
didn't, lets the user hand-upload the misses, then runs `--stage build`):

    competitor-analysis --fy FY25-26 --quarter Q3 --stage download
    competitor-analysis --fy FY25-26 --quarter Q3 --stage build
    competitor-analysis --fy FY25-26 --quarter Q3 --stage all

Phase 1 never blocks Phase 2: any insurer whose PDF isn't present is skipped
(not fatal) and the report is built from whichever companies/GIC data are
actually available. If truly nothing is found at all, the build fails with a
clear error instead of silently producing an empty report.
"""
import argparse
import asyncio
import os
import sys

from competitor_analysis import config as cfg


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fy", required=True, help="e.g. FY26")
    parser.add_argument("--quarter", required=True, help="Q1, Q2, Q3 or Q4")
    parser.add_argument("--stage", default="all", choices=["download", "build", "all"],
                         help="download: Phase 1 only, then report what landed on disk. "
                              "build: Phase 2 + 3 against whatever is already in "
                              "data/downloads/{FY}/{Quarter}/. all: both, back to back.")
    parser.add_argument("--download", choices=["yes", "no"],
                         help="Deprecated alias for --stage: 'yes' => all, 'no' => build.")
    parser.add_argument("--companies",
                         help="Comma-separated source_links.json keys to restrict retrieval "
                              "to (default: all configured sources).")
    return parser.parse_args()


def print_availability(fy: str, quarter: str) -> dict:
    """Logs (and returns) which source files are actually on disk. Purely a
    filesystem check, so it reflects hand-supplied files too."""
    avail = cfg.source_availability(fy, quarter)
    print(f"--- Source availability for {fy} {quarter} ({avail['download_dir']}) ---")
    print(f"GIC.xlsx: {'found' if avail['gic_found'] else 'NOT FOUND - Slides 3-11/14 will be skipped'}")
    for c in sorted(avail["companies_found"]):
        print(f"  {c}: found")
    for c in sorted(avail["companies_missing"]):
        print(f"  {c}: NOT FOUND - will be skipped")
    print()
    return avail


def run_download(fy: str, quarter: str, companies: list[str] | None = None) -> dict:
    """Phase 1 only. Returns the resulting source-availability dict.

    Deliberately does NOT import the period-dependent modules (pdf_cache and
    everything downstream of it bake in which PDFs exist at import time) -
    that's the build stage's job, once the files have settled.

    `companies`, if given, restricts retrieval to that subset of
    source_links.json keys; other configured sources are left untouched.
    """
    cfg.set_period(fy, quarter)
    fy, quarter = cfg.FY, cfg.QUARTER

    from competitor_analysis.ingestion import scraper

    print(f"=== Phase 1: downloading source files for {fy} {quarter} ===")
    print(f"Target directory: {cfg.download_dir()}\n")
    asyncio.run(scraper.main(fy, quarter, companies=companies))
    print()

    avail = print_availability(fy, quarter)
    n_found, n_total = len(avail["companies_found"]), len(cfg.COMPANY_PDF_FILENAMES)
    print(f"=== Phase 1 done: {n_found}/{n_total} insurer PDFs, "
          f"GIC.xlsx {'present' if avail['gic_found'] else 'missing'} ===")
    return avail


def run_build(fy: str, quarter: str) -> dict:
    """Phase 2 + Phase 3 against whatever source files are on disk now.
    Returns a summary dict. Raises RuntimeError if there's nothing to build
    from at all."""
    cfg.set_period(fy, quarter)
    fy, quarter = cfg.FY, cfg.QUARTER

    # Deferred imports: these modules compute FY/Quarter-derived path
    # constants (including which insurer PDFs exist on disk) at import time,
    # so they must only be imported *after* cfg.set_period() has run and
    # after any downloading/uploading has actually placed the files -
    # otherwise pdf_cache.COMPANY_PDFS (and everything that imports it:
    # pdf_extract, gemini_extract, data_engine) would be computed
    # against an empty/stale downloads folder.
    from competitor_analysis.extraction import pdf_cache
    from competitor_analysis.extraction import data_engine as p
    from competitor_analysis import pipeline as run_full_pipeline
    from competitor_analysis.reporting import report as pdf_report

    print(f"=== Build run: {fy} {quarter} ===\n")

    companies_found = sorted(pdf_cache.COMPANY_PDFS.keys())
    companies_missing = sorted(set(cfg.COMPANY_PDF_FILENAMES) - set(pdf_cache.COMPANY_PDFS))
    gic_available = os.path.exists(cfg.gic_path())
    print_availability(fy, quarter)

    if not companies_found and not gic_available:
        raise RuntimeError(
            f"No source files found under {cfg.download_dir()} (no GIC.xlsx, no insurer PDFs). "
            f"Re-run Phase 1, or place the files there manually."
        )

    engine_path = cfg.data_engine_output_path(fy, quarter)
    print(f"--- Phase 2: extraction into a fresh Data Engine file ({engine_path}) ---")
    wb, ws = p.load_engine(path=cfg.DATA_ENGINE_TEMPLATE)
    cleared = p.clear_period_values(ws)
    print(f"Cleared {cleared} stale value cells from the template.\n")
    run_full_pipeline.run_phase2(ws, companies=companies_found, run_gic=gic_available)
    os.makedirs(os.path.dirname(engine_path), exist_ok=True)
    wb.save(engine_path)
    print(f"Saved {engine_path}.\n")

    print("--- Phase 3: report generation ---")
    out_path = pdf_report.build(cfg.output_pdf_path(), data_engine_path=engine_path)
    print()

    summary = {
        "output_path": out_path,
        "data_engine_path": engine_path,
        "companies_included": companies_found,
        "companies_skipped": companies_missing,
        "gic_included": gic_available,
    }
    print(f"=== Done: {out_path} ===")
    return summary


def run(fy: str, quarter: str, download: bool, companies: list[str] | None = None) -> dict:
    """Phase 1 (optional) then Phase 2 + 3, in one process."""
    if download:
        run_download(fy, quarter, companies=companies)
    return run_build(fy, quarter)


def main():
    args = parse_args()
    stage = args.stage
    if args.download is not None:
        stage = "all" if args.download == "yes" else "build"
    companies = [c.strip() for c in args.companies.split(",")] if args.companies else None
    try:
        if stage == "download":
            run_download(args.fy, args.quarter, companies=companies)
        elif stage == "build":
            run_build(args.fy, args.quarter)
        else:
            run(args.fy, args.quarter, download=True, companies=companies)
    except (ValueError, RuntimeError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
