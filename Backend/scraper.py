import os
import json
import asyncio
from pathlib import Path
from urllib.parse import urlparse

import httpx
import aiofiles
import openpyxl
from pypdf import PdfReader
from parallel import AsyncParallel  # Parallel AI Python SDK
from dotenv import load_dotenv

load_dotenv()

# 1. Setup paths & client
BASE_DIR = Path(__file__).parent
LINKS_FILE = BASE_DIR / "source_links.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"

client = AsyncParallel(api_key=os.getenv("PARALLEL_API_KEY"))

# "core" gives more reasoning depth than "base" for navigating dropdown/accordion
# filters before the real file link resolves in the DOM.
# https://docs.parallel.ai/task-api/guides/choose-a-processor
PROCESSOR = "core"

# api_timeout for task_run.result(): how long we're willing to block/poll for the
# agent to finish navigating a slow, JS-heavy, WAF-protected page.
RESULT_TIMEOUT_SECONDS = 1800

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


def ensure_download_dir(fy: str, quarter: str) -> Path:
    target_dir = DOWNLOAD_DIR / fy / quarter
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


# 2. Financial Year Calendar Mapping
def get_calendar_mapping(fy: str, quarter: str) -> dict:
    """Maps FY26 Q3 to calendar year 2025, Month December"""
    fy_end_year = int(fy.replace("FY", "20"))
    start_year = fy_end_year - 1

    mapping = {
        "Q1": {"month": "June", "year": start_year},
        "Q2": {"month": "September", "year": start_year},
        "Q3": {"month": "December", "year": start_year},
        "Q4": {"month": "March", "year": fy_end_year},
    }
    return mapping[quarter.upper()]


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


def build_objective(company_key: str, url: str, cal_data: dict) -> tuple[str, str]:
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
        fy_label = f"{cal_data['year']}-{str(cal_data['year'] + 1)[-2:]}"
        objective = (
            f"Go to {url}, the public disclosures page for this insurer. Find the "
            f"Quarterly Public Disclosure document for Financial Year {fy_label}, the "
            f"quarter ending {cal_data['month']} {cal_data['year']}. If the page has a "
            f"financial-year dropdown, filter tab, accordion section, or a document-category "
            f"filter (e.g. a 'Download Center' with categories like 'Public Disclosure' or "
            f"'Financial Results'), select or expand the one for FY {fy_label} first. Ignore unrelated documents such "
            f"as stewardship/voting disclosures, investor presentations, media "
            f"clippings, or annual reports — only the quarterly public disclosure PDF "
            f"for this exact quarter counts. Return the direct PDF download URL." + anti_hallucination
        )
    return objective, file_type


async def resolve_download_url(
    company_key: str, url: str, cal_data: dict, processor: str, feedback: str | None = None
) -> tuple[dict | None, str]:
    objective, file_type = build_objective(company_key, url, cal_data)
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
    print(f"[{company_key}] Task submitted (run_id={task_run.run_id}, processor={processor}), waiting for result...")

    result = await client.task_run.result(task_run.run_id, api_timeout=RESULT_TIMEOUT_SECONDS)
    content = result.output.content

    if not content.get("found") or not content.get("download_url"):
        print(f"[{company_key}] Agent did not find a matching {file_type}. Notes: {content.get('notes')}")
        return None, file_type

    print(f"[{company_key}] Found: {content.get('document_title')!r} -> {content['download_url']}")
    return content, file_type


def extract_lead_text(path: Path, ext: str) -> str:
    """Pull enough text to sanity-check the reporting period: PDF page 1, or the
    first row of the XLSX sheet (GIC's title row states the period covered)."""
    if ext == "pdf":
        reader = PdfReader(str(path))
        return reader.pages[0].extract_text() or ""
    if ext == "xlsx":
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        first_row = next(wb.active.iter_rows(min_row=1, max_row=1, values_only=True), ())
        return " ".join(str(c) for c in first_row if c)
    return ""


def validate_period(path: Path, ext: str, cal_data: dict) -> bool:
    """Confirm the downloaded file's own text actually mentions the requested
    quarter-ending month/year, not just that it's a well-formed PDF/XLSX.

    This catches insurers whose site mislabels or reuses a stale file under the
    current quarter's link/filename (observed with Galaxy Health, whose "Q3"-named
    asset actually contained "QUARTER ENDED SEP 30, 2025" content).
    """
    try:
        text = extract_lead_text(path, ext).lower()
    except Exception as e:
        print(f"[validate_period] Could not read {path.name} to verify period: {e}")
        return True  # don't block on a parser issue we can't attribute to wrong content

    month = cal_data["month"].lower()
    year = str(cal_data["year"])
    if year not in text:
        return False
    return month in text or MONTH_ABBREV[month] in text


# 4. Download the resolved file, with browser headers + a WAF-challenge check,
# then verify its content actually covers the requested quarter.
async def download_file(company_key: str, download_url: str, file_type: str, target_dir: Path, referer: str, cal_data: dict) -> Path | None:
    ext = file_type.lower()
    filename = f"{company_key.replace(' ', '_').replace('&', 'and')}.{ext}"
    dest = target_dir / filename
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

            if not validate_period(dest, ext, cal_data):
                print(
                    f"[{company_key}] Downloaded {file_type} does not mention "
                    f"{cal_data['month']} {cal_data['year']} — wrong quarter/stale file. "
                    f"Attempt {attempt}/2."
                )
                dest.unlink(missing_ok=True)
                if attempt == 2:
                    return None
                await asyncio.sleep(2)
                continue

            print(f"[{company_key}] Saved -> {dest}")
            return dest

    return None


# Escalating processor per retry. "core"/"pro" only reason over already-rendered
# page content, but "ultra" was empirically confirmed (Aditya Birla's Download
# Center category filter) to actually interact with tabs/category filters rather
# than just read static content — worth the extra cost as a last resort before
# falling back to Search.
RETRY_PROCESSORS = [PROCESSOR, "pro", "ultra"]


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


async def search_fallback(company_key: str, cal_data: dict, quarter: str, file_type: str) -> list[str]:
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
            f"{company_key} {quarter} FY{str(cal_data['year'])[-2:]} disclosure pdf",
        ]

    print(f"[{company_key}] Falling back to Search API...")
    result = await client.search(objective=objective, search_queries=queries)
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
async def process_company(company_key: str, data: dict, fy: str, quarter: str, target_dir: Path):
    url = data.get("quarterly_disclosure_page_link")
    cal_data = get_calendar_mapping(fy, quarter)
    is_gic = company_key == "GIC"
    file_type = "XLSX" if is_gic else "PDF"

    if is_gic:
        print(f"\n[GIC] Trying known deterministic URL pattern first...")
        dest = await download_file(company_key, gic_deterministic_url(cal_data), file_type, target_dir, referer=url, cal_data=cal_data)
        if dest is not None:
            return

    feedback = None
    for attempt, processor in enumerate(RETRY_PROCESSORS, start=1):
        print(f"\n[{company_key}] Dispatching Parallel AI agent -> {url} (attempt {attempt}/{len(RETRY_PROCESSORS)})")
        try:
            content, resolved_file_type = await resolve_download_url(company_key, url, cal_data, processor, feedback)
        except Exception as e:
            print(f"[{company_key}] Agent task failed: {e}")
            content = None
            resolved_file_type = file_type

        if content is not None:
            dest = await download_file(company_key, content["download_url"], resolved_file_type, target_dir, referer=url, cal_data=cal_data)
            if dest is not None:
                return

            feedback = (
                f"IMPORTANT: A previous attempt returned {content['download_url']!r} "
                f"(titled {content.get('document_title')!r}), but that either failed to download "
                f"or, once downloaded, did not actually cover the quarter ended "
                f"{cal_data['month']} {cal_data['year']} (it may have been a different quarter's "
                f"file mislabeled or reused under this link). Do not repeat that URL or guess "
                f"another one by copying its naming pattern; find a link that is genuinely present "
                f"in the page's rendered content and confirm the document itself states the correct "
                f"quarter-ending date before returning it."
            )

    try:
        fallback_candidates = await search_fallback(company_key, cal_data, quarter, file_type)
    except Exception as e:
        print(f"[{company_key}] Search fallback failed: {e}")
        fallback_candidates = []

    for candidate_url in fallback_candidates:
        dest = await download_file(company_key, candidate_url, file_type, target_dir, referer=url, cal_data=cal_data)
        if dest is not None:
            return

    print(f"[{company_key}] Giving up — no valid {file_type} could be located or downloaded.")


async def main(fy: str, quarter: str):
    print(f"Starting Phase 1 Agentic Retrieval for {quarter} {fy}...\n")
    sources = load_sources()
    target_dir = ensure_download_dir(fy, quarter)

    tasks = [process_company(company_key, data, fy, quarter, target_dir) for company_key, data in sources.items()]
    await asyncio.gather(*tasks)
    print(f"\nPhase 1 Agentic Execution Complete.")


if __name__ == "__main__":
    TARGET_FY = "FY26"
    TARGET_QUARTER = "Q3"
    asyncio.run(main(TARGET_FY, TARGET_QUARTER))
