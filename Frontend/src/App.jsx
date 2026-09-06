import { useState, useEffect, useCallback, useRef } from "react";
import { API } from "./api.js";
import { AppIcon } from "./Icons.jsx";
import { Button, Spinner, useToast } from "./ui.jsx";
import { OverviewView, RetrievalView, ExtractionView, ReportsView } from "./views.jsx";

// Sections of the generated PDF. Descriptive only: the backend builds the
// report in a single call, so these are marked complete together when the
// reporting phase finishes rather than being ticked off on a fake timer.
const REPORT_SECTIONS = [
  "Cover & Executive Summary",
  "Market Overview (GIC industry statistics)",
  "Per-Insurer Deep Dive",
  "Geographic Zone Breakdown",
  "Ratio Benchmarking Charts",
  "Appendix & Methodology",
];

const POLL_MS = 1500;

// Sentinel value for the "Other…" entry in the financial-year select.
const CUSTOM_FY = "__custom__";

// A financial year is a SPAN: FY26-27 means April 2026 to March 2027. The
// backend accepts more spellings than this (FY26, 2026-27, 2026-2027) and is
// the authority; this pattern only decides whether to enable the Set button.
const FY_PATTERN = /^(FY)?\s?\d{2,4}\s?[-/_]\s?\d{2,4}$/i;

function nowTime() {
  return new Date().toLocaleTimeString("en-IN", { hour12: false });
}

const EMPTY_PHASES = [
  { key: "retrieval", label: "Document Retrieval", subtitle: "Phase 1", icon: "download", status: "pending" },
  { key: "extraction", label: "Data Extraction", subtitle: "Phase 2", icon: "cpu", status: "pending" },
  { key: "reporting", label: "Report Generation", subtitle: "Phase 3", icon: "fileText", status: "pending" },
];

const PHASE_ICONS = {
  retrieval: "download",
  extraction: "cpu",
  reporting: "fileText",
};

/** Company shells so the dashboard renders before any run has started. */
function idleCompanies(companyList) {
  const obj = {};
  companyList.forEach((c) => {
    obj[c.id] = {
      retrieval: { status: "queued", progress: 0, tier: null, size: null },
      extraction: { status: "queued", progress: 0 },
    };
  });
  return obj;
}

export default function App() {
  const toast = useToast();
  const [fy, setFy] = useState("FY25-26");
  const [quarter, setQuarter] = useState("Q3");
  const [customFy, setCustomFy] = useState(false);
  const [fyDraft, setFyDraft] = useState("");
  const [fyOptions, setFyOptions] = useState(["FY25-26"]);
  const [quarterOptions, setQuarterOptions] = useState(["Q1", "Q2", "Q3", "Q4"]);
  const [view, setView] = useState("overview");

  const [backend, setBackend] = useState({ status: "connecting", detail: null });
  const [companyList, setCompanyList] = useState([]);
  const [selectedCompanies, setSelectedCompanies] = useState(() => new Set());
  const [periods, setPeriods] = useState([]);

  const [runId, setRunId] = useState(null);
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState("idle");
  const [phases, setPhases] = useState(EMPTY_PHASES);
  const [companies, setCompanies] = useState({});
  const [activity, setActivity] = useState([
    { level: "info", text: "Connecting to backend…", time: nowTime() },
  ]);
  const [reportProgress, setReportProgress] = useState(0);
  const [reportReady, setReportReady] = useState(false);
  const [dataEngineReady, setDataEngineReady] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  const pollTimer = useRef(null);

  const log = useCallback((level, text) => {
    setActivity((prev) => [...prev.slice(-200), { level, text, time: nowTime() }]);
  }, []);

  // -- connect ---------------------------------------------------------------
  const loadMetadata = useCallback(async () => {
    try {
      const [health, list, periodInfo] = await Promise.all([
        API.health(),
        API.getCompanies(),
        API.getPeriods(),
      ]);
      setBackend({ status: "online", detail: health.backendRoot });
      setCompanyList(list);
      setCompanies((prev) => (Object.keys(prev).length ? prev : idleCompanies(list)));
      setSelectedCompanies((prev) => (prev.size ? prev : new Set(list.map((c) => c.id))));
      setFyOptions(periodInfo.fyOptions);
      setQuarterOptions(periodInfo.quarterOptions);
      setPeriods(periodInfo.periods);
      setActivity([
        { level: "success", text: `Backend connected (${list.length} sources configured).`, time: nowTime() },
      ]);
    } catch (e) {
      setBackend({ status: "offline", detail: e.message });
      setActivity([{ level: "error", text: e.message, time: nowTime() }]);
    }
  }, []);

  useEffect(() => {
    loadMetadata();
  }, [loadMetadata]);

  // Adopt a run that is already in flight (e.g. after a page refresh), so the
  // dashboard reattaches to it instead of showing an idle pipeline.
  useEffect(() => {
    if (backend.status !== "online") return;
    let cancelled = false;
    API.listRuns()
      .then((d) => {
        if (cancelled || !d.activeRunId) return;
        log("info", `Reattaching to run ${d.activeRunId} already in progress.`);
        setRunId(d.activeRunId);
        setRunning(true);
      })
      .catch(() => {
        /* listing is best-effort */
      });
    return () => {
      cancelled = true;
    };
  }, [backend.status, log]);

  // -- apply a status snapshot ----------------------------------------------
  const applySnapshot = useCallback((snap) => {
    setPhases(
      snap.phases.map((p) => ({ ...p, icon: PHASE_ICONS[p.key] || "cpu" })),
    );
    setCompanies(snap.companies);
    setReportProgress(snap.reportProgress);
    setReportReady(snap.reportReady);
    setDataEngineReady(snap.dataEngineReady);
    setElapsed(Math.round(snap.elapsed));
    setRunStatus(snap.status);
    // The backend owns the activity feed - it is the pipeline's own stdout,
    // so it is the real log rather than a UI-side narration of it.
    if (snap.activity?.length) setActivity(snap.activity);
  }, []);

  // -- polling -----------------------------------------------------------
  // Gated on `running` (not just `runId`) so it restarts on its own when
  // continueToBuild() flips running back to true after a paused review -
  // the run id doesn't change, only whether there's anything to poll for.
  useEffect(() => {
    if (!runId || !running) return;
    let stopped = false;

    async function poll() {
      try {
        const snap = await API.getPipelineStatus(runId);
        if (stopped) return;
        applySnapshot(snap);

        if (snap.status === "awaiting_review") {
          setRunning(false);
          toast({
            kind: "info",
            title: "Documents downloaded",
            description: "Review the retrieved files, then continue to extraction.",
          });
          return; // stop polling until the reviewer continues
        }
        if (snap.status === "completed" || snap.status === "failed") {
          setRunning(false);
          if (snap.status === "failed") {
            toast({ kind: "error", title: "Pipeline failed", description: snap.error || "See the activity log." });
          } else {
            toast({ kind: "success", title: "Pipeline complete", description: "Report is ready for export" });
          }
          return; // stop polling
        }
        pollTimer.current = setTimeout(poll, POLL_MS);
      } catch (e) {
        if (stopped) return;
        setBackend({ status: "offline", detail: e.message });
        setRunning(false);
        toast({ kind: "error", title: "Lost connection", description: e.message });
      }
    }

    poll();
    return () => {
      stopped = true;
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [runId, running, applySnapshot, toast]);

  // -- actions ---------------------------------------------------------------
  async function startRun(stages) {
    if (running) return;
    if (selectedCompanies.size === 0) {
      toast({
        kind: "error",
        title: "No companies selected",
        description: "Select at least one company before starting a run.",
      });
      return;
    }
    setElapsed(0);
    setReportProgress(0);
    setReportReady(false);
    setPhases(EMPTY_PHASES);
    setCompanies(idleCompanies(companyList));
    setRunStatus("queued");
    setActivity([{ level: "info", text: `Starting ${fy} ${quarter} (${stages.join(" + ")})…`, time: nowTime() }]);
    try {
      const run = await API.startPipeline(fy, quarter, stages, Array.from(selectedCompanies));
      setRunId(run.runId);
      setRunning(true);
      applySnapshot(run);
      toast({ kind: "info", title: "Pipeline started", description: `${fy} ${quarter} · run ${run.runId}` });
    } catch (e) {
      setRunning(false);
      // 409 means a run is already in flight - a real condition, not a bug,
      // because the pipeline writes shared files.
      toast({
        kind: e.status === 409 ? "info" : "error",
        title: e.status === 409 ? "Already running" : "Could not start",
        description: e.message,
      });
      log("error", e.message);
      if (e.status === 0) setBackend({ status: "offline", detail: e.message });
    }
  }

  // Retrieval-only: the pipeline always pauses after Phase 1 so a reviewer
  // can confirm the documents before extraction reads them. See
  // continueToBuild() for the second half.
  const runPipeline = () => startRun(["download"]);
  const runBuildOnly = () => startRun(["build"]);

  function toggleCompany(id) {
    setSelectedCompanies((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  const selectAllCompanies = () => setSelectedCompanies(new Set(companyList.map((c) => c.id)));
  const selectNoCompanies = () => setSelectedCompanies(new Set());

  async function continueToBuild() {
    if (!runId) return;
    try {
      const snap = await API.continuePipeline(runId);
      applySnapshot(snap);
      setRunning(true);
      toast({ kind: "info", title: "Continuing pipeline", description: "Extraction, reporting and review run next." });
    } catch (e) {
      toast({
        kind: e.status === 409 ? "info" : "error",
        title: e.status === 409 ? "Cannot continue yet" : "Could not continue",
        description: e.message,
      });
    }
  }

  async function viewDownloadedFile(companyId) {
    if (!runId) return;
    API.viewDownloadedFile(runId, companyId);
  }

  async function deleteDownloadedFile(companyId) {
    if (!runId) return;
    try {
      const snap = await API.deleteDownloadedFile(runId, companyId);
      applySnapshot(snap);
      toast({ kind: "info", title: "File removed", description: "Upload a replacement or continue without it." });
    } catch (e) {
      toast({ kind: "error", title: "Could not remove file", description: e.message });
    }
  }

  async function uploadDownloadedFile(companyId, file) {
    if (!runId) return;
    try {
      const snap = await API.uploadDownloadedFile(runId, companyId, file);
      applySnapshot(snap);
      toast({ kind: "success", title: "File uploaded", description: "Replaces the source document for this company." });
    } catch (e) {
      toast({ kind: "error", title: "Upload failed", description: e.message });
    }
  }

  function commitCustomFy() {
    const typed = fyDraft.trim();
    if (!FY_PATTERN.test(typed)) return;
    // Uppercase and add the FY prefix for display; the backend normalises
    // whatever form actually reaches it, so this is presentation only.
    const tidy = typed.toUpperCase().replace(/\s/g, "").replace(/[/_]/g, "-");
    setFy(tidy.startsWith("FY") ? tidy : `FY${tidy}`);
    setCustomFy(false);
    setFyDraft("");
  }

  function retryRetrieval() {
    // Per-company retry needs a backend endpoint that re-runs Phase 1 for one
    // source; until that exists, re-running the download stage is the honest
    // equivalent rather than a no-op that looks like it worked.
    toast({
      kind: "info",
      title: "Re-running retrieval",
      description: "Per-source retry isn't available yet — running the download stage for all sources.",
    });
    startRun(["download"]);
  }

  function downloadReport() {
    if (!reportReady) {
      toast({ kind: "info", title: "Not ready", description: "The report hasn't been generated for this run yet." });
      return;
    }
    API.downloadReport(runId);
  }

  function downloadDataEngine() {
    if (!dataEngineReady) {
      toast({ kind: "info", title: "Not ready",
              description: "Extraction hasn't produced a Data Engine workbook for this run yet." });
      return;
    }
    API.downloadDataEngine(runId);
  }

  const companyMeta = Object.fromEntries(companyList.map((c) => [c.id, c]));
  const reportingDone = phases.find((p) => p.key === "reporting")?.status === "done";
  const reportSections = REPORT_SECTIONS.map((name) => ({
    name,
    done: reportingDone,
    active: !reportingDone && reportProgress > 0,
  }));

  const selectedPeriod = periods.find((p) => p.fy === fy && p.quarter === quarter);

  const state = {
    fy, quarter, phases, companies, activity, running, runStatus, elapsed,
    reportProgress, reportSections, companyMeta, runId, reportReady,
    dataEngineReady,
    selectedPeriod, backend, companyList, selectedCompanies,
  };

  const NAV = [
    { key: "overview", label: "Overview", icon: "layoutGrid" },
    ...EMPTY_PHASES.map((p) => ({ key: p.key, label: p.label, icon: p.icon })),
  ];

  const connBadge = {
    connecting: ["text-slate-400 bg-slate-500/10 border-slate-500/25", "bg-slate-400", "Connecting…"],
    online: ["text-emerald-400 bg-emerald-500/10 border-emerald-500/25", "bg-emerald-400", "Backend connected"],
    offline: ["text-rose-400 bg-rose-500/10 border-rose-500/25", "bg-rose-400", "Backend offline"],
  }[backend.status];

  return (
    <div className="min-h-screen flex bg-slate-950 text-slate-200">
      {/* Sidebar */}
      <aside className="w-64 shrink-0 border-r border-slate-800 bg-slate-950/80 flex flex-col">
        <div className="h-16 flex items-center gap-2.5 px-5 border-b border-slate-800">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-brand-500 to-cyan-400 flex items-center justify-center">
            <AppIcon name="database" className="w-[18px] h-[18px] text-white" />
          </div>
          <div>
            <div className="text-sm font-semibold text-white leading-tight">Competitor Intel</div>
            <div className="text-[10px] text-slate-500 leading-tight">Health Insurance · SAHI</div>
          </div>
        </div>
        <nav className="flex-1 px-3 py-4 flex flex-col gap-1">
          {NAV.map((n) => {
            const phase = phases.find((p) => p.key === n.key);
            return (
              <button
                key={n.key}
                onClick={() => setView(n.key)}
                className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                  view === n.key ? "bg-brand-500/15 text-white border border-brand-500/30" : "text-slate-400 hover:text-white hover:bg-slate-900"
                }`}
              >
                <AppIcon name={n.icon} className="w-4 h-4 shrink-0" />
                <span className="flex-1 text-left">{n.label}</span>
                {phase && (
                  <span
                    className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                      phase.status === "done"
                        ? "bg-emerald-500"
                        : phase.status === "running"
                        ? "bg-brand-400 animate-pulse"
                        : phase.status === "failed"
                        ? "bg-rose-500"
                        : phase.status === "skipped"
                        ? "bg-slate-600"
                        : "bg-slate-700"
                    }`}
                  />
                )}
              </button>
            );
          })}
        </nav>
        <div className="p-4 border-t border-slate-800 text-[11px] text-slate-600">
          {runId ? (
            <>
              Run <span className="text-slate-400">{runId}</span>
              <br />
              {fy} {quarter}
            </>
          ) : (
            <>Phase 1–4 pipeline.<br />No run yet.</>
          )}
        </div>
      </aside>

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-16 border-b border-slate-800 bg-slate-950/60 backdrop-blur flex items-center justify-between px-6 gap-4 shrink-0">
          <div className="flex items-center gap-3">
            {customFy ? (
              // Any financial year can be typed, not only the few the backend
              // lists. The backend normalises the input (FY25-26, FY26,
              // 2025-26 all work) and is the authority on validity, so this
              // only does enough checking to give immediate feedback.
              <span className="flex items-center gap-1.5">
                <input
                  autoFocus
                  value={fyDraft}
                  onChange={(e) => setFyDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitCustomFy();
                    if (e.key === "Escape") { setCustomFy(false); setFyDraft(""); }
                  }}
                  placeholder="FY26-27"
                  aria-label="Financial year"
                  className={`w-28 bg-slate-900 border rounded-lg px-2.5 py-1.5 text-sm text-slate-200 outline-none ${
                    fyDraft && !FY_PATTERN.test(fyDraft.trim())
                      ? "border-rose-500/60 focus:border-rose-500"
                      : "border-slate-800 focus:border-brand-500"
                  }`}
                />
                <Button variant="subtle" onClick={commitCustomFy}
                        disabled={!FY_PATTERN.test(fyDraft.trim())}>
                  Set
                </Button>
                <Button variant="ghost" onClick={() => { setCustomFy(false); setFyDraft(""); }}>
                  <AppIcon name="x" className="w-4 h-4" />
                </Button>
              </span>
            ) : (
              <select
                value={fy}
                onChange={(e) => {
                  if (e.target.value === CUSTOM_FY) {
                    setFyDraft(fy);
                    setCustomFy(true);
                  } else {
                    setFy(e.target.value);
                  }
                }}
                disabled={running}
                className="bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-sm text-slate-200 outline-none focus:border-brand-500"
              >
                {/* The selected year is included even when it isn't one of
                    the listed options, so a custom entry stays visible. */}
                {(fyOptions.includes(fy) ? fyOptions : [fy, ...fyOptions]).map((o) => (
                  <option key={o}>{o}</option>
                ))}
                <option value={CUSTOM_FY}>Other…</option>
              </select>
            )}
            <select value={quarter} onChange={(e) => setQuarter(e.target.value)} disabled={running} className="bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-sm text-slate-200 outline-none focus:border-brand-500">
              {quarterOptions.map((o) => (
                <option key={o}>{o}</option>
              ))}
            </select>
            {selectedPeriod ? (
              <span className="text-xs text-slate-500 hidden sm:inline">
                ends {selectedPeriod.periodEnding} · {selectedPeriod.companiesPresent}/{selectedPeriod.companiesTotal} filings
                {selectedPeriod.gicPresent ? " + GIC" : ""} on disk
              </span>
            ) : (
              <span className="text-xs text-slate-600 hidden sm:inline">Reporting period</span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <div
              title={backend.detail || ""}
              className={`flex items-center gap-1.5 text-xs border rounded-full px-2.5 py-1 ${connBadge[0]}`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${connBadge[1]} ${backend.status !== "online" ? "animate-pulse" : ""}`} />
              {connBadge[2]}
            </div>
            {backend.status === "offline" && (
              <Button variant="ghost" onClick={loadMetadata}>
                <AppIcon name="refresh" className="w-4 h-4" />
                Retry
              </Button>
            )}
            <Button variant="ghost" onClick={runBuildOnly} disabled={running || backend.status !== "online"}>
              <AppIcon name="cpu" className="w-4 h-4" />
              Build only
            </Button>
            {runStatus === "awaiting_review" ? (
              <Button onClick={continueToBuild} disabled={running}>
                <AppIcon name="arrowRight" className="w-4 h-4" />
                Continue to Extraction
              </Button>
            ) : (
              <Button
                onClick={runPipeline}
                disabled={running || backend.status !== "online"}
                title="Retrieves documents, then pauses for you to confirm them before extraction runs."
              >
                {running ? <Spinner className="w-4 h-4" /> : <AppIcon name="play" className="w-4 h-4" />}
                {running ? "Running…" : "Run Pipeline"}
              </Button>
            )}
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-6">
          {view === "overview" && (
            <OverviewView
              state={state}
              onNavigate={setView}
              onRun={runPipeline}
              onToggleCompany={toggleCompany}
              onSelectAll={selectAllCompanies}
              onSelectNone={selectNoCompanies}
            />
          )}
          {view === "retrieval" && (
            <RetrievalView
              state={state}
              onRetry={retryRetrieval}
              onView={viewDownloadedFile}
              onDelete={deleteDownloadedFile}
              onUpload={uploadDownloadedFile}
              onContinue={continueToBuild}
            />
          )}
          {view === "extraction" && <ExtractionView state={state} onDownloadDataEngine={downloadDataEngine} />}
          {view === "reporting" && <ReportsView state={state} onDownload={downloadReport} />}
        </main>
      </div>
    </div>
  );
}
