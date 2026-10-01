"""
Fully LLM-driven report generation: given the Data Engine workbook and a
detailed system prompt (prompts/competition_report_system_prompt.md), the
LLM decides structure, what to feature, commentary, and Chart.js chart
configs - none of it is a fixed Python template.

Deliberately NOT built on report.py/report_v2.py's page-by-page pipeline:
those assume a fixed slide list computed the same way every quarter, which
is the wrong shape for a report meant to read like an analyst's judgment
call about what's notable THIS quarter.

One page per LLM call, not one call for the whole report. Empirically, a
single call asked to produce the full ~33-page report (or even one 5-slide
chapter) failed reliably with a 503 "high demand" error from the model,
while a single slide's worth of output succeeded reliably - the failure
tracks the SIZE of what's being generated, not the data payload or the
system prompt (both of those alone, with a trivial requested output,
succeeded fine). So the report is built as one call per Slide #, run
concurrently under a shared rate limit with retry (gemini.py's own
_is_retryable/_retry_delay_from - its docstring notes this exact 503
already took down a real run elsewhere in this codebase and a bare retry
fixed it), then assembled.

The one thing that stays deterministic is the numbers: nothing here trusts
the model's arithmetic. _verify_numbers re-extracts every number the model
wrote (from both the assembled report's visible text/tables and its
Chart.js `data:` arrays) and confirms each one traces back to a real Data
Engine cell, flagging anything that doesn't for human review rather than
silently shipping it.
"""
import asyncio
import hashlib
import json
import re
from html.parser import HTMLParser

from google.genai import types

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis import logging_setup
from competitor_analysis.extraction import gemini as gemini_extract
from competitor_analysis.extraction.gemini import RateLimiter, _is_retryable, _retry_delay_from
from competitor_analysis.reporting import data
from competitor_analysis.reporting.validate import extract_numbers, _flatten_numbers, _normalize

log = logging_setup.get_logger(__name__)

MODEL = "gemini-flash-latest"
CACHE_ROOT = paths.GEMINI_CACHE / "report_llm"

# Provenance-only columns the prompt never asks the model to cite, and the
# CUR/PRIOR aliases data.load_rows() adds for report.py's convenience -
# redundant with the real period-header columns the prompt is told to read,
# so dropping them keeps the payload smaller without losing anything.
_DROP_COLUMNS = {"Source Tab", "Link to Source document", "CUR", "PRIOR"}


def _rows_to_json(rows):
    return [{k: v for k, v in r.items() if k not in _DROP_COLUMNS} for r in rows]


def _load_system_prompt():
    path = paths.PROMPTS_DIR / "competition_report_system_prompt.md"
    return path.read_text(encoding="utf-8")


def _extract_html(text):
    """Strip a ```html ... ``` fence if the model wrapped its output in one,
    despite being asked for a bare fragment/file."""
    text = text.strip()
    m = re.match(r"^```(?:html)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


# ---------------------------------------------------------------------------
# Gemini calls: cached on disk (same pattern as narrative.py), retried on a
# transient server error, rate-limited across all concurrent calls.
# ---------------------------------------------------------------------------

def _cache_path(key: str):
    d = CACHE_ROOT / (cfg.FY or "unset") / (cfg.QUARTER or "unset")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def _cache_key(prompt_name: str, payload) -> str:
    blob = json.dumps({"model": MODEL, "period": [cfg.FY, cfg.QUARTER],
                       "prompt_name": prompt_name, "payload": payload},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


async def _call_gemini_html(prompt_name, user_message, system_prompt, limiter,
                            max_output_tokens=16384, max_attempts=6):
    path = _cache_path(_cache_key(prompt_name, user_message))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["text"]

    for attempt in range(max_attempts):
        await limiter.acquire()
        try:
            resp = await asyncio.to_thread(
                gemini_extract.client().models.generate_content,
                model=MODEL, contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt, temperature=0.2,
                    max_output_tokens=max_output_tokens,
                ),
            )
            text = (resp.text or "").strip()
            path.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
            return text
        except Exception as e:
            if not _is_retryable(e) or attempt == max_attempts - 1:
                raise
            delay = _retry_delay_from(e, attempt)
            log.info("%s: retryable error (%s), retrying in %.1fs (attempt %d/%d)...",
                      prompt_name, type(e).__name__, delay, attempt + 1, max_attempts)
            await asyncio.sleep(delay)


async def _generate_slide_page(slide_no, slide_rows, system_prompt, limiter):
    category = slide_rows[0].get("Category", "")
    payload = _rows_to_json(slide_rows)
    user_msg = (
        f"Build ONLY the report page(s) for Slide #{slide_no} (chapter: {category}) of the "
        "Competition Analysis report, as one or more HTML page `<div>`s - no `<html>`/`<head>` "
        "wrapper, no cover, no table of contents, no other slide.\n\n"
        f"DATA ENGINE ROWS FOR THIS SLIDE ONLY (JSON):\n{json.dumps(payload, default=str)}"
    )
    text = await _call_gemini_html(f"slide_{slide_no}", user_msg, system_prompt, limiter)
    return _extract_html(text)


async def _generate_key_highlights(rows, system_prompt, limiter):
    payload = _rows_to_json(rows)
    user_msg = (
        "Build ONLY the Key Highlights page (see §5) of the Competition Analysis report, "
        "as one HTML page `<div>` - no other page, no cover, no TOC.\n\n"
        f"FULL DATA ENGINE (JSON):\n{json.dumps(payload, default=str)}"
    )
    text = await _call_gemini_html("key_highlights", user_msg, system_prompt, limiter,
                                   max_output_tokens=8192)
    return _extract_html(text)


async def _generate_front_and_back_matter(rows, system_prompt, limiter):
    payload = _rows_to_json(rows)
    user_msg = (
        "Build ONLY the front matter (Cover, Table of Contents) and back matter (Glossary/Notes) "
        "of the Competition Analysis report, as HTML page `<div>`s, in that order - no chapter "
        "pages, no Key Highlights.\n\n"
        f"FULL DATA ENGINE (JSON):\n{json.dumps(payload, default=str)}"
    )
    text = await _call_gemini_html("front_back_matter", user_msg, system_prompt, limiter,
                                   max_output_tokens=8192)
    return _extract_html(text)


async def _generate_all(rows, system_prompt, limiter):
    by_slide = {}
    for r in rows:
        by_slide.setdefault(r["Slide #"], []).append(r)
    slide_nos = sorted(by_slide)

    front_back_task = _generate_front_and_back_matter(rows, system_prompt, limiter)
    slide_tasks = [_generate_slide_page(sn, by_slide[sn], system_prompt, limiter) for sn in slide_nos]
    highlights_task = _generate_key_highlights(rows, system_prompt, limiter)

    front_back, *slide_results_and_highlights = await asyncio.gather(
        front_back_task, *slide_tasks, highlights_task, return_exceptions=True)
    *slide_pages, highlights = slide_results_and_highlights

    if isinstance(front_back, Exception):
        raise front_back  # front/back matter failing is fatal - no report without a cover

    ordered_pages = []
    for sn, page in zip(slide_nos, slide_pages):
        if isinstance(page, Exception):
            log.error("Slide %s: failed after retries (%s); omitting from report.", sn, page)
            continue
        if page.strip():
            ordered_pages.append(page)

    if isinstance(highlights, Exception):
        log.error("Key Highlights: failed after retries (%s); omitting.", highlights)
        highlights = ""

    return front_back, ordered_pages, highlights


# ---------------------------------------------------------------------------
# Number verification: re-extracts every number the model wrote (from
# visible text AND Chart.js `data:` arrays) and confirms it traces to a
# real Data Engine cell.
# ---------------------------------------------------------------------------

class _HtmlSplitter(HTMLParser):
    """Splits HTML into (visible text, script text) so verification checks
    the right things: the prose/table content a reader sees, and a
    Chart.js config's actual `data:` arrays - while ignoring CSS and
    non-data JS (chart type strings, colors, options), which would
    otherwise flood the check with numbers that were never claims about
    the business (a `14px` font size, a `0.5` opacity)."""

    def __init__(self):
        super().__init__()
        self._in_script = False
        self._in_style = False
        self.text_parts = []
        self.script_parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._in_script = True
        elif tag == "style":
            self._in_style = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data_):
        if self._in_script:
            self.script_parts.append(data_)
        elif not self._in_style:
            self.text_parts.append(data_)


_CHART_DATA_ARRAY_RE = re.compile(r"\bdata\s*:\s*\[([^\]]*)\]")
_NUM_RE = re.compile(r"-?\d[\d.]*")


def _numbers_from_chart_configs(script_text: str) -> set[str]:
    """Pulls only the numeric literals out of Chart.js `data: [...]` arrays
    - not `labels: [...]` (company/category name arrays) and not the rest of
    the config (chart `type`, colors, options)."""
    found = set()
    for m in _CHART_DATA_ARRAY_RE.finditer(script_text):
        for n in _NUM_RE.findall(m.group(1)):
            found.add(_normalize(n))
    return found


def _verify_numbers(html: str, source_rows: list[dict]) -> list[str]:
    """Every number appearing in the report's visible text/tables or its
    chart data arrays that doesn't trace back (in any tolerated rounding/
    formatting - see validate.py) to a real Data Engine row. An empty list
    means every number the model wrote is traceable to real data."""
    splitter = _HtmlSplitter()
    splitter.feed(html)
    stated = extract_numbers("".join(splitter.text_parts))
    stated |= _numbers_from_chart_configs("".join(splitter.script_parts))

    allowed = _flatten_numbers(source_rows)
    suspicious = []
    for tok in stated:
        bare = tok.lstrip("-")
        if len(bare.replace(".", "")) < 2:
            continue
        if tok not in allowed:
            suspicious.append(tok)
    return sorted(suspicious)


_PAGE_CSS = """
body { margin: 0; background: #e5e7eb; font-family: -apple-system, BlinkMacSystemFont,
       'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
body > div { margin: 24px auto; }
@media print { body { background: #fff; } body > div { margin: 0; page-break-after: always; } }
"""


def assemble_document(front_back_html, page_htmls, highlights_html):
    """Deterministic, LLM-free: just concatenates already-generated HTML
    fragments into one file with a shared print stylesheet. No content
    decisions happen here."""
    parts = [front_back_html]
    if highlights_html:
        parts.append(highlights_html)
    parts.extend(page_htmls)
    body = "\n".join(parts)
    return f"<!doctype html>\n<html><head><meta charset=\"utf-8\"><style>{_PAGE_CSS}</style></head><body>{body}</body></html>"


def generate_report(data_engine_path=None, out_path=None):
    """Calls the LLM once per Slide # (plus front/back matter and Key
    Highlights), assembles the results, saves the HTML, and returns
    (out_path, suspicious_numbers) - `suspicious_numbers` is non-empty
    exactly when something in the report couldn't be traced back to real
    data and needs human review before the report is trusted."""
    rows = data.load_rows(data_engine_path)
    system_prompt = _load_system_prompt()
    limiter = RateLimiter(gemini_extract.GEMINI_RPM)

    front_back, pages, highlights = asyncio.run(_generate_all(rows, system_prompt, limiter))
    html_doc = assemble_document(front_back, pages, highlights)

    if out_path is None:
        out_path = str(paths.OUTPUT_DIR / f"Competition_Analysis_{cfg.FY}_{cfg.QUARTER}.html")
    paths.ensure_parent(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    suspicious = _verify_numbers(html_doc, rows)
    if suspicious:
        log.warning("%d number(s) in the report could not be traced to the Data Engine - "
                    "review before use: %s", len(suspicious), suspicious)
    else:
        log.info("Every number in the report traces back to the Data Engine.")

    log.info("Saved %s (%d slide pages)", out_path, len(pages))
    return out_path, suspicious
