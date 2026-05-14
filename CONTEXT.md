# Agentic AI — Domain Context

> Read this before touching any code. It defines the vocabulary, boundaries, and
> architectural contracts every agent and human contributor must respect.

---

## 1. What this system is

**Agentic AI** is an LLM-coordinated analytics platform for small businesses.
A user uploads their sales / purchase records (CSV or XLSX), then asks
natural-language questions. The system replies with grounded numeric results,
narrative insight, and chart-ready aggregates — all streamed live via SSE.

---

## 2. Glossary (use these terms exactly)

| Term | Meaning |
|---|---|
| **Turn** | One complete question → answer cycle, from `POST /query_stream` to `turn.end` |
| **Sub-agent** | A named pipeline that handles one class of analytic intent (`QueryAgent`, `AnalyticsAgent`, `RCAAgent`, `ForecastAgent`) |
| **Tool** | A single deterministic step in a sub-agent pipeline (e.g. `SqlPlanner`, `SqlExecutor`) |
| **TurnState** | The immutable, frozen state object passed between tools in a single turn |
| **Intent router** | The first-stage classifier — deterministic keyword+regex; outputs `chat` or `agentic` |
| **Dispatcher** | The second-stage LLM call that picks which sub-agent to invoke |
| **Pipeline** | The ordered list of `(tool_name, args)` steps a sub-agent declares |
| **Batch** | A single file upload — tracked in the `uploads` table with a `batch_id` |
| **Dataset** | Either `sales` or `purchase` — two physically identical SQLite tables |
| **Response cache** | SHA-256-keyed JSON store in `data/response_store.json`; invalidated on any upload/disconnect |
| **Cost guard** | Hard per-turn limits on LLM iterations (default: 8) and USD spend (default: $1) |
| **Capability token** | `INGESTION_PIN` / `READ_PIN` — the only way to access the Database tool; prevents ad-hoc SQL from tools that shouldn't write |
| **SSE event** | Server-Sent Event emitted during a turn: `tool.call`, `tool.result`, `cache.hit`, `final`, `turn.end` |
| **KPI** | A named metric (e.g. total_sales, orders) computed by the KPI engine without an LLM |
| **ADR** | Architectural Decision Record — a `docs/adr/NNNN-*.md` file recording a past design decision |

---

## 3. Module map

### Backend — `backend/`

| File / folder | Owns |
|---|---|
| `app/infrastructure.py` | Settings (pydantic-settings), SQLite schema, DB connection helpers, upload parsers, JSON cache, memory/synonyms |
| `app/analytics_engine.py` | TurnState + 14 tools + tool registry + Groq client + SSE EventEmitter + cost guard + Coordinator + 7 sub-agents |
| `app/core_system.py` | FastAPI app, all HTTP routes, CORS, exception handlers, startup hook |
| `app/kpi/` | KPI registry, matcher, engine — computes named metrics without LLM |
| `app/hierarchy/` | Product / location / clarification hierarchy resolution |
| `app/vector/` | Vector store, embeddings, semantic search (pluggable adapter pattern) |
| `app/monitoring/` | Sentry config, tracing, instrumentation |
| `app/enrichment/` | Forecast, inventory, cost enrichment (mock + real adapters) |
| `app/database/` | DB engine abstraction (SQLite default; Postgres via `DATABASE_URL`) |
| `app/time_engine.py` | Date token resolution and dataset date-range queries |
| `app/dedup.py` | Deduplication logic for uploaded batches |
| `app/errors.py` | Typed error hierarchy |

Import direction (never reverse): `core_system → analytics_engine → infrastructure`

### Frontend — `frontend/src/`

| File | Owns |
|---|---|
| `App.tsx` | Layout shell, auth gate, view switcher |
| `ui_system.tsx` | All routable pages + subcomponents |
| `client_core.ts` | TypeScript types + API client (REST + SSE) + Zustand store |
| `index.css` | Tailwind directives + theme tokens |

---

## 4. Data model

Two tables — `sales` and `purchase` — sharing an identical 19-column schema:

```
Date (required), Total Amount (required), Party Name, Voucher No,
Voucher Type, Quantity, Rate, Discount, Tax, Batch No,
Item Name, Group, Category, Unit, Narration,
id (PK), batch_id (FK→uploads), source, file_name, inserted_at
```

Header aliases: a closed `HEADER_ALIASES` map normalises messy ERP headers on ingest.

---

## 5. Key invariants (never break these)

1. **No cross-request Groq key leakage** — each turn uses a `contextvars`-scoped `GroqClient`; the browser supplies the key in `X-Groq-Api-Key`.
2. **Database tool is the only SQL path** — no tool may query SQLite directly; all access must go through `Database` tool with `READ_PIN` or `INGESTION_PIN`.
3. **Sub-agent pipelines are deterministic** — no dynamic tool selection inside a pipeline; only the Dispatcher LLM chooses the sub-agent.
4. **Cache invalidation is total** — any upload or disconnect clears `response_store.json` completely.
5. **Import direction is downward** — `core_system → analytics_engine → infrastructure`. No upward imports.

---

## 6. LLM provider

- **Provider:** Groq (`/openai/v1/chat/completions`)
- **Default model:** `llama-3.3-70b-versatile`
- **Used for:** Dispatcher (sub-agent selection), InsightEngine (narrative), SqlPlanner (SQL generation), chat responder
- **Not used for:** intent routing (deterministic), KPI calculation, dashboard aggregates, forecasting (in-house linear regression)
