# Agentic AI — Domain Context

> Read this before touching any code. Defines the vocabulary, module boundaries,
> invariants, and known risks every agent and contributor must respect.
> For deep architecture details see ARCHITECTURE.md.
> For production risks see PRODUCTION_READINESS_REPORT.md.

---

## 1. What this system is

**Agentic AI** is a local-first, single-user, LLM-coordinated analytics platform for small businesses.
Users upload CSV/XLSX financial records (sales + purchases), then ask natural-language questions.
The system answers with grounded numeric results, narrative insight, and chart-ready aggregates — streamed live.

Current maturity: **MVP / pre-production** (score 58/100). Not suitable for multi-tenant or high-traffic deployments without auth and Postgres.

---

## 2. Glossary — use these terms exactly

| Term | Definition |
|---|---|
| **Turn** | One complete question → answer cycle, from `POST /query_stream` to `turn.end` SSE event |
| **TurnState** | Frozen Pydantic model — single source of truth passed between tools. Never mutated; use `.apply(**updates)` |
| **Sub-agent** | A named class declaring a fixed `pipeline` of `(tool_name, args)` steps |
| **Tool** | A single deterministic step in a pipeline. Extends `Tool` ABC, implements `async run(state, args) → ToolResult` |
| **ToolResult** | Return value of a tool: `{ok, output, state_updates, delta_metrics, error}` |
| **Intent router** | `classify_query_kind()` — deterministic keyword+regex classifier. Outputs `data_query | chat | general_knowledge | missing_data` |
| **KPI fast-path** | Before the 14-tool pipeline, the coordinator matches the question against the KPI registry. On a confident match, executes a pre-validated SQL template — no LLM |
| **Dispatcher** | Single Groq LLM call that picks which sub-agent to run. Returns `{sub_agent, reason}` |
| **Pipeline** | Ordered list of `(tool_name, args)` steps a sub-agent declares at definition time |
| **Batch** | A single file upload — tracked in the `uploads` table with a `batch_id` (UUID) |
| **Dataset** | Either `sales` or `purchase` — two physically identical SQLite tables |
| **Response cache** | SHA-256-keyed JSON store in `data/response_store.json`. Invalidated on any upload/disconnect |
| **Cost guard** | Hard per-turn limits: iterations (default 8) and USD spend (default $1.00) |
| **Capability token** | `INGESTION_PIN` / `READ_PIN` — the only way to access the `Database` tool |
| **SSE event** | Server-Sent Event emitted during a turn: `tool.call`, `tool.result`, `cache.hit`, `final`, `turn.end` |
| **PresentationEmitter** | Wrapper that strips internal fields (`sql_used`, `formula`, `stack_trace`, etc.) before events reach the browser |
| **KPI** | A named metric (e.g. `total_sales`, `orders`) computed by the KPI engine without an LLM |
| **Data version** | Monotonic counter bumped on every upload/disconnect — used by frontend to detect stale data |
| **ADR** | Architectural Decision Record in `docs/adr/NNNN-*.md` |

---

## 3. Module map

### Backend — `backend/`

| File / folder | Owns |
|---|---|
| `app/infrastructure.py` | Settings, SQLite schema, DB helpers, upload parsers, response cache, synonyms |
| `app/analytics_engine.py` | TurnState, EventEmitter, CostGuard, GroqClient, 14 tools, registry, 7 sub-agents, Coordinator |
| `app/core_system.py` | FastAPI app, all HTTP routes, CORS, rate limiter, startup, exception handlers |
| `app/kpi/` | KPI registry (SQLite-backed), matcher, formula execution engine |
| `app/hierarchy/` | Product + location tree management (v1 adjacency list + v2 6-level enterprise) |
| `app/vector/` | Vector store, embeddings, semantic search — **installed but not yet wired into pipeline** |
| `app/monitoring/` | Sentry config, tracing, request context |
| `app/enrichment/` | Forecast (linear regression), inventory snapshot, cost master |
| `app/database/` | Engine abstraction — SQLite default, asyncpg Postgres when `DATABASE_URL` is set |
| `app/time_engine.py` | Dataset-relative date token resolution |
| `app/dedup.py` | File-level SHA-256 deduplication |
| `app/errors.py` | Typed error log (SQLite-backed, queryable via `/errors`) |

**Import direction (never reverse):**
```
core_system → analytics_engine → infrastructure
core_system → kpi / hierarchy / vector / monitoring / enrichment / database / time_engine / dedup / errors
```

### Frontend — `frontend/src/`

| File | Owns |
|---|---|
| `App.tsx` | Layout shell, login gate (cosmetic only — not real auth), view switcher, error boundaries |
| `ui_system.tsx` | All pages: Dashboard, AiAssistant, UploadData, ShopInfo |
| `client_core.ts` | TypeScript types, API client (REST + SSE), Zustand global store |
| `index.css` | Tailwind directives + theme tokens |

---

## 4. Key invariants — never break these

1. **TurnState is immutable.** Never `state.field = value`. Always `state = state.apply(field=value)`.
2. **Database tool is the only SQL path.** All SQLite access from the pipeline must go through the `Database` tool with `READ_PIN` or `INGESTION_PIN`. No direct `fetch_all()` calls from tools.
3. **Pipelines are deterministic.** No LLM-driven tool selection inside a pipeline. Only the Dispatcher chooses the sub-agent.
4. **No cross-request Groq key leakage.** Each turn uses a `contextvars`-scoped `GroqClient`. Never store user keys in shared state.
5. **Cache invalidation is total.** Any upload or disconnect calls `invalidate_all()` — no partial invalidation.
6. **Import direction is downward.** `core_system → analytics_engine → infrastructure`. No reverse imports.
7. **PresentationEmitter wraps all user-facing SSE.** Never emit raw internal events directly to the browser.

---

## 5. Known critical issues (from audit)

| ID | Issue | Severity |
|---|---|---|
| C1 | Frontend credentials hardcoded in `App.tsx` (user: Mansuri, pass: 182012) | CRITICAL |
| C2 | Backend has no authentication — all routes are public | CRITICAL |
| C3 | LLM-generated SQL executed with keyword-denylist only (no parameterization) | HIGH |
| C4 | Response cache unbounded flat JSON file (no TTL, no eviction) | HIGH |
| C5 | `analytics_engine.py` is a 57 KB monolith | HIGH |

See `PRODUCTION_READINESS_REPORT.md` for full list and remediation plan.

---

## 6. LLM provider

- **Provider:** Groq (`/openai/v1/chat/completions`)
- **Default model:** `llama-3.3-70b-versatile`
- **Used for:** Dispatcher (sub-agent selection), SqlPlanner, InsightEngine, chat/knowledge responder
- **Not used for:** intent routing (deterministic), KPI calculation, dashboard aggregates, forecasting (linear regression)
- **Fallback:** None — if Groq is down, agentic pipeline is unavailable

---

## 7. Scores (audit 2025-05-15)

| Dimension | Score |
|---|---|
| System Maturity | 58 / 100 |
| Production Readiness | 42 / 100 |
| Scalability | 35 / 100 |
| Security | 30 / 100 |
| AI Orchestration Quality | 68 / 100 |
