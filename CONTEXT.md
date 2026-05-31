# Metric AI — Domain Context

> Read this before touching any code. Defines the vocabulary, module boundaries,
> invariants, and known risks every agent and contributor must respect.
> For deep architecture details see ARCHITECTURE.md.
> For production risks see PRODUCTION_READINESS_REPORT.md.

---

## 1. What this system is

**Metric AI** is a local-first, single-user, LLM-coordinated analytics platform for small businesses.
Users upload CSV/XLSX financial records (sales + purchases), then ask natural-language questions.
The system answers with grounded numeric results, narrative insight, and chart-ready aggregates — streamed live.

Current maturity: **MVP / pre-production** (score 58/100). Not suitable for multi-tenant or high-traffic deployments without auth and Postgres.

---

## 2. Glossary — use these terms exactly

| Term | Definition |
|---|---|
| **Turn** | One complete question → answer cycle, from `POST /query_stream` to `turn.end` SSE event |
| **TurnState** | Frozen Pydantic model — single source of truth passed between tools. Never mutated; use `.apply(**updates)` |
| **Agentic loop** | `AgenticLoop` — the LLM-orchestrated engine for the natural-language question path. The LLM picks a capability, inspects the result, and loops until satisfied, then writes the final answer. Replaced the fixed sub-agent pipelines (see ADR-0004) |
| **Capability** | A coarse, LLM-callable wrapper the agentic loop chooses from: `resolve_time_window`, `resolve_entities`, `run_data_query`, `generate_narrative`. Each composes a deterministic sub-sequence of the 14 fine-grained tools |
| **Sub-agent** | A named class for a UI-triggered deterministic flow — now only `DashboardAgent` (dashboard payload) and `DataCleanAgent` (upload ingest). The query path no longer uses sub-agents |
| **Tool** | A single deterministic step. Extends `Tool` ABC, implements `async run(state, args) → ToolResult`. The 14 fine-grained tools are composed by capabilities; the LLM never picks them directly |
| **ToolResult** | Return value of a tool: `{ok, output, state_updates, delta_metrics, error}` |
| **Intent router** | `classify_query_kind()` — deterministic keyword+regex classifier. Outputs `data_query | chat | general_knowledge | missing_data` |
| **KPI fast-path** | Before the agentic loop, the coordinator matches the question against the KPI registry. On a confident match, executes a pre-validated SQL template — no LLM |
| **Deterministic pre-pass** | `RouteClassifier` + `IntentAnalyzer` run free before the loop; their output is a *non-binding hint* in the loop's opening context, not a routing decision |
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
| `app/analytics_engine.py` | TurnState, EventEmitter, CostGuard, LLM client, 14 tools + 4 capabilities, registry, AgenticLoop, Dashboard/DataClean sub-agents, Coordinator |
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
3. **The LLM orchestrates capabilities; capabilities are deterministic inside.** On the query path the LLM picks capabilities dynamically (the agentic loop). But each capability composes a *fixed* sub-sequence of the 14 tools, and the LLM never picks those 14 directly. The cheap front door (response cache, KPI fast-path) and the `RouteClassifier`/`IntentAnalyzer` pre-pass stay fully deterministic. See ADR-0004.
4. **No cross-request state leakage.** Each turn uses a fresh LLM call scoped to that request. Never store per-request state in shared module-level variables.
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
| C6 | Margin/profit logic and the 22 re-pointed KPIs assume a fixed workbook schema (`u_sales_transactions`/`u_inventory_master` with `net_sales`, `quantity`, `sku_id`, `unit_cost`, `final_product`). Other schemas degrade gracefully — the LLM is told margin is unavailable and KPIs return a capability message — but cannot compute these metrics. | MEDIUM |

See `PRODUCTION_READINESS_REPORT.md` for full list and remediation plan.

---

## 6. LLM provider

- **Local dev:** Ollama (`http://localhost:11434/v1`) — model `qwen3:1.7b` (CPU, no GPU required)
- **Production (Render):** Together.ai (`https://api.together.xyz/v1`) — model `Qwen/Qwen3-8B`
- **Config:** `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` env vars — any OpenAI-compatible endpoint works
- **Used for:** AgenticLoop orchestration (native tool calling — the LLM picks capabilities), SqlPlanner, InsightEngine, chat/knowledge responder
- **Not used for:** the deterministic pre-pass (`RouteClassifier`/`IntentAnalyzer`), `classify_query_kind`, KPI calculation, dashboard aggregates, forecasting (linear regression), the response cache + KPI fast-path
- **Fallback:** None — if the LLM endpoint is down, the agentic loop returns a turn-level error (the cache + KPI fast-path still serve what they can)

---

## 7. Scores (audit 2025-05-15)

| Dimension | Score |
|---|---|
| System Maturity | 58 / 100 |
| Production Readiness | 42 / 100 |
| Scalability | 35 / 100 |
| Security | 30 / 100 |
| AI Orchestration Quality | 68 / 100 |
