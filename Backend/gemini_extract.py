"""
Hybrid PDF -> JSON -> Gemini extraction pipeline.

Step 1 (deterministic): pull raw tables for the relevant IRDAI form pages out
of a company's public-disclosure PDF via pdfplumber (falling back to plain
text for pages with no ruled gridlines), and serialize them as JSON.

Step 2 (LLM): hand that JSON to Gemini along with a description of exactly
which Data Engine metrics we need (current-quarter-cumulative FY26_Q3 and the
prior-year comparative FY25_Q3), and require a structured JSON response
matching a fixed schema so results can be written back mechanically - no
free-text parsing of the model's answer.
"""
import json
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

import pdf_cache
from pdf_cache import COMPANY_PDFS, FORM_PATTERNS

load_dotenv()

MODEL = "gemini-flash-lite-latest"

_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


# ---------------------------------------------------------------------------
# Step 1: raw table -> JSON
# ---------------------------------------------------------------------------

def build_company_payload(pdf_path, form_keys, max_pages_per_form=2):
    """Sources pages from the on-disk PDF-JSON cache (pdf_cache.py) - the
    cache itself does the one-pass pdfplumber scan (text + tables for every
    page), persisted to disk so repeat calls/runs don't re-parse the PDF at
    all. Same output shape as before: {form_key: {"form_name", "pages": [...]}}"""
    doc = pdf_cache.get_company_json(pdf_path)
    payload = {}
    for key in form_keys:
        form_name, _ = FORM_PATTERNS[key]
        pages = pdf_cache.pages_for_form(doc, key, max_pages=max_pages_per_form)
        pages_out = []
        for p in pages:
            entry = {"page_index": p["page_number"] - 1}
            if p["tables"]:
                entry["tables"] = p["tables"]
            else:
                entry["text"] = p["text"]
            pages_out.append(entry)
        payload[key] = {"form_name": form_name, "pages": pages_out}
    return payload


# ---------------------------------------------------------------------------
# Master metric list for the company-level Data Engine slides (13-35).
# `kind` controls unit handling downstream:
#   money   - Rs. Lakhs in the source -> divided by 100 to Crores
#   percent - reported as a percentage (e.g. 111.88 for 111.88%) -> /100
#   ratio   - a plain multiple, e.g. "2.49 times" -> stored as-is
#   count   - a headcount/policy count -> stored as-is (integer)
# `rows` maps the metric to Data Engine sheet rows: (slide, metric1, metric2)
# metric2 of None means "match this metric1 regardless of metric2" (used
# where metric2 in the sheet is a static annotation, not a distinguishing key).
# ---------------------------------------------------------------------------

ALL_FORMS = ["NL-1", "NL-2", "NL-3", "NL-4", "NL-6", "NL-7", "NL-12", "NL-20",
             "NL-29", "NL-33", "NL-34", "NL-36", "NL-37", "NL-41"]

CHANNELS_36 = [
    ("Individual Agents", "Individual Agents"),
    ("Corporate Agents - Banks", "Corporate Agents - Banks"),
    ("Corporate Agents - Others", "Corporate Agents - Others"),
    ("Brokers", "Brokers"),
    ("Direct Business", "Direct Business"),
    ("Common Service Centers / CSC", "CSC"),
    ("Insurance Marketing Firm / IMF", "IMF"),
    ("Web Aggregators", "Web Aggregator"),
    ("Point of Sales person / POS", "POS"),
]
# Slide 13's channel labels use no space around the hyphen and drop "Direct
# Business" - map CHANNELS_36's metric2 -> slide13's exact metric2 text.
SLIDE13_METRIC2 = {
    "Individual Agents": "Individual Agents", "Corporate Agents - Banks": "Corporate Agents-Banks",
    "Corporate Agents - Others": "Corporate Agents-Others", "Brokers": "Brokers",
    "CSC": "CSC", "IMF": "IMF", "Web Aggregator": "Web Aggregator", "POS": "POS",
}
# Slide 12's channel buckets are coarser (6, spaced hyphens) - CSC/IMF/WA/POS
# all fold into "Others" there (computed, not fetched directly).
SLIDE12_DIRECT_METRIC2 = {
    "Individual Agents": "Individual Agents", "Corporate Agents - Banks": "Corporate Agents - Banks",
    "Corporate Agents - Others": "Corporate Agents - Others", "Brokers": "Brokers",
    "Direct Business": "Direct Business",
}
SLIDE12_OTHERS_KEYS = ["CSC", "IMF", "Web Aggregator", "POS"]
SLIDE12_COMPANY_METRIC1 = {
    "NBHI": "Niva Bupa", "Star Health": "Star", "ABHI": "ABHI", "Care Health": "CARE",
    "Manipal Cigna": "Manipal Cigna", "Narayana Health": "Narayana", "Galaxy Health": "Galaxy",
}

REGIONS = ["North", "East", "West", "Central", "South"]
STATES = ["Uttar Pradesh", "Maharashtra", "Karnataka", "Haryana", "Tamil Nadu", "Kerala", "Delhi", "Others"]
DEBT_RATINGS = [
    ("Sovereign", "Any other (Sovereign)"), ("AAA rated", "AAA rated"),
    ("AA or better", "AA or better"),
    ("Rated below AA but above A", "Rated below AA but above A"),
    ("Rated below A", "Rated below A but above B / Rated Below B combined"),
]
MATURITY_BUCKETS = [
    "Up to 1 year", "More than 1 year and upto 3 years",
    "More than 3 years and upto 7 years", "More than 7 years and upto 10 years",
    "Above 10 years",
]
INTERMEDIARIES = [
    ("Individual Agents", "Individual Agents"), ("Corporate Agents-Banks", "CA-Banks"),
    ("Corporate Agents-Others", "CA-Others"), ("Insurance Brokers", "Brokers"),
    ("Web Aggregators", "WA"), ("Insurance Marketing Firm", "IMF"),
    ("Point of Sales persons", "POS"),
]


def master_metric_specs():
    specs = []

    def add(key, description, forms, kind, rows):
        specs.append({"key": key, "description": description, "forms": forms, "kind": kind, "rows": rows})

    # --- NL-3 Balance Sheet: Capital, Net Worth ---
    add("capital", "Share Capital (paid-up equity capital), from the Balance Sheet (NL-3).", ["NL-3"], "money",
        [(23, "Capital", None)])
    add("bs_reserves_surplus", "Reserves and Surplus, from the Balance Sheet (NL-3), 'Sources of Funds' section.", ["NL-3"], "money", [])
    add("bs_fair_value_change_sh", "Fair Value Change Account - Shareholders' Funds portion, from the Balance Sheet (NL-3), 'Sources of Funds' section.", ["NL-3"], "money", [])
    add("bs_debit_balance_pl", "Debit Balance in Profit and Loss Account (accumulated losses carried on the balance sheet, if any - 0 if not present), from the Balance Sheet (NL-3), 'Application of Funds' section.", ["NL-3"], "money", [])

    # --- NL-6 Commission Schedule: channel-wise commission (Lakhs) - combined
    # with channel_premium_* in Python to give slide 13's "% to GDPI" ratio ---
    for form_label, metric2 in CHANNELS_36:
        add(f"commission_ch_{metric2}", f"Commission paid/payable for the '{form_label}' channel, from the Commission Schedule (NL-6).",
            ["NL-6"], "money", [])
    add("ri_commission", "Commission received on reinsurance ceded (reinsurance commission), from the Commission Schedule (NL-6).",
        ["NL-6"], "money", [])  # combined into slide30 ratio in Python

    # --- NL-7 Operating Expenses: manpower/employee cost, IT expense, rent ---
    add("manpower_cost", "Employees' remuneration & welfare benefits (total manpower/employee cost) line item, from the Operating Expenses Schedule (NL-7).",
        ["NL-7"], "money", [])
    add("it_spend", "Information technology / IT expenses line item, from the Operating Expenses Schedule (NL-7).",
        ["NL-7"], "money", [])
    add("rent_expense", "Rent, rates & taxes (office rent) expense line item, from the Operating Expenses Schedule (NL-7).",
        ["NL-7"], "money", [])

    # --- NL-12 & 12A Investment Schedule: portfolio breakdown + AUM ---
    add("inv_govt_bonds", "Sum of Central Government Securities + State Government Securities/Development Loans + Treasury Bills, GRAND TOTAL (Long term + Short term, Shareholders + Policyholders), from the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(24, "Govt Bonds", None)])
    add("inv_corporate_bonds", "Sum of Debentures/Bonds + Approved Investment Infrastructure/Housing/Other securities + Other Approved Securities (corporate debt), GRAND TOTAL, from the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(24, "Corporate Bonds/Debentures", None)])
    add("inv_deposits", "Fixed Deposits / Short-term deposits with banks (deposit investments), GRAND TOTAL, from the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(24, "Deposits", None)])
    add("inv_equity", "Equity Shares (incl. InvITs/REITs if listed separately), GRAND TOTAL, from the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(24, "Equity/Invits/REIT", None)])
    add("inv_mutual_funds", "Mutual Fund / Money Market investments, GRAND TOTAL, from the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(24, "Mutual Funds", None)])
    add("aum_total", "GRAND TOTAL of all investments (Shareholders + Policyholders, Long term + Short term) - the bottom-line total of the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(32, "AUM (Overall)", None)])
    add("aum_shareholders", "Total investments attributable to the SHAREHOLDERS' fund only (Long term + Short term shareholders columns), from the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(33, "AUM -Shareholders", None)])
    add("aum_policyholders", "Total investments attributable to the POLICYHOLDERS' fund only (Long term + Short term policyholders columns), from the Investment Schedule (NL-12 & 12A).",
        ["NL-12"], "money", [(33, "AUM -Policyholders", None)])

    # --- NL-20 Analytical Ratios ---
    add("combined_ratio", "Combined Ratio (overall company total, not segment-wise), from the Analytical Ratios Schedule (NL-20).",
        ["NL-20"], "percent", [(19, "Combined Ratio", None), (28, "Combined Ratio", None)])
    add("loss_ratio", "'Net Incurred Claims to Net Earned Premium' ratio (overall company total), from the Analytical Ratios Schedule (NL-20).",
        ["NL-20"], "percent", [(19, "Loss Ratio", None), (28, "Loss Ratio", None)])
    add("expense_ratio_nwp", "'Expense of Management to Net Written Premium' Ratio (overall company total), from the Analytical Ratios Schedule (NL-20).",
        ["NL-20"], "percent", [(19, "Expense Ratio", None), (29, "Expense Ratio", None)])
    add("eom_ratio_gdp", "'Expense of Management to Gross Direct Premium' Ratio (overall company total), from the Analytical Ratios Schedule (NL-20).",
        ["NL-20"], "percent", [(29, "Expense of Management Ratio", None)])
    add("solvency_ratio", "'Available Solvency Margin Ratio to Required Solvency Margin Ratio' (No. of times), from the Analytical Ratios Schedule (NL-20).",
        ["NL-20"], "ratio", [(31, "Solvency Ratios", None)])

    # --- NL-29 Debt Securities: rating & maturity mix (use Book Value % of total) ---
    for metric1, label in DEBT_RATINGS:
        add(f"debt_rating_{metric1}", f"'{label}' row's Book Value 'as % of total for this class' (the credit-rating breakdown percentage), from the Detail Regarding Debt Securities Schedule (NL-29). If 'Rated below A' isn't a single row, sum 'Rated below A but above B' + 'Rated Below B'.",
            ["NL-29"], "percent", [(25, metric1, None)])
    for bucket in MATURITY_BUCKETS:
        add(f"debt_maturity_{bucket}", f"'{bucket}' row's Book Value 'as % of total for this class' (the residual-maturity breakdown percentage), from the Detail Regarding Debt Securities Schedule (NL-29).",
            ["NL-29"], "percent", [(26, bucket, None)])

    # --- NL-33 Reinsurance: total premium ceded ---
    add("ri_ceded_total", "Total premium ceded to reinsurers (Upto the Quarter), from the 'Grand Total (C)' row of the Reinsurance/Retrocession Risk Concentration Schedule (NL-33). That row's total is usually split across 'Proportional' + 'Non-Proportional' + 'Facultative' sub-columns - if so, SUM those sub-column values together to get the one total figure requested here.",
        ["NL-33"], "money", [])  # used in slide30 ratio

    # --- NL-34 Geographical Distribution: state GDPI ---
    # (NL-34 lists individual states only, no zone/region rollup - Slide 16's
    # North/East/West/Central/South split isn't directly derivable and is
    # intentionally left blank rather than guessed at a state->zone mapping.)
    # rows=[] - Slide 17 wants each state's share of company GWP, not the
    # absolute Rs. Crore figure fetched here; the fraction is computed in
    # compute_derived_metrics (phase2_extraction.py) instead.
    for state in STATES:
        add(f"state_{state}", f"Gross Direct Premium for '{state}' (if 'Others' - the residual/all-other-states total), from the Geographical Distribution of Business Schedule (NL-34).",
            ["NL-34"], "money", [])

    # --- NL-36 Business Channels: premium & policy count by channel ---
    # (slides 12 and 13 both derive from these but need per-company metric1
    # text and a ratio/aggregation computed in Python - see build_derived_metrics)
    for form_label, metric2 in CHANNELS_36:
        add(f"channel_premium_{metric2}", f"Premium (Rs. Lakhs) for the '{form_label}' channel, from the Business-Channels Wise Schedule (NL-36).",
            ["NL-36"], "money", [])
    add("channel_policies_individual_agents", "Number of Policies (a count, not Rs.) for the 'Individual Agents' channel, from the Business-Channels Wise Schedule (NL-36).",
        ["NL-36"], "count", [])

    # --- NL-37 Claims Data ---
    # (NL-37 reports claim COUNTS, not a pre-computed ratio; combined into
    # slide20's Claims Settlement Ratio in Python. Average Claim Size and
    # claims-to-policies need claim AMOUNTS / total policy counts this
    # schedule doesn't carry, so are left as an accepted gap.)
    add("claims_os_start", "'Claims O/S at the beginning of the period', Total (overall company, rightmost/Total column), from the Claims Data Schedule (NL-37) - a claim COUNT, not an amount.",
        ["NL-37"], "count", [])
    add("claims_reported", "'Claims reported during the period', Total (overall company, rightmost/Total column), from the Claims Data Schedule (NL-37) - a claim COUNT, not an amount.",
        ["NL-37"], "count", [])
    add("claims_settled", "'Claims Settled during the period', Total (overall company, rightmost/Total column), from the Claims Data Schedule (NL-37) - a claim COUNT, not an amount.",
        ["NL-37"], "count", [])

    # --- NL-41 Offices Information (point-in-time; no prior-year column expected) ---
    add("employees_onroll", "No. of Employees - On-roll, from the Offices Information Schedule (NL-41).",
        ["NL-41"], "count", [(34, "Employees", "On-roll Employee")])
    add("agents_individual", "No. of Insurance Agents - Individual Agents, from the Offices Information Schedule (NL-41).",
        ["NL-41"], "count", [(34, "Agents", "Individual Agents")])
    add("offices_count", "No. of branches/offices at the end of the period, from the Offices Information Schedule (NL-41).",
        ["NL-41"], "count", [(35, "No. of Offices", None)])
    for form_label, metric2 in INTERMEDIARIES:
        add(f"intermediary_{metric2}", f"No. of '{form_label}', from the Offices Information Schedule (NL-41).",
            ["NL-41"], "count", [(35, "Intermediaries", metric2)])

    return specs



# ---------------------------------------------------------------------------
# Step 2: JSON -> Gemini -> structured metrics
# ---------------------------------------------------------------------------

def build_schema(metric_keys):
    props = {}
    for k in metric_keys:
        props[k] = {
            "type": "object",
            "properties": {
                "fy26_q3_value": {"type": ["number", "null"]},
                "fy25_q3_value": {"type": ["number", "null"]},
                "found": {"type": "boolean"},
            },
            "required": ["fy26_q3_value", "fy25_q3_value", "found"],
        }
    return {
        "type": "object",
        "properties": props,
        "required": metric_keys,
    }


PROMPT_TEMPLATE = """You are extracting specific line items from IRDAI (India) quarterly public-disclosure regulatory tables for {company}.

The JSON below contains raw tables (and, for a few pages with no ruled gridlines, raw text) extracted from the company's Form NL schedules for the quarter ended 31 December 2025. Most schedules report FOUR figures per line: "For the quarter" and "Up to the quarter" (i.e. year-to-date/cumulative), each for both the current year (ended Dec 2025) and the prior year comparative (ended Dec 2024) - sometimes phrased as "For the period ended" instead of "Up to the quarter". A few schedules (e.g. Offices Information / NL-41) are a single point-in-time snapshot "as on" 31 Dec 2025 with no prior-year comparative at all - for those, report the snapshot value as fy26_q3_value and leave fy25_q3_value null.

For EACH metric listed below (its own description states its unit - Rs. Lakhs, a plain rupee amount, a percentage, a ratio/"no. of times" multiple, or a headcount/policy count - report the number exactly as printed in that unit, don't convert units yourself), find its value and report the CUMULATIVE ("Up to the quarter" / "up to the period ended" / "for the period ended", i.e. YTD Apr-Dec) figure — NOT the single-quarter figure — for both:
  - fy26_q3_value: cumulative value for the current year (period ended 31 Dec 2025), or the snapshot value for point-in-time schedules
  - fy25_q3_value: cumulative value for the prior year (period ended 31 Dec 2024), or null if the schedule has no prior-year comparative
Convert "(1,234)" style parentheses to a negative number; treat "-" as 0. If a metric is genuinely not present anywhere in the provided tables, set found=false and both values to null - do not guess, estimate, or compute a value that isn't directly shown.

Where a schedule breaks amounts down by class of business (Health / Personal Accident / Travel / etc.), use the GRAND TOTAL / overall company figure, not a single class, unless the metric explicitly says otherwise.

IMPORTANT for any metric described as a percentage or ratio (e.g. Combined Ratio, Loss Ratio, Expense of Management Ratio): insurers format the SAME ratio inconsistently across these source documents - some print an explicit percentage like "111.88%", others print the bare decimal multiple with no "%" sign, e.g. "1.11" (which ALSO means 111%, just written as a multiple instead of a percentage). Always normalize your reported number to the "percentage-with-%-removed" scale: if the source cell has a "%" sign, report the number as printed (111.88% -> 111.88); if the source cell has NO "%" sign but is clearly the same kind of ratio (a bare decimal typically between 0 and ~20 for things like Combined/Loss/Expense ratios), MULTIPLY it by 100 before reporting (1.11 -> 111). The output must always be consistent: a metric worth "around 100" should be reported as ~100, never as ~1. "No. of times" ratios explicitly described as such (e.g. Solvency Ratio) are the one exception - report those as printed, unscaled.

Metrics to extract:
{metric_list}

Source tables (JSON):
{tables_json}
"""


def extract_metrics_via_gemini(company, payload, metric_specs):
    """metric_specs: list of {"key":..., "description":...}. Returns
    {key: {"fy26_q3": float|None, "fy25_q3": float|None, "found": bool}}."""
    metric_keys = [m["key"] for m in metric_specs]
    metric_list = "\n".join(f"- {m['key']}: {m['description']}" for m in metric_specs)
    tables_json = json.dumps(payload, ensure_ascii=False, default=str)
    prompt = PROMPT_TEMPLATE.format(
        company=company, metric_list=metric_list, tables_json=tables_json
    )
    schema = build_schema(metric_keys)
    resp = client().models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema,
            temperature=0,
        ),
    )
    result = json.loads(resp.text)
    out = {}
    for k in metric_keys:
        v = result.get(k, {})
        out[k] = {
            "fy26_q3": v.get("fy26_q3_value"),
            "fy25_q3": v.get("fy25_q3_value"),
            "found": v.get("found", False),
        }
    return out


def extract_company_metrics(company, pdf_path, metric_specs, batch_size=20, all_forms=None):
    """Runs the full master metric list for one company, batching into
    several Gemini calls (large single calls - 75+ metrics - were rejected
    by the API, ~40 worked reliably). The PDF is scanned for ALL forms ONCE
    up front (a single pass over every page), then each batch's Gemini call
    reuses the relevant slice of that cached payload - no repeated PDF I/O
    per batch."""
    all_forms = all_forms or sorted({f for m in metric_specs for f in m["forms"]})
    full_payload = build_company_payload(pdf_path, all_forms)
    out = {}
    for i in range(0, len(metric_specs), batch_size):
        batch = metric_specs[i:i + batch_size]
        forms_needed = sorted({f for m in batch for f in m["forms"]})
        payload = {k: full_payload[k] for k in forms_needed}
        batch_result = extract_metrics_via_gemini(company, payload, batch)
        out.update(batch_result)
    return out
