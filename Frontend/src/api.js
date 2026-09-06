// ---------------------------------------------------------------------------
// Backend client.
//
// The UI never calls fetch() directly — every view goes through the `API`
// object below, which talks to the FastAPI service in Backend/.
//
// In development Vite proxies /api to the backend (see vite.config.js), so
// requests are same-origin. Point VITE_API_BASE at the server to bypass the
// proxy (the backend allows CORS from the Vite dev ports).
// ---------------------------------------------------------------------------

// Trailing slash stripped defensively - a `VITE_API_BASE` with one would
// otherwise produce a double slash before `/api` (e.g. `.../` + `/api/health`),
// which the backend 404s on rather than collapsing.
const BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/+$/, "");

class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, { method = "GET", body, signal } = {}) {
  let res;
  try {
    res = await fetch(`${BASE}/api${path}`, {
      method,
      signal,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    // Network-level failure: the backend isn't running, or is unreachable.
    // Surfaced distinctly so the UI can say "backend offline" rather than
    // reporting a pipeline error that never happened.
    throw new ApiError(
      "Cannot reach the backend. Is it running on port 8000?",
      0,
      String(cause),
    );
  }
  if (!res.ok) {
    // FastAPI puts the human-readable reason in `detail`.
    let detail = null;
    try {
      detail = (await res.json())?.detail ?? null;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail || `Request failed (${res.status})`, res.status, detail);
  }
  return res.status === 204 ? null : res.json();
}

// File uploads need a multipart body, which `request()` doesn't build - kept
// separate rather than teaching one helper both content types.
async function requestFile(path, method, formData) {
  let res;
  try {
    res = await fetch(`${BASE}/api${path}`, { method, body: formData });
  } catch (cause) {
    throw new ApiError("Cannot reach the backend. Is it running on port 8000?", 0, String(cause));
  }
  if (!res.ok) {
    let detail = null;
    try {
      detail = (await res.json())?.detail ?? null;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail || `Request failed (${res.status})`, res.status, detail);
  }
  return res.json();
}

export const API = {
  isConnected: true,
  ApiError,

  // -- metadata ------------------------------------------------------------
  health: () => request("/health"),
  getCompanies: () => request("/companies").then((d) => d.companies),
  getPeriods: () => request("/periods"),

  // -- pipeline ------------------------------------------------------------
  /** Start a run. `stages` selects the halves: ["download"], ["build"], or both.
   * `companies`, if given, is a subset of company ids to include; null/omitted
   * means all configured sources. */
  startPipeline(fy, quarter, stages = ["download", "build"], companies = null) {
    return request("/pipeline/run", { method: "POST", body: { fy, quarter, stages, companies } });
  },

  getPipelineStatus(runId, signal) {
    return request(`/pipeline/${runId}/status`, { signal });
  },

  listRuns: () => request("/pipeline/runs"),

  /** Resume a run that paused after Phase 1 for document review, into
   * extraction/reporting. Same run id, not a new run. */
  continuePipeline: (runId) => request(`/pipeline/${runId}/continue`, { method: "POST" }),

  // -- Phase 1 document review ----------------------------------------------
  // A downloaded (or manually uploaded) source file, viewable while a run is
  // paused after retrieval - or at any other time, since viewing is read-only.
  downloadedFileUrl: (runId, companyId) =>
    `${BASE}/api/downloads/${runId}/${encodeURIComponent(companyId)}/file`,

  viewDownloadedFile(runId, companyId) {
    window.open(API.downloadedFileUrl(runId, companyId), "_blank", "noopener");
  },

  /** Remove a downloaded file the reviewer judged wrong. Only accepted while
   * the run is awaiting review. Returns the updated run snapshot. */
  deleteDownloadedFile: (runId, companyId) =>
    request(`/downloads/${runId}/${encodeURIComponent(companyId)}/file`, { method: "DELETE" }),

  /** Manually supply a file for a source that failed or was removed. Returns
   * the updated run snapshot. */
  uploadDownloadedFile(runId, companyId, file) {
    const form = new FormData();
    form.append("file", file);
    return requestFile(`/downloads/${runId}/${encodeURIComponent(companyId)}/file`, "POST", form);
  },

  // -- artifacts -----------------------------------------------------------
  // Downloads are plain links rather than fetches so the browser handles the
  // save dialog and the file never passes through JS memory.
  reportUrl: (runId) => `${BASE}/api/reports/${runId}/download`,
  dataEngineUrl: (runId) => `${BASE}/api/data-engine/${runId}/download`,

  downloadReport(runId) {
    if (!runId) throw new ApiError("No run selected.", 0, null);
    window.open(API.reportUrl(runId), "_blank", "noopener");
  },

  /** The filled Data Engine workbook for this run. Served from the same
   * artifacts/output/ directory that gets synced to R2, so it survives an
   * instance restart in production. */
  downloadDataEngine(runId) {
    if (!runId) throw new ApiError("No run selected.", 0, null);
    window.open(API.dataEngineUrl(runId), "_blank", "noopener");
  },
};
