import os
import json
import re
import asyncio
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx
import aiofiles
import openpyxl
from pypdf import PdfReader
from parallel import AsyncParallel  # Parallel AI Python SDK
from dotenv import load_dotenv

from competitor_analysis import config as cfg
from competitor_analysis import paths

load_dotenv()

# 1. Setup paths & client
# Paths resolve through competitor_analysis.paths, not this file's location.
LINKS_FILE = paths.SOURCE_LINKS_FILE
DOWNLOAD_DIR = paths.DOWNLOADS_DIR

client = AsyncParallel(api_key=os.getenv("PARALLEL_API_KEY"))

# "core" gives more reasoning depth than "base" for navigating dropdown/accordion
# filters before the real file link resolves in the DOM.
# https://docs.parallel.ai/task-api/guides/choose-a-processor
PROCESSOR = "core"

# Per-processor wall-clock budget for task_run.result(). These are deliberately
# tight: a single global 30-minute ceiling meant one hung "core" run blocked the
# next processor for half an hour, which dominated Phase 1's runtime. A run that
# hasn't resolved a link within its budget is far more likely stuck than slow,
# so we cut it and escalate instead of waiting.
PROCESSOR_TIMEOUTS = {"core": 150, "pro": 240, "ultra": 420}
DEFAULT_PROCESSOR_TIMEOUT = 240

# Escalation ladder, cheapest first. "ultra" is deliberately NOT here: it costs
# materially more than core/pro and, being the third sequential attempt, it also
# inflated worst-case latency exactly when a portal was already failing. Any
# company can opt back into a different ladder via a "processors" list in
# source_links.json (e.g. ["core", "pro", "ultra"]) without a code change.
PROCESSOR_LADDER = ["core", "pro"]

# Search API budget - it's the cheap safety net, so it gets a short leash too.
SEARCH_TIMEOUT_SECONDS = 90

# Run the Search API concurrently with the FIRST processor rather than only
# after every processor has failed. Whichever produces a downloadable file first
# wins and the loser is cancelled, so a portal the agent can't crawl no longer
# pays the full ladder before Search is even tried.
RACE_SEARCH_WITH_FIRST_PROCESSOR = True

# Resolved-URL memory. Disclosure URLs are overwhelmingly templated by period,
# so a URL that worked once is turned into a {month}/{year}-parameterised
# pattern and tried directly next quarter - which skips the agent entirely for
# most companies. This is a cache, never a source of truth: a rendered pattern
# still has to pass download_file's magic-bytes and validate_document checks.
URL_PATTERNS_FILE = paths.URL_PATTERNS_FILE

# How each company's file was located this run, for the end-of-run summary -
# this is what tells you which processors actually earn their place in the
# ladder and which never win.
ATTEMPT_LOG: dict[str, dict] = {}

# Browser-like headers so Cloudflare/AWS WAF on the insurer domains doesn't 403/406
# the actual file download (see "Web Application Firewall Protections" in the brief).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Magic bytes to confirm we actually got the binary file and not an HTML WAF
# challenge page or a login/error page served with a 200 status.
FILE_MAGIC = {
    "pdf": b"%PDF",
    "xlsx": b"PK",
}

MONTH_ABBREV = {
    "january": "jan", "february": "feb", "march": "mar", "april": "apr",
    "may": "may", "june": "jun", "july": "jul", "august": "aug",
    "september": "sep", "october": "oct", "november": "nov", "december": "dec",
}


def load_sources() -> dict:
    with open(LINKS_FILE, "r") as f:
        return json.load(f)


def load_url_patterns() -> dict:
    if not URL_PATTERNS_FILE.exists():
        return {}
    try:
        with open(URL_PATTERNS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[url-cache] Ignoring unreadable {URL_PATTERNS_FILE.name}: {e}")
        return {}


def save_url_pattern(company_key: str, url: str, cal_data: dict, quarter: str):
    """Remember a working URL as a period-parameterised template."""
    template = templatize_url(url, cal_data, quarter)
    if template is None:
        return  # nothing period-specific in it, so it can't be re-rendered
    patterns = load_url_patterns()
    if patterns.get(company_key) == template:
        return
    patterns[company_key] = template
    try:
        paths.ensure(URL_PATTERNS_FILE.parent)
        with open(URL_PATTERNS_FILE, "w") as f:
            json.dump(patterns, f, indent=2, sort_keys=True)
        print(f"[{company_key}] Cached URL pattern -> {template}")
    except OSError as e:
        print(f"[{company_key}] Could not write URL pattern cache: {e}")


# Query-string markers of a pre-signed, single-file, time-limited URL (Azure
# SAS, S3 presigned, generic tokens). These can never be re-rendered for
# another period: the payload is one specific blob and the signature covers the
# very parameters a template would rewrite. Caching them produced a template
# with placeholders substituted into a SAS expiry timestamp, which then
# rendered a URL whose signature no longer matched.
_SIGNED_URL_MARKERS = ("sig=", "signature=", "x-amz-", "token=", "se=&", "&se=", "?se=")


def _is_signed_url(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in _SIGNED_URL_MARKERS)


def _period_tokens(cal_data: dict, quarter: str) -> list[tuple[str, str]]:
    """(literal, placeholder) pairs for the period spellings that can appear in
    a disclosure URL, longest-first so 'december' is replaced before 'dec' and
    the 4-digit year before any 2-digit form.

    Deliberately excludes a bare 2-digit year: "25" matches inside unrelated
    numbers (document ids, timestamps, paths), and substituting a placeholder
    there silently rewrites the URL to point at a different file. Only
    delimited 2-digit forms are safe, and those are covered by the FY labels
    below."""
    month = cal_data["month"]
    year = cal_data["year"]
    abbrev = MONTH_ABBREV[month.lower()]
    yy, next_yy = str(year)[-2:], str(year + 1)[-2:]
    return [
        (f"{yy}-{next_yy}", "{fy_short}"),
        (f"{year}-{next_yy}", "{fy_label}"),
        (month, "{month}"),
        (abbrev, "{mon}"),
        (str(year), "{year}"),
        (quarter, "{quarter}"),
    ]


def templatize_url(url: str, cal_data: dict, quarter: str) -> str | None:
    """Turn a working URL into a period-parameterised template, or None if it
    cannot safely be one.

    Only the path is templatised. The query string is left untouched because
    that is where signatures, tokens and API versions live - rewriting a date
    inside those either invalidates the signature or points at a different
    object. Pre-signed URLs are rejected outright for the same reason.
    """
    if _is_signed_url(url):
        return None
    parts = urlsplit(url)
    path = parts.path
    replaced = False
    for literal, placeholder in _period_tokens(cal_data, quarter):
        if not literal:
            continue
        pattern = re.compile(re.escape(literal), re.IGNORECASE)
        if pattern.search(path):
            path = pattern.sub(placeholder, path)
            replaced = True
    if not replaced:
        return None
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def render_url_template(template: str, cal_data: dict, quarter: str) -> list[str]:
    """Render a cached template for a period. Returns several casing variants,
    since insurers are inconsistent about capitalising month names in paths."""
    month = cal_data["month"]
    year = cal_data["year"]
    abbrev = MONTH_ABBREV[month.lower()]
    variants = []
    for month_form, mon_form, q_form in (
        (month, abbrev, quarter),
        (month.lower(), abbrev.lower(), quarter.lower()),
        (month.capitalize(), abbrev.capitalize(), quarter.upper()),
        (month.upper(), abbrev.upper(), quarter.upper()),
    ):
        rendered = (template
                    .replace("{month}", month_form)
                    .replace("{mon}", mon_form)
                    .replace("{quarter}", q_form)
                    .replace("{year}", str(year))
                    .replace("{fy_short}", f"{str(year)[-2:]}-{str(year + 1)[-2:]}")
                    .replace("{fy_label}", f"{year}-{str(year + 1)[-2:]}"))
        if rendered not in variants:
            variants.append(rendered)
    return variants


def ladder_for(data: dict) -> list[str]:
    """Per-company processor ladder, falling back to the shared default. Lets a
    single stubborn portal keep an expensive tier without every company paying
    for it."""
    ladder = data.get("processors")
    if isinstance(ladder, list) and ladder:
        return ladder
    return PROCESSOR_LADDER


def ensure_download_dir(fy: str, quarter: str) -> Path:
    target_dir = DOWNLOAD_DIR / fy / quarter
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


# 2. Financial Year Calendar Mapping
def get_calendar_mapping(fy: str, quarter: str) -> dict:
    """Maps FY26 Q3 to calendar year 2025, Month December"""
    return cfg.calendar_mapping(fy, quarter.upper())


# 3. Parallel Task API — structured output schema
# https://docs.parallel.ai/core-concepts/task-spec
def build_output_schema(file_type: str) -> dict:
    ext = file_type.lower()
    return {
        "type": "json",
        "json_schema": {
            "type": "object",
            "properties": {
                "found": {
                    "type": "boolean",
                    "description": (
                        "True only if a direct, working download link to the matching "
                        "document was located on the site."
                    ),
                },
                "download_url": {
                    "type": "string",
                    "description": (
                        f"The exact, direct download URL for the {file_type} file "
                        f"(should resolve to a binary .{ext} download, not an HTML "
                        f"landing page). Empty string if not found."
                    ),
                },
                "document_title": {
                    "type": "string",
                    "description": "The title, link text, or filename of the matched document as shown on the page.",
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "If not found: what you actually saw — e.g. which tabs/filters/categories "
                        "exist, whether a category showed 'No records found' or an API error, or "
                        "whether the period simply isn't published yet. Empty string if found."
                    ),
                },
            },
            "required": ["found", "download_url", "document_title", "notes"],
        },
    }


def build_objective(company_key: str, url: str, cal_data: dict, fy: str) -> tuple[str, str]:
    is_gic = company_key == "GIC"
    file_type = "XLSX" if is_gic else "PDF"

    anti_hallucination = (
        " Only return a URL that you can actually see rendered as a link or button in the "
        "page's own content — never invent or pattern-match a filename by copying the naming "
        "convention of a different month/quarter you did find, since that file may not exist. "
        "If the period requested isn't in the default/most-recent listing, look for an "
        "archive, 'previous years', or older-reports section before giving up."
    )

    if is_gic:
        objective = (
            f"Go to {url}, the General Insurance Council's segment-wise report page. "
            f"It lists monthly cumulative segment-wise premium data tables. Find the "
            f"row or link for the month of {cal_data['month']} {cal_data['year']} "
            f"(the cumulative figure for the quarter ending that month) and return the "
            f"direct Excel (.xlsx) download URL for that month." + anti_hallucination
        )
    else:
        # From the FY itself, not the quarter's calendar year - see cfg.fy_label.
        label = cfg.fy_label(fy)
        label_long = cfg.fy_label_long(fy)
        objective = (
            f"Go to {url}, the public disclosures page for this insurer. Find the "
            f"Quarterly Public Disclosure document for Financial Year {label} "
            f"(also written {label_long}), the "
            f"quarter ending {cal_data['month']} {cal_data['year']}. If the page has a "
            f"financial-year dropdown, filter tab, accordion section, or a document-category "
            f"filter (e.g. a 'Download Center' with categories like 'Public Disclosure' or "
            f"'Financial Results'), select or expand the one for FY {label} first. Ignore unrelated documents such "
            f"as stewardship/voting disclosures, investor presentations, media "
            f"clippings, or annual reports — only the quarterly public disclosure PDF "
            f"for this exact quarter counts. Return the direct PDF download URL." + anti_hallucination
        )
    return objective, file_type


async def resolve_download_url(
    company_key: str, url: str, cal_data: dict, processor: str, fy: str,
    feedback: str | None = None
) -> tuple[dict | None, str]:
    objective, file_type = build_objective(company_key, url, cal_data, fy)
    if feedback:
        objective += " " + feedback
    domain = urlparse(url).netloc

    task_run = await client.task_run.create(
        input=objective,
        processor=processor,
        task_spec={"output_schema": build_output_schema(file_type)},
        # Keep the agent on the insurer's own domain instead of wandering the web.
        # https://docs.parallel.ai (SourcePolicy.include_domains)
        source_policy={"include_domains": [domain]},
    )
    budget = PROCESSOR_TIMEOUTS.get(processor, DEFAULT_PROCESSOR_TIMEOUT)
    print(f"[{company_key}] Task submitted (run_id={task_run.run_id}, "
          f"processor={processor}, budget={budget}s), waiting for result...")

    # Bound the wait twice over: the SDK's own api_timeout, plus an outer
    # wait_for so a call that ignores or exceeds it still can't stall the
    # ladder. Abandoning the wait leaves the run going server-side, which is
    # fine - we simply stop paying wall-clock for it.
    result = await asyncio.wait_for(
        client.task_run.result(task_run.run_id, api_timeout=budget),
        timeout=budget + 15,
    )
    content = result.output.content

    if not content.get("found") or not content.get("download_url"):
        print(f"[{company_key}] Agent did not find a matching {file_type}. Notes: {content.get('notes')}")
        return None, file_type

    print(f"[{company_key}] Found: {content.get('document_title')!r} -> {content['download_url']}")
    return content, file_type


# How many leading PDF pages to read when validating. The IRDAI form
# schedules start after a cover/index page or two, so page 1 alone is not
# enough to confirm we actually got a disclosure bundle.
VALIDATION_PAGES = 6

# Every genuine IRDAI quarterly public disclosure is a bundle of numbered
# "FORM NL-n" schedules. Requiring at least one is what separates the real
# filing from the things that otherwise sail through a period-only check:
# the insurer's LISTED PARENT's stock-exchange results, a BSE covering
# letter, a different disclosure series ("Health Services Rendered"), or - as
# actually happened - a completely unrelated PDF returned by a web search.
_FORM_NL_RE = re.compile(r"FORM\s*NL-\s*\d+", re.I)

_MONTH_NUMBER = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def extract_lead_text(path: Path, ext: str, pages: int = VALIDATION_PAGES) -> str:
    """Text from the first `pages` PDF pages, or the XLSX title row (GIC's
    title row states the period covered)."""
    if ext == "pdf":
        reader = PdfReader(str(path))
        out = []
        for page in reader.pages[:pages]:
            out.append(page.extract_text() or "")
        return "\n".join(out)
    if ext == "xlsx":
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        first_row = next(wb.active.iter_rows(min_row=1, max_row=1, values_only=True), ())
        return " ".join(str(c) for c in first_row if c)
    return ""


def _mentions_period(text: str, cal_data: dict) -> bool:
    """Whether `text` states the target month AND year *together*.

    Requiring adjacency matters: a filing for a different quarter will often
    contain the target year somewhere (a maturity date, a signing date) and
    the target month somewhere else, so testing the two independently passes
    documents that cover an entirely different period."""
    month = cal_data["month"].lower()
    year = str(cal_data["year"])
    yy = year[-2:]
    mon = MONTH_ABBREV[month]
    mnum = _MONTH_NUMBER[month]

    # "June 2026", "30th JUNE , 2026", "Jun-26", "period ended June 30, 2026"
    named = re.compile(
        rf"\b({month}|{mon})\b[^0-9A-Za-z]{{0,4}}(\d{{1,2}}[^0-9A-Za-z]{{0,4}})?({year}|{yy})\b",
        re.I)
    # "30/06/2026", "30-6-26"
    numeric = re.compile(rf"\b\d{{1,2}}[/-]0?{mnum}[/-]({year}|{yy})\b")
    return bool(named.search(text) or numeric.search(text))


def validate_document(path: Path, ext: str, cal_data: dict, company_key: str) -> bool:
    """Confirm the downloaded file is the right DOCUMENT for the right PERIOD,
    not merely a well-formed PDF/XLSX that happens to mention the date.

    Two independent checks, because each alone lets real mistakes through:

      * period  - catches an insurer reusing or mislabelling a stale file
                  under the current quarter's link (seen with Galaxy Health,
                  whose "Q3"-named asset contained Sep-30 content).
      * form    - catches a right-period file that isn't the disclosure at
                  all. A period-only check accepted Aditya Birla *Capital*'s
                  quarterly results, a Star Health covering letter to BSE,
                  and an unrelated third-party PDF from web search - all of
                  which mention the quarter-end date somewhere.
    """
    try:
        text = extract_lead_text(path, ext)
    except Exception as e:
        print(f"[validate] Could not read {path.name} to verify it: {e}")
        return True  # don't block on a parser issue we can't attribute to content

    if not _mentions_period(text, cal_data):
        print(f"[{company_key}] Rejected {path.name}: does not state the period "
              f"{cal_data['month']} {cal_data['year']}.")
        return False

    # The GIC workbook is a statistics table, not a disclosure bundle, so the
    # form check doesn't apply to it - its title row already names the period.
    if ext == "xlsx":
        return True

    if not _FORM_NL_RE.search(text):
        print(f"[{company_key}] Rejected {path.name}: no 'FORM NL-n' schedule "
              f"in the first {VALIDATION_PAGES} pages, so this is not an IRDAI "
              f"public disclosure (wrong document, right date).")
        return False
    return True


# 4. Download the resolved file, with browser headers + a WAF-challenge check,
# then verify its content actually covers the requested quarter.
async def download_file(company_key: str, download_url: str, file_type: str, target_dir: Path,
                        referer: str, cal_data: dict, dest_name: str | None = None) -> Path | None:
    ext = file_type.lower()
    filename = cfg.download_filename(company_key, ext)
    # dest_name lets several candidate URLs be validated concurrently without
    # racing each other for the same output path; the winner is renamed to the
    # canonical filename by the caller.
    dest = target_dir / (dest_name or filename)
    headers = {**BROWSER_HEADERS, "Referer": referer}
    magic = FILE_MAGIC[ext]

    async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=headers) as http_client:
        for attempt in range(1, 3):
            try:
                resp = await http_client.get(download_url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                print(f"[{company_key}] Download error on attempt {attempt}/2: {e}")
                if attempt == 2:
                    return None
                await asyncio.sleep(2)
                continue

            if not resp.content.startswith(magic):
                print(
                    f"[{company_key}] Response did not look like a {file_type} "
                    f"(content-type={resp.headers.get('content-type')}); likely a WAF "
                    f"challenge or wrong link. Attempt {attempt}/2."
                )
                if attempt == 2:
                    return None
                await asyncio.sleep(2)
                continue

            async with aiofiles.open(dest, "wb") as f:
                await f.write(resp.content)

            if not validate_document(dest, ext, cal_data, company_key):
                print(f"[{company_key}] Discarded {file_type} from {download_url} "
                      f"(attempt {attempt}/2).")
                dest.unlink(missing_ok=True)
                if attempt == 2:
                    return None
                await asyncio.sleep(2)
                continue

            print(f"[{company_key}] Saved -> {dest}")
            return dest

    return None


# NOTE on "ultra": it was previously the third rung here because it was the only
# processor observed to interact with tabs/category filters rather than just read
# rendered content (Aditya Birla's Download Center). It has been dropped from the
# default ladder on cost grounds. If ABHI (or any portal) regresses, give just
# that company "processors": ["core", "pro", "ultra"] in source_links.json - the
# capability is retained, it simply isn't billed for all seven companies. The
# URL-pattern cache also blunts this: once a company resolves even once, later
# quarters reuse its pattern and never dispatch an agent at all.


def gic_deterministic_url(cal_data: dict) -> str:
    """GIC's segment-wise report follows a fixed, verified path — no need to
    gamble on the agent for a mechanically-named file:
    /Industry Statistics/Segmentwise report/Year_{year}/segment_{month}_{year}.xlsx
    """
    month = cal_data["month"].lower()
    year = cal_data["year"]
    return (
        "https://www.gicouncil.in/Industry%20Statistics/Segmentwise%20report/"
        f"Year_{year}/segment_{month}_{year}.xlsx"
    )


async def search_fallback(company_key: str, cal_data: dict, quarter: str, file_type: str, fy: str) -> list[str]:
    """Last resort when the Task agent can't resolve a link from the listing page
    itself (JS-tab-gated content, or the period isn't on the visible page at all):
    query Parallel's Search API, which draws on indexed/crawled web content rather
    than only what's reachable from the seed URL, and validate each candidate.
    https://docs.parallel.ai/search/search-quickstart
    """
    is_gic = company_key == "GIC"
    if is_gic:
        objective = f"Direct Excel download link for the GIC industry segment-wise report for {cal_data['month']} {cal_data['year']}"
        queries = [
            f"GIC segment wise report {cal_data['month']} {cal_data['year']} xlsx",
            "gicouncil.in segmentwise report download",
        ]
    else:
        objective = (
            f"Direct PDF download link for {company_key}'s quarterly public disclosure "
            f"for the quarter ended {cal_data['month']} {cal_data['year']} ({quarter})"
        )
        queries = [
            f"{company_key} public disclosure {cal_data['month']} {cal_data['year']} pdf",
            f"{company_key} {quarter} FY {cfg.fy_label(fy)} public disclosure pdf",
        ]

    print(f"[{company_key}] Querying Search API...")
    result = await asyncio.wait_for(
        client.search(objective=objective, search_queries=queries),
        timeout=SEARCH_TIMEOUT_SECONDS,
    )
    # Don't filter by the insurer's own domain here: disclosure PDFs are often
    # hosted off-domain on a CDN (e.g. Star Health's live on a *.cloudfront.net
    # bucket, not starhealth.in) — that's exactly the case Search is meant to
    # cover when the Task agent, restricted to the seed domain, can't resolve it.
    # download_file's magic-bytes + validate_period checks do the real filtering.
    candidates = [r.url for r in result.results[:8]]
    print(f"[{company_key}] Search candidates: {candidates}")
    return candidates


# 5. Agentic Task Execution per company, with a bounded self-correcting retry:
# if the agent's link fails the file-integrity check in download_file, we re-run
# the task telling it the previous URL was invalid instead of silently giving up.
# A Search API fallback runs if the Task agent never resolves a valid link at all.
async def try_candidates_concurrently(company_key: str, candidate_urls: list[str], file_type: str,
                                      target_dir: Path, referer: str, cal_data: dict) -> Path | None:
    """Download and validate several candidate URLs at once, keeping the
    BEST-RANKED one that yields a genuine, correct-period document.

    Candidates arrive in the caller's priority order (Search relevance, or
    cached-pattern-before-fallback). Validating them concurrently is purely a
    speed measure - the choice between two that both validate must still go by
    rank, never by which download happened to finish first. An earlier version
    took the first to complete, which made the result depend on network timing:
    with a weak validator that let several unrelated PDFs pass, it could and
    did pick the wrong document."""
    if not candidate_urls:
        return None
    ext = file_type.lower()
    final_name = cfg.download_filename(company_key, ext)

    async def attempt(i, cand):
        try:
            return await download_file(
                company_key, cand, file_type, target_dir, referer=referer,
                cal_data=cal_data, dest_name=f".{final_name}.cand{i}")
        except Exception as e:
            print(f"[{company_key}] Candidate {cand} errored: {e}")
            return None

    results = await asyncio.gather(
        *[attempt(i, c) for i, c in enumerate(candidate_urls)])

    winner = None
    for cand, path in zip(candidate_urls, results):
        if path is None:
            continue
        if winner is None:
            winner = (path, cand)
        else:
            path.unlink(missing_ok=True)  # validated, but out-ranked
    if winner is None:
        return None
    path, cand = winner
    final = target_dir / final_name
    path.replace(final)
    print(f"[{company_key}] Saved -> {final} (from {cand})")
    return final


async def process_company(company_key: str, data: dict, fy: str, quarter: str, target_dir: Path):
    url = data.get("quarterly_disclosure_page_link")
    cal_data = get_calendar_mapping(fy, quarter)
    is_gic = company_key == "GIC"
    file_type = "XLSX" if is_gic else "PDF"
    ext = file_type.lower()
    final_name = cfg.download_filename(company_key, ext)
    started = asyncio.get_event_loop().time()

    def record(method: str, detail: str = ""):
        ATTEMPT_LOG[company_key] = {
            "method": method,
            "detail": detail,
            "seconds": round(asyncio.get_event_loop().time() - started, 1),
        }

    # ---- Step 0: skip entirely if this period's file is already on disk ----
    dest = target_dir / final_name
    if dest.exists():
        record("already-downloaded")
        print(f"[{company_key}] {final_name} already present for {quarter} {fy} "
              f"- retrieval done, skipping agent dispatch.")
        return

    # ---- Step 1: cheap deterministic attempts, before any agent is billed ----
    # A cached pattern from a previous quarter, or GIC's known fixed path. Both
    # still go through download_file, so a stale pattern can't produce a wrong
    # file - it just fails and we fall through to the agent.
    direct_urls = []
    if is_gic:
        direct_urls.append(gic_deterministic_url(cal_data))
    cached = load_url_patterns().get(company_key)
    if cached:
        direct_urls.extend(render_url_template(cached, cal_data, quarter))

    if direct_urls:
        print(f"\n[{company_key}] Trying {len(direct_urls)} direct URL(s) before dispatching an agent...")
        dest = await try_candidates_concurrently(
            company_key, direct_urls, file_type, target_dir, referer=url, cal_data=cal_data)
        if dest is not None:
            record("direct-url", "gic-pattern" if is_gic else "cached-pattern")
            return
        print(f"[{company_key}] No direct URL worked; falling back to the agent.")

    ladder = ladder_for(data)

    async def run_processor(processor: str, feedback: str | None, dest_name: str | None = None):
        """Resolve + download for one processor. Returns (path, content).

        During the race, dest_name keeps this write off the canonical filename
        so it cannot interleave with the Search branch promoting its own temp
        file to the same path; the winner is promoted by the caller."""
        content, resolved_file_type = await resolve_download_url(
            company_key, url, cal_data, processor, fy, feedback)
        if content is None:
            return None, None
        path = await download_file(company_key, content["download_url"], resolved_file_type,
                                   target_dir, referer=url, cal_data=cal_data,
                                   dest_name=dest_name)
        return path, content

    async def run_search():
        candidates = await search_fallback(company_key, cal_data, quarter, file_type, fy)
        return await try_candidates_concurrently(
            company_key, candidates, file_type, target_dir, referer=url, cal_data=cal_data)

    # ---- Step 2: first processor raced against Search ----
    feedback = None
    searched = False
    if RACE_SEARCH_WITH_FIRST_PROCESSOR and ladder:
        searched = True
        print(f"\n[{company_key}] Racing processor {ladder[0]!r} against the Search API...")
        proc_task = asyncio.create_task(
            run_processor(ladder[0], None, dest_name=f".{final_name}.agent"))
        search_task = asyncio.create_task(run_search())
        labels = {proc_task: f"processor:{ladder[0]}", search_task: "search"}
        pending = {proc_task, search_task}
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        outcome = task.result()
                    except Exception as e:
                        print(f"[{company_key}] {labels[task]} failed: {e}")
                        continue
                    path = outcome[0] if isinstance(outcome, tuple) else outcome
                    if path is not None:
                        for other in pending:
                            other.cancel()
                        if task is proc_task:
                            # Promote the agent branch's temp file now that it
                            # has won uncontested.
                            path.replace(target_dir / final_name)
                            print(f"[{company_key}] Saved -> {target_dir / final_name}")
                            save_url_pattern(company_key, outcome[1]["download_url"], cal_data, quarter)
                        record(labels[task])
                        return
                    if task is proc_task and isinstance(outcome, tuple) and outcome[1] is not None:
                        feedback = _retry_feedback(outcome[1], cal_data)
        finally:
            for task in (proc_task, search_task):
                if not task.done():
                    task.cancel()
            # If the agent branch downloaded a file but Search got there first,
            # its temp file is now orphaned.
            (target_dir / f".{final_name}.agent").unlink(missing_ok=True)
        ladder = ladder[1:]

    # ---- Step 3: remaining processors, escalating ----
    for attempt, processor in enumerate(ladder, start=1):
        print(f"\n[{company_key}] Dispatching Parallel AI agent -> {url} "
              f"(processor {processor}, {attempt}/{len(ladder)})")
        try:
            path, content = await run_processor(processor, feedback)
        except asyncio.TimeoutError:
            print(f"[{company_key}] Processor {processor} exceeded its "
                  f"{PROCESSOR_TIMEOUTS.get(processor, DEFAULT_PROCESSOR_TIMEOUT)}s budget; escalating.")
            continue
        except Exception as e:
            print(f"[{company_key}] Agent task failed: {e}")
            continue
        if path is not None:
            save_url_pattern(company_key, content["download_url"], cal_data, quarter)
            record(f"processor:{processor}")
            return
        if content is not None:
            feedback = _retry_feedback(content, cal_data)

    # ---- Step 4: Search, if it wasn't already raced ----
    if not searched:
        try:
            dest = await run_search()
        except Exception as e:
            print(f"[{company_key}] Search fallback failed: {e}")
            dest = None
        if dest is not None:
            record("search")
            return

    record("failed")
    print(f"[{company_key}] Giving up — no valid {file_type} could be located or downloaded.")


def _retry_feedback(content: dict, cal_data: dict) -> str:
    return (
        f"IMPORTANT: A previous attempt returned {content['download_url']!r} "
        f"(titled {content.get('document_title')!r}), but that either failed to download "
        f"or, once downloaded, did not actually cover the quarter ended "
        f"{cal_data['month']} {cal_data['year']} (it may have been a different quarter's "
        f"file mislabeled or reused under this link). Do not repeat that URL or guess "
        f"another one by copying its naming pattern; find a link that is genuinely present "
        f"in the page's rendered content and confirm the document itself states the correct "
        f"quarter-ending date before returning it."
    )


async def main(fy: str, quarter: str, companies: list[str] | None = None):
    print(f"Starting Phase 1 Agentic Retrieval for {quarter} {fy}...\n")
    sources = load_sources()
    if companies is not None:
        selected = set(companies)
        unknown = selected - set(sources)
        if unknown:
            print(f"[main] Ignoring unknown company key(s): {sorted(unknown)}")
        sources = {k: v for k, v in sources.items() if k in selected}
    target_dir = ensure_download_dir(fy, quarter)

    started = asyncio.get_event_loop().time()
    tasks = [process_company(company_key, data, fy, quarter, target_dir) for company_key, data in sources.items()]
    await asyncio.gather(*tasks)
    elapsed = asyncio.get_event_loop().time() - started

    # Which route actually won, per company. Read this over a few quarters to
    # decide whether a processor still earns its place in the ladder - anything
    # that never appears here is pure cost and latency.
    print(f"\n=== Phase 1 retrieval summary ({elapsed:.1f}s wall clock) ===")
    print(f"{'Company':<22}{'Resolved by':<22}{'Detail':<18}{'Seconds':>8}")
    for company_key in sources:
        info = ATTEMPT_LOG.get(company_key, {"method": "not run", "detail": "", "seconds": 0.0})
        print(f"{company_key:<22}{info['method']:<22}{info['detail']:<18}{info['seconds']:>8.1f}")

    by_method = Counter(i["method"] for i in ATTEMPT_LOG.values())
    print("\nBy route: " + ", ".join(f"{m}={n}" for m, n in sorted(by_method.items())))
    failed = [c for c, i in ATTEMPT_LOG.items() if i["method"] == "failed"]
    if failed:
        print(f"FAILED ({len(failed)}): {', '.join(failed)}")
    print(f"\nPhase 1 Agentic Execution Complete.")


if __name__ == "__main__":
    TARGET_FY = "FY25-26"
    TARGET_QUARTER = "Q3"
    asyncio.run(main(TARGET_FY, TARGET_QUARTER))
