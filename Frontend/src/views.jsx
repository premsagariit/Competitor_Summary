import { AppIcon } from "./Icons.jsx";
import { Card, Button, Spinner, Badge, Checkbox, ProgressBar, CircularProgress, PhaseStepper, CompanyAvatar, ActivityLog, formatElapsed } from "./ui.jsx";

// ---------------------------------------------------------------------------
// Shared small pieces
// ---------------------------------------------------------------------------
function StatCard({ icon, label, value, sub, tone = "brand" }) {
  const tones = {
    brand: "text-brand-300 bg-brand-500/10",
    emerald: "text-emerald-300 bg-emerald-500/10",
    amber: "text-amber-300 bg-amber-500/10",
    violet: "text-violet-300 bg-violet-500/10",
  };
  return (
    <Card className="p-4 flex items-start gap-3">
      <div className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${tones[tone]}`}>
        <AppIcon name={icon} className="w-5 h-5" />
      </div>
      <div className="min-w-0">
        <div className="text-2xl font-semibold text-white tabular-nums leading-tight">{value}</div>
        <div className="text-xs text-slate-400 mt-0.5">{label}</div>
        {sub && <div className="text-[11px] text-slate-500 mt-1">{sub}</div>}
      </div>
    </Card>
  );
}

// Company display metadata comes from the backend (/api/companies) via
// state.companyMeta, so the ids here always match the ones the pipeline uses.
// The fallback keeps a newly configured source renderable before its metadata
// has arrived, rather than crashing the view on an unknown id.
function metaFor(state, id) {
  return (
    (state.companyMeta && state.companyMeta[id]) || {
      id,
      short: id,
      kind: "sahi",
      logo: null,
      color: "#64748b",
    }
  );
}

function phaseTone(status) {
  return status === "done" ? "emerald" : status === "error" ? "rose" : status === "running" || status === "downloading" || status === "extracting" ? "brand" : "slate";
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------
export function OverviewView({ state, onNavigate, onRun, onToggleCompany, onSelectAll, onSelectNone }) {
  const { phases, companies, activity, running, elapsed, reportProgress } = state;
  const awaitingReview = state.runStatus === "awaiting_review";

  return (
    <div className="flex flex-col gap-6">
      <Card className="p-6">
        <div className="flex items-center justify-between flex-wrap gap-4 mb-6">
          <div>
            <h2 className="text-lg font-semibold text-white">Pipeline Status</h2>
            <p className="text-sm text-slate-400 mt-0.5">{state.fy} {state.quarter} · Competitor Analysis run</p>
          </div>
          <div className="flex items-center gap-4">
            <CircularProgress
              value={(phases.filter((p) => p.status === "done").length / phases.length) * 100}
              size={84}
              stroke={7}
              label={formatElapsed(elapsed)}
              sub="elapsed"
            />
            {awaitingReview ? (
              <Button onClick={() => onNavigate("retrieval")}>
                <AppIcon name="alertTriangle" className="w-4 h-4" />
                Review documents
              </Button>
            ) : (
              <Button onClick={onRun} disabled={running}>
                {running ? <Spinner className="w-4 h-4" /> : <AppIcon name="play" className="w-4 h-4" />}
                {running ? "Running…" : "Start Pipeline"}
              </Button>
            )}
          </div>
        </div>
        {awaitingReview && (
          <div className="mb-5 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 flex items-center gap-2.5 text-amber-200 text-sm">
            <AppIcon name="alertTriangle" className="w-4 h-4 shrink-0" />
            Documents retrieved for {state.fy} {state.quarter}. Confirm each file on the
            Document Retrieval screen before extraction runs.
          </div>
        )}
        <PhaseStepper phases={phases} activeKey={null} onSelect={onNavigate} />
      </Card>

      <div className="grid grid-cols-2 gap-4">
        <StatCard icon="building" label="Companies tracked" value={Object.keys(companies).length} tone="brand" />
        <StatCard icon="fileText" label="Report progress" value={`${reportProgress}%`} tone="emerald" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">
        <Card className="p-5 lg:col-span-3">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold text-white">Retrieval &amp; Extraction Snapshot</h3>
            <div className="flex gap-2">
              <Button variant="ghost" className="!py-1 !px-2 text-xs" disabled={running} onClick={onSelectAll}>Select all</Button>
              <Button variant="ghost" className="!py-1 !px-2 text-xs" disabled={running} onClick={onSelectNone}>Select none</Button>
            </div>
          </div>
          <div className="flex flex-col gap-3">
            {Object.entries(companies).map(([id, c]) => {
              const company = metaFor(state, id);
              const isSelected = state.selectedCompanies.has(id);
              return (
                <div key={id} className={`flex items-center gap-3 ${isSelected ? "" : "opacity-40"}`}>
                  <Checkbox
                    checked={isSelected}
                    disabled={running}
                    onChange={() => onToggleCompany(id)}
                  />
                  <CompanyAvatar company={company} size={28} />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-slate-300 truncate mb-1">
                      {company.short}{company.self && <span className="text-brand-400 ml-1">(you)</span>}
                    </div>
                    <ProgressBar value={c.retrieval.progress} tone={phaseTone(c.retrieval.status) === "rose" ? "rose" : "brand"} />
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <Card className="p-5 lg:col-span-2 flex flex-col h-[340px]">
          <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <AppIcon name="database" className="w-4 h-4 text-slate-400" /> Activity Log
          </h3>
          <ActivityLog entries={activity} />
        </Card>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Retrieval (Phase 1)
// ---------------------------------------------------------------------------
function statusBadge(status) {
  const map = {
    queued: ["slate", "Queued"],
    downloading: ["sky", "Downloading"],
    extracting: ["sky", "Extracting"],
    done: ["emerald", "Done"],
    failed: ["rose", "Failed"],
    retrying: ["amber", "Retrying"],
    missing: ["amber", "Missing"],
    skipped: ["slate", "Not selected"],
  };
  const [tone, text] = map[status] || ["slate", status];
  return <Badge tone={tone}>{text}</Badge>;
}

function tierBadge(tier) {
  const tones = { core: "slate", pro: "sky", ultra: "violet", search: "amber", "already-downloaded": "emerald" };
  return <Badge tone={tones[tier] || "slate"}>{tier}</Badge>;
}

export function RetrievalView({ state, onRetry, onView, onDelete, onUpload, onContinue }) {
  // Files can only be viewed while a run is paused for review (or crashed
  // during retrieval) - matching the backend's guard, so a click never just
  // bounces off a 409.
  const reviewable = state.runStatus === "awaiting_review" || state.runStatus === "failed";

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between mb-5 flex-wrap gap-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Document Retrieval</h2>
          <p className="text-sm text-slate-400 mt-0.5">Parallel AI agents fetching public disclosures for {state.fy} {state.quarter}</p>
        </div>
        <Badge tone="slate">{Object.keys(state.companies).length} sources</Badge>
      </div>

      {state.runStatus === "awaiting_review" && (
        <div className="mb-5 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2.5 text-amber-200 text-sm">
            <AppIcon name="alertTriangle" className="w-4 h-4 shrink-0" />
            Confirm the file for each source below — open it with the eye icon, remove a
            wrong one, or upload a replacement. Continue once the set looks right.
          </div>
          <Button onClick={onContinue}>
            <AppIcon name="arrowRight" className="w-4 h-4" /> Continue to Extraction
          </Button>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
        {Object.entries(state.companies).map(([id, c]) => {
          const company = metaFor(state, id);
          const hasFile = c.retrieval.status === "done";
          return (
            <div key={id} className="rounded-xl border border-slate-800 bg-slate-900/50 p-4 flex flex-col gap-3">
              <div className="flex items-center gap-3">
                <CompanyAvatar company={company} size={40} />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-white truncate">{company.short}</div>
                  <div className="text-[11px] text-slate-500">{company.kind === "industry" ? "Industry-wide" : "SAHI"}</div>
                </div>
                {statusBadge(c.retrieval.status)}
              </div>
              <ProgressBar value={c.retrieval.progress} tone={c.retrieval.status === "failed" ? "rose" : c.retrieval.status === "done" ? "emerald" : "brand"} />
              <div className="flex items-center justify-between text-[11px] text-slate-500">
                <span className="flex items-center gap-1.5">Processor {tierBadge(c.retrieval.tier)}</span>
                <span className="flex items-center gap-2">
                  <span>{c.retrieval.size ? `${c.retrieval.size} MB` : "—"}</span>
                  {hasFile && (
                    <span className="flex items-center gap-0.5">
                      <button
                        title="View downloaded file"
                        onClick={() => onView(id)}
                        className="p-1 rounded text-slate-400 hover:text-white hover:bg-slate-800"
                      >
                        <AppIcon name="eye" className="w-3.5 h-3.5" />
                      </button>
                      <button
                        title={reviewable ? "Remove this file — it looks wrong" : "Only removable during document review"}
                        disabled={!reviewable}
                        onClick={() => onDelete(id)}
                        className="p-1 rounded text-rose-400 hover:bg-rose-500/10 disabled:opacity-30 disabled:cursor-not-allowed"
                      >
                        <AppIcon name="trash" className="w-3.5 h-3.5" />
                      </button>
                    </span>
                  )}
                </span>
              </div>
              {c.retrieval.status === "failed" && (
                <Button variant="danger" className="w-full justify-center !py-1.5" onClick={() => onRetry(id)}>
                  <AppIcon name="refresh" className="w-3.5 h-3.5" /> Retry retrieval
                </Button>
              )}
              {!hasFile && reviewable && c.retrieval.status !== "skipped" && (
                <label className="w-full flex items-center justify-center gap-1.5 text-xs text-brand-300 border border-dashed border-brand-500/40 rounded-lg py-1.5 cursor-pointer hover:bg-brand-500/10">
                  <AppIcon name="upload" className="w-3.5 h-3.5" />
                  Upload file manually
                  <input
                    type="file"
                    accept=".pdf,.xlsx"
                    className="hidden"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) onUpload(id, file);
                      e.target.value = "";
                    }}
                  />
                </label>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Extraction (Phase 2)
// ---------------------------------------------------------------------------
export function ExtractionView({ state, onDownloadDataEngine }) {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4 mb-5">
        <div>
          <h2 className="text-lg font-semibold text-white">Data Extraction</h2>
          <p className="text-sm text-slate-400 mt-0.5">Gemini-batched line-item extraction into the Data Engine workbook</p>
        </div>
        <div className="flex flex-col items-end shrink-0">
          <Button
            variant={state.dataEngineReady ? "primary" : "ghost"}
            disabled={!state.dataEngineReady}
            onClick={onDownloadDataEngine}
            title={state.dataEngineReady
              ? "Download the filled Data Engine workbook for this run"
              : "Available once extraction has written the workbook"}
          >
            <AppIcon name="download" className="w-4 h-4" />
            Data Engine
          </Button>
          <span className="text-[11px] text-slate-600 mt-1.5">
            {state.dataEngineReady ? `.xlsx · run ${state.runId}` : "Ready after extraction"}
          </span>
        </div>
      </div>
      <div className="overflow-x-auto scrollbar-thin">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500 text-xs uppercase tracking-wide border-b border-slate-800">
              <th className="py-2.5 pr-4 font-medium">Company</th>
              <th className="py-2.5 pr-4 font-medium">Progress</th>
              <th className="py-2.5 pr-4 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(state.companies).map(([id, c]) => {
              const company = metaFor(state, id);
              return (
                <tr key={id} className="border-b border-slate-800/60 last:border-0">
                  <td className="py-3 pr-4">
                    <div className="flex items-center gap-2.5">
                      <CompanyAvatar company={company} size={28} />
                      <span className="text-slate-200">{company.short}</span>
                    </div>
                  </td>
                  <td className="py-3 pr-4 w-64">
                    <ProgressBar value={c.extraction.progress} tone={c.extraction.status === "done" ? "emerald" : "brand"} />
                  </td>
                  <td className="py-3 pr-4">{statusBadge(c.extraction.status)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Reports (Phase 3)
// ---------------------------------------------------------------------------
export function ReportsView({ state, onDownload }) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">
      <Card className="p-5 lg:col-span-2 flex flex-col items-center text-center">
        <div className="w-32 h-40 rounded-lg bg-gradient-to-br from-slate-800 to-slate-900 border border-slate-700 flex items-center justify-center mb-4 relative overflow-hidden">
          <AppIcon name="fileText" className="w-10 h-10 text-slate-600" />
          {state.reportProgress < 100 && (
            <div className="absolute inset-x-0 bottom-0 bg-slate-950/70 py-1">
              <span className="text-[10px] text-slate-400">generating…</span>
            </div>
          )}
        </div>
        <div className="text-sm font-medium text-white">Competition_Summary_{state.fy}_{state.quarter}.pdf</div>
        <div className="text-xs text-slate-500 mt-1 mb-4">Matplotlib-assembled PDF · A4 landscape</div>
        <ProgressBar value={state.reportProgress} tone="emerald" className="mb-4" />
        <Button
          variant={state.reportReady ? "primary" : "ghost"}
          disabled={!state.reportReady}
          onClick={onDownload}
          className="w-full justify-center"
          title={state.reportReady ? "Download the generated PDF" : "Run the pipeline to generate a report"}
        >
          <AppIcon name="download" className="w-4 h-4" />
          Download report
        </Button>
        <p className="text-[11px] text-slate-600 mt-2">
          {state.reportReady
            ? `Ready · run ${state.runId}`
            : "Available once a run has produced a report."}
        </p>
      </Card>

      <Card className="p-5 lg:col-span-3">
        <h3 className="text-sm font-semibold text-white mb-4">Section Checklist</h3>
        <div className="flex flex-col gap-2.5">
          {state.reportSections.map((s, i) => (
            <div key={i} className="flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/40 px-3.5 py-2.5">
              <div className={`w-5 h-5 rounded-full flex items-center justify-center shrink-0 ${s.done ? "bg-emerald-500/20 text-emerald-400" : "bg-slate-800 text-slate-600"}`}>
                {s.done ? <AppIcon name="check" className="w-3.5 h-3.5" /> : <span className="text-[10px]">{i + 1}</span>}
              </div>
              <span className={`text-sm ${s.done ? "text-slate-200" : "text-slate-500"}`}>{s.name}</span>
              {s.active && <Spinner className="w-3.5 h-3.5 text-brand-400 ml-auto" />}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

