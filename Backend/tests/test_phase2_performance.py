"""Tests for Phase 2 throughput work: concurrent extraction and the on-disk
Gemini response cache. No network - the model call is stubbed throughout.

Run:  myenv/Scripts/python.exe -m pytest test_phase2_performance.py -v
"""
import asyncio
import time

import pytest

from competitor_analysis import config as cfg

from competitor_analysis.extraction import gemini as g  # noqa: E402


SPECS = [{"key": f"m{i}", "description": f"metric {i}", "forms": ["NL-1"],
          "kind": "money", "rows": []} for i in range(80)]


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Keep every test off the real cache directory and reset counters."""
    monkeypatch.setattr(g, "_cache_path",
                        lambda key: str(tmp_path / f"{key}.json"))
    g.CACHE_STATS.update(hit=0, miss=0)
    monkeypatch.setattr(g, "USE_GEMINI_CACHE", True)
    yield


def _stub_call(calls, delay=0.05):
    def fake(company, payload, metric_specs):
        start = time.monotonic()
        time.sleep(delay)
        calls.append((start, time.monotonic()))
        return {m["key"]: {"fy26_q3": 1.0, "fy25_q3": 2.0, "found": True,
                           "source_form": "NL-1", "page_number": 1,
                           "evidence": "e", "notes": None}
                for m in metric_specs}
    return fake


def max_overlap(intervals):
    """Greatest number of (start, end) intervals in flight simultaneously.

    Concurrency tests assert on this rather than on total elapsed time: a
    wall-clock threshold conflates "ran concurrently" with "the machine was
    fast", and fails spuriously when the rest of the suite is loading the CPU.
    Overlap measures the property we actually care about and is independent of
    how quickly any single call returns."""
    events = [(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals]
    events.sort(key=lambda x: (x[0], -x[1]))
    peak = running = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


# ---------------------------------------------------------------------------
# Step 1: concurrency
# ---------------------------------------------------------------------------

def test_batch_size_stays_within_api_limit():
    """75+ metrics in one call was rejected outright; ~40 was observed to work.
    Batches must stay at or under 40 - and because the quota counts requests,
    not tokens, they should be as LARGE as that allows rather than as small as
    parallelism would prefer (see test_call_count_fits_the_per_minute_quota)."""
    assert 0 < g.DEFAULT_BATCH_SIZE <= 40


def test_batches_cover_every_metric_exactly_once():
    seen = []
    for batch, forms in g._batches(SPECS, g.DEFAULT_BATCH_SIZE):
        assert len(batch) <= g.DEFAULT_BATCH_SIZE
        seen.extend(m["key"] for m in batch)
        assert forms == ["NL-1"]
    assert seen == [m["key"] for m in SPECS], "metrics lost or duplicated"


def test_company_batches_run_concurrently(monkeypatch):
    """The four (now six) batches per company were awaited one at a time."""
    calls = []
    monkeypatch.setattr(g, "extract_metrics_via_gemini", _stub_call(calls, 0.1))
    monkeypatch.setattr(g, "build_company_payload",
                        lambda p, f, **k: {"NL-1": {"form_name": "x", "pages": []}})

    out = g.extract_company_metrics("ACME", "x.pdf", SPECS)

    n_batches = -(-len(SPECS) // g.DEFAULT_BATCH_SIZE)
    assert len(calls) == n_batches
    assert len(out) == len(SPECS)
    assert max_overlap(calls) > 1, (
        f"all {n_batches} batches ran one at a time - they should overlap")


def test_all_companies_and_batches_share_one_gate(monkeypatch):
    """The main win: companies x batches issued together, not nested serially."""
    calls = []
    monkeypatch.setattr(g, "extract_metrics_via_gemini", _stub_call(calls, 0.1))
    monkeypatch.setattr(g, "build_company_payload",
                        lambda p, f, **k: {"NL-1": {"form_name": "x", "pages": []}})
    jobs = [(f"C{i}", f"Company {i}", f"{i}.pdf") for i in range(7)]

    results = asyncio.run(g.extract_many_companies_async(jobs, SPECS, max_concurrency=8, rpm=0))

    n_batches = -(-len(SPECS) // g.DEFAULT_BATCH_SIZE)
    total_calls = n_batches * len(jobs)
    assert len(calls) == total_calls
    assert set(results) == {j[0] for j in jobs}
    assert all(len(r) == len(SPECS) for r in results.values())
    # Calls from DIFFERENT companies must be in flight together, which the
    # old company-by-company loop could never achieve.
    assert max_overlap(calls) > 1, (
        f"none of the {total_calls} calls overlapped - still serialised")


def test_concurrency_gate_is_respected(monkeypatch):
    """Rate limits matter more than raw speed - never exceed the cap."""
    in_flight = 0
    peak = 0

    def fake(company, payload, metric_specs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        time.sleep(0.05)
        in_flight -= 1
        return {m["key"]: {"found": False, "fy26_q3": None, "fy25_q3": None,
                           "source_form": None, "page_number": None,
                           "evidence": None, "notes": None}
                for m in metric_specs}

    monkeypatch.setattr(g, "extract_metrics_via_gemini", fake)
    monkeypatch.setattr(g, "build_company_payload",
                        lambda p, f, **k: {"NL-1": {"form_name": "x", "pages": []}})
    jobs = [(f"C{i}", f"Company {i}", f"{i}.pdf") for i in range(7)]
    asyncio.run(g.extract_many_companies_async(jobs, SPECS, max_concurrency=4, rpm=0))
    assert peak <= 4, f"peak in-flight {peak} exceeded the cap of 4"


def test_one_company_failing_does_not_sink_the_run(monkeypatch):
    def fake(company, payload, metric_specs):
        if "BAD" in company:
            raise RuntimeError("model unavailable")
        return {m["key"]: {"found": True, "fy26_q3": 1.0, "fy25_q3": 1.0,
                           "source_form": None, "page_number": None,
                           "evidence": None, "notes": None}
                for m in metric_specs}

    monkeypatch.setattr(g, "extract_metrics_via_gemini", fake)
    monkeypatch.setattr(g, "build_company_payload",
                        lambda p, f, **k: {"NL-1": {"form_name": "x", "pages": []}})
    jobs = [("GOOD", "Good Co", "g.pdf"), ("BAD", "BAD Co", "b.pdf")]
    results = asyncio.run(g.extract_many_companies_async(jobs, SPECS, rpm=0))
    assert results["GOOD"]
    assert results["BAD"] == {}, "a failing company should yield empty, not raise"


# ---------------------------------------------------------------------------
# Step 2: response cache
# ---------------------------------------------------------------------------

PAYLOAD = {"NL-1": {"form_name": "Revenue", "pages": [{"page_index": 0, "tables": [["a"]]}]}}


def test_second_identical_call_is_served_from_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "_extract_metrics_via_gemini_uncached", _stub_call(calls, 0))

    first = g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:5])
    second = g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:5])

    assert first == second
    assert len(calls) == 1, "cache did not prevent the second model call"
    assert g.CACHE_STATS == {"hit": 1, "miss": 1}


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda: {"company": "OTHER"}, id="different-company"),
    pytest.param(lambda: {"payload": {"NL-1": {"form_name": "Revenue",
                                               "pages": [{"page_index": 0,
                                                          "tables": [["CHANGED"]]}]}}},
                 id="different-source-tables"),
    pytest.param(lambda: {"specs": [dict(SPECS[0], description="reworded")]},
                 id="different-metric-prompt"),
])
def test_changed_inputs_invalidate_the_cache(monkeypatch, mutate):
    """A cache that ignored any of these would serve a stale, wrong answer."""
    calls = []
    monkeypatch.setattr(g, "_extract_metrics_via_gemini_uncached", _stub_call(calls, 0))
    g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:1])

    change = mutate()
    g.extract_metrics_via_gemini(change.get("company", "ACME"),
                                 change.get("payload", PAYLOAD),
                                 change.get("specs", SPECS[:1]))
    assert len(calls) == 2, "changed inputs were wrongly served from cache"


def test_period_is_part_of_the_cache_key(monkeypatch):
    """Same PDF tables, different reporting period - the prompt differs, so
    the entry must not be shared."""
    calls = []
    monkeypatch.setattr(g, "_extract_metrics_via_gemini_uncached", _stub_call(calls, 0))
    g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:1])
    cfg.set_period("FY26-27", "Q1")
    try:
        g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:1])
    finally:
        cfg.set_period("FY25-26", "Q3")
    assert len(calls) == 2


def test_cache_can_be_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "_extract_metrics_via_gemini_uncached", _stub_call(calls, 0))
    monkeypatch.setattr(g, "USE_GEMINI_CACHE", False)
    g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:1])
    g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:1])
    assert len(calls) == 2
    assert g.CACHE_STATS["hit"] == 0


def test_corrupt_cache_entry_falls_back_to_a_live_call(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(g, "_extract_metrics_via_gemini_uncached", _stub_call(calls, 0))
    key = g._cache_key("ACME", PAYLOAD, SPECS[:1])
    with open(g._cache_path(key), "w", encoding="utf-8") as f:
        f.write("{not json")
    out = g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:1])
    assert out and len(calls) == 1


# ---------------------------------------------------------------------------
# Step 3: concurrent income-statement prefetch
# ---------------------------------------------------------------------------

def test_income_statements_prefetch_concurrently(monkeypatch):
    from competitor_analysis.extraction import data_engine as p2

    calls = []
    spans = []

    def slow_extract(company, pdf_path):
        start = time.monotonic()
        calls.append(company)
        time.sleep(0.1)
        spans.append((start, time.monotonic()))
        return {"PBT": (1.0, 2.0)}

    monkeypatch.setattr(p2, "extract_income_statement", slow_extract)
    monkeypatch.setattr(p2, "COMPANY_PDFS", {f"C{i}": f"{i}.pdf" for i in range(7)})
    p2.apply_income_statement_rows._cache.clear()

    companies = [f"C{i}" for i in range(7)]
    p2.prefetch_income_statements(companies)

    assert sorted(calls) == sorted(companies)
    assert all(c in p2.apply_income_statement_rows._cache for c in companies)
    assert max_overlap(spans) > 1, "extractions ran one at a time"


def test_prefetch_skips_already_cached_companies(monkeypatch):
    from competitor_analysis.extraction import data_engine as p2
    calls = []
    monkeypatch.setattr(p2, "extract_income_statement",
                        lambda c, p: calls.append(c) or {"PBT": (1.0, 1.0)})
    monkeypatch.setattr(p2, "COMPANY_PDFS", {"A": "a.pdf", "B": "b.pdf"})
    p2.apply_income_statement_rows._cache.clear()
    p2.apply_income_statement_rows._cache["A"] = {"PBT": (9.0, 9.0)}

    p2.prefetch_income_statements(["A", "B"])
    assert calls == ["B"], "an already-extracted company was re-extracted"


# ---------------------------------------------------------------------------
# Free-tier quota handling (the actual binding constraint)
# ---------------------------------------------------------------------------

def test_total_call_count_stays_within_a_few_quota_minutes():
    """The quota counts REQUESTS/minute, so the TOTAL number of calls - not
    local parallelism - sets the floor on a cold run. Batch size therefore
    wants to be as large as the API accepts; this guards against someone
    shrinking it again for "more parallelism" and quietly multiplying the
    request count (a batch of 13 would mean 49 calls, over 3 quota-minutes)."""
    n = len(g.master_metric_specs())
    per_company = -(-n // g.DEFAULT_BATCH_SIZE)
    total = per_company * 7
    floor_minutes = total / max(g.GEMINI_RPM, 1)
    assert floor_minutes <= 2.0, (
        f"{total} calls at {g.GEMINI_RPM}/min needs {floor_minutes:.1f} minutes "
        f"of quota; use a larger batch_size")
    assert g.DEFAULT_BATCH_SIZE <= 40


def test_rate_limiter_caps_requests_per_window():
    limiter = g.RateLimiter(rpm=5)
    limiter.rpm = 5

    async def drive():
        # 5 should pass immediately; the 6th must wait for the window.
        for _ in range(5):
            await limiter.acquire()
        assert len(limiter._times) == 5

    asyncio.run(drive())


def test_rate_limiter_zero_rpm_is_unlimited():
    """rpm<=0 disables limiting, for paid tiers or tests."""
    limiter = g.RateLimiter(rpm=0)

    async def drive():
        for _ in range(50):
            await limiter.acquire()
    asyncio.run(drive())


def test_retry_delay_honours_server_supplied_value():
    exc = Exception("429 RESOURCE_EXHAUSTED ... 'retryDelay': '37s' ...")
    assert g._retry_delay_from(exc, 0) == pytest.approx(38.0)


def test_retry_delay_falls_back_to_exponential():
    exc = Exception("429 RESOURCE_EXHAUSTED with no delay hint")
    assert g._retry_delay_from(exc, 0) == 1.0
    assert g._retry_delay_from(exc, 3) == 8.0
    assert g._retry_delay_from(exc, 20) == 60.0  # capped


def test_rate_limit_errors_are_recognised():
    assert g._is_rate_limit(Exception("429 RESOURCE_EXHAUSTED"))
    assert g._is_rate_limit(Exception("RESOURCE_EXHAUSTED"))
    assert not g._is_rate_limit(Exception("500 internal"))


def test_quota_rejection_is_retried_then_succeeds(monkeypatch):
    """A 429 must be retried rather than losing the company's metrics."""
    attempts = []

    def flaky(company, payload, metric_specs):
        attempts.append(1)
        if len(attempts) == 1:
            raise Exception("429 RESOURCE_EXHAUSTED 'retryDelay': '0s'")
        return {m["key"]: {"found": True, "fy26_q3": 1.0, "fy25_q3": 1.0,
                           "source_form": None, "page_number": None,
                           "evidence": None, "notes": None}
                for m in metric_specs}

    monkeypatch.setattr(g, "extract_metrics_via_gemini", flaky)
    limiter = g.RateLimiter(rpm=0)
    out = asyncio.run(g._call_with_retry("ACME", PAYLOAD, SPECS[:2], limiter))
    assert len(attempts) == 2
    assert len(out) == 2


def test_non_quota_errors_are_not_retried(monkeypatch):
    attempts = []

    def broken(company, payload, metric_specs):
        attempts.append(1)
        raise ValueError("schema rejected")

    monkeypatch.setattr(g, "extract_metrics_via_gemini", broken)
    limiter = g.RateLimiter(rpm=0)
    with pytest.raises(ValueError):
        asyncio.run(g._call_with_retry("ACME", PAYLOAD, SPECS[:2], limiter))
    assert len(attempts) == 1, "a non-quota error should fail fast"


def test_validation_failure_is_retried_once_then_succeeds(monkeypatch):
    """A malformed key (schemas.ExtractedValue validation failure) must be
    retried like a 429 - but via the SAME retry loop, not a second call
    shape: the retry re-sends the whole batch, not a single-metric call."""
    attempts = []

    def flaky(company, payload, metric_specs):
        attempts.append(1)
        if len(attempts) == 1:
            raise g.MetricValidationError(partial_out={}, invalid_keys=["m0"])
        return {m["key"]: {"found": True, "fy26_q3": 1.0, "fy25_q3": 1.0,
                           "source_form": None, "page_number": None,
                           "evidence": None, "notes": None}
                for m in metric_specs}

    monkeypatch.setattr(g, "extract_metrics_via_gemini", flaky)
    limiter = g.RateLimiter(rpm=0)
    out = asyncio.run(g._call_with_retry("ACME", PAYLOAD, SPECS[:2], limiter))
    assert len(attempts) == 2
    assert out["m0"]["found"] is True, "the retry's real answer, not a placeholder"


def test_validation_failure_isolates_to_one_key_after_final_retry(monkeypatch):
    """Unlike an exhausted 429 (which loses the whole batch), a metric still
    invalid after the retry is marked unresolved on its own - its batch
    siblings that DID validate must survive, not be discarded with it."""
    attempts = []

    def always_bad(company, payload, metric_specs):
        attempts.append(1)
        raise g.MetricValidationError(
            partial_out={"m1": {"found": True, "fy26_q3": 5.0, "fy25_q3": 4.0,
                                "source_form": "NL-1", "page_number": 2,
                                "evidence": "e", "notes": None}},
            invalid_keys=["m0"],
        )

    monkeypatch.setattr(g, "extract_metrics_via_gemini", always_bad)
    limiter = g.RateLimiter(rpm=0)
    out = asyncio.run(g._call_with_retry("ACME", PAYLOAD, SPECS[:2], limiter))
    assert len(attempts) == 2, "one initial attempt plus exactly one retry"
    assert out["m1"]["found"] is True, "the batch's valid sibling must survive"
    assert out["m0"] == {"fy26_q3": None, "fy25_q3": None, "found": False,
                         "source_form": None, "page_number": None,
                         "evidence": None, "notes": "validation failed after retry"}


def test_cached_calls_do_not_consume_quota(monkeypatch):
    """A fully-cached run must not wait on the per-minute limiter - gating
    before the cache lookup made cached runs as slow as live ones."""
    monkeypatch.setattr(g, "_extract_metrics_via_gemini_uncached", _stub_call([], 0))
    # Warm the cache.
    g.extract_metrics_via_gemini("ACME", PAYLOAD, SPECS[:2])

    acquired = []

    class CountingLimiter(g.RateLimiter):
        async def acquire(self):
            acquired.append(1)
            await super().acquire()

    limiter = CountingLimiter(rpm=1)
    out = asyncio.run(g._call_with_retry("ACME", PAYLOAD, SPECS[:2], limiter))
    assert out is not None and len(out) == 2
    assert acquired == [], "a cache hit consumed a rate-limit slot"


# ---------------------------------------------------------------------------
# Memory: pdfplumber page caches must be released during the parse
# ---------------------------------------------------------------------------

def test_parse_releases_each_page_cache(monkeypatch):
    """Every page must be closed as the parse walks past it.

    extract_text/extract_tables populate Page._objects/_edges/_layout, and
    pdf.pages holds every Page for the document's lifetime - so skipping
    close() retains the entire PDF's object graph at once. Measured on Star
    Health's 51-page filing that was 822MB peak RSS versus 196MB with it,
    which on its own exceeded Render's 512MB free tier and took Phase 2 down
    with an out-of-memory kill.
    """
    from competitor_analysis.extraction import pdf_cache

    closed = []

    class FakePage:
        def __init__(self, n):
            self.n = n
        def extract_text(self):
            return f"FORM NL-1-B-RA page {self.n}"
        def extract_tables(self):
            return [[["a", "b"]]]
        def close(self):
            closed.append(self.n)

    class FakePdf:
        pages = [FakePage(i) for i in range(5)]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(pdf_cache.pdfplumber, "open", lambda p: FakePdf())
    monkeypatch.setattr(pdf_cache.os, "stat",
                        lambda p: type("S", (), {"st_mtime": 1.0, "st_size": 2})())

    doc = pdf_cache.parse_pdf_to_json("ignored.pdf", "TestCo")

    assert closed == [0, 1, 2, 3, 4], "every page must be closed, in order"
    # And the parse still returns the content it is supposed to.
    assert len(doc["pages"]) == 5
    assert doc["pages"][0]["forms_detected"] == ["NL-1"]
    assert doc["pages"][0]["tables"] == [[["a", "b"]]]


# ---------------------------------------------------------------------------
# Memory budget: degrade one filing, never kill the process
# ---------------------------------------------------------------------------

def test_memory_budget_is_off_unless_configured(monkeypatch):
    """A dev machine or CI box must never have work abandoned under it."""
    from competitor_analysis import memory
    monkeypatch.delenv("MEMORY_BUDGET_MB", raising=False)
    assert memory.budget_mb() == 0
    memory.check("anything")  # must not raise, whatever RSS happens to be


def test_memory_check_raises_once_over_budget(monkeypatch):
    from competitor_analysis import memory
    monkeypatch.setenv("MEMORY_BUDGET_MB", "100")
    monkeypatch.setattr(memory, "rss_mb", lambda: 99.0)
    memory.check("under")                       # no raise
    monkeypatch.setattr(memory, "rss_mb", lambda: 100.0)
    with pytest.raises(memory.MemoryBudgetExceeded, match="memory budget reached"):
        memory.check("Star Health (parsing page 18/51)")


def test_unreadable_rss_never_blocks_a_run(monkeypatch):
    """rss_mb() returning None means 'no opinion' - a platform without a
    probe must not have every document abandoned under it."""
    from competitor_analysis import memory
    monkeypatch.setenv("MEMORY_BUDGET_MB", "1")
    monkeypatch.setattr(memory, "rss_mb", lambda: None)
    memory.check("no probe available")          # must not raise


def test_parse_aborts_the_document_between_pages(monkeypatch):
    """The checkpoint has to fire where the half-built result can still be
    dropped - between pages, not mid-write."""
    from competitor_analysis import memory
    from competitor_analysis.extraction import pdf_cache

    seen = []

    class FakePage:
        def __init__(self, n): self.n = n
        def extract_text(self): return f"page {self.n}"
        def extract_tables(self): return []
        def close(self): seen.append(self.n)

    class FakePdf:
        pages = [FakePage(i) for i in range(10)]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(pdf_cache.pdfplumber, "open", lambda p: FakePdf())
    monkeypatch.setenv("MEMORY_BUDGET_MB", "100")
    # Cross the budget partway through the document.
    rss = iter([10.0] * 3 + [999.0] * 20)
    monkeypatch.setattr(memory, "rss_mb", lambda: next(rss))

    with pytest.raises(memory.MemoryBudgetExceeded, match="parsing page 4/10"):
        pdf_cache.parse_pdf_to_json("x.pdf", "HeavyCo")
    assert seen == [0, 1, 2, 3], "should stop at the page that crossed the budget"


def test_a_heavy_filing_is_skipped_without_sinking_the_run(monkeypatch):
    """The whole point: one filing over budget costs that filing, not the
    process. The others still extract, and the skip is reported."""
    from competitor_analysis import memory
    from competitor_analysis.extraction import data_engine as p

    monkeypatch.setattr(p, "COMPANY_PDFS", {"Heavy": "h.pdf", "Light": "l.pdf"}, raising=False)
    monkeypatch.setattr(p.apply_income_statement_rows, "_cache", {}, raising=False)

    def fake_extract(company, path):
        if company == "Heavy":
            raise memory.MemoryBudgetExceeded("Heavy (parsing page 9/60): memory budget reached")
        return {"GWP": (1.0, 2.0)}

    monkeypatch.setattr(p, "extract_income_statement", fake_extract)

    errors = p.prefetch_income_statements(["Heavy", "Light"], max_workers=1)

    assert set(errors) == {"Heavy"}
    assert "memory budget reached" in errors["Heavy"]
    # Light extracted normally; Heavy cached as empty so nothing re-parses it.
    assert p.apply_income_statement_rows._cache["Light"] == {"GWP": (1.0, 2.0)}
    assert p.apply_income_statement_rows._cache["Heavy"] == {}


# ---------------------------------------------------------------------------
# Live per-company progress
# ---------------------------------------------------------------------------

def test_income_prefetch_reports_each_company_as_it_finishes(monkeypatch):
    """Progress must be reported per company as work lands.

    pool.map yields in SUBMISSION order, so a company that finished early was
    not reported until everything queued ahead of it had finished too - which
    is why the dashboard's bars all jumped at the end instead of moving.
    """
    import threading
    from competitor_analysis.extraction import data_engine as p

    monkeypatch.setattr(p, "COMPANY_PDFS",
                        {"Slow": "s.pdf", "Fast": "f.pdf"}, raising=False)
    monkeypatch.setattr(p.apply_income_statement_rows, "_cache", {}, raising=False)

    started = threading.Event()

    def fake_extract(company, path):
        if company == "Slow":
            started.set()
            time.sleep(0.30)        # submitted first, finishes last
        else:
            started.wait(1.0)
            time.sleep(0.02)
        return {"GWP": (1.0, 2.0)}

    monkeypatch.setattr(p, "extract_income_statement", fake_extract)

    order = []
    p.prefetch_income_statements(["Slow", "Fast"], max_workers=2,
                                 on_company_done=order.append)

    assert order == ["Fast", "Slow"], (
        "completion order, not submission order - got " + repr(order))


def test_gemini_prefetch_reports_each_company(monkeypatch):
    """Stage 3's callback fires once per company, including one that failed,
    so a company erroring out cannot leave its bar stuck forever."""
    done = []

    # First positional is the PROMPT NAME (the company's full name), not the
    # short key the results are returned under.
    async def fake_one(prompt_name, pdf_path, specs, batch_size, all_forms,
                       semaphore, limiter):
        if prompt_name == "B Ltd":
            raise RuntimeError("model refused")
        return {"m0": {"found": True}}

    monkeypatch.setattr(g, "extract_company_metrics_async", fake_one)

    jobs = [("A", "A Ltd", "a.pdf"), ("Broken", "B Ltd", "b.pdf")]
    results = asyncio.run(g.extract_many_companies_async(
        jobs, SPECS[:1], rpm=0, on_company_done=done.append))

    assert sorted(done) == ["A", "Broken"]
    assert results["Broken"] == {}          # failed, but still reported
