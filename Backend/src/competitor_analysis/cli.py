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
from competitor_analysis import logging_setup
from competitor_analysis.logging_setup import phase

# Named explicitly, not __name__: run as `python -m competitor_analysis.cli`
# this module is "__main__", outside the "competitor_analysis" logger tree
# that logging_setup.configure() attaches its handler to - so every INFO line
# from here was silently dropped (only WARNING+ reached stderr).
log = logging_setup.get_logger("competitor_analysis.cli")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fy", required=True, help="e.g. FY26")
    parser.add_argument("--quarter", required=True, help="Q1, Q2, Q3 or Q4")
    parser.add_argument("--stage", default="all",
                         choices=["download", "build", "all", "report", "append-history"],
                         help="download: Phase 1 only, then report what landed on disk. "
                              "build: Phase 2 + 3 against whatever is already in "
                              "data/downloads/{FY}/{Quarter}/. all: both, back to back. "
                              "report: Phase 3 only, from an existing Data Engine workbook. "
                              "append-history: add the year from a reviewed Q4 Data Engine to "
                              "data/historical/Historical_Trends.xlsx - append-only, Q4 only.")
    parser.add_argument("--data-engine",
                         help="--stage report / append-history: the Data Engine .xlsx to use (default: "
                              "the latest artifacts/output/Data_Engine_{FY}_{Quarter}_*.xlsx).")
    parser.add_argument("--dry-run", action="store_true",
                         help="--stage append-history only: list what would be written, change nothing.")
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
    log.info("Source availability for %s %s (%s)", fy, quarter, avail["download_dir"])
    if avail["gic_found"]:
        log.info("GIC.xlsx: found")
    else:
        log.warning("GIC.xlsx: NOT FOUND - Slides 3-11/14 will be skipped")
    for c in sorted(avail["companies_found"]):
        log.info("%s: found", c)
    for c in sorted(avail["companies_missing"]):
        log.warning("%s: NOT FOUND - will be skipped", c)
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

    with phase("Phase 1"):
        log.info("Downloading source files for %s %s", fy, quarter)
        log.info("Target directory: %s", cfg.download_dir())
        asyncio.run(scraper.main(fy, quarter, companies=companies))

        avail = print_availability(fy, quarter)
        n_found, n_total = len(avail["companies_found"]), len(cfg.COMPANY_PDF_FILENAMES)
        log.info("Phase 1 done: %d/%d insurer PDFs, GIC.xlsx %s",
                  n_found, n_total, "present" if avail["gic_found"] else "missing")
    return avail


def run_build(fy: str, quarter: str) -> dict:
    """Phase 2 + Phase 3 against whatever source files are on disk now.
    Returns a summary dict. Raises RuntimeError if there's nothing to build
    from at all."""
    cfg.set_period(fy, quarter)
    fy, quarter = cfg.FY, cfg.QUARTER

    # Deferred imports. These modules' period-derived state now follows
    # cfg.set_period() via its listener hook rather than freezing at import,
    # so the ordering is no longer load-bearing for correctness - it is kept
    # because importing them with no period configured still raises.
    from competitor_analysis.extraction import pdf_cache
    from competitor_analysis.extraction import data_engine as p
    from competitor_analysis import pipeline as run_full_pipeline
    from competitor_analysis.reporting import report as pdf_report

    log.info("Build run: %s %s", fy, quarter)

    companies_found = sorted(pdf_cache.COMPANY_PDFS.keys())
    companies_missing = sorted(set(cfg.COMPANY_PDF_FILENAMES) - set(pdf_cache.COMPANY_PDFS))
    gic_available = os.path.exists(cfg.gic_path())

    engine_path = cfg.data_engine_output_path(fy, quarter)
    with phase("Phase 2"):
        print_availability(fy, quarter)

        if not companies_found and not gic_available:
            raise RuntimeError(
                f"No source files found under {cfg.download_dir()} (no GIC.xlsx, no insurer PDFs). "
                f"Re-run Phase 1, or place the files there manually."
            )

        log.info("Extraction into a fresh Data Engine file (%s)", engine_path)
        wb, ws = p.load_engine(path=cfg.DATA_ENGINE_TEMPLATE)
        cleared = p.clear_period_values(ws)
        log.info("Cleared %d stale value cells from the template.", cleared)
        run_full_pipeline.run_phase2(ws, companies=companies_found, run_gic=gic_available)
        os.makedirs(os.path.dirname(engine_path), exist_ok=True)
        wb.save(engine_path)
        log.info("Saved %s.", engine_path)

    with phase("Phase 3"):
        log.info("Report generation")
        out_path = pdf_report.build(cfg.output_pdf_path(), data_engine_path=engine_path)

    summary = {
        "output_path": out_path,
        "data_engine_path": engine_path,
        "companies_included": companies_found,
        "companies_skipped": companies_missing,
        "gic_included": gic_available,
    }
    log.info("Done: %s", out_path)
    return summary


def _resolve_data_engine(fy: str, quarter: str, data_engine_path: str | None) -> str:
    """The given Data Engine path, or the newest saved one for the period."""
    import glob

    from competitor_analysis import paths

    if data_engine_path is None:
        # Timestamped names (YYYYMMDD_HHMMSS) sort chronologically.
        matches = sorted(glob.glob(str(paths.OUTPUT_DIR / f"Data_Engine_{fy}_{quarter}_*.xlsx")))
        if not matches:
            raise RuntimeError(f"No Data Engine workbook for {fy} {quarter} under {paths.OUTPUT_DIR} - "
                               f"pass --data-engine PATH, or run --stage build first.")
        return matches[-1]
    if not os.path.isfile(data_engine_path):
        raise RuntimeError(f"Data Engine workbook not found: {data_engine_path}")
    return data_engine_path


def run_append_history(fy: str, quarter: str, data_engine_path: str | None = None,
                       dry_run: bool = False) -> dict:
    """Adds the year a reviewed Q4 Data Engine completes to
    data/historical/Historical_Trends.xlsx - append-only (see
    historical.append_year_from_data_engine), then uploads the workbook to
    S3 (a no-op without S3 credentials)."""
    cfg.set_period(fy, quarter)
    fy, quarter = cfg.FY, cfg.QUARTER
    if quarter != "Q4":
        raise ValueError(f"append-history needs a Q4 Data Engine (a completed year) - got {fy} {quarter}.")
    data_engine_path = _resolve_data_engine(fy, quarter, data_engine_path)

    from dotenv import load_dotenv
    load_dotenv()  # before storage.s3 reads its credentials at import
    from competitor_analysis import paths
    from competitor_analysis.reporting import historical
    from competitor_analysis.storage import s3

    with phase("Historical append"):
        log.info("%s from %s%s", "Dry run" if dry_run else "Appending", data_engine_path,
                 "" if dry_run else f" into {paths.HISTORICAL_TRENDS_WORKBOOK}")
        result = historical.append_year_from_data_engine(data_engine_path, dry_run=dry_run, log=log.info)
        for title, reason in result["skipped"].items():
            log.warning("%s: skipped - %s", title, reason)
        log.info("%s: %d table(s), %d cell(s) %s", result["year"], len(result["added"]), result["cells"],
                 "would be written (dry run)" if dry_run else "written")
        if result["backup"]:
            log.info("Backup of the previous workbook: %s", result["backup"])
            s3.upload_file(paths.HISTORICAL_TRENDS_WORKBOOK)
    return result


def run_report(fy: str, quarter: str, data_engine_path: str | None = None) -> str:
    """Phase 3 only: renders the PDF from an already-filled Data Engine
    workbook - no downloads, no PDF parsing, no Gemini calls. Defaults to the
    newest saved workbook for the period. Returns the PDF path."""
    cfg.set_period(fy, quarter)
    fy, quarter = cfg.FY, cfg.QUARTER
    data_engine_path = _resolve_data_engine(fy, quarter, data_engine_path)

    from competitor_analysis.reporting import report as pdf_report

    with phase("Phase 3"):
        log.info("Report generation from %s", data_engine_path)
        out_path = pdf_report.build(cfg.output_pdf_path(), data_engine_path=data_engine_path)
    log.info("Done: %s", out_path)
    return out_path


def run(fy: str, quarter: str, download: bool, companies: list[str] | None = None) -> dict:
    """Phase 1 (optional) then Phase 2 + 3, in one process."""
    if download:
        run_download(fy, quarter, companies=companies)
    return run_build(fy, quarter)


def main():
    logging_setup.configure()
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
        elif stage == "report":
            run_report(args.fy, args.quarter, data_engine_path=args.data_engine)
        elif stage == "append-history":
            run_append_history(args.fy, args.quarter, data_engine_path=args.data_engine, dry_run=args.dry_run)
        else:
            run(args.fy, args.quarter, download=True, companies=companies)
    except (ValueError, RuntimeError) as e:
        log.critical("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
