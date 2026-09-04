"""Tests for Phase 1 retrieval latency/cost optimisations in scraper.py.

These are pure-logic tests - no network, no Parallel AI calls.

Run:  myenv/Scripts/python.exe -m pytest test_scraper_retrieval.py -v
"""
import asyncio

import pytest

from competitor_analysis import config as cfg

from competitor_analysis.ingestion import scraper  # noqa: E402

Q3 = {"month": "December", "year": 2025}
Q1_NEXT = {"month": "June", "year": 2026}


# ---------------------------------------------------------------------------
# Processor ladder / cost controls
# ---------------------------------------------------------------------------

def test_default_ladder_excludes_ultra():
    """'ultra' costs materially more and was the third sequential attempt.
    It must not be in the default ladder."""
    assert "ultra" not in scraper.PROCESSOR_LADDER
    assert scraper.PROCESSOR_LADDER == ["core", "pro"]


def test_ultra_remains_available_per_company():
    """Dropping ultra from the default must not remove the capability - a
    single stubborn portal can still opt in via source_links.json."""
    assert scraper.ladder_for({"processors": ["core", "pro", "ultra"]}) == \
        ["core", "pro", "ultra"]
    # Absent/empty config falls back to the shared default.
    assert scraper.ladder_for({}) == scraper.PROCESSOR_LADDER
    assert scraper.ladder_for({"processors": []}) == scraper.PROCESSOR_LADDER
    assert "ultra" in scraper.PROCESSOR_TIMEOUTS


def test_processor_timeouts_are_bounded():
    """The old single 1800s ceiling let one hung run dominate Phase 1. Every
    per-processor budget must be far below that, and escalate with cost."""
    assert scraper.PROCESSOR_TIMEOUTS["core"] < scraper.PROCESSOR_TIMEOUTS["pro"]
    assert scraper.PROCESSOR_TIMEOUTS["pro"] < scraper.PROCESSOR_TIMEOUTS["ultra"]
    for name, budget in scraper.PROCESSOR_TIMEOUTS.items():
        assert budget <= 600, f"{name} budget {budget}s is too generous"
    assert scraper.SEARCH_TIMEOUT_SECONDS <= 180
    assert not hasattr(scraper, "RESULT_TIMEOUT_SECONDS"), \
        "the unbounded global timeout is back"


def test_worst_case_ladder_is_bounded():
    """Total agent wall-clock for a fully-failing company should be minutes,
    not the ~90 the old core->pro->ultra x 1800s ladder allowed."""
    worst = sum(scraper.PROCESSOR_TIMEOUTS[p] for p in scraper.PROCESSOR_LADDER)
    worst += scraper.SEARCH_TIMEOUT_SECONDS
    assert worst < 900, f"worst case still {worst}s"


# ---------------------------------------------------------------------------
# URL pattern cache
# ---------------------------------------------------------------------------

def test_templatize_replaces_period_tokens():
    url = "https://x.com/disclosures/2025/december/Q3_public_disclosure_dec2025.pdf"
    t = scraper.templatize_url(url, Q3, "Q3")
    assert t is not None
    for literal in ("2025", "december", "dec", "Q3"):
        assert literal not in t, f"{literal!r} survived templatization: {t}"
    assert "{year}" in t and "{month}" in t


def test_templatize_returns_none_when_nothing_period_specific():
    """A URL with no period tokens can't be re-rendered, so it must not be
    cached as a template."""
    assert scraper.templatize_url(
        "https://x.com/static/latest_disclosure.pdf", Q3, "Q3") is None


def test_template_round_trip_renders_next_period():
    """The whole point of the cache: a URL that worked this quarter renders to
    the right URL next quarter with no agent call."""
    url = "https://x.com/pd/2025/december/disclosure_december_2025.pdf"
    template = scraper.templatize_url(url, Q3, "Q3")

    # Re-rendering for the SAME period must reproduce the original exactly.
    assert url in scraper.render_url_template(template, Q3, "Q3")

    # And for the next period it must produce the correctly-substituted URL.
    nxt = scraper.render_url_template(template, Q1_NEXT, "Q1")
    assert "https://x.com/pd/2026/june/disclosure_june_2026.pdf" in nxt
    for candidate in nxt:
        assert "2025" not in candidate and "december" not in candidate.lower()


def test_render_offers_casing_variants():
    """Insurers are inconsistent about capitalising month names in paths."""
    template = scraper.templatize_url(
        "https://x.com/PD/December2025.pdf", Q3, "Q3")
    variants = scraper.render_url_template(template, Q3, "Q3")
    lowered = [v.lower() for v in variants]
    assert len(set(lowered)) >= 1
    assert any("December" in v for v in variants)
    assert any("december" in v for v in variants)


def test_url_pattern_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "URL_PATTERNS_FILE", tmp_path / "url_patterns.json")
    assert scraper.load_url_patterns() == {}
    scraper.save_url_pattern(
        "ACME", "https://a.com/2025/december/x.pdf", Q3, "Q3")
    patterns = scraper.load_url_patterns()
    assert "ACME" in patterns
    rendered = scraper.render_url_template(patterns["ACME"], Q1_NEXT, "Q1")
    assert any("2026" in r and "june" in r.lower() for r in rendered)


def test_unreadable_cache_is_ignored_not_fatal(tmp_path, monkeypatch):
    bad = tmp_path / "url_patterns.json"
    bad.write_text("{not json")
    monkeypatch.setattr(scraper, "URL_PATTERNS_FILE", bad)
    assert scraper.load_url_patterns() == {}


# ---------------------------------------------------------------------------
# Concurrent candidate validation
# ---------------------------------------------------------------------------

def test_candidates_are_validated_concurrently(tmp_path, monkeypatch):
    """Eight dead Search candidates used to cost eight sequential
    download+parse cycles. They must now overlap, and the first genuine hit
    must win and be promoted to the canonical filename."""
    order = []

    async def fake_download(company_key, download_url, file_type, target_dir,
                            referer, cal_data, dest_name=None):
        order.append(download_url)
        # The good candidate is deliberately the slowest, so a sequential
        # implementation would still pass - what we assert is the overlap.
        if download_url == "good":
            await asyncio.sleep(0.05)
            path = target_dir / dest_name
            path.write_bytes(b"%PDF-ok")
            return path
        await asyncio.sleep(0.01)
        return None

    monkeypatch.setattr(scraper, "download_file", fake_download)
    cands = [f"bad{i}" for i in range(7)] + ["good"]

    result = asyncio.run(scraper.try_candidates_concurrently(
        "ACME", cands, "PDF", tmp_path, referer="r", cal_data=Q3))

    assert result is not None
    assert result.name == "ACME.pdf", "winner was not promoted to the canonical name"
    assert result.read_bytes() == b"%PDF-ok"
    # All candidates were dispatched before the slow winner resolved.
    assert len(order) == 8
    # No temp candidate files left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["ACME.pdf"]


def test_no_candidates_returns_none(tmp_path):
    assert asyncio.run(scraper.try_candidates_concurrently(
        "ACME", [], "PDF", tmp_path, referer="r", cal_data=Q3)) is None


def test_all_candidates_failing_returns_none_and_cleans_up(tmp_path, monkeypatch):
    async def fake_download(*a, **k):
        return None
    monkeypatch.setattr(scraper, "download_file", fake_download)
    assert asyncio.run(scraper.try_candidates_concurrently(
        "ACME", ["a", "b"], "PDF", tmp_path, referer="r", cal_data=Q3)) is None
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Racing / attempt logging
# ---------------------------------------------------------------------------

def test_search_is_raced_with_first_processor():
    assert scraper.RACE_SEARCH_WITH_FIRST_PROCESSOR is True


def test_cached_pattern_short_circuits_before_any_agent(tmp_path, monkeypatch):
    """The biggest win: when a cached pattern resolves, no processor and no
    Search call may be dispatched at all."""
    monkeypatch.setattr(scraper, "URL_PATTERNS_FILE", tmp_path / "p.json")
    scraper.save_url_pattern("ACME", "https://a.com/2025/december/x.pdf", Q3, "Q3")

    async def boom_resolve(*a, **k):
        raise AssertionError("an agent was dispatched despite a working cached URL")

    async def boom_search(*a, **k):
        raise AssertionError("Search was called despite a working cached URL")

    async def fake_download(company_key, download_url, file_type, target_dir,
                            referer, cal_data, dest_name=None):
        path = target_dir / (dest_name or "x")
        path.write_bytes(b"%PDF-ok")
        return path

    monkeypatch.setattr(scraper, "resolve_download_url", boom_resolve)
    monkeypatch.setattr(scraper, "search_fallback", boom_search)
    monkeypatch.setattr(scraper, "download_file", fake_download)
    scraper.ATTEMPT_LOG.clear()

    asyncio.run(scraper.process_company(
        "ACME", {"quarterly_disclosure_page_link": "https://a.com"},
        "FY25-26", "Q3", tmp_path))

    assert scraper.ATTEMPT_LOG["ACME"]["method"] == "direct-url"
    assert scraper.ATTEMPT_LOG["ACME"]["detail"] == "cached-pattern"


def test_attempt_log_records_route_for_summary(tmp_path, monkeypatch):
    """The per-company route log is what shows which processors never win."""
    monkeypatch.setattr(scraper, "URL_PATTERNS_FILE", tmp_path / "none.json")

    async def no_resolve(*a, **k):
        return None, "PDF"

    async def no_search(*a, **k):
        return []

    monkeypatch.setattr(scraper, "resolve_download_url", no_resolve)
    monkeypatch.setattr(scraper, "search_fallback", no_search)
    scraper.ATTEMPT_LOG.clear()

    asyncio.run(scraper.process_company(
        "ACME", {"quarterly_disclosure_page_link": "https://a.com"},
        "FY25-26", "Q3", tmp_path))

    assert scraper.ATTEMPT_LOG["ACME"]["method"] == "failed"
    assert "seconds" in scraper.ATTEMPT_LOG["ACME"]


def test_race_promotes_agent_file_and_leaves_no_temp(tmp_path, monkeypatch):
    """When the agent branch wins the race, its temp file must be promoted to
    the canonical name and no '.agent' temp may be left behind."""
    monkeypatch.setattr(scraper, "URL_PATTERNS_FILE", tmp_path / "p.json")

    async def fast_resolve(company_key, url, cal_data, processor, fy, feedback=None):
        return {"download_url": "https://a.com/2025/december/x.pdf",
                "document_title": "PD"}, "PDF"

    async def slow_search(*a, **k):
        await asyncio.sleep(0.2)
        return []

    async def fake_download(company_key, download_url, file_type, target_dir,
                            referer, cal_data, dest_name=None):
        path = target_dir / (dest_name or "canonical")
        path.write_bytes(b"%PDF-ok")
        return path

    monkeypatch.setattr(scraper, "resolve_download_url", fast_resolve)
    monkeypatch.setattr(scraper, "search_fallback", slow_search)
    monkeypatch.setattr(scraper, "download_file", fake_download)
    scraper.ATTEMPT_LOG.clear()

    asyncio.run(scraper.process_company(
        "ACME", {"quarterly_disclosure_page_link": "https://a.com"},
        "FY25-26", "Q3", tmp_path))

    assert scraper.ATTEMPT_LOG["ACME"]["method"] == "processor:core"
    assert (tmp_path / "ACME.pdf").read_bytes() == b"%PDF-ok"
    assert not list(tmp_path.glob(".*.agent")), "orphaned temp file left behind"
    # A winning agent URL is cached for next quarter.
    assert "ACME" in scraper.load_url_patterns()


# ---------------------------------------------------------------------------
# Wrong-document guards
#
# These reproduce a real FY27 Q1 failure: four of six "downloaded" files were
# the wrong document (the insurer's listed PARENT's results, a BSE covering
# letter, a different disclosure series, and an unrelated third-party PDF),
# all of which passed a period-only validity check.
# ---------------------------------------------------------------------------

JUNE_2026 = {"month": "June", "year": 2026}


def test_period_match_requires_month_and_year_together():
    """The old check tested for the month and the year independently, so a
    document from another quarter passed whenever both happened to appear
    somewhere on the page."""
    assert scraper._mentions_period("FOR THE PERIOD ENDED 30th JUNE , 2026", JUNE_2026)
    assert scraper._mentions_period("quarter ended June 30, 2026", JUNE_2026)
    assert scraper._mentions_period("as on 30/06/2026", JUNE_2026)
    assert scraper._mentions_period("Jun-26 figures", JUNE_2026)

    # Both tokens present, but describing different things - must NOT pass.
    assert not scraper._mentions_period(
        "Information as on 31/03/2025. Signed 12 June in the year 2026 crore", JUNE_2026)
    assert not scraper._mentions_period("quarter ended 31 December 2025", JUNE_2026)
    assert not scraper._mentions_period("", JUNE_2026)


def _fake_pdf(tmp_path, name, text):
    """A stand-in for a downloaded PDF: validate_document reads text via
    extract_lead_text, so patching that is enough to exercise the rules."""
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4 stub")
    return p


@pytest.mark.parametrize("label,text,expected", [
    ("real disclosure",
     "NARAYANA HEALTH INSURANCE LIMITED PUBLIC DISCLOSURES FOR THE PERIOD "
     "ENDED 30th JUNE , 2026 ... FORM NL-1-B-RA ... FORM NL-2-B-PL", True),
    # The four real-world false positives:
    ("listed parent's results",
     "ADITYA BIRLA CAPITAL LIMITED STATEMENT OF STANDALONE UNAUDITED "
     "FINANCIAL RESULTS FOR THE QUARTER ENDED 30th JUNE 2026", False),
    ("stock-exchange covering letter",
     "Date: July 29, 2026 Ref: SHAI/B & S/SE/59/2026-27 To The Manager, "
     "Listing Department, BSE Limited ... quarter ended June 2026", False),
    ("different disclosure series",
     "Public Disclosures on Quantative and Qualitative Parameters of Health "
     "Services Rendered Information as on 30 June 2026", False),
    ("unrelated third-party pdf",
     "119TH CONGRESS H. R. LANGWO_065.XML introduced June 2026", False),
])
def test_validate_document_rejects_wrong_documents(tmp_path, monkeypatch, label, text, expected):
    monkeypatch.setattr(scraper, "extract_lead_text", lambda p, e, pages=6: text)
    got = scraper.validate_document(_fake_pdf(tmp_path, "x.pdf", text),
                                    "pdf", JUNE_2026, "ACME")
    assert got is expected, f"{label}: expected {expected}, got {got}"


def test_gic_workbook_is_exempt_from_the_form_check(tmp_path, monkeypatch):
    """The GIC file is a statistics workbook, not a disclosure bundle - its
    title row names the period and that is the right check for it."""
    title = ("GROSS DIRECT PREMIUM INCOME ... FOR THE PERIOD UPTO June 2026 "
             "(PROVISIONAL & UNAUDITED ) IN FY 2026-27")
    monkeypatch.setattr(scraper, "extract_lead_text", lambda p, e, pages=6: title)
    assert scraper.validate_document(tmp_path / "GIC.xlsx", "xlsx", JUNE_2026, "GIC")


# ---------------------------------------------------------------------------
# Candidate ranking
# ---------------------------------------------------------------------------

def test_best_ranked_candidate_wins_not_the_fastest(tmp_path, monkeypatch):
    """Search returns candidates in relevance order. Validating them
    concurrently must not let a lower-ranked one win just because it
    downloaded faster - that made the chosen document depend on network
    timing."""
    async def fake_download(company_key, download_url, file_type, target_dir,
                            referer, cal_data, dest_name=None):
        # The best-ranked candidate is deliberately the SLOWEST.
        delay = {"best": 0.10, "second": 0.01, "third": 0.01}[download_url]
        await asyncio.sleep(delay)
        path = target_dir / dest_name
        path.write_bytes(b"%PDF-ok")
        return path

    monkeypatch.setattr(scraper, "download_file", fake_download)
    result = asyncio.run(scraper.try_candidates_concurrently(
        "ACME", ["best", "second", "third"], "PDF", tmp_path,
        referer="r", cal_data=JUNE_2026))

    assert result is not None and result.name == "ACME.pdf"
    # Only the winner survives; the out-ranked validated files are removed.
    assert [p.name for p in tmp_path.iterdir()] == ["ACME.pdf"]


def test_lower_ranked_candidate_used_when_better_ones_fail(tmp_path, monkeypatch):
    async def fake_download(company_key, download_url, file_type, target_dir,
                            referer, cal_data, dest_name=None):
        if download_url in ("bad1", "bad2"):
            return None
        path = target_dir / dest_name
        path.write_bytes(b"%PDF-ok")
        return path

    monkeypatch.setattr(scraper, "download_file", fake_download)
    result = asyncio.run(scraper.try_candidates_concurrently(
        "ACME", ["bad1", "bad2", "good"], "PDF", tmp_path,
        referer="r", cal_data=JUNE_2026))
    assert result is not None and result.read_bytes() == b"%PDF-ok"


# ---------------------------------------------------------------------------
# URL-template safety
# ---------------------------------------------------------------------------

def test_presigned_urls_are_never_templatised():
    """A real cached template had placeholders substituted into an Azure SAS
    expiry timestamp; rendering it for the next quarter produced a URL whose
    signature no longer matched the parameters."""
    sas = ("https://acct.blob.core.windows.net/prd/Finacial_Report_Q3_2025_27_5f0f.pdf"
           "?sv=2025-05-05&st=2025-08-13T05%3A25%3A31Z&se=2036-08-10T05%3A25%3A31Z"
           "&sr=b&sp=r&sig=69MzvZ1BKC7EhsJt")
    assert scraper.templatize_url(sas, {"month": "December", "year": 2025}, "Q3") is None


def test_query_string_is_left_untouched():
    """Even for an unsigned URL, dates inside the query string belong to API
    versions and ids, not to the reporting period."""
    url = "https://x.com/disclosures/december2025/pd.pdf?v=2025&id=1250"
    t = scraper.templatize_url(url, {"month": "December", "year": 2025}, "Q3")
    assert t is not None
    assert t.endswith("?v=2025&id=1250"), f"query string was rewritten: {t}"
    assert "{month}" in t and "{year}" in t


def test_bare_two_digit_year_is_not_substituted():
    """'25' matches inside unrelated numbers. Templatising it rewrote document
    ids and timestamps, pointing the rendered URL at a different file."""
    url = "https://x.com/docs/1250/december-2025/report.pdf"
    t = scraper.templatize_url(url, {"month": "December", "year": 2025}, "Q3")
    assert "1250" in t, f"an unrelated id was templatised: {t}"
    rendered = scraper.render_url_template(t, {"month": "June", "year": 2026}, "Q1")
    assert all("1250" in r for r in rendered), rendered
