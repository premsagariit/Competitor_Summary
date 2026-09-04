# Competitor Intel — Pipeline Console

Dashboard for the competitor-analysis pipeline (Retrieval → Extraction →
Report Generation), wired to the FastAPI service in `Backend/`.

Every number, status and log line comes from the backend: the company list is
`source_links.json` and the activity feed is the pipeline's own output.
Nothing is simulated. The pipeline always pauses after retrieval so a
reviewer can confirm, replace or remove a downloaded document (or upload one
by hand) before extraction reads it.

Built with Vite + React 19 + Tailwind CSS v4.

## Running it

Two processes. Start the backend first:

```bash
cd Backend
myenv/Scripts/python.exe -m uvicorn competitor_analysis.api.main:app --reload --port 8000
```

Then the UI:

```bash
cd Frontend
npm install
npm run dev
```

Open the URL Vite prints (default http://localhost:5173). Vite proxies `/api`
to `http://127.0.0.1:8000`, so the browser sees a single origin. If the
backend runs elsewhere:

```bash
VITE_API_TARGET=http://host:port npm run dev   # proxy target (dev server)
VITE_API_BASE=http://host:port npm run build   # bake an absolute base into a build
```

The topbar shows a live connection indicator; if the backend is down it says
so and offers a retry, rather than showing stale or invented data.

```bash
npm run build      # production build -> dist/
npm run preview    # serve the production build locally
```

## What's here

- `index.html` / `src/main.jsx` — Vite entry point.
- `src/api.js` — the only place that talks to the network. Every view goes
  through the exported `API` object; it wraps `fetch`, unwraps FastAPI's
  `detail` error messages, and distinguishes "backend unreachable" (status 0)
  from a real HTTP error so the UI can report the right thing.
- `src/Icons.jsx` — small hand-rolled SVG icon set (no external icon package).
- `src/ui.jsx` — shared primitives: buttons, cards, badges, progress bars, the
  circular elapsed-time ring, the phase stepper, toasts, the activity log.
- `src/views.jsx` — the four screens: Overview, Retrieval (Phase 1),
  Extraction (Phase 2), Reports (Phase 3).
- `src/App.jsx` — layout plus the run lifecycle: starts a run, polls
  `/api/pipeline/{runId}/status` every 1.5s, and maps the snapshot onto view
  state. Reattaches to a run already in flight on reload.
- `src/index.css` — Tailwind import, fonts, animations, and the `brand` color
  theme (Tailwind v4 `@theme` block).
- `public/assets/logos/` — insurer logos, served at `/assets/logos/...`.

## Endpoints used

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | connection indicator |
| `GET /api/companies` | tracked sources + display metadata |
| `GET /api/periods` | selectable FY/quarters, and which filings are on disk |
| `POST /api/pipeline/run` | start a run (`stages`: `download`, `build`) |
| `GET /api/pipeline/{id}/status` | phases, per-company progress, activity |
| `GET /api/pipeline/runs` | reattach to an in-flight run |
| `POST /api/pipeline/{id}/continue` | resume a paused run into extraction/reporting |
| `GET /api/downloads/{run}/{company}/file` | view a retrieved document |
| `DELETE /api/downloads/{run}/{company}/file` | remove a wrongly-matched document |
| `POST /api/downloads/{run}/{company}/file` | upload a document by hand |
| `GET /api/reports/{run}/download` | the generated PDF |
| `GET /api/data-engine/{run}/download` | the populated workbook |

## Known gaps

These are deliberate, not oversights:

- **Per-source retry** — the Retrieval screen's retry re-runs the download
  stage for *all* sources, because there is no single-source endpoint yet. The
  toast says so rather than pretending otherwise.
- **Report section checklist** — the backend builds the PDF in one call, so
  sections complete together. They are not ticked off individually, because
  there is no per-section progress to report.
- **Runs are in-memory** — a backend restart loses run history. The outputs
  themselves are on disk under `Backend/artifacts/`.
