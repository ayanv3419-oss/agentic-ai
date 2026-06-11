# MetricAI — Domain Context

> Read this before touching any code. Defines the vocabulary, module boundaries,
> invariants, and known risks every agent and contributor must respect.

---

## 1. What this system is

**MetricAI** is a self-serve, multi-tenant SaaS analytics platform. Tenants upload
CSV/XLSX financial records then ask natural-language questions. The system answers
with grounded numeric results, narrative insight, and chart-ready aggregates —
streamed live.

**Phase-3 architecture**: the LLM writes SQL directly against per-tenant `u_*`
tables using a ReAct (tool-calling) loop. There is no KPI registry, no hierarchy
engine, no enrichment layer — those packages have been deleted.

---

## 2. Glossary

| Term | Definition |
|---|---|
| **Turn** | One complete question → answer cycle, from `POST /query_stream` to `turn.end` SSE event |
| **ReAct loop** | `AgenticCoordinator` — LLM picks from 11 fine-grained tools per iteration until satisfied, then writes the final answer |
| **Tool** | Single deterministic step callable by the LLM. Extends `Tool` ABC in `coordinator/`. The LLM picks these directly (not via capability wrappers) |
| **Schema tool** | First tool the LLM calls: inspects `information_schema` + `_relationships` to emit column/JOIN hints. Registered as `SchemaInspector` |
| **sqlWriter** | LLM-facing tool that generates SQL via `MetricSqlBuilder` deterministic shortcuts for margin/profit, then falls back to raw LLM SQL |
| **SqlDryRun** | EXPLAIN-based validator that rejects SQL before execution |
| **SqlExecutor** | Runs validated SQL against Postgres; returns rows |
| **Tenant** | Isolated user unit — each gets a dedicated Postgres schema `tenant_<id>` with `SET LOCAL search_path` per transaction |
| **`u_*` tables** | Dynamic per-tenant tables created by ingest (`u_sales_transactions`, `u_inventory_master`, etc.). Schema is inferred from uploaded data |
| **MetricSqlBuilder** | `schema_mapping/builder.py` — deterministic SQL shortcuts for margin/profit/ranking. Used live by `sql_writer.py`. NOT dead code |
| **SSE event** | Server-Sent Event: `tool.call`, `tool.result`, `cache.hit`, `final`, `turn.end` |
| **Data version** | Monotonic counter bumped on every upload — frontend uses this to detect stale data |
| **Conversation** | Stored chat session in `conversations` / `conversation_messages` tables in the tenant's schema |
| **ADR** | Architectural Decision Record in `docs/adr/NNNN-*.md` |

---

## 3. Module map

### Backend — `backend/app/`

| File / folder | Owns |
|---|---|
| `core_system.py` | FastAPI app, all HTTP routes, CORS, rate limiter, startup lifespan, exception handlers |
| `coordinator/` | ReAct loop (`AgenticCoordinator`), 11 tools, `llm.py` (Qwen + Gemini client), prompt builders |
| `coordinator/sub_agents/sql_writer.py` | SQL generation; imports `MetricSqlBuilder` for margin/profit shortcuts |
| `schema_mapping/builder.py` | `MetricSqlBuilder` — deterministic margin/profit/ranking SQL. **Keep — used live** |
| `schema_mapping/relationships.py` | Cross-table JOIN-key graph, stored in `_relationships`. Self-provisions on first load |
| `dynamic_ingest.py` | XLSX/CSV upload → per-tenant `u_*` tables. Type inference, date parsing, sheet dedup |
| `infrastructure.py` | Settings, DB helpers, upload parser, response cache, synonyms, ALLOWED_TABLES |
| `db_engine.py` | Engine abstraction — asyncpg Postgres + `translate_sql()` (SQLite→PG dialect translator) |
| `identity.py` | HMAC Bearer tokens, bcrypt passwords, timing-safe auth, TOCTOU-safe signup |
| `tenant_context.py` | `Principal` dataclass, `require_principal` FastAPI dependency, search_path binding |
| `conversation_store.py` | Chat history CRUD against `conversations` / `conversation_messages` |
| `time_engine.py` | Dataset-relative date token resolution (`dataset_today`, `dataset_month`, etc.) |
| `dedup.py` | File-level SHA-256 deduplication |
| `errors.py` | Typed error log, async fire-and-forget write to Postgres |
| `vector/` | Vector store + embeddings — installed, not yet wired into the query pipeline |
| `database/` | `engine_kind()`, `engine_status()` helpers |

**Import direction (never reverse):**
```
core_system → coordinator → infrastructure / db_engine / schema_mapping
core_system → dynamic_ingest / identity / tenant_context / conversation_store
core_system → time_engine / dedup / errors / vector / database
```

### Frontend — `frontend/src/`

| File | Owns |
|---|---|
| `App.tsx` | Layout shell, auth gate, view switcher, error boundaries |
| `ui_system.tsx` | All pages: Dashboard, AiAssistant, UploadData, ShopInfo |
| `client_core.ts` | TypeScript types, API client (REST + SSE), Zustand global store |
| `index.css` | Tailwind directives + theme tokens |

---

## 4. Key invariants — never break these

1. **Postgres is required.** The startup lifespan (`core_system.py`) calls `require_postgres()` at boot; no SQLite fallback in production. Local tests use SQLite via `conftest.py`.
2. **Schema-per-tenant.** Every DB operation inside a request runs inside `SET LOCAL search_path = tenant_<id>, public`. Never access cross-tenant data or use fully-qualified table names from per-tenant code.
3. **`u_*` tables only.** The LLM must only query tables in the tenant's own schema. `SqlExecutor` enforces this via `ALLOWED_TABLES` denylist.
4. **No cross-request state leakage.** Each turn uses a fresh LLM call scoped to that request. Never store per-request state in shared module-level variables.
5. **Cache invalidation is total.** Any upload calls `invalidate_all()` — no partial invalidation.
6. **`MetricSqlBuilder` is live.** `schema_mapping/builder.py` is imported in `sql_writer.py:335`. Do not delete it.
7. **Timing-safe auth.** `identity.py:authenticate()` always runs a bcrypt comparison even on email-not-found paths (uses `_DUMMY_HASH`).
8. **`conversation_store.py` uses unqualified table names.** The search_path set by `require_principal` routes conversation reads/writes to the correct tenant schema automatically.

---

## 5. Data flow — a single question

```
POST /query_stream  →  require_principal (sets search_path)
  →  classify_query_kind()  [deterministic keyword/regex]
  →  AgenticCoordinator.run_turn()
       loop:
         LLM picks tool from {SchemaInspector, sqlWriter, SqlDryRun, SqlExecutor, ...}
         tool runs deterministically
         result fed back to LLM context
       LLM emits final answer  →  SSE stream to browser
```

---

## 6. LLM providers

| Environment | Primary | Fallback |
|---|---|---|
| Local dev | Ollama (`localhost:11434`) — `qwen3:1.7b` or similar ≤4B Q4 | None |
| Production (Render) | Qwen (Alibaba) via `LLM_BASE_URL` | Gemini via `FALLBACK_LLM_BASE_URL` |

Config: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` env vars — any OpenAI-compatible endpoint.

Fallback logic: primary LLM failure triggers `_complete_with_tools_one_shot()` on the fallback (single attempt, no retry cascade).

---

## 7. Multi-tenant + auth (prod)

- `AUTH_ENABLED=true` in production
- All routes except `/health`, `/auth/*`, `/docs`, `/openapi.json`, `/redoc` require `Authorization: Bearer <token>`
- HMAC-signed tokens; bcrypt passwords
- Schema-per-tenant isolation: `tenant_<uuid>` Postgres schema per user
- `_AUTH_PUBLIC_PREFIXES` in `core_system.py` controls the public surface

---

## 8. Known remaining gaps

| ID | Issue | Status |
|---|---|---|
| C3 | LLM SQL executed with denylist only — no full parameterization | Open |
| C6 | `MetricSqlBuilder` assumes `u_sales_transactions`/`u_inventory_master` schema — other schemas get LLM-only path | Open |
| schema-portability | GH issues #1-#6 — Concept Resolver + Metric SQL Builder epic | In progress |
| two-tenant isolation | Per-tenant `_column_profile` table still not verified in prod | Open |
