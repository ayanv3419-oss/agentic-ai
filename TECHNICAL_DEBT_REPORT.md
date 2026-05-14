# Technical Debt Report — Agentic AI Platform

> Audit date: 2025-05-15
> Format: debt item → actual code evidence → remediation

---

## Debt Classification

| Label | Meaning |
|---|---|
| 🔴 Critical | Blocks production, causes data loss or security holes |
| 🟠 High | Significantly slows development or hides bugs |
| 🟡 Medium | Increases maintenance cost |
| 🟢 Low | Cosmetic / minor cleanup |

---

## TD-001 🔴 — Monolithic analytics_engine.py (57 KB, ~1,900 lines)

**What:** Every component of the AI pipeline lives in one file:
- `TurnState` (frozen data model)
- `EventEmitter` (SSE queue)
- `CostGuard` (budget enforcement)
- `GroqClient` (HTTP client + retry logic)
- 14 `Tool` subclasses
- `ToolRegistry`
- `classify_query_kind` (250+ line intent router)
- `classify_intent` (another 150 lines)
- 7 `SubAgent` subclasses
- `Coordinator` (orchestration logic)
- `run_query_turn` (entry point)

**Why it hurts:**
- Every PR touches this file → merge conflicts on every feature
- Import time is slow → cold start penalty
- Impossible to mock individual tools in tests
- No clear ownership boundary

**Remediation:**
```
app/
  turn_state.py          ← TurnState + ToolCallRecord
  event_emitter.py       ← EventEmitter + format_sse
  cost_guard.py          ← CostGuard + check_* functions
  groq_client.py         ← GroqClient + GroqResponse
  intent_router.py       ← classify_query_kind + classify_intent
  tools/
    base.py              ← Tool ABC + ToolResult + require()
    database.py          ← DatabaseTool
    route_classifier.py  ← RouteClassifierTool
    sql_tools.py         ← SqlPlanner + SqlWriter + SqlValidator + SqlExecutor
    insight_engine.py    ← InsightEngine + ResultAggregator
    ...
  agents/
    base.py              ← SubAgent ABC
    query_agent.py
    analytics_agent.py
    forecast_agent.py
    rca_agent.py
    dashboard_agent.py
    data_clean_agent.py
  coordinator.py         ← Coordinator + run_query_turn
```

**Effort:** 3-4 days | **Risk of change:** Medium (no behavior change, pure restructure)

---

## TD-002 🔴 — No real authentication (backend is fully public)

**What:** `app/core_system.py`:
```python
_app_log.info("auth: DISABLED — all routes public")
```
The `backend/.env.example` has `AUTH_TOKEN_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` but these are never read by `core_system.py`.

**Why it hurts:** Any public deploy exposes every API endpoint to the internet with no credentials.

**Remediation:**
```python
# Add to core_system.py
async def require_auth(request: Request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token or token != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

# Apply to sensitive routes
@api_router.post("/upload")
async def upload(..., _auth = Depends(require_auth)):
```

**Effort:** 1 day

---

## TD-003 🔴 — Hardcoded credentials in frontend source

**What:** `frontend/src/App.tsx`:
```ts
const AUTH_USERNAME = 'Mansuri'   // line 62
const AUTH_PASSWORD = '182012'    // line 63
```

**Why it hurts:** These ship verbatim in the compiled JS bundle on every Vercel deploy.

**Remediation:**
```ts
// Move to Vercel environment variables
const AUTH_USERNAME = import.meta.env.VITE_GATE_USER ?? ''
const AUTH_PASSWORD = import.meta.env.VITE_GATE_PASS ?? ''
```
Set `VITE_GATE_USER` and `VITE_GATE_PASS` in Vercel dashboard.

**Effort:** 30 minutes

---

## TD-004 🟠 — Schema evolution via ad-hoc ALTER TABLE strings

**What:** `app/infrastructure.py` contains multiple lists of raw SQL strings:
```python
_UPLOADS_ALTERS: list[str] = [
    "ALTER TABLE uploads ADD COLUMN status ...",
    "ALTER TABLE uploads ADD COLUMN min_date ...",
    "ALTER TABLE uploads ADD COLUMN file_hash ...",
    # 8 more...
]
_TABLE_HASH_ALTERS: tuple[str, ...] = ("row_hash",)
```
These are applied idempotently via `try/except` (silently swallowing "column already exists" errors).

**Why it hurts:**
- No migration history — can't tell what schema version a DB is at
- Silent errors hide real problems
- Adding a new column requires hand-editing a Python list
- No rollback capability

**Remediation:** Add Alembic with a single initial migration from the current schema.

**Effort:** 1 day

---

## TD-005 🟠 — Tests are plain scripts, not pytest

**What:** All 14 test files follow this pattern:
```python
def main() -> int:
    passed = failed = 0
    for case in CASES:
        result = run_case(case)
        ...
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
```

**Why it hurts:**
- No `pytest --cov` → zero coverage measurement
- No fixtures → each test re-imports and re-inits the full app
- Single failure kills the entire suite
- Can't run individual test cases
- No parameterization (`@pytest.mark.parametrize`)

**Remediation:**
```python
# tests/conftest.py
@pytest.fixture(scope="session")
def db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test.db"
    os.environ["FINANCIAL_DB_PATH"] = str(db_path)
    asyncio.run(init_database())
    yield db_path

# tests/test_intent_router.py
@pytest.mark.parametrize("question,expected", ANALYTICS_CASES)
def test_analytics_routes_correctly(question, expected):
    kind, _, _ = classify_query_kind(question, has_data=True)
    assert kind == expected
```

**Effort:** 2 days

---

## TD-006 🟠 — SYSTEM_SUMMARY.md references files that no longer exist

**What:** `SYSTEM_SUMMARY.md` section 2 lists:
```
backend/app/database.py    ← does not exist (it's infrastructure.py)
backend/app/tools.py       ← does not exist (it's analytics_engine.py)
backend/app/agents.py      ← does not exist (it's analytics_engine.py)
backend/app/api.py         ← does not exist (it's core_system.py)
```

**Why it hurts:** Developers reading `SYSTEM_SUMMARY.md` look for the wrong files.

**Remediation:** Replace `SYSTEM_SUMMARY.md` with `ARCHITECTURE.md` (already created in this audit). Delete the old file.

**Effort:** 5 minutes (delete the file)

---

## TD-007 🟠 — DuckDB installed but unused

**What:** `requirements.txt`:
```
duckdb>=1.0.0
```
Grep result: zero imports of `duckdb` anywhere in the codebase.

**Why it hurts:** Adds ~30 MB to the Render container image and 5-10s to pip install time in CI.

**Remediation:** Either wire DuckDB for analytics (genuine performance win for aggregations over 100K+ rows) or remove from requirements.

**Effort:** 10 minutes to remove | 2 days to wire properly

---

## TD-008 🟡 — Response cache has no TTL or size limit

**What:** `app/infrastructure.py` — `put_cached()` / `get_cached()`:
The cache is a dict serialized to `data/response_store.json`.
It grows without bound. The only eviction is `invalidate_all()` (wipes everything on any upload).

**Why it hurts:**
- Memory grows linearly with unique questions
- Load/save is O(N) on every operation
- No way to expire stale answers

**Remediation:**
```python
MAX_CACHE_ENTRIES = 500

def put_cached(key: str, value: dict) -> None:
    with _cache_lock:
        cache = _load_cache()
        cache[key] = {"data": value, "ts": time.time()}
        # LRU eviction
        if len(cache) > MAX_CACHE_ENTRIES:
            oldest = sorted(cache, key=lambda k: cache[k].get("ts", 0))
            for k in oldest[:len(cache) - MAX_CACHE_ENTRIES]:
                del cache[k]
        _save_cache(cache)
```

**Effort:** 2 hours

---

## TD-009 🟡 — Version number inconsistency

**What:**
- `frontend/package.json`: `"version": "2.0.0"`
- `app/core_system.py`: `version="3.1.0-no-auth"`
- `SYSTEM_SUMMARY.md`: references v1 file names

Three different version namespaces with no relationship.

**Remediation:** Pick one version (3.1.0), put it in a `VERSION` file at the root, read it in both `package.json` and `core_system.py`.

**Effort:** 1 hour

---

## TD-010 🟡 — Debug artifacts committed to repository root

**What:** Files in `agentic-ai/`:
```
_dbg_test.db
_dbg_test.version
_dbg_test_cache.json
```
These are test/debug artifacts from development sessions. The `.gitignore` now excludes `_dbg_test*` but the files themselves were committed before the fix.

**Remediation:**
```bash
git rm _dbg_test.db _dbg_test.version _dbg_test_cache.json
```

**Effort:** 2 minutes

---

## TD-011 🟡 — Google OAuth is scaffolded but non-functional

**What:** Frontend shows a "Connect with Google Drive" card on the Upload page.
Backend has `/auth/google/login`, `/auth/google/callback`, `/drive/sync` routes — all return HTTP 501.

**Why it hurts:** Users click the button, see an error, and lose trust.

**Remediation options:**
1. Hide the Google Drive UI until the feature is implemented
2. Implement it (Google OAuth + Drive API)
3. Leave as-is but show a clear "Coming soon" tooltip instead of a 501

**Effort:** 30 minutes to hide | 3 days to implement

---

## TD-012 🟡 — Vector module is wired at startup but not called by any tool

**What:** `app/core_system.py` startup:
```python
syns = load_synonyms()
if syns:
    n = register_vocabulary("entity", syns)
```
The vocabulary is registered in the vector store, but `EntityResolver` in `analytics_engine.py` uses a simple `resolve_entities()` dict lookup from `infrastructure.py` — not the vector store.

**Why it hurts:** The vector infrastructure is operational but produces no value. It's dead code from the pipeline's perspective.

**Remediation:** Wire `EntityResolver.run()` to call `app.vector.semantic_search.search()` for entity disambiguation, falling back to the synonym dict.

**Effort:** 1 day

---

## TD-013 🟢 — Groq singleton leaks if no request context

**What:** `app/analytics_engine.py`:
```python
_groq_singleton: GroqClient | None = None

def get_groq() -> GroqClient:
    cur = _request_groq.get()
    if cur is not None:
        return cur
    global _groq_singleton         # ← used for non-request contexts (KPI engine, etc.)
    if _groq_singleton is None:
        _groq_singleton = GroqClient()
    return _groq_singleton
```
The singleton uses `settings.groq_api_key` (server-side key) rather than the per-request user key. This is intentional for KPI paths but easy to misuse.

**Remediation:** Document the singleton clearly and add an assertion that it's only called from non-request contexts. No functional change needed — just guard rails.

**Effort:** 30 minutes

---

## TD-014 🟢 — `_startup()` uses deprecated `@app.on_event`

**What:** `app/core_system.py`:
```python
@app.on_event("startup")
async def _startup() -> None:
```
FastAPI deprecated `on_event` in favor of `lifespan` context managers.

**Remediation:**
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup()
    yield
    await _shutdown()

app = FastAPI(lifespan=lifespan, ...)
```

**Effort:** 20 minutes

---

## Debt Summary

| ID | Label | Severity | Effort |
|---|---|---|---|
| TD-001 | Monolithic analytics_engine.py | 🔴 Critical | 3-4 days |
| TD-002 | No backend auth | 🔴 Critical | 1 day |
| TD-003 | Hardcoded frontend credentials | 🔴 Critical | 30 min |
| TD-004 | Ad-hoc schema ALTER TABLE | 🟠 High | 1 day |
| TD-005 | Tests not pytest | 🟠 High | 2 days |
| TD-006 | SYSTEM_SUMMARY.md outdated | 🟠 High | 5 min |
| TD-007 | DuckDB unused | 🟠 High | 10 min |
| TD-008 | Cache no TTL/limit | 🟡 Medium | 2 hours |
| TD-009 | Version mismatch | 🟡 Medium | 1 hour |
| TD-010 | Debug files committed | 🟡 Medium | 2 min |
| TD-011 | Google OAuth stub in UI | 🟡 Medium | 30 min |
| TD-012 | Vector module dead | 🟡 Medium | 1 day |
| TD-013 | Groq singleton scope | 🟢 Low | 30 min |
| TD-014 | Deprecated on_event | 🟢 Low | 20 min |

**Total estimated remediation: ~12 engineering days**
