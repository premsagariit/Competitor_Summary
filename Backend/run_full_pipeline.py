"""
End-to-end Phase 2 pipeline runner.

Assumes the source documents are already present locally (GIC.xlsx and the
7 insurers' Q3 FY26 disclosure PDFs under downloads/FY26/Q3/) - does NOT
call scraper.py / Phase 1 at all. Fills Data_Engine_UI.xlsx from scratch
(run this against a sheet with FY26_Q3/FY25_Q3/Growth already cleared),
then compares the result against GT_Data_Engine.xlsx.

Usage:
    python run_full_pipeline.py
"""
import time
from collections import defaultdict

import compare_to_gt
import gemini_extract
import phase2_extraction as p


def main():
    t_start = time.time()
    per_company = defaultdict(dict)  # company -> {"income_statement": s, "gemini": s}

    wb, ws = p.load_engine()

    # ---- Stage 1: GIC.xlsx-sourced rows (Slides 3-11, 14) ----
    t0 = time.time()
    gic = p.GicData()
    lookups = p.build_gic_lookups(gic)
    gic_updated, gic_skipped = p.apply_gic_rows(ws, lookups)
    t_gic = time.time() - t0
    print(f"[GIC.xlsx]        {gic_updated} rows written, {len(gic_skipped)} unmatched  -  {t_gic:.1f}s")

    companies = list(gemini_extract.COMPANY_PDFS.keys())

    # ---- Stage 2: per-company deterministic income statement (Slide 18) ----
    for company in companies:
        t0 = time.time()
        pdf_path = gemini_extract.COMPANY_PDFS[company]
        p.apply_income_statement_rows._cache[company] = p.extract_income_statement(company, pdf_path)
        per_company[company]["income_statement"] = time.time() - t0
    t0 = time.time()
    inc_updated, inc_skipped = p.apply_income_statement_rows(ws)
    t_income_write = time.time() - t0
    print(f"[Income Statement] {inc_updated} rows written, {len(inc_skipped)} unmatched  -  "
          f"extract: {sum(v['income_statement'] for v in per_company.values()):.1f}s, write: {t_income_write:.1f}s")

    # ---- Stage 3: Gemini-based metrics (Slides 12-35) ----
    gemini_written = 0
    for company in companies:
        t0 = time.time()
        written, log, raw = p.apply_company_gemini_pipeline(ws, company)
        per_company[company]["gemini"] = time.time() - t0
        gemini_written += written
        not_found = sum(1 for v in raw.values() if not v["found"])
        print(f"[Gemini]          {company}: wrote {written} cell-pairs, {not_found}/{len(raw)} not found  -  "
              f"{per_company[company]['gemini']:.1f}s")

    # ---- Stage 4: Slide 8/12 convention fixes (needs Stage 3's Slide 12 values already written) ----
    t0 = time.time()
    fix_written, fix_log = p.fix_slide8_and_slide12(ws)
    t_fix = time.time() - t0
    print(f"[Fix Slide 8/12]  {fix_written} cell-pairs written  -  {t_fix:.1f}s")

    wb.save(p.XLSX_PATH)
    t_total = time.time() - t_start
    print(f"\nSaved {p.XLSX_PATH}. Total pipeline time: {t_total:.1f}s")

    # ---- Per-company/per-document timing summary ----
    print("\n=== Per-company (PDF) timing, seconds ===")
    print(f"{'Company':<16}{'Income Stmt':<14}{'Gemini':<10}{'Total':<10}")
    for c in companies:
        t_inc = per_company[c].get("income_statement", 0.0)
        t_gem = per_company[c].get("gemini", 0.0)
        print(f"{c:<16}{t_inc:<14.1f}{t_gem:<10.1f}{t_inc + t_gem:<10.1f}")

    # ---- Compare against GT_Data_Engine.xlsx ----
    print("\n=== GT Comparison ===")
    compare_to_gt.main()


if __name__ == "__main__":
    main()
