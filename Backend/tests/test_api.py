"""Tests for the HTTP API.

These exercise the routes through FastAPI's TestClient with the pipeline
itself stubbed out - the point is the contract the dashboard consumes
(shapes, ids, status codes), not re-testing extraction.

Run:  myenv/Scripts/python.exe -m pytest tests/test_api.py -v
"""
import pytest
from fastapi.testclient import TestClient

from competitor_analysis import config as cfg
from competitor_analysis.api import runs as runs_mod
from competitor_analysis.api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test gets an empty run registry, so a leftover 'active' run from
    a previous test can't make the next one fail with 409."""
    runs_mod.REGISTRY._runs.clear()
    runs_mod.REGISTRY._active = None
    yield


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_companies_come_from_source_links_not_a_hardcoded_list():
    from competitor_analysis.ingestion import scraper
    r = client.get("/api/companies")
    assert r.status_code == 200
    got = r.json()["companies"]
    assert {c["name"] for c in got} == set(scraper.load_sources())
    # Every company carries the fields the dashboard renders.
    for c in got:
        assert c["id"] and c["short"]
        assert c["kind"] in ("sahi", "industry")


def test_company_ids_match_the_pipelines_own_identity():
    """The UI keys per-company progress by these ids, so they must be the
    same slugs the run state uses - not a second, parallel naming scheme."""
    got = client.get("/api/companies").json()["companies"]
    for c in got:
        assert c["id"] == cfg.company_slug(c["name"])
    # GIC has no per-company PDF; the rest map onto extraction short keys.
    assert {c["id"] for c in got} >= {"gic", "nbhi", "care-health"}


def test_periods_reports_real_on_disk_availability():
    r = client.get("/api/periods")
    assert r.status_code == 200
    body = r.json()
    assert body["fyOptions"] and body["quarterOptions"]
    by_key = {(p["fy"], p["quarter"]): p for p in body["periods"]}
    q3 = by_key[("FY25-26", "Q3")]
    assert q3["periodEnding"] == "31 Dec 2025"
    assert q3["curColumn"] == "FY25-26_Q3"
    assert q3["priorColumn"] == "FY24-25_Q3"
    # This period's filings are committed to the repo.
    assert q3["companiesPresent"] == q3["companiesTotal"]
    assert q3["gicPresent"] is True
    # A future period derives correctly rather than being absent.
    q1 = by_key[("FY26-27", "Q1")]
    assert q1["periodEnding"] == "30 Jun 2026"
    assert q1["curColumn"] == "FY26-27_Q1" and q1["priorColumn"] == "FY25-26_Q1"


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replace the phase bodies so the lifecycle can be tested without
    running extraction or calling any external API."""
    calls = []

    def fake_retrieval(run):
        calls.append("retrieval")
        run.set_phase("retrieval", "done")
        for cs in run.companies.values():
            cs.retrieval_status, cs.retrieval_progress = "done", 100

    def fake_extraction(run):
        calls.append("extraction")
        run.set_phase("extraction", "done")
        run.data_engine_path = None

    def fake_reporting(run):
        calls.append("reporting")
        run.report_progress = 100
        run.set_phase("reporting", "done")

    monkeypatch.setattr(runs_mod, "_phase_retrieval", fake_retrieval)
    monkeypatch.setattr(runs_mod, "_phase_extraction", fake_extraction)
    monkeypatch.setattr(runs_mod, "_phase_reporting", fake_reporting)
    return calls


def _await_run(run_id, timeout=15):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/pipeline/{run_id}/status").json()
        # "awaiting_review" is a legitimate stopping point for a download-only
        # run - the thread has finished its work and is waiting on a human,
        # not still in flight.
        if body["status"] in ("completed", "failed", "awaiting_review"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


def test_full_run_walks_every_phase(stub_pipeline):
    r = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    assert r.status_code == 200
    body = _await_run(r.json()["runId"])
    assert body["status"] == "completed"
    assert stub_pipeline == ["retrieval", "extraction", "reporting"]
    assert {p["key"]: p["status"] for p in body["phases"]} == {
        "retrieval": "done", "extraction": "done", "reporting": "done",
    }


def test_build_only_skips_retrieval(stub_pipeline):
    r = client.post("/api/pipeline/run",
                    json={"fy": "FY25-26", "quarter": "Q3", "stages": ["build"]})
    body = _await_run(r.json()["runId"])
    assert "retrieval" not in stub_pipeline
    phases = {p["key"]: p["status"] for p in body["phases"]}
    assert phases["retrieval"] == "skipped"
    assert phases["extraction"] == "done"


def test_download_only_leaves_later_phases_pending(stub_pipeline):
    r = client.post("/api/pipeline/run",
                    json={"fy": "FY25-26", "quarter": "Q3", "stages": ["download"]})
    body = _await_run(r.json()["runId"])
    assert stub_pipeline == ["retrieval"]
    phases = {p["key"]: p["status"] for p in body["phases"]}
    assert phases["extraction"] == "pending" and phases["reporting"] == "pending"


def test_status_payload_has_every_field_the_dashboard_reads(stub_pipeline):
    r = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    body = _await_run(r.json()["runId"])
    for key in ("runId", "fy", "quarter", "status", "elapsed", "phases",
                "companies", "activity", "reportProgress",
                "reportReady", "dataEngineReady"):
        assert key in body, f"missing {key}"
    company = next(iter(body["companies"].values()))
    assert set(company["retrieval"]) == {"status", "progress", "tier", "size"}
    assert set(company["extraction"]) == {"status", "progress", "metricsDone",
                                          "metricsTotal", "cacheHits"}


def test_a_failing_phase_marks_the_run_failed(monkeypatch):
    def boom(run):
        raise RuntimeError("no source documents found")
    monkeypatch.setattr(runs_mod, "_phase_retrieval", boom)

    r = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    body = _await_run(r.json()["runId"])
    assert body["status"] == "failed"
    assert "no source documents found" in body["error"]
    # The phase that raised is reported failed, not left spinning.
    assert {p["key"]: p["status"] for p in body["phases"]}["retrieval"] == "failed"


def test_concurrent_runs_are_refused(monkeypatch):
    """The pipeline writes shared files (workbook, downloads, caches), so a
    second simultaneous run would corrupt the first."""
    import threading
    release = threading.Event()

    def slow(run):
        release.wait(timeout=10)
        run.set_phase("retrieval", "done")

    monkeypatch.setattr(runs_mod, "_phase_retrieval", slow)
    monkeypatch.setattr(runs_mod, "_phase_extraction", lambda r: None)
    monkeypatch.setattr(runs_mod, "_phase_reporting", lambda r: None)

    first = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    assert first.status_code == 200
    second = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    assert second.status_code == 409
    assert "already in progress" in second.json()["detail"]
    release.set()
    _await_run(first.json()["runId"])


# ---------------------------------------------------------------------------
# Validation and error handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ({"fy": "FY9", "quarter": "Q3"}, "Invalid financial year"),
    ({"fy": "FY25-27", "quarter": "Q3"}, "spans exactly one year boundary"),
    ({"fy": "FY25-26", "quarter": "Q9"}, "Invalid quarter"),
    ({"fy": "FY25-26", "quarter": "Q3", "stages": ["nope"]}, "Unknown stage"),
])
def test_bad_requests_are_rejected_with_a_reason(payload, expected):
    r = client.post("/api/pipeline/run", json=payload)
    assert r.status_code == 400
    assert expected in r.json()["detail"]


def test_unknown_run_is_404():
    assert client.get("/api/pipeline/nope/status").status_code == 404
    assert client.get("/api/reports/nope/download").status_code == 404


def test_report_download_before_generation_is_409(stub_pipeline):
    r = client.post("/api/pipeline/run",
                    json={"fy": "FY25-26", "quarter": "Q3", "stages": ["download"]})
    rid = r.json()["runId"]
    _await_run(rid)
    got = client.get(f"/api/reports/{rid}/download")
    assert got.status_code == 409
    assert "not been generated" in got.json()["detail"]


def test_a_legacy_fy_is_normalised_on_the_run_record(stub_pipeline):
    """Whatever spelling the caller sends, the run - and so everything the
    dashboard displays for it - reports the canonical span form."""
    r = client.post("/api/pipeline/run", json={"fy": "FY26", "quarter": "Q3"})
    assert r.status_code == 200
    assert r.json()["fy"] == "FY25-26"
    body = _await_run(r.json()["runId"])
    assert body["fy"] == "FY25-26"


@pytest.mark.parametrize("spelling", ["FY26-27", "2026-27", "2026-2027", "FY27"])
def test_every_accepted_spelling_reaches_the_same_period(stub_pipeline, spelling):
    r = client.post("/api/pipeline/run",
                    json={"fy": spelling, "quarter": "Q1", "stages": ["download"]})
    assert r.status_code == 200, r.json()
    assert r.json()["fy"] == "FY26-27"
    _await_run(r.json()["runId"])


# ---------------------------------------------------------------------------
# Document review (the Phase 1 -> Phase 2 gate)
# ---------------------------------------------------------------------------

@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """Points the file-review endpoints at an isolated directory instead of
    the real, committed data/downloads tree."""
    base = tmp_path / "downloads"
    base.mkdir()
    monkeypatch.setattr(cfg, "expected_pdf_path",
                        lambda company, *a, **k: str(base / cfg.COMPANY_PDF_FILENAMES[company]))
    monkeypatch.setattr(cfg, "gic_path", lambda *a, **k: str(base / cfg.GIC_FILENAME))
    return base


@pytest.fixture
def awaiting_review_run(stub_pipeline):
    r = client.post("/api/pipeline/run",
                    json={"fy": "FY25-26", "quarter": "Q3", "stages": ["download"]})
    rid = r.json()["runId"]
    _await_run(rid)
    return rid


def test_download_only_run_reports_awaiting_review(awaiting_review_run):
    body = client.get(f"/api/pipeline/{awaiting_review_run}/status").json()
    assert body["status"] == "awaiting_review"


def test_awaiting_review_blocks_a_second_run(awaiting_review_run):
    """A paused review still owns the download directory a second run would
    write to, so it counts as active exactly like a running one does."""
    r = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    assert r.status_code == 409


def test_view_download_serves_the_file(downloads, awaiting_review_run):
    (downloads / cfg.COMPANY_PDF_FILENAMES["NBHI"]).write_bytes(b"%PDF-1.4 fake")
    r = client.get(f"/api/downloads/{awaiting_review_run}/nbhi/file")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 fake"
    assert r.headers["content-type"] == "application/pdf"


def test_view_missing_download_is_404(downloads, awaiting_review_run):
    r = client.get(f"/api/downloads/{awaiting_review_run}/nbhi/file")
    assert r.status_code == 404


def test_delete_download_marks_the_company_missing(downloads, awaiting_review_run):
    path = downloads / cfg.COMPANY_PDF_FILENAMES["NBHI"]
    path.write_bytes(b"wrong file")
    r = client.delete(f"/api/downloads/{awaiting_review_run}/nbhi/file")
    assert r.status_code == 200
    assert not path.exists()
    assert r.json()["companies"]["nbhi"]["retrieval"]["status"] == "missing"


def test_delete_missing_download_is_404(downloads, awaiting_review_run):
    assert client.delete(f"/api/downloads/{awaiting_review_run}/nbhi/file").status_code == 404


def test_delete_is_blocked_once_review_is_over(downloads, stub_pipeline):
    """Once a run has moved into extraction it owns the files Phase 2 is
    reading; a reviewer can no longer pull one out from under it."""
    r = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    rid = r.json()["runId"]
    _await_run(rid)
    got = client.delete(f"/api/downloads/{rid}/nbhi/file")
    assert got.status_code == 409


def test_upload_replaces_a_missing_file_and_marks_it_done(downloads, awaiting_review_run):
    r = client.post(f"/api/downloads/{awaiting_review_run}/nbhi/file",
                    files={"file": ("Niva_Bupa_Health_Insurance.pdf", b"%PDF-1.4 uploaded",
                                   "application/pdf")})
    assert r.status_code == 200
    assert r.json()["companies"]["nbhi"]["retrieval"]["status"] == "done"
    assert (downloads / cfg.COMPANY_PDF_FILENAMES["NBHI"]).read_bytes() == b"%PDF-1.4 uploaded"


def test_upload_rejects_mismatched_extension(downloads, awaiting_review_run):
    r = client.post(f"/api/downloads/{awaiting_review_run}/nbhi/file",
                    files={"file": ("wrong.xlsx", b"data",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 400


def test_upload_rejects_an_empty_file(downloads, awaiting_review_run):
    r = client.post(f"/api/downloads/{awaiting_review_run}/nbhi/file",
                    files={"file": ("Niva_Bupa_Health_Insurance.pdf", b"", "application/pdf")})
    assert r.status_code == 400


def test_upload_is_blocked_once_review_is_over(downloads, stub_pipeline):
    r = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    rid = r.json()["runId"]
    _await_run(rid)
    got = client.post(f"/api/downloads/{rid}/nbhi/file",
                      files={"file": ("x.pdf", b"data", "application/pdf")})
    assert got.status_code == 409


def test_continue_runs_the_build_stage_on_the_same_run(downloads, stub_pipeline, awaiting_review_run):
    assert stub_pipeline == ["retrieval"]
    r = client.post(f"/api/pipeline/{awaiting_review_run}/continue")
    assert r.status_code == 200
    body = _await_run(awaiting_review_run)
    assert body["runId"] == awaiting_review_run
    assert stub_pipeline == ["retrieval", "extraction", "reporting"]
    assert body["status"] == "completed"


def test_continue_is_rejected_when_not_awaiting_review(downloads, stub_pipeline):
    r = client.post("/api/pipeline/run", json={"fy": "FY25-26", "quarter": "Q3"})
    rid = r.json()["runId"]
    _await_run(rid)
    got = client.post(f"/api/pipeline/{rid}/continue")
    assert got.status_code == 409


def test_continue_on_unknown_run_is_404():
    assert client.post("/api/pipeline/nope/continue").status_code == 404


def test_view_delete_upload_on_unknown_run_is_404(downloads):
    assert client.get("/api/downloads/nope/nbhi/file").status_code == 404
    assert client.delete("/api/downloads/nope/nbhi/file").status_code == 404
    r = client.post("/api/downloads/nope/nbhi/file",
                    files={"file": ("x.pdf", b"data", "application/pdf")})
    assert r.status_code == 404
