# Architecture — Metric AI Platform

> Last updated: 2025-05-15 | Audited against actual source

---

## 1. System Overview

Metric AI is a **local-first, single-user, LLM-coordinated analytics platform** for small business owners.
Users upload CSV/XLSX financial records, then ask natural-language questions.
The system answers with grounded SQL results, narrative insight, and chart-ready aggregates — all streamed live.

```
Browser (React SPA)
    │
    │  POST /query_stream   →  Server-Sent Events (streaming)
    │  POST /upload         →  JSON
    │  GET  /dashboard      →  JSON
    │
FastAPI Backend (Python async)
    │
    ├── Intent Router (deterministic keyword/regex)
    ├── Dispatcher (one Groq LLM call → sub-agent selection)
    ├── Sub-agent Pipeline (14 deterministic tools)
    │       RouteClassifier → IntentAnalyzer → TimeKPI → EntityResolver
    │       → SchemaRetriever → SqlPlanner → SqlWriter → SqlValidator
    │       → SqlExecutor → ResultAggregator → InsightEngine
    │       → ResponseFormatter → ResponseStored
    │
    ├── KPI Engine (no-LLM formula registry)
    ├── Hierarchy Engine (product + location trees)
    ├── Vector Store (pluggable — currently in-memory)
    │
    └── SQLite (aiosqlite, WAL mode)
            ├── sales / purchase tables
            ├── uploads registry
            ├── product_hierarchy, product_master
            ├── location_hierarchy, branch_master
            ├── kpi_registry
            └── error_log
```

---

## 2. Module Map

### Backend

| Module | File | Responsibility |
|---|---|---|
| Infrastructure | `app/infrastructure.py` | Settings, schema spec, DB DDL, connection helpers, upload parsers, response cache, synonyms |
| Analytics Engine | `app/analytics_engine.py` | TurnState, EventEmitter, CostGuard, GroqClient, 14 tools, tool registry, 7 sub-agents, Coordinator |
| Core System | `app/core_system.py` | FastAPI app, all HTTP routes, CORS, rate limiter, startup hook, exception handlers |
| KPI Engine | `app/kpi/` | KPI registry (SQLite-backed), matcher, formula execution engine |
| Hierarchy | `app/hierarchy/` | Product + location tree management, v1 + v2 hierarchy |
| Vector | `app/vector/` | Embeddings, vector store, semantic search (pluggable adapters) |
| Monitoring | `app/monitoring/` | Sentry config, tracing, request context instrumentation |
| Enrichment | `app/enrichment/` | Forecast (linear regression), inventory snapshot, cost master |
| Database | `app/database/` | Engine abstraction (SQLite default, asyncpg Postgres adapter) |
| Time Engine | `app/time_engine.py` | Dataset-relative date token resolution |
| Dedup | `app/dedup.py` | File-level SHA-256 deduplication |
| Errors | `app/errors.py` | Typed error log (SQLite-backed) |

**Import direction (never reverse):**
```
core_system → analytics_engine → infrastructure
core_system → kpi / hierarchy / vector / monitoring / enrichment / database / time_engine / dedup / errors
```

### Frontend

| File | Responsibility |
|---|---|
| `App.tsx` | Layout shell, login gate, view switcher, error boundaries |
| `ui_system.tsx` | All pages: Dashboard, AiAssistant, UploadData, ShopInfo |
| `client_core.ts` | TypeScript types, API client (REST + SSE), Zustand store |
| `index.css` | Tailwind directives + theme tokens |

---

## 3. Request Lifecycle

### Upload Flow
```
POST /upload (multipart)
  → rate limit check (5/min per IP)
  → file type validation (.csv / .xlsx)
  → stream to data/uploads/{batch_id}{suffix} in 1 MB chunks
  → SHA-256 dedup check
  → DataCleanAgent.run():
      FileParser → HeaderMapper → DataNormalizer → RowValidator
      → Database.insert (INGESTION_PIN) → PostValidator
  → post-upload chain (all wrapped in try/except, failures non-fatal):
      backfill_missing_product_names
      → sync_product_master_from_data (v1 hierarchy)
      → sync_product_sku_master (v2 hierarchy)
      → refresh_inventory
      → refresh_forecast
      → refresh_product_costs + backfill_quantities
  → bump_data_version + invalidate_all() + invalidate_time_cache()
  → return batch metadata JSON
```

### Query Flow
```
POST /query_stream (JSON)
  → validate Groq key shape
  → rate limit check (30/min per IP)
  → create TurnState
  → spawn runner task + heartbeat task (15s ping)
  → return StreamingResponse(text/event-stream) immediately

runner task:
  → set_request_groq(GroqClient) via contextvars
  → wrap emitter in PresentationEmitter (sanitizes payload fields)
  → run_query_turn(TurnState, emitter):

      1. Cache lookup (SHA-256 of normalized question)
         → HIT: emit cache.hit + final + turn.end (no LLM)

      2. Intent router classify_query_kind():
         → "chat"              → chat_responder (1 Groq call)
         → "missing_data"      → template response (no LLM)
         → "general_knowledge" → knowledge_responder (1 Groq call)
         → "data_query"        → agentic pipeline ↓

      3. KPI fast-path: match question against KPI registry
         → MATCH: execute_kpi() → format → emit final (no LLM needed)

      4. Dispatcher (1 Groq call, JSON):
         → returns {sub_agent: "QueryAgent"|"AnalyticsAgent"|"RCAAgent"|"ForecastAgent"}

      5. sub_agent.run():
         for each (tool_name, args) in pipeline:
           → cost_guard check (iterations + USD)
           → emit tool.call
           → tool.execute(state, args)
           → emit tool.result
           → state = state.apply(**result.state_updates)
           → halt on first tool failure

      6. emit final + turn.end
```

---

## 4. Sub-agents and Their Pipelines

| Sub-agent | Pipeline steps |
|---|---|
| **QueryAgent** | RouteClassifier → IntentAnalyzer → TimeKPI → EntityResolver → SchemaRetriever → SqlPlanner → SqlWriter → SqlValidator → SqlExecutor → ResultAggregator → InsightEngine → ResponseFormatter → ResponseStored |
| **AnalyticsAgent** | Same as QueryAgent (trend/comparison intent) |
| **RCAAgent** | Same pipeline + RCA-specific InsightEngine prompt |
| **ForecastAgent** | Same pipeline + linear regression step before ResponseFormatter |
| **DashboardAgent** | No LLM — direct SQL aggregation for KPI cards + time series |
| **DataCleanAgent** | No LLM — FileParser → HeaderMapper → DataNormalizer → RowValidator → Database.insert → PostValidator |
| **ResponseAgent** | Single-step — format an existing result |

---

## 5. Key Design Decisions

### 5.1 Deterministic pipelines
Sub-agents declare a **fixed ordered pipeline** at definition time.
The LLM is used only for: dispatcher, SqlPlanner, InsightEngine, chat/knowledge responder.
Everything else is deterministic — testable, traceable, cost-bounded.

### 5.2 TurnState immutability
`TurnState` is a frozen Pydantic model.
Tools return `state_updates` dict; the pipeline applies them via `state.model_copy(update=...)`.
This makes the entire turn a pure functional data transformation — auditable, replayable.

### 5.3 Capability token access control
The `Database` tool requires either `INGESTION_PIN` or `READ_PIN`.
No tool can access SQLite directly; all SQL paths go through the Database tool.
This prevents ad-hoc data mutations from LLM-influenced tools.

### 5.4 Per-request Groq key isolation
Each `/query_stream` request creates a `GroqClient` scoped via `contextvars`.
The user's key from `X-Groq-Api-Key` header never leaks between concurrent requests.

### 5.5 PresentationEmitter sanitization
Every SSE event passes through `PresentationEmitter` before reaching the browser.
Strips: `formula`, `sql_used`, `required_columns`, `missing_columns`, `stack_trace`, `kpi_id`, `_internal`.
Internal events (`tool.start`, `tool.end`, `kpi.matched`) are silently dropped.

### 5.6 KPI fast-path
Before invoking the full 14-tool pipeline, the Coordinator matches the question against the KPI registry.
On a confident match, `execute_kpi()` runs a pre-validated SQL template — no LLM, instant answer.

---

## 6. Data Model

### Financial tables (sales + purchase — identical schema)
```sql
id            INTEGER PK AUTOINCREMENT
batch_id      TEXT NOT NULL          -- FK → uploads
source        TEXT DEFAULT 'upload'
file_name     TEXT
row_hash      TEXT                   -- SHA-256 of row content (dedup)
is_mock_named INTEGER DEFAULT 0      -- 1 = product name was backfilled
is_mock_quantity INTEGER DEFAULT 0   -- 1 = quantity was synthesized
inserted_at   TEXT DEFAULT datetime()

-- 20 business columns:
"Date"                 TEXT NOT NULL
"Total Amount"         REAL NOT NULL
"Party Name"           TEXT
"Product Name"         TEXT
"Order No"             TEXT
-- ... 15 more optional columns
```

### Uploads registry
```sql
batch_id      TEXT PK
filename      TEXT NOT NULL
target        TEXT NOT NULL          -- 'sales' | 'purchase'
rows_inserted INTEGER DEFAULT 0
status        TEXT DEFAULT 'active'  -- active | error | removed | archived
min_date      TEXT
max_date      TEXT
file_path     TEXT                   -- absolute path to persisted source file
file_hash     TEXT                   -- SHA-256 for dedup
```

### Indexes
```sql
idx_{table}_date     ON (Date)
idx_{table}_party    ON ("Party Name")
idx_{table}_batch    ON (batch_id)
idx_{table}_row_hash ON (row_hash)
```

---

## 7. External Dependencies

| Dependency | Used for | Version |
|---|---|---|
| FastAPI + Uvicorn | HTTP server | ≥0.110 |
| Pydantic v2 | Validation, settings, TurnState | ≥2.5 |
| httpx | Async Groq API calls with retry | ≥0.26 |
| aiosqlite | Async SQLite | ≥0.20 |
| openpyxl | XLSX parsing | ≥3.1 |
| DuckDB | Analytics acceleration (installed, **not yet wired**) | ≥1.0 |
| numpy | Linear regression (ForecastAgent) | ≥1.26 |
| sentry-sdk | Error monitoring (optional) | ≥1.40 |
| asyncpg | Postgres adapter (optional, env-activated) | ≥0.29 |
| redis | Cache adapter (optional, env-activated) | ≥5.0 |
| React 18 + Vite | Frontend SPA | 18.3 / 5.4 |
| Zustand | Frontend state | ≥5.0 |
| Recharts | Charts | ≥2.13 |
| Tailwind CSS | Styling | ≥3.4 |
