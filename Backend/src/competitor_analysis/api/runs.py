"""Pipeline run execution and progress tracking for the HTTP API.

A "run" is one execution of the pipeline for one reporting period. Runs
execute on a worker thread so the request that starts one returns
immediately, and the UI polls for progress.

Progress here is derived from what the pipeline actually did - files present
on disk, scraper.ATTEMPT_LOG, the Gemini cache counters, the extraction
audit - never from a simulated timer. A status this API reports as "done"
means the corresponding artifact exists.
"""
import io
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass, field

from competitor_analysis import config as cfg
from competitor_analysis import paths
from competitor_analysis.storage import r2

# Report sections, only used to give the reporting phase a progress
# denominator. The PDF is built in one call, so this phase reports
# start/finish rather than per-section progress.
REPORT_STEP_COUNT = 4

_MAX_ACTIVITY_LINES = 500


def _company_id(company_key: str) -> str:
    """Stable slug the frontend uses as a company key. Delegates to
    config.company_slug so the API, the pipeline and the UI share one id
    space across the two company-naming systems."""
    return cfg.company_slug(company_key)


@dataclass
class PhaseState:
    key: str
    label: str
    subtitle: str
    status: str = "pending"  # pending | running | done | failed | skipped


@dataclass
class CompanyState:
    id: str
    name: str
    retrieval_status: str = "queued"
    retrieval_progress: int = 0
    tier: str | None = None
    size: int | None = None
    extraction_status: str = "queued"
    extraction_progress: int = 0


@dataclass
class RunState:
    run_id: str
    fy: str
    quarter: str
    status: str = "queued"  # queued | running | awaiting_review | completed | failed | cancelled
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    report_path: str | None = None
    data_engine_path: str | None = None
    report_progress: int = 0
    phases: list = field(default_factory=list)
    companies: dict = field(default_factory=dict)
    activity: list = field(default_factory=list)
    stages: tuple = ("download", "build")
    selected_companies: frozenset | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, level: str, text: str):
        with self._lock:
            self.activity.append({
                "level": level,
                "text": text,
                "time": time.strftime("%H:%M:%S"),
            })
            if len(self.activity) > _MAX_ACTIVITY_LINES:
                del self.activity[:-_MAX_ACTIVITY_LINES]

    def set_phase(self, key: str, status: str):
        for p in self.phases:
            if p.key == key:
                p.status = status

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 1)


class _ActivityStream(io.TextIOBase):
    """Routes the pipeline's own stdout into a run's activity feed.

    The pipeline is a print-heavy batch program; rather than silence it or
    re-instrument every message, its output becomes the run's log. Lines the
    pipeline already marks (`!` warnings, `Rejected`, `Giving up`) are
    classified so the UI can colour them."""

    def __init__(self, run: RunState):
        self._run = run
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self._run.log(_classify(line), line)
        return len(s)

    def flush(self):
        if self._buf.strip():
            self._run.log(_classify(self._buf), self._buf.strip())
            self._buf = ""


def _classify(line: str) -> str:
    low = line.lower()
    if any(t in low for t in ("giving up", "failed", "error", "traceback")):
        return "error"
    if any(t in low for t in (" ! ", "rejected", "discarded", "skipped",
                              "rate-limited", "not found", "unmatched",
                              "abandoned", "memory budget")):
        return "warn"
    if any(t in low for t in ("saved", "done", "complete", "wrote")):
        return "success"
    return "info"


class RunRegistry:
    """In-memory registry of runs, with a one-at-a-time execution guard.

    In-memory is a deliberate limit: runs do not survive a server restart.
    The durable results (the workbook and the PDF) are on disk under
    artifacts/, so a lost run record costs progress history, not output.
    """

    def __init__(self):
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._active: str | None = None

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def list(self) -> list[RunState]:
        return sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)

    @property
    def active_run_id(self) -> str | None:
        rid = self._active
        run = self._runs.get(rid) if rid else None
        # A run paused for document review still owns the download directory
        # and workbook a second run would write to, so it counts as active
        # exactly like "running" does - and the frontend needs it here to
        # reattach to a paused review after a page refresh.
        if run and run.status in ("queued", "running", "awaiting_review"):
            return rid
        return None

    def start(self, fy: str, quarter: str, stages=("download", "build"),
              companies=None) -> RunState:
        """Create and begin a run. Raises RuntimeError if one is already in
        flight - the pipeline writes to shared files (the workbook, the
        download directory, the caches), so concurrent runs would corrupt
        each other's state.

        `companies`, if given, is a subset of company ids (the same id space
        GET /api/companies uses) to include; other configured sources are
        left untouched for this run."""
        with self._lock:
            if self.active_run_id:
                raise RuntimeError(
                    f"A run is already in progress ({self.active_run_id}). "
                    f"Wait for it to finish before starting another.")
            run = _new_run(fy, quarter, stages, companies)
            self._runs[run.run_id] = run
            self._active = run.run_id

        thread = threading.Thread(target=_execute, args=(run, run.stages),
                                  name=f"run-{run.run_id}", daemon=True)
        thread.start()
        return run

    def continue_build(self, run_id: str) -> RunState:
        """Resume a run that paused after Phase 1 for document review, now
        running Phases 2-3 against whatever is on disk - including anything a
        reviewer replaced, removed or uploaded by hand since the pause.

        Reuses the same RunState (and run_id) rather than starting a second
        run, so the dashboard's review screen and the extraction/report
        screens that follow are one continuous run to the user.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(f"No such run: {run_id}")
            if run.status not in ("awaiting_review", "failed"):
                raise RuntimeError(
                    f"Run {run_id} is not awaiting review (status: {run.status}).")
            run.stages = tuple(dict.fromkeys((*run.stages, "build")))
            self._active = run.run_id

        thread = threading.Thread(target=_execute, args=(run, ("build",)),
                                  name=f"run-{run_id}-build", daemon=True)
        thread.start()
        return run


def _new_run(fy: str, quarter: str, stages, companies=None) -> RunState:
    run = RunState(run_id=uuid.uuid4().hex[:12], fy=fy, quarter=quarter,
                   stages=tuple(stages),
                   selected_companies=frozenset(companies) if companies is not None else None)
    run.phases = [
        PhaseState("retrieval", "Document Retrieval", "Phase 1"),
        PhaseState("extraction", "Data Extraction", "Phase 2"),
        PhaseState("reporting", "Report Generation", "Phase 3"),
    ]
    # Companies come from source_links.json, so the UI reflects the actual
    # configured sources rather than a hardcoded list.
    from competitor_analysis.ingestion import scraper
    for key in scraper.load_sources():
        run.companies[_company_id(key)] = CompanyState(id=_company_id(key), name=key)
    return run


def _execute(run: RunState, stages: tuple):
    """Run the given stage(s) against `run`.

    Called once from RunRegistry.start() with the run's initial stages, and
    again from RunRegistry.continue_build() with just ("build",) once a human
    has reviewed Phase 1's output - the two calls share one RunState, so a
    paused-for-review run and its continuation are one run to the API and the
    dashboard, not two.
    """
    stream = _ActivityStream(run)
    run.status = "running"
    run.log("info", f"Run {run.run_id}: running {'/'.join(stages)}.")
    try:
        with redirect_stdout(stream), redirect_stderr(stream):
            cfg.set_period(run.fy, run.quarter)
            retrieval_phase = next(p for p in run.phases if p.key == "retrieval")
            if "download" in stages:
                _phase_retrieval(run)
            elif retrieval_phase.status == "pending":
                # "build" requested without "download" ever having run in this
                # run (the standalone build-only entrypoint) - reflect
                # retrieval from whatever is already on disk rather than
                # leaving the phase stuck at "pending". A continue_build()
                # call also reaches this branch, but retrieval is already
                # "done" there, so it's a no-op.
                run.set_phase("retrieval", "skipped")
                _sync_retrieval_from_disk(run)
            if "build" in stages:
                _phase_extraction(run)
                _phase_reporting(run)
        if "build" in stages:
            run.status = "completed"
            run.log("success", "Run completed.")
        else:
            # Deliberately not "completed" - Phase 1 finished, but the
            # pipeline stops here so a human can confirm the retrieved
            # documents (or replace/remove/upload one) before extraction
            # reads them.
            run.status = "awaiting_review"
            run.log("info", "Documents retrieved. Review them, then continue "
                            "to extraction.")
    except Exception as e:
        run.status = "failed"
        run.error = f"{type(e).__name__}: {e}"
        # Attribute the failure to the phase it happened in: whichever was
        # running, or - if it raised before marking itself running - the first
        # phase that had not yet completed. Leaving a phase "pending" on a
        # failed run reads as "never attempted", which is wrong.
        running = [p for p in run.phases if p.status == "running"]
        if running:
            for p in running:
                p.status = "failed"
        else:
            for p in run.phases:
                if p.status not in ("done", "skipped"):
                    p.status = "failed"
                    break
        run.log("error", f"Run failed: {run.error}")
        run.log("error", traceback.format_exc(limit=6))
    finally:
        stream.flush()
        # Not set on "awaiting_review": the run isn't over, it's paused, so
        # elapsed keeps ticking (and a later continue can still fail/finish).
        if run.status in ("completed", "failed"):
            run.finished_at = time.time()
        # Runs also pause at "awaiting_review" with real retrieved documents
        # already on disk, so sync regardless of which of the three end
        # states this run landed in.
        r2.sync_run_outputs()


# ---------------------------------------------------------------------------
# Phase 1: retrieval
# ---------------------------------------------------------------------------

def _sync_retrieval_from_disk(run: RunState):
    """Reflect which source files are actually present for this period.

    Authoritative over whatever Phase 1 reported: it catches a partial
    download failure, and equally picks up a file a human supplied by hand."""
    import os

    avail = cfg.source_availability(run.fy, run.quarter)
    found = avail["companies_found"]
    for cs in run.companies.values():
        # A company left out of this run's selection stays "skipped"
        # regardless of what's on disk - the selection is independent of
        # download state, by design.
        if cs.retrieval_status == "skipped":
            continue
        short = cfg.short_key_for_source(cs.name)
        # GIC is the industry workbook, not a per-company PDF, so it has its
        # own presence flag rather than an entry in companies_found.
        path = found.get(short) if short else (
            avail["gic_path"] if avail["gic_found"] else None)
        if path and os.path.exists(path):
            cs.retrieval_status = "done"
            cs.retrieval_progress = 100
            try:
                cs.size = os.path.getsize(path)
            except OSError:
                pass
        elif cs.retrieval_status not in ("downloading", "retrying"):
            cs.retrieval_status = "missing"


def _phase_retrieval(run: RunState):
    import asyncio
    from competitor_analysis.ingestion import scraper

    run.set_phase("retrieval", "running")
    run.log("info", "Phase 1: retrieving filings from disclosure portals.")
    scraper.ATTEMPT_LOG.clear()

    selected_keys = None
    if run.selected_companies is not None:
        by_id = {_company_id(key): key for key in scraper.load_sources()}
        selected_keys = [by_id[i] for i in run.selected_companies if i in by_id]
        skipped = [c.name for c in run.companies.values()
                  if c.id not in run.selected_companies]
        if skipped:
            run.log("info", f"Excluded from this run: {', '.join(skipped)}.")

    for cs in run.companies.values():
        if run.selected_companies is not None and cs.id not in run.selected_companies:
            cs.retrieval_status, cs.retrieval_progress = "skipped", 0
        else:
            cs.retrieval_status, cs.retrieval_progress = "downloading", 5

    stop = threading.Event()

    def watch():
        """Poll the scraper's own attempt log so per-company status updates as
        each company resolves, instead of all flipping at the end."""
        while not stop.wait(1.0):
            for key, info in list(scraper.ATTEMPT_LOG.items()):
                cs = run.companies.get(_company_id(key))
                if cs is None:
                    continue
                method = info.get("method", "")
                cs.tier = method.replace("processor:", "") or None
                if method == "failed":
                    cs.retrieval_status, cs.retrieval_progress = "failed", 100
                else:
                    cs.retrieval_status, cs.retrieval_progress = "done", 100

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        asyncio.run(scraper.main(run.fy, run.quarter, companies=selected_keys))
    finally:
        stop.set()
        watcher.join(timeout=2)

    for key, info in scraper.ATTEMPT_LOG.items():
        cs = run.companies.get(_company_id(key))
        if cs is None:
            continue
        method = info.get("method", "")
        cs.tier = method.replace("processor:", "") or None
        cs.retrieval_status = "failed" if method == "failed" else "done"
        cs.retrieval_progress = 100
    _sync_retrieval_from_disk(run)

    failed = [c.name for c in run.companies.values()
              if c.retrieval_status not in ("done", "skipped")]
    run.set_phase("retrieval", "done" if not failed else "done")
    if failed:
        run.log("warn", f"{len(failed)} source(s) unavailable: {', '.join(failed)}. "
                        f"The build continues with whatever landed.")


# ---------------------------------------------------------------------------
# Phase 2: extraction
# ---------------------------------------------------------------------------

def _phase_extraction(run: RunState):
    import os
    from competitor_analysis.extraction import data_engine as p
    from competitor_analysis.extraction import gemini
    from competitor_analysis import pipeline as pipeline_mod

    run.set_phase("extraction", "running")
    avail = cfg.source_availability(run.fy, run.quarter)
    # Keys here are the short extraction keys ("NBHI", "Care Health"), which
    # is what the extraction side consumes.
    short_keys = sorted(avail["companies_found"])
    gic_available = avail["gic_found"]

    # A company left out of this run's selection is excluded here too,
    # independent of whether its file is actually on disk - selection scopes
    # extraction exactly like it scopes retrieval.
    gic_selected = (run.selected_companies is None
                    or _company_id("GIC") in run.selected_companies)
    if run.selected_companies is not None:
        short_keys = [k for k in short_keys
                      if cfg.company_slug(k) in run.selected_companies]

    if not short_keys and not (gic_available and gic_selected):
        raise RuntimeError(
            f"No source documents found for the selected companies in "
            f"{run.fy} {run.quarter} - nothing to extract. Run the download "
            f"stage first, or upload the filings.")

    present_ids = {cfg.company_slug(k) for k in short_keys}
    for cs in run.companies.values():
        if cfg.short_key_for_source(cs.name) is None:
            # GIC feeds the industry slides via a different path (the
            # workbook, not per-company metric extraction), so it has no
            # per-company metric progress to report.
            if not gic_selected:
                cs.extraction_status, cs.extraction_progress = "skipped", 0
            else:
                cs.extraction_status = "done" if gic_available else "missing"
                cs.extraction_progress = 100 if gic_available else 0
        elif cs.id in present_ids:
            cs.extraction_status = "extracting"
            cs.extraction_progress = 10
        else:
            cs.extraction_status = "skipped"

    gemini.CACHE_STATS.update(hit=0, miss=0)

    wb, ws = p.load_engine(cfg.DATA_ENGINE_TEMPLATE
                           if os.path.exists(cfg.DATA_ENGINE_TEMPLATE)
                           else str(paths.DATA_ENGINE_WORKBOOK))
    p.sync_period_headers(ws)
    p.clear_period_values(ws)

    # Re-scan before reading the map: this process may have served an earlier
    # run for a different period, and Phase 1 downloads land after
    # set_period. Without it, extraction reads whichever period's PDFs were
    # on disk when the module was first imported.
    from competitor_analysis.extraction import pdf_cache
    pdf_cache.refresh_company_pdfs()
    runnable = [k for k in short_keys if k in pdf_cache.COMPANY_PDFS]
    missing_from_cache = sorted(set(short_keys) - set(runnable))
    if missing_from_cache:
        run.log("warn", f"Present on disk but not registered for extraction: "
                        f"{', '.join(missing_from_cache)}")

    def _on_progress(key, percent, status=None):
        """Reflect one unit of per-company work onto the run snapshot the
        dashboard polls. Without this the whole table sat at its starting
        value until run_phase2 returned, then jumped to 100% at once."""
        cs = run.companies.get(_company_id(key))
        if cs is None:
            return
        cs.extraction_progress = max(cs.extraction_progress, int(percent))
        if status:
            cs.extraction_status = status

    pipeline_mod.run_phase2(ws, companies=runnable,
                            run_gic=gic_available and gic_selected,
                            on_progress=_on_progress)

    engine_path = cfg.data_engine_output_path()
    paths.ensure_parent(engine_path)
    wb.save(engine_path)
    run.data_engine_path = str(engine_path)
    run.log("success", f"Data Engine written to {engine_path}")

    # Anything still mid-flight had no progress callback fire for it (a
    # company skipped inside run_phase2, say) - settle it here.
    for cs in run.companies.values():
        if cs.extraction_status == "extracting":
            cs.extraction_status = "done"
            cs.extraction_progress = 100
    run.set_phase("extraction", "done")


# ---------------------------------------------------------------------------
# Phase 3: reporting
# ---------------------------------------------------------------------------

def _phase_reporting(run: RunState):
    from competitor_analysis.reporting import report

    run.set_phase("reporting", "running")
    run.report_progress = 10
    out = report.build(cfg.output_pdf_path(), data_engine_path=run.data_engine_path)
    run.report_path = str(out)
    run.report_progress = 100
    run.set_phase("reporting", "done")
    run.log("success", f"Report written to {out}")


REGISTRY = RunRegistry()
