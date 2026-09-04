"""
Hybrid PDF -> JSON -> Gemini extraction pipeline.

Step 1 (deterministic): pull raw tables for the relevant IRDAI form pages out
of a company's public-disclosure PDF via pdfplumber (falling back to plain
text for pages with no ruled gridlines), and serialize them as JSON.

Step 2 (LLM): hand that JSON to Gemini along with a description of exactly
which Data Engine metrics we need (the current quarter's cumulative figure and
the prior-year comparative), and require a structured JSON response
matching a fixed schema so results can be written back mechanically - no
free-text parsing of the model's answer.
"""
import asyncio
import collections
import re
import threading
import time
import hashlib
import json
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from competitor_analysis.extraction import pdf_cache
from competitor_analysis import config as cfg
from competitor_analysis.extraction.pdf_cache import COMPANY_PDFS, FORM_PATTERNS
from competitor_analysis import paths

# Closing day of each quarter-end month, for phrasing the reporting period in
# the extraction prompt (all four are 30/31, no February case arises).
_MONTH_END_DAY = {"June": 30, "September": 30, "December": 31, "March": 31}

load_dotenv()

MODEL = "gemini-flash-lite-latest"

# Ceiling on in-flight Gemini calls across the WHOLE run (all companies, all
# batches).
MAX_CONCURRENT_GEMINI = int(os.getenv("GEMINI_MAX_CONCURRENCY", "8"))

# Requests-per-minute quota. THIS, not local parallelism, is the real limit on
# Phase 2: the free tier allows only 15 generate_content requests per minute
# per model, and exceeding it returns 429 RESOURCE_EXHAUSTED. Without an
# explicit limiter the SDK's own retry/back-off absorbs the 429s silently, so
# the run just appears mysteriously slow. Raise this if the account is on a
# paid tier.
GEMINI_RPM = int(os.getenv("GEMINI_RPM", "15"))

# Metrics per call. The quota counts REQUESTS, not tokens, and the payloads are
# small (~2-3k tokens), so fewer/larger calls are strictly cheaper - but the
# response schema grows with the batch and the API rejects it beyond a point.
# Measured on this metric set: 40 returns 400 INVALID_ARGUMENT, 20 works. (An
# earlier comment claimed "~40 worked reliably"; it does not.) 20 gives 4 calls
# per company, 28 for all seven - about two minutes' quota at 15 RPM.
DEFAULT_BATCH_SIZE = int(os.getenv("GEMINI_BATCH_SIZE", "20"))

# On-disk cache of model responses, keyed by a fingerprint of the exact inputs
# (see _cache_key). Extraction is deterministic at temperature=0, so re-running
# with unchanged PDFs and metric definitions need not re-pay for the calls.
# Disable with GEMINI_NO_CACHE=1 or run_full_pipeline's --no-cache.
USE_GEMINI_CACHE = os.getenv("GEMINI_NO_CACHE", "") not in ("1", "true", "True")
CACHE_STATS = {"hit": 0, "miss": 0}

_thread_local = threading.local()


def client():
    """One client PER THREAD.

    The batches now run in worker threads, and a single shared genai.Client
    does not survive that: its underlying HTTP client gets closed out from
    under the other threads ("Cannot send a request, as the client has been
    closed"). A thread-local client keeps each worker self-contained."""
    c = getattr(_thread_local, "client", None)
    if c is None:
        c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        _thread_local.client = c
    return c


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
    # compute_derived_metrics (extraction/data_engine.py) instead.
    for state in STATES:
        add(f"state_{state}", f"Gross Direct Premium for '{state}' (if 'Others' - the residual/all-other-states total), from the Geographical Distribution of Business Schedule (NL-34).",
            ["NL-34"], "money", [])

    # --- NL-36 Business Channels: premium & policy count by channel ---
    # (slides 12 and 13 both derive from these but need per-company metric1
    # text and a ratio/aggregation computed in Python - see build_derived_metrics)
    for form_label, metric2 in CHANNELS_36:
        add(f"channel_premium_{metric2}", f"Premium (Rs. Lakhs) for the '{form_label}' channel, from the Business-Channels Wise Schedule (NL-36).",
            ["NL-36"], "money", [])
    # Number of Policies per channel, for every channel (not just Individual
    # Agents - that was the only one extracted previously) - summed in
    # Python to give the company's total policy count, the denominator
    # Slide 20's "No. of claims to No. of policies" needs. Reuses the same
    # NL-36 payload already fetched for channel_premium_*, no extra PDF I/O.
    for form_label, metric2 in CHANNELS_36:
        add(f"channel_policies_{metric2}", f"Number of Policies (a count, not Rs.) for the '{form_label}' channel, from the Business-Channels Wise Schedule (NL-36).",
            ["NL-36"], "count", [])

    # --- NL-37 Claims Data ---
    # (NL-37 reports claim COUNTS, not a pre-computed ratio; combined with
    # NL-36's total policy count and NL-1's Claims amount in Python for
    # Slide 20's Claims Settlement Ratio / Average Claim Size / No. of
    # claims to policies. Average Claim Size is a best-effort estimate -
    # verified ~10% off GT for at least one company - flagged as such
    # wherever it's written.)
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
                "source_form": {"type": ["string", "null"]},
                "page_number": {"type": ["integer", "null"]},
                "evidence": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["fy26_q3_value", "fy25_q3_value", "found",
                         "source_form", "page_number", "evidence", "notes"],
        }
    return {
        "type": "object",
        "properties": props,
        "required": metric_keys,
    }


PROMPT_TEMPLATE = """You are extracting specific line items from IRDAI (India) quarterly public-disclosure regulatory tables for {company}.

The JSON below contains raw tables (and, for a few pages with no ruled gridlines, raw text) extracted from the company's Form NL schedules for the quarter ended {period_end}. Most schedules report FOUR figures per line: "For the quarter" and "Up to the quarter" (i.e. year-to-date/cumulative), each for both the current year (ended {cur_short}) and the prior year comparative (ended {prior_short}) - sometimes phrased as "For the period ended" instead of "Up to the quarter". A few schedules (e.g. Offices Information / NL-41) are a single point-in-time snapshot "as on" {cur_short} with no prior-year comparative at all - for those, report the snapshot value as fy26_q3_value and leave fy25_q3_value null.

For EACH metric listed below (its own description states its unit - Rs. Lakhs, a plain rupee amount, a percentage, a ratio/"no. of times" multiple, or a headcount/policy count - report the number exactly as printed in that unit, don't convert units yourself), find its value and report the CUMULATIVE ("Up to the quarter" / "up to the period ended" / "for the period ended", i.e. year-to-date from the start of the financial year) figure — NOT the single-quarter figure — for both:
  - fy26_q3_value: cumulative value for the current year (period ended {period_end}), or the snapshot value for point-in-time schedules
  - fy25_q3_value: cumulative value for the prior year (period ended {prior_period_end}), or null if the schedule has no prior-year comparative
Convert "(1,234)" style parentheses to a negative number; treat "-" as 0. If a metric is genuinely not present anywhere in the provided tables, set found=false and both values to null - do not guess, estimate, or compute a value that isn't directly shown.

For EVERY metric (found or not), also report: source_form (the form_name string from the JSON block you found it in, e.g. "Investment Schedule (NL-12 & 12A)" - null if not found), page_number (that page's "page_index" value from the JSON, PLUS ONE, i.e. 1-based - null if not found), evidence (the exact row label and cell text you read the value from, as printed, e.g. "Gross Direct Premium: 2,394.98" - null if not found), and notes (a short note on anything non-obvious about this extraction - e.g. "used the 'Up to the Period Ended' column at a stacked-block year layout", "value read from the schedule's own TOTAL row" - null if nothing worth noting). These are for a human audit trail, not used to compute anything - keep them factual and short.

Where a schedule breaks amounts down by class of business (Health / Personal Accident / Travel / etc.), use the GRAND TOTAL / overall company figure, not a single class, unless the metric explicitly says otherwise.

IMPORTANT for any metric described as a percentage or ratio (e.g. Combined Ratio, Loss Ratio, Expense of Management Ratio): insurers format the SAME ratio inconsistently across these source documents - some print an explicit percentage like "111.88%", others print the bare decimal multiple with no "%" sign, e.g. "1.11" (which ALSO means 111%, just written as a multiple instead of a percentage). Always normalize your reported number to the "percentage-with-%-removed" scale: if the source cell has a "%" sign, report the number as printed (111.88% -> 111.88); if the source cell has NO "%" sign but is clearly the same kind of ratio (a bare decimal typically between 0 and ~20 for things like Combined/Loss/Expense ratios), MULTIPLY it by 100 before reporting (1.11 -> 111). The output must always be consistent: a metric worth "around 100" should be reported as ~100, never as ~1. "No. of times" ratios explicitly described as such (e.g. Solvency Ratio) are the one exception - report those as printed, unscaled.

Metrics to extract:
{metric_list}

Source tables (JSON):
{tables_json}
"""


def _cache_key(company, payload, metric_specs):
    """Fingerprint of everything that determines the model's answer: the
    company, the exact source tables handed over, the metric set and its
    instructions, the model, and the reporting period. Any change to the PDF
    content, the metric descriptions or the period invalidates the entry, so a
    stale answer can't be served for changed inputs."""
    blob = json.dumps({
        "company": company,
        "model": MODEL,
        "period": [cfg.FY, cfg.QUARTER],
        "payload": payload,
        "metrics": [(m["key"], m.get("description")) for m in metric_specs],
    }, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _cache_path(key):
    d = str(paths.GEMINI_CACHE / (cfg.FY or "unset") / (cfg.QUARTER or "unset"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{key}.json")


def _cache_lookup(company, payload, metric_specs):
    """The cached response for these exact inputs, or None. Counts the hit."""
    if not USE_GEMINI_CACHE:
        return None
    path = _cache_path(_cache_key(company, payload, metric_specs))
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! Ignoring unreadable Gemini cache entry {path}: {e}")
        return None
    CACHE_STATS["hit"] += 1
    return cached


def extract_metrics_via_gemini(company, payload, metric_specs):
    """Cached wrapper around the model call. Extraction is deterministic
    (temperature=0) over fixed inputs, so a repeat run with unchanged PDFs and
    metric definitions has no reason to pay for the call again - which is what
    made iterating on downstream mapping bugs so slow."""
    if USE_GEMINI_CACHE:
        cached = _cache_lookup(company, payload, metric_specs)
        if cached is not None:
            return cached
        path = _cache_path(_cache_key(company, payload, metric_specs))
        CACHE_STATS["miss"] += 1
        out = _extract_metrics_via_gemini_uncached(company, payload, metric_specs)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1)
        except OSError as e:
            print(f"  ! Could not write Gemini cache entry: {e}")
        return out
    CACHE_STATS["miss"] += 1
    return _extract_metrics_via_gemini_uncached(company, payload, metric_specs)


def _extract_metrics_via_gemini_uncached(company, payload, metric_specs):
    """metric_specs: list of {"key":..., "description":...}. Returns
    {key: {"fy26_q3": float|None, "fy25_q3": float|None, "found": bool,
    "source_form": str|None, "page_number": int|None, "evidence": str|None,
    "notes": str|None}} - the last four are an audit trail (which form/page/
    row the value was read from), not used in any downstream computation."""
    metric_keys = [m["key"] for m in metric_specs]
    metric_list = "\n".join(f"- {m['key']}: {m['description']}" for m in metric_specs)
    tables_json = json.dumps(payload, ensure_ascii=False, default=str)
    # Period wording is derived from pipeline_config, not written into the
    # template, so the prompt describes whichever quarter is being run.
    cal = cfg.calendar_mapping()
    month, year = cal["month"], cal["year"]
    day = _MONTH_END_DAY[month]
    prompt = PROMPT_TEMPLATE.format(
        company=company, metric_list=metric_list, tables_json=tables_json,
        period_end=f"{day} {month} {year}",
        prior_period_end=f"{day} {month} {year - 1}",
        cur_short=f"{month[:3]} {year}",
        prior_short=f"{month[:3]} {year - 1}",
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
            "source_form": v.get("source_form"),
            "page_number": v.get("page_number"),
            "evidence": v.get("evidence"),
            "notes": v.get("notes"),
        }
    return out


class RateLimiter:
    """Sliding-window limiter over the last 60s, shared by every call in a run.

    Needed because the quota is per-minute per-model and applies across the
    whole project - so it has to be enforced globally, not per company. Waiting
    here is strictly cheaper than being rejected: a 429 costs the round-trip
    and then a back-off anyway."""

    def __init__(self, rpm):
        self.rpm = rpm
        self._times = collections.deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        if self.rpm <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
                if len(self._times) < self.rpm:
                    self._times.append(now)
                    return
                wait = 60.0 - (now - self._times[0]) + 0.05
            await asyncio.sleep(wait)


def _retry_delay_from(exc, attempt):
    """Honour the server's own retryDelay when it supplies one, else back off
    exponentially. Guessing shorter than the server asked for just earns
    another 429."""
    text = str(exc)
    m = re.search(r"'retryDelay':\s*'(\d+)s'", text) or \
        re.search(r"retry in (\d+(?:\.\d+)?)s", text)
    if m:
        return float(m.group(1)) + 1.0
    return min(60.0, 2.0 ** attempt)


def _is_rate_limit(exc):
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


async def _call_with_retry(company, payload, batch, limiter, max_attempts=5):
    """One batch call: rate-limited, and retried on quota rejection.

    The cache is consulted BEFORE the limiter: a cached answer makes no request
    and so must not consume a slot in the per-minute budget. (Gating it first
    made a fully-cached run take as long as a live one.)"""
    cached = _cache_lookup(company, payload, batch)
    if cached is not None:
        return cached
    for attempt in range(max_attempts):
        await limiter.acquire()
        try:
            return await asyncio.to_thread(
                extract_metrics_via_gemini, company, payload, batch)
        except Exception as e:
            if not _is_rate_limit(e) or attempt == max_attempts - 1:
                raise
            delay = _retry_delay_from(e, attempt)
            print(f"  . {company}: rate-limited (429), retrying in {delay:.0f}s "
                  f"(attempt {attempt + 2}/{max_attempts})")
            await asyncio.sleep(delay)


def _batches(metric_specs, batch_size):
    """(batch, forms_needed) pairs. Batching exists because large single calls
    (75+ metrics) were rejected by the API; ~40 worked reliably."""
    for i in range(0, len(metric_specs), batch_size):
        batch = metric_specs[i:i + batch_size]
        yield batch, sorted({f for m in batch for f in m["forms"]})


def extract_company_metrics(company, pdf_path, metric_specs, batch_size=DEFAULT_BATCH_SIZE,
                            all_forms=None):
    """One company's full master metric list. Batches run CONCURRENTLY - they
    are independent calls over disjoint metric sets, and were previously
    awaited one at a time.

    The PDF is scanned for ALL forms ONCE up front (a single pass over every
    page), then each batch's call reuses the relevant slice of that cached
    payload - no repeated PDF I/O per batch."""
    return asyncio.run(extract_company_metrics_async(
        company, pdf_path, metric_specs, batch_size, all_forms))


async def extract_company_metrics_async(company, pdf_path, metric_specs,
                                        batch_size=DEFAULT_BATCH_SIZE, all_forms=None,
                                        semaphore=None, limiter=None):
    all_forms = all_forms or sorted({f for m in metric_specs for f in m["forms"]})
    full_payload = build_company_payload(pdf_path, all_forms)
    semaphore = semaphore or asyncio.Semaphore(MAX_CONCURRENT_GEMINI)
    limiter = limiter if limiter is not None else RateLimiter(GEMINI_RPM)

    async def run_batch(batch, forms_needed):
        payload = {k: full_payload[k] for k in forms_needed}
        async with semaphore:
            # generate_content is a blocking SDK call, so it goes to a worker
            # thread; these are network-bound, so threads are the right tool.
            return await _call_with_retry(company, payload, batch, limiter)

    results = await asyncio.gather(*[
        run_batch(batch, forms) for batch, forms in _batches(metric_specs, batch_size)
    ])
    out = {}
    for r in results:
        out.update(r)
    return out


async def extract_many_companies_async(jobs, metric_specs, batch_size=DEFAULT_BATCH_SIZE,
                                       all_forms=None, max_concurrency=None, rpm=None):
    """Extract for several companies at once, with EVERY (company, batch) call
    sharing ONE concurrency gate.

    This is the main Phase 2 speed-up: the work was previously serialised twice
    over - companies in an outer loop, batches in an inner one - so 7 companies
    x 4 batches meant 28 strictly-sequential API round-trips despite being
    completely independent of each other.

    jobs: [(company_key, prompt_name, pdf_path), ...]
    Returns {company_key: {metric_key: {...}}}. A company whose extraction
    raises is returned as an empty dict rather than failing the whole run.
    """
    semaphore = asyncio.Semaphore(max_concurrency or MAX_CONCURRENT_GEMINI)
    # One limiter for the whole run: the per-minute quota is project-wide, so
    # per-company limiters would collectively overshoot it. rpm=0 disables
    # limiting (paid tiers, and tests with a stubbed model call).
    limiter = RateLimiter(GEMINI_RPM if rpm is None else rpm)

    async def one(company_key, prompt_name, pdf_path):
        try:
            return company_key, await extract_company_metrics_async(
                prompt_name, pdf_path, metric_specs, batch_size, all_forms,
                semaphore, limiter)
        except Exception as e:
            print(f"  ! Gemini extraction failed for {company_key}: {e}")
            return company_key, {}

    pairs = await asyncio.gather(*[one(*job) for job in jobs])
    return dict(pairs)
