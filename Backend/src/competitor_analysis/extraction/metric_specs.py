"""
Consolidated metric-specification registry for the Data Engine's company-level
slides (12-35).

Two kinds of spec, both describing where a Data Engine cell's value comes
from and how, for auditability:

  - "llm" specs (`llm_specs()`): gemini_extract.master_metric_specs(),
    extended here with `source`/`companies` metadata. Each already defines
    the required NL form(s), an extraction instruction (`description`), an
    expected unit (`kind`), and the sheet row(s) it targets (`rows`). This
    wrapper adds fields without changing extraction behavior - it's the
    metric_specs.llm_specs() call sites should use, not a re-implementation.

  - "derived" specs (`DERIVED_METRIC_SPECS`): a purely-documentary registry
    for the ~20 Python-computed ratios/derivations in
    data_engine.compute_derived_metrics(). The formula bodies
    themselves are NOT reproduced or re-executed here - each one has a
    comment in compute_derived_metrics documenting an empirically
    GT-verified convention (a unit quirk, a "ratio-labeled-but-actually-
    absolute" case, etc.), and re-deriving them as declarative rules would
    risk that calibration for no accuracy gain. This registry exists so
    every filled cell's provenance - raw LLM extraction vs Python-derived,
    and which formula/inputs - is inspectable in one place, per requirement
    that every value be traceable back to its source.
"""
from competitor_analysis.extraction import gemini as gemini_extract
from competitor_analysis import paths


def llm_specs():
    """gemini_extract.master_metric_specs(), with source/companies metadata
    added. Purely additive - does not change `key`/`forms`/`kind`/`rows`
    semantics, so every existing `rows` mapping keeps working unchanged."""
    specs = gemini_extract.master_metric_specs()
    for m in specs:
        m.setdefault("source", "llm")
        m.setdefault("companies", "all")
    return specs


DERIVED_METRIC_SPECS = [
    # --- Slide 21: expense ratios to GWP ---
    {"key": "opex_to_gwp_ratio", "slide": 21, "metric1": "Opex. To GWP ratio", "metric2": None,
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: opex_alone / gwp",
     "inputs": ["opex_alone (NL-7, llm key='it_spend' sibling, Operating Expenses alone)", "gwp (NL-4, deterministic income statement)"],
     "notes": "'Opex' here means NL-7 Operating Expenses ALONE, not commission+opex combined "
              "(Slide 30 uses the combined figure) - GT-verified."},
    {"key": "manpower_to_gwp_ratio", "slide": 21, "metric1": "Manpower to GWP ratio", "metric2": None,
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: manpower_cost / gwp",
     "inputs": ["manpower_cost (NL-7, llm)", "gwp (deterministic income statement)"]},
    {"key": "it_spend_to_gwp_ratio", "slide": 21, "metric1": "IT spend to GWP ratio", "metric2": None,
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: it_spend / gwp",
     "inputs": ["it_spend (NL-7, llm)", "gwp (deterministic income statement)"]},

    # --- Slide 22: manpower/facility metrics (Rs. Lakhs) ---
    {"key": "manpower_to_opex", "slide": 22, "metric1": "Manpower cost to total Opex", "metric2": None,
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: manpower_cost / opex_alone",
     "inputs": ["manpower_cost (NL-7, llm)", "opex_alone (NL-7, llm)"]},
    {"key": "manpower_per_employee", "slide": 22, "metric1": "Manpower cost per employee", "metric2": None,
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: manpower_cur * 100 / employees_cur (Rs. Lakhs -> Rs.)",
     "inputs": ["manpower_cost (NL-7, llm)", "employees_onroll (NL-41, llm, current period only)"],
     "notes": "Current-period only - NL-41 has no prior-year comparative."},
    {"key": "facility_rent_per_office_month", "slide": 22, "metric1": "Facility rental per office per month", "metric2": None,
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: rent_cur * 100 / cfg.months_elapsed() / offices_cur",
     "inputs": ["rent_expense (NL-7, llm, cumulative YTD)", "offices_count (NL-41, llm, current period only)"],
     "notes": "The divisor is cfg.months_elapsed() (3/6/9/12 for Q1-Q4), turning the "
              "cumulative YTD rent into a monthly run-rate for any quarter. "
              "Current-period only (NL-41 has no prior column)."},

    # --- Slide 23: Net Worth (Rs. Lakhs, unit-corrected) ---
    {"key": "net_worth", "slide": 23, "metric1": "Net Worth", "metric2": None,
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics:net_worth: (capital + reserves_surplus + fair_value_change_sh - debit_balance_pl) * 100",
     "inputs": ["capital (NL-3, llm)", "bs_reserves_surplus (NL-3, llm)", "bs_fair_value_change_sh (NL-3, llm)", "bs_debit_balance_pl (NL-3, llm)"],
     "notes": "GT wants this row in Rs. Lakhs, not Crores - the '*100' is an empirically "
              "GT-verified unit correction (matched for 5 of 7 companies within 1% at the "
              "time this was calibrated; not a documented spec)."},

    # --- Slide 27: Historical Trends duplicates of Slide 18 ---
    {"key": "hist_gwp", "slide": 27, "metric1": "GWP", "metric2": None,
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: passthrough of income['gwp']",
     "inputs": ["gwp (NL-4, deterministic income statement, same figure as Slide 18)"]},
    {"key": "hist_pbt_27", "slide": 27, "metric1": "PBT", "metric2": None,
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: passthrough of income['pbt']",
     "inputs": ["pbt (NL-2, deterministic income statement)"],
     "notes": "The same cumulative ('up to the quarter') figure as Slide 18's PBT, for "
              "every company without exception. The cumulative column is resolved from "
              "each filing's own NL-2 period header, so insurers that order their "
              "quarter/cumulative columns unusually (Narayana Health prints the "
              "cumulative column FIRST, and swaps its prior-year pair relative to its "
              "own NL-20) still read correctly."},
    {"key": "net_worth_pbt_23", "slide": 23, "metric1": "PBT", "metric2": None,
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: same value as hist_pbt_27",
     "inputs": ["same as hist_pbt_27"],
     "notes": "Slide 23 has its own PBT row alongside Capital/Net Worth - identical figure to Slide 27's."},

    # --- Slide 32: Investment Yield (read directly, not computed) ---
    {"key": "investment_yield_32", "slide": 32, "metric1": "Investment Yield", "metric2": None,
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "data_engine.extract_investment_yield: NL-31's own TOTAL row Gross Yield sub-column, read directly",
     "inputs": ["NL-31 TOTAL row (deterministic, forms.py)"],
     "notes": "Not actually computed from AUM/income - NL-31 already prints a precomputed, "
              "annualized yield %, read as-is (GT-verified exactly, e.g. NBHI 5.45%/5.55%)."},

    # --- Slide 30: reinsurance ratios (current period only) ---
    {"key": "ri_ceding_to_gwp", "slide": 30, "metric1": "RI Ceding to GWP Ratio", "metric2": "Risk Ceded",
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: ri_ceded_total / gwp",
     "inputs": ["ri_ceded_total (NL-33, llm)", "gwp (deterministic income statement)"],
     "notes": "Current period only - NL-33 has no prior-year column."},
    {"key": "ri_commission_to_ceding", "slide": 30, "metric1": "RI Commission to RI Ceding", "metric2": "Risk Ceded",
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: ri_commission / ri_ceded_total",
     "inputs": ["ri_commission (NL-6, llm)", "ri_ceded_total (NL-33, llm)"]},

    # --- Slide 31: ROE (current period only) ---
    {"key": "roe_sahi", "slide": 31, "metric1": "ROE (SAHI)", "metric2": "PAT/Avg. Net Worth",
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: pat_cur / ((net_worth_cur + net_worth_prior) / 2)",
     "inputs": ["pat (NL-2, deterministic income statement)", "net_worth (derived, this same registry)"],
     "notes": "Current period only."},

    # --- Slide 13: channel commission (absolute, despite the "% to GDPI" label) ---
    {"key": "channel_commission_pct_13", "slide": 13, "metric1": "Channel-wise Gross Commision % to GDPI", "metric2": "<per NL-36 channel>",
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: commission_ch_X * 100 (Lakhs -> the sheet's expected scale)",
     "inputs": ["commission_ch_<channel> (NL-6, llm, one per gemini_extract.CHANNELS_36 entry present in gemini_extract.SLIDE13_METRIC2)"],
     "notes": "Despite the '% to GDPI' column label, GT wants the ABSOLUTE commission amount "
              "in Rs. Lakhs here, not a computed ratio - GT-verified exactly."},

    # --- Slide 17: state-wise GDPI share (fraction of company GWP) ---
    {"key": "state_share_17", "slide": 17, "metric1": "<state name>", "metric2": None,
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: state_premium / gwp, one row per gemini_extract.STATES entry",
     "inputs": ["state_<name> (NL-34, llm, one per named state)", "gwp (deterministic income statement)"],
     "notes": "Fraction of company GWP, not the absolute Rs. Crore figure fetched from NL-34 - "
              "GT-verified. 'Others' state uses the residual (GWP minus all 7 named states) "
              "rather than trusting NL-34's own Others row, which is often absent."},
    {"key": "state_share_others_17", "slide": 17, "metric1": "Others", "metric2": None,
     "kind": "percent", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: (gwp - sum(named states)) / gwp",
     "inputs": ["all 7 named state_<name> values", "gwp"]},

    # --- Slide 12: coarser 6-bucket channel split, company-specific metric1 ---
    {"key": "channel_direct_12", "slide": 12, "metric1": "<company short name, redirected via '__SLIDE12__' prefix>",
     "metric2": "<Individual Agents|Corporate Agents - Banks|Corporate Agents - Others|Brokers|Direct Business>",
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: passthrough of channel_premium_<channel> (NL-36, llm), redirected to the shared 'GDPI by Channel - SAHI' company row via apply_company_gemini_pipeline's '__SLIDE12__' sentinel",
     "inputs": ["channel_premium_<channel> (NL-36, llm)"]},
    {"key": "channel_others_12", "slide": 12, "metric1": "<company short name, redirected>", "metric2": "Others",
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: sum(channel_premium_CSC/IMF/Web Aggregator/POS)",
     "inputs": ["channel_premium_CSC, _IMF, _Web Aggregator, _POS (NL-36, llm)"],
     "notes": "CSC/IMF/WA/POS all fold into a single 'Others' bucket for Slide 12's coarser split."},

    # --- Slide 15: agent productivity ---
    {"key": "individual_ats_15", "slide": 15, "metric1": "Individual ATS",
     "metric2": "Individual agents GWP/Individual agents no. of policies",
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: channel_premium_Individual Agents * 1e7 / channel_policies_individual_agents (Lakhs -> Rs. per policy)",
     "inputs": ["channel_premium_Individual Agents (NL-36, llm)", "channel_policies_individual_agents (NL-36, llm)"]},
    {"key": "avg_productivity_15", "slide": 15, "metric1": "Average Productivity (per agent)",
     "metric2": "Premium/No. of Individual Agents",
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: channel_premium_Individual Agents * 100 / agents_individual (Rs. Lakhs per agent)",
     "inputs": ["channel_premium_Individual Agents (NL-36, llm)", "agents_individual (NL-41, llm, current period only)"],
     "notes": "Uses the Individual Agents channel's OWN premium (not total company GWP) - GT-verified. Current-period only."},

    # --- Slide 20: claims settlement (count-based) ---
    {"key": "claims_settlement_ratio_20", "slide": 20, "metric1": "Claims Settlement Ratio", "metric2": None,
     "kind": "ratio", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: claims_settled / claims_reported",
     "inputs": ["claims_settled (NL-37, llm, count)", "claims_reported (NL-37, llm, count)"],
     "notes": "Current-period only."},
    {"key": "avg_claim_size_20", "slide": 20, "metric1": "Average Claim Size", "metric2": None,
     "kind": "money", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: claims (NL-1, Rs.) / claims_settled (NL-37, count)",
     "inputs": ["claims (deterministic income statement, converted Cr -> Rs.)", "claims_settled (NL-37, llm, count)"],
     "notes": "Best-effort estimate, NOT GT-verified - checked against one company previously "
              "and landed ~10% off GT (e.g. NBHI: 27,332 computed vs GT's 30,582); whatever "
              "exact numerator GT uses for this row isn't simply 'Claims Incurred'. Included as "
              "a reasonable draft figure rather than left blank - flag for review before external use."},
    {"key": "claims_to_policies_20", "slide": 20, "metric1": "No. of claims to No. of policies", "metric2": None,
     "kind": "ratio", "source": "derived", "companies": "all",
     "formula": "compute_derived_metrics: claims_reported / sum(channel_policies_<channel> for every NL-36 channel)",
     "inputs": ["claims_reported (NL-37, llm, count)", "channel_policies_<channel> (NL-36, llm, count, one per gemini_extract.CHANNELS_36 entry)"],
     "notes": "Total policy count previously only covered the Individual Agents channel "
              "(used for Slide 15's ATS) - extended to sum policy counts across all 9 NL-36 "
              "channels so this row has a real (if approximate) denominator."},
]


def audit(ws_path=None):
    """Cross-check every llm/derived spec's (slide, metric1, metric2) target
    against what's literally in the sheet, to catch label-drift - the
    exact-string-match failure mode that silently produces 'no matching row'
    log entries during a real pipeline run. Prints anything that doesn't
    resolve to at least one sheet row (skips specs with a wildcard metric1/
    metric2, e.g. Slide 12/13/17's per-channel/per-state templates)."""
    import openpyxl

    HEADERS = ["Slide #", "Category", "Company", "Meric 1", "Metric 2",
               "Source Tab", "Link to Source document", "CUR", "PRIOR", "Growth"]
    COL = {h: i + 1 for i, h in enumerate(HEADERS)}

    def normalize(s):
        if s is None:
            return ""
        import re
        s = str(s).strip().lower()
        s = re.sub(r"\s*-\s*", "-", s)
        return re.sub(r"\s+", " ", s)

    wb = openpyxl.load_workbook(ws_path or str(paths.DATA_ENGINE_WORKBOOK))
    ws = wb["Data Engine"]
    sheet_keys = set()        # exact (slide, metric1, metric2)
    sheet_keys_any_m2 = set()  # (slide, metric1) - any metric2 present
    for r in range(2, ws.max_row + 1):
        slide = ws.cell(row=r, column=COL["Slide #"]).value
        if not isinstance(slide, int):
            continue
        m1 = normalize(ws.cell(row=r, column=COL["Meric 1"]).value)
        m2 = normalize(ws.cell(row=r, column=COL["Metric 2"]).value)
        sheet_keys.add((slide, m1, m2))
        sheet_keys_any_m2.add((slide, m1))

    def resolves(slide, metric1, metric2):
        # metric2=None means "match this metric1 regardless of metric2" -
        # the same convention apply_metric_to_rows uses (metric2 in the
        # sheet can be a static annotation, not a distinguishing key).
        if metric2 is None:
            return (slide, normalize(metric1)) in sheet_keys_any_m2
        return (slide, normalize(metric1), normalize(metric2)) in sheet_keys

    missing = []
    for m in llm_specs():
        for (slide, metric1, metric2) in m["rows"]:
            if not resolves(slide, metric1, metric2):
                missing.append(("llm", m["key"], slide, metric1, metric2))
    for m in DERIVED_METRIC_SPECS:
        if "<" in str(m["metric1"]) or "<" in str(m["metric2"]):
            continue  # templated spec (per-channel/state/company) - not a single sheet key
        if not resolves(m["slide"], m["metric1"], m["metric2"]):
            missing.append(("derived", m["key"], m["slide"], m["metric1"], m["metric2"]))

    if not missing:
        print("All non-templated spec targets resolve to a sheet row.")
    else:
        print(f"{len(missing)} spec target(s) with no matching sheet row:")
        for source, key, slide, m1, m2 in missing:
            print(f"  [{source}] {key}: slide{slide} / {m1!r} / {m2!r}")
    return missing


if __name__ == "__main__":
    audit()
