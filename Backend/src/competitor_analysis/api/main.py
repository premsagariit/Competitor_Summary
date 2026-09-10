"""HTTP API for the competitor-analysis pipeline.

Exposes the pipeline's three phases to the React dashboard. The route shapes
match the integration seam the frontend already documented in src/api.js, so
the UI's views did not need restructuring to consume real data.

Run:
    uvicorn competitor_analysis.api.main:app --reload --port 8000
"""
import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis.api import runs as runs_mod
from competitor_analysis.api.runs import REGISTRY
from competitor_analysis.storage import r2

app = FastAPI(
    title="Competitor Analysis API",
    description="Retrieval, extraction and reporting for Indian "
                "health-insurer competitor analysis.",
    version="0.1.0",
)

# The dashboard is served by Vite on a different port in development. Vite's
# proxy makes same-origin requests in the normal case; CORS is here so the UI
# also works when pointed straight at this server. In production the deployed
# frontend is a different origin (Vercel) than the API (Render), so its URL
# is added via CORS_ALLOWED_ORIGINS rather than hardcoded here.
_extra_origins = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   "http://localhost:4173", *_extra_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _restore_documents_from_r2():
    """Pull previously-synced downloads/reports back onto local disk. A
    no-op when R2 isn't configured (plain local dev)."""
    r2.restore_all()

# Derived from the clock, newest first - no annual edit needed. A caller may
# also POST any other financial year; /api/pipeline/run normalises whatever
# form it is given, so a custom entry works without appearing in this list.
FY_OPTIONS = cfg.fy_options(back=3, forward=1)
QUARTER_OPTIONS = ["Q1", "Q2", "Q3", "Q4"]

# Display metadata the pipeline itself has no opinion about. Keyed by the
# source_links.json company name so it survives renames of the short keys.
_COMPANY_META = {
    "GIC": {"short": "GIC", "kind": "industry", "logo": None, "color": "#64748b"},
    "Niva Bupa Health Insurance": {"short": "Niva Bupa", "kind": "sahi",
                                   "logo": "/assets/logos/NBHI.png",
                                   "color": "#ec1c4e", "self": True},
    "Aditya Birla Health Insurance": {"short": "Aditya Birla", "kind": "sahi",
                                      "logo": "/assets/logos/ABHI.png", "color": "#c0392b"},
    "Care Health Insurance": {"short": "Care", "kind": "sahi",
                              "logo": "/assets/logos/CARE.png", "color": "#f39c12"},
    "ManipalCigna Health Insurance": {"short": "ManipalCigna", "kind": "sahi",
                                      "logo": "/assets/logos/CIGNA.png", "color": "#2980b9"},
    "Star Health & Allied Insurance": {"short": "Star Health", "kind": "sahi",
                                       "logo": "/assets/logos/STAR.png", "color": "#e74c3c"},
    "Galaxy Health Insurance": {"short": "Galaxy", "kind": "sahi", "logo": None,
                                "color": "#8e44ad"},
    "Narayana Health Insurance": {"short": "Narayana", "kind": "sahi", "logo": None,
                                  "color": "#16a085"},
}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    fy: str = Field(..., examples=["FY25-26"],
                    description="Financial year span, e.g. FY25-26 for April 2025 - "
                                "March 2026. FY26, 2025-26 and 2025-2026 are also "
                                "accepted and normalised.")
    quarter: str = Field(..., examples=["Q3"])
    stages: list[str] = Field(
        default=["download", "build"],
        description='Which halves to run: "download" (Phase 1) and/or "build" '
                    '(Phases 2-3). Splitting them allows a human check on the '
                    'retrieved files before extraction.')
    companies: list[str] | None = Field(
        default=None,
        description="Subset of company ids (GET /api/companies `id` field) to "
                    "include in this run. Omit or null for all configured sources.")


class ManualRunRequest(BaseModel):
    fy: str = Field(..., examples=["FY25-26"])
    quarter: str = Field(..., examples=["Q3"])
    companies: list[str] | None = Field(
        default=None,
        description="Subset of company ids to include; omit or null for all "
                    "configured sources.")


class FetchMissingRequest(BaseModel):
    companies: list[str] | None = Field(
        default=None,
        description="Subset of company ids to fetch, regardless of their "
                    "current status. Omit or null to fetch every currently "
                    "missing/failed source selected for this run.")


def _serialise_run(run) -> dict:
    return {
        "runId": run.run_id,
        "fy": run.fy,
        "quarter": run.quarter,
        "status": run.status,
        "error": run.error,
        "elapsed": run.elapsed,
        "reportProgress": run.report_progress,
        "reportReady": bool(run.report_path and os.path.exists(run.report_path)),
        "dataEngineReady": bool(run.data_engine_path and os.path.exists(run.data_engine_path)),
        "phases": [
            {"key": p.key, "label": p.label, "subtitle": p.subtitle, "status": p.status}
            for p in run.phases
        ],
        "companies": {
            cid: {
                "id": cs.id,
                "name": cs.name,
                "retrieval": {"status": cs.retrieval_status,
                              "progress": cs.retrieval_progress,
                              "tier": cs.tier, "size": cs.size},
                "extraction": {"status": cs.extraction_status,
                               "progress": cs.extraction_progress},
            }
            for cid, cs in run.companies.items()
        },
        "activity": run.activity[-200:],
    }


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "backendRoot": str(paths.BACKEND_ROOT)}


@app.get("/api/periods")
def periods():
    """Selectable reporting periods, and which source files already exist for
    each - so the UI can show what is runnable without guessing."""
    out = []
    for fy in FY_OPTIONS:
        for q in QUARTER_OPTIONS:
            try:
                avail = cfg.source_availability(fy, q)
            except Exception:
                continue
            found = avail["companies_found"]
            missing = avail["companies_missing"]
            out.append({
                "fy": fy,
                "quarter": q,
                "periodEnding": cfg.period_ending_str(fy, q),
                "curColumn": cfg.period_column(fy, q),
                "priorColumn": cfg.period_column(cfg.prior_fy(fy), q),
                "gicPresent": avail["gic_found"],
                "companiesPresent": len(found),
                "companiesTotal": len(found) + len(missing),
            })
    return {"fyOptions": FY_OPTIONS, "quarterOptions": QUARTER_OPTIONS, "periods": out}


@app.get("/api/companies")
def companies():
    """The configured sources, from source_links.json - not a hardcoded list,
    so adding an insurer there surfaces it in the UI."""
    from competitor_analysis.ingestion import scraper
    out = []
    for name, entry in scraper.load_sources().items():
        meta = _COMPANY_META.get(name, {})
        out.append({
            "id": runs_mod._company_id(name),
            "name": name,
            "short": meta.get("short", name),
            "kind": meta.get("kind", "sahi"),
            "logo": meta.get("logo"),
            "color": meta.get("color", "#64748b"),
            "self": meta.get("self", False),
            "sourceUrl": entry.get("quarterly_disclosure_page_link"),
            "processors": entry.get("processors"),
        })
    return {"companies": out}


# ---------------------------------------------------------------------------
# Pipeline runs
# ---------------------------------------------------------------------------

@app.post("/api/pipeline/run")
def start_run(req: RunRequest):
    quarter = req.quarter.strip().upper()
    try:
        # Normalise before anything else, so the run - and everything the UI
        # shows for it - carries the canonical span form regardless of which
        # spelling the caller sent.
        fy = cfg.normalize_fy(req.fy)
        cfg.set_period(fy, quarter)  # also validates the quarter
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    bad = [s for s in req.stages if s not in ("download", "build")]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"Unknown stage(s): {bad}. Use 'download' and/or 'build'.")
    if req.companies is not None and len(req.companies) == 0:
        raise HTTPException(status_code=400,
                            detail="At least one company must be selected to run the pipeline.")
    try:
        run = REGISTRY.start(fy, quarter, stages=req.stages, companies=req.companies)
    except RuntimeError as e:
        # 409: the pipeline writes shared files, so runs must not overlap.
        raise HTTPException(status_code=409, detail=str(e))
    return _serialise_run(run)


@app.post("/api/pipeline/manual")
def start_manual_run(req: ManualRunRequest):
    """Start a run that skips retrieval entirely, for a user who already has
    the filings and wants to upload them directly rather than wait on (or
    pay for) Phase 1. Lands straight in "awaiting_review" - the same state
    the review screen already handles - with every source starting "missing"
    so its upload control is available immediately."""
    quarter = req.quarter.strip().upper()
    try:
        fy = cfg.normalize_fy(req.fy)
        cfg.set_period(fy, quarter)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.companies is not None and len(req.companies) == 0:
        raise HTTPException(status_code=400,
                            detail="At least one company must be selected to run the pipeline.")
    try:
        run = REGISTRY.start_manual(fy, quarter, companies=req.companies)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _serialise_run(run)


@app.get("/api/pipeline/runs")
def list_runs():
    return {"runs": [{"runId": r.run_id, "fy": r.fy, "quarter": r.quarter,
                      "status": r.status, "elapsed": r.elapsed}
                     for r in REGISTRY.list()],
            "activeRunId": REGISTRY.active_run_id}


@app.get("/api/pipeline/{run_id}/status")
def run_status(run_id: str):
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    return _serialise_run(run)


@app.post("/api/pipeline/{run_id}/fetch-missing")
def fetch_missing(run_id: str, req: FetchMissingRequest = FetchMissingRequest()):
    """Fetch automatically whichever sources are still missing/failed on a
    paused run, without starting a new run - for a manual-upload run where
    the user supplied some sources by hand and wants the rest retrieved, or
    a normal run where one source failed and the user wants just that one
    retried. Runs Phase 1 scoped to those sources, then returns to
    awaiting_review either way."""
    if REGISTRY.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    try:
        run = REGISTRY.fetch_missing(run_id, companies=req.companies)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _serialise_run(run)


@app.post("/api/pipeline/{run_id}/continue")
def continue_run(run_id: str):
    """Resume a run that paused after Phase 1 so a reviewer could confirm the
    retrieved documents. Runs Phase 2 against whatever is on disk now,
    including anything replaced, removed or uploaded during the pause, then
    pauses again at "awaiting_report" for the Data Engine workbook to be
    reviewed before Phase 3 reads it - see /continue-report."""
    if REGISTRY.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    try:
        run = REGISTRY.continue_build(run_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _serialise_run(run)


@app.post("/api/pipeline/{run_id}/continue-report")
def continue_report(run_id: str):
    """Resume a run that paused after Phase 2 so a reviewer could check the
    filled Data Engine workbook. Runs Phase 3 against whatever workbook is on
    disk now, including a replacement uploaded during the pause."""
    if REGISTRY.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    try:
        run = REGISTRY.continue_report(run_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _serialise_run(run)


@app.post("/api/pipeline/{run_id}/cancel")
def cancel_run(run_id: str):
    """Abandon a run paused waiting for a human (awaiting_review,
    awaiting_report) or not yet started (queued), freeing the registry so a
    new run can start - e.g. the dashboard calls this when the user switches
    to a different FY/Quarter without continuing the run they'd started.
    Refuses (409) if the run's background thread is actively executing right
    now, since it writes to files shared across every period."""
    if REGISTRY.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    try:
        run = REGISTRY.cancel(run_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _serialise_run(run)


# ---------------------------------------------------------------------------
# Phase 1 document review
#
# Between the download and build stages, a reviewer can inspect what was
# retrieved for each source, remove a wrongly-matched file, and upload a
# replacement by hand. The three endpoints below all resolve the same
# deterministic on-disk path Phase 2 itself reads from (config.expected_pdf_
# path / config.gic_path), so "the file the reviewer sees" and "the file
# extraction uses" can never diverge.
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # a regulatory filing runs a few MB; this only guards against a mistaken upload


def _resolve_company_file(run, company_id: str):
    cs = run.companies.get(company_id)
    if cs is None:
        raise HTTPException(status_code=404, detail=f"No such company: {company_id}")
    short = cfg.short_key_for_source(cs.name)
    path = (cfg.expected_pdf_path(short, run.fy, run.quarter) if short
           else cfg.gic_path(run.fy, run.quarter))
    return path, cs


def _require_reviewable(run):
    """Files may only be changed while a run is paused for review (or crashed
    during retrieval) - not once Phase 2 has started reading them."""
    if run.status not in ("awaiting_review", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Documents can only be viewed as changeable while a run is "
                   f"awaiting review (current status: {run.status}).")


@app.get("/api/downloads/{run_id}/{company_id}/file")
def view_downloaded_file(run_id: str, company_id: str):
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    path, cs = _resolve_company_file(run, company_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"No downloaded file for {cs.name} yet.")
    media_type = ("application/pdf" if path.lower().endswith(".pdf")
                 else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return FileResponse(path, media_type=media_type,
                        headers={"Content-Disposition": f'inline; filename="{os.path.basename(path)}"'})


@app.delete("/api/downloads/{run_id}/{company_id}/file")
def delete_downloaded_file(run_id: str, company_id: str):
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    _require_reviewable(run)
    path, cs = _resolve_company_file(run, company_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"No downloaded file for {cs.name} to remove.")
    os.remove(path)
    r2.delete_file(path)
    cs.retrieval_status = "missing"
    cs.retrieval_progress = 0
    cs.size = None
    cs.tier = None
    run.log("warn", f"{cs.name}: downloaded file removed for review.")
    return _serialise_run(run)


@app.post("/api/downloads/{run_id}/{company_id}/file")
async def upload_downloaded_file(run_id: str, company_id: str, file: UploadFile = File(...)):
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    _require_reviewable(run)
    path, cs = _resolve_company_file(run, company_id)
    expected_ext = os.path.splitext(path)[1].lower()
    uploaded_ext = os.path.splitext(file.filename or "")[1].lower()
    if uploaded_ext and uploaded_ext != expected_ext:
        raise HTTPException(
            status_code=400,
            detail=f"{cs.name} expects a {expected_ext} file, got {uploaded_ext}.")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large (limit 50 MB).")
    paths.ensure_parent(path)
    with open(path, "wb") as f:
        f.write(content)
    r2.upload_file(path)
    cs.retrieval_status = "done"
    cs.retrieval_progress = 100
    cs.size = len(content)
    cs.tier = "manual"
    run.log("success", f"{cs.name}: file uploaded manually for review ({len(content)} bytes).")
    return _serialise_run(run)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@app.get("/api/reports/{run_id}/download")
def download_report(run_id: str):
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    if not run.report_path or not os.path.exists(run.report_path):
        raise HTTPException(
            status_code=409,
            detail="The report for this run has not been generated yet.")
    return FileResponse(run.report_path, media_type="application/pdf",
                        filename=os.path.basename(run.report_path))


@app.get("/api/data-engine/{run_id}/download")
def download_data_engine(run_id: str):
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    if not run.data_engine_path or not os.path.exists(run.data_engine_path):
        raise HTTPException(status_code=409,
                            detail="The Data Engine workbook for this run is not ready.")
    return FileResponse(
        run.data_engine_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=os.path.basename(run.data_engine_path))


@app.post("/api/data-engine/{run_id}/upload")
async def upload_data_engine(run_id: str, file: UploadFile = File(...)):
    """Replace the Data Engine workbook with a hand-edited version, while the
    run is paused at "awaiting_report" for exactly this review. Report
    generation (continue-report) reads whatever is at run.data_engine_path,
    so this is the same swap-in-place the document review endpoints already
    do for a source PDF."""
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    if run.status not in ("awaiting_report", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"The Data Engine workbook can only be replaced while a run is "
                   f"awaiting report review (current status: {run.status}).")
    if not run.data_engine_path:
        raise HTTPException(status_code=409,
                            detail="This run has no Data Engine workbook to replace yet.")
    uploaded_ext = os.path.splitext(file.filename or "")[1].lower()
    if uploaded_ext and uploaded_ext != ".xlsx":
        raise HTTPException(status_code=400,
                            detail=f"Expected a .xlsx file, got {uploaded_ext}.")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large (limit 50 MB).")
    # A quick load (not just the extension) catches a corrupted or non-Excel
    # file here, at upload time, rather than as a confusing failure deep
    # inside report generation once continue-report reads it.
    import io
    import openpyxl
    try:
        openpyxl.load_workbook(io.BytesIO(content), read_only=True).close()
    except Exception:
        raise HTTPException(status_code=400,
                            detail="Not a valid Excel (.xlsx) workbook.")
    paths.ensure_parent(run.data_engine_path)
    with open(run.data_engine_path, "wb") as f:
        f.write(content)
    r2.upload_file(run.data_engine_path)
    run.log("success", f"Data Engine workbook replaced manually ({len(content)} bytes).")
    return _serialise_run(run)
