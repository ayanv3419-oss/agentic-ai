# Metric AI — System Summary

A concise architectural overview of the platform as actually implemented in the
codebase under `Agentic Ai/`. References to files use the project's relative
paths.

---

## 1. System Overview

**Purpose.** Metric AI is an LLM-coordinated analytics platform that lets a
small business upload its sales / purchase records (CSV or XLSX) and then ask
natural-language questions about that data. The system answers with grounded
numeric results, narrative insight, and chart-ready aggregates.

**High-level description.** A React single-page application is the user
surface. A FastAPI backend authenticates the user, ingests files into a
SQLite financial database, and serves an SSE-streamed query pipeline in which
an LLM coordinator selects one analytic sub-agent that walks a deterministic
sequence of internal tools (route → schema → SQL plan → SQL write → validate →
execute → aggregate → narrate → format → store).

---

## 2. Architecture

The codebase is organised into **5 backend modules** and **5 frontend
modules**, each owning one clear area of responsibility. Anything else
(tests, migrations, dist artefacts, data files) lives outside this surface.

### Backend — `backend/`
| File | Responsibility |
|---|---|
| [main.py](backend/main.py) | FastAPI entry: middleware, exception handlers, startup hook, logging setup |
| [app/database.py](backend/app/database.py) | Settings + envelope helpers + DB schema + connection helpers + upload parsers + JSON cache + memory/synonyms — the entire storage and ingestion data layer |
| [app/tools.py](backend/app/tools.py) | TurnState + 14 tools + registry + Groq client + SSE EventEmitter + cost-guard — the runtime substrate every pipeline step uses |
| [app/agents.py](backend/app/agents.py) | Coordinator (intent router, dispatcher, chat responder, loop) + sub-agent base + 7 sub-agents (`QueryAgent`, `AnalyticsAgent`, `RCAAgent`, `ForecastAgent`, `DashboardAgent`, `DataCleanAgent`, `ResponseAgent`) |
| [app/api.py](backend/app/api.py) | Bearer-token auth + every HTTP route the frontend calls |

Each cross-module import goes downward only:
`main → api → agents → tools → database`. There are no cycles.

### Frontend — `frontend/src/`
| File | Responsibility |
|---|---|
| [main.tsx](frontend/src/main.tsx) | Vite entry — mounts `<App/>` |
| [App.tsx](frontend/src/App.tsx) | Layout shell (Sidebar, TopBar, PageHeader), Login screen, auth gate, view switcher |
| [pages.tsx](frontend/src/pages.tsx) | All four routable pages — `Dashboard`, `AiAssistant`, `UploadData`, `ShopInfo` — and their tightly-coupled subcomponents (`UploadsTable`, `ChatChart`, KPI cards, Google glyph) |
| [charts.ts](frontend/src/charts.ts) | Chart-axis helpers shared between the Dashboard and the AI Assistant: `inferGranularity`, `formatBucketTick`, `formatBucketTooltip`, `getXAxisTickConfig` |
| [api.ts](frontend/src/api.ts) | TypeScript types + API client (REST + SSE) + Zustand global store + `cn()` class-name utility |

Plus `index.css` (Tailwind directives + theme tokens). Imports flow `App → pages → api / charts`; nothing imports back into App.

### Database layer
- **Engine:** SQLite via `aiosqlite` (async) and `sqlite3` (sync ingest path),
  WAL journaling, indexes on `Date`, `Party Name`, `batch_id`.
- **File:** `data/financial_records.db`.
- **Schema:** Two physically identical tables, `sales` and `purchase`, defined
  in [database.py](backend/app/database.py). 19 user columns plus `id`,
  `batch_id`, `source`, `file_name`, `inserted_at`. Required columns: `Date`,
  `Total Amount`. A closed `HEADER_ALIASES` map drives fuzzy-matching of
  incoming spreadsheet headers.
- **Operational tables (same DB):** `uploads` (batch registry with status =
  `active | error | removed`).
- **Cache:** `data/response_store.json`, atomic JSON store keyed by SHA-256 of
  the normalized question.

### AI / agent orchestration
1. **Intent router** — deterministic keyword + regex classifier. Routes the
   question to `chat` (small talk, single LLM call, no tools) or `agentic`.
2. **Dispatcher** — one Groq call returns strict JSON `{sub_agent, reason}`.
   The selected sub-agent must be one of `QueryAgent`, `AnalyticsAgent`,
   `RCAAgent`, `ForecastAgent`.
3. **Sub-agent pipeline** — each sub-agent declares a fixed `pipeline` of
   `(tool_name, args)` steps. The base `SubAgent.run()` walks them, emits
   SSE `tool.call` / `tool.result` events, merges `state_updates` into a
   frozen `TurnState`, and halts on first error.
4. **Tool registry** — exactly **14 tools** explicitly registered at boot:
   `RouteClassifier`, `IntentAnalyzer`, `TimeKPI`, `EntityResolver`,
   `SchemaRetriever`, `SqlPlanner`, `SqlWriter`, `SqlValidator`,
   `SqlExecutor`, `ResultAggregator`, `InsightEngine`, `ResponseFormatter`,
   `ResponseStored`, `Database`. Database access is restricted to the
   `Database` tool via `INGESTION_PIN` and `READ_PIN` capability tokens.
5. **Cost / safety guard** — caps per-turn iterations and USD spend before
   each loop step.

### External integrations
- **Groq Chat Completions** — async `httpx` client with retry/back-off;
  `complete()` and `complete_stream()` never raise (they return a typed
  error). Per-request scoping via `contextvars` so a key sent in
  `X-Groq-Api-Key` does not leak between concurrent requests. Default model:
  `llama-3.3-70b-versatile`.
- **Google OAuth & Drive:** Frontend has a "Continue with Google" flow and a
  `/drive/sync` call site, but the backend endpoints are intentionally
  **disabled** in the current build (return `auth_disabled`).

---

## 3. Core Workflow

### Data ingestion
1. `UploadData` page POSTs a CSV / XLSX (≤ 1 GB) plus `target` (`sales` or
   `purchase`) to `/upload`.
2. Route streams the body to a tempfile in 1 MB chunks and hands it to
   `DataCleanAgent` (in [agents.py](backend/app/agents.py)).
3. The agent runs a non-LLM pipeline: FileParser (header auto-detection) →
   HeaderMapper (closed alias map; rejects files missing required columns) →
   DataNormalizer (date / numeric coercion) → RowValidator → atomic
   `Database.insert` (via `INGESTION_PIN`) → PostValidator (row counts and
   date-range sanity).
4. The batch is recorded in `uploads`; the answer cache is invalidated.
5. On any failure, an `uploads` row with `status='error'` and a sanitized
   error message is still written so the UI can display it.

### Query / request processing
1. Browser POSTs `/query_stream` with `Authorization: Bearer …` and
   `X-Groq-Api-Key: …`; payload is a 1–4000 char `question`.
2. Pre-stream validation checks the key shape and a non-empty question.
3. The handler creates a per-request `GroqClient`, opens an SSE
   `EventEmitter` queue, spawns a runner task and a 15 s heartbeat task,
   and returns a `StreamingResponse(text/event-stream)` immediately.

### Agent / tool execution pipeline
1. **Cache lookup** — SHA-256 of the normalized question. On hit the answer
   is replayed as `cache.hit` + `final` + `turn.end`; no LLM, no tools.
2. **Intent router** classifies `chat` vs `agentic`.
   - `chat` → `chat_responder` performs one Groq call and emits `final`.
   - `agentic` → cost-guard checks → dispatcher LLM picks a sub-agent.
3. The sub-agent's pipeline executes step-by-step inside `SubAgent.run`:
   route classification, intent extraction, time / KPI resolution, entity
   resolution, schema retrieval, SQL planning, SQL writing, SQL validation,
   SQL execution (read-only via `Database` tool), aggregation, insight
   generation, response formatting, persistence. `ForecastAgent` inserts a
   linear-regression forecast step before formatting.

### Response generation
- Each tool emits `tool.call` and `tool.result` events.
- Successful turns end with a `final` event carrying `{ answer, chart, mode,
  from_cache }`, then `turn.end` carrying iteration counts, token counts,
  errors, and the executed tool-call audit trail.
- The frontend `streamQuery` parses each `event:` / `data:` SSE block and the
  AI Assistant page renders narrative text, status pills per tool, and an
  optional Recharts area chart.

---

## 4. Technologies Used

| Layer | Technology |
|---|---|
| Frontend framework | React 18 + Vite 5 + TypeScript 5 |
| Frontend styling | Tailwind CSS 3, `lucide-react` icons |
| Frontend state | Zustand 5 with `persist` (localStorage) |
| Charts | Recharts |
| Backend framework | FastAPI + Uvicorn |
| Backend language | Python 3 (async / `asyncio`) |
| Validation | Pydantic v2, `pydantic-settings` |
| HTTP client | `httpx` (async) |
| Database | SQLite (`aiosqlite` + `sqlite3`), WAL mode |
| Spreadsheet parsing | `openpyxl` (XLSX, read-only streaming), built-in `csv` |
| Sessions / signing | `itsdangerous`, `starlette.middleware.sessions` |
| LLM provider | Groq (`/openai/v1/chat/completions`) — default model `llama-3.3-70b-versatile` |
| Cache | Atomic JSON file (`data/response_store.json`) |
| Dev tooling | `concurrently` (parallel FE+BE dev), `tsc`, `vite build` |
| Deployment hint | Production frontend points at `https://agentic-ai-anet.onrender.com` (Render-style hosting) |

---

## 5. Core Features

### Main capabilities
- Single-command dev startup (`npm run dev` runs FastAPI on `:8000` and Vite on
  `:5173`).
- Authenticated, single-tenant analytics console with persistent client state.
- Chat-first natural-language analytics over a strictly-typed financial
  schema.
- Server-Sent Events streaming with per-tool progress, heartbeats, and a
  structured turn audit trail.

### AI functionality
- Two-stage routing: deterministic chat-vs-agentic intent router, followed by
  an LLM dispatcher that picks one of four analytic sub-agents (`QueryAgent`,
  `AnalyticsAgent`, `RCAAgent`, `ForecastAgent`).
- 14-tool deterministic pipeline per sub-agent — including LLM-assisted
  insight generation, validated SQL execution, restricted SQL access, and
  response storage.
- Per-turn cost / iteration guard with hard limits (default: 8 iterations,
  $1 USD).
- Forecasting via in-house least-squares linear regression on the bucketed
  series (no external ML dependencies).
- Per-request Groq client scoping via `contextvars`, enabling multi-tenant
  Groq keys submitted from the browser without cross-request leakage.

### Data handling
- CSV and XLSX ingestion (≤ 1 GB, streamed to a temp file in 1 MB chunks).
- Auto-detection of header rows in messy ERP exports plus a closed alias map
  for header normalization.
- Atomic batched inserts behind a capability-token-protected `Database` tool.
- Two logical datasets sharing one schema: `sales` and `purchase`.
- Upload registry with `active`, `error`, and `removed` lifecycle states; a
  `disconnect` endpoint deletes a batch and invalidates the cache.
- SHA-256 question cache for instant replay of previously answered questions;
  any upload or disconnect invalidates it.

### Authentication & access
- Admin login (`/auth/login`) issues a signed bearer token whose secret and
  TTL are environment-configured. Tokens are stateless — restarting the
  backend with a new `AUTH_TOKEN_SECRET` invalidates every outstanding
  session.
- Frontend gates the entire app on a non-expired token; 401 responses
  automatically clear the local auth state and return the user to the login
  screen.
- Every protected endpoint (upload, dashboard, uploads list, query stream,
  cache clear) is wrapped with `Depends(require_auth)`.
- Google OAuth / Drive sync exists as front-end UI and back-end placeholder
  endpoints, but is **disabled in the current build**.

### Reporting & analytics
- `/dashboard?month=YYYY-MM` returns KPI totals (`total_sales`, `orders`,
  `customers`) plus a per-day series, computed entirely without an LLM by
  `DashboardAgent`.
- The AI Assistant view returns chart-ready aggregates alongside narrative
  answers; charts are rendered with Recharts.
- The query pipeline persists every successful answer (question, SQL, rows,
  insight) into `response_store.json` for reuse and auditability.
