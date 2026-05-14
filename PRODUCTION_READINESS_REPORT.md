# Production Readiness Report — Agentic AI Platform

> Audit date: 2025-05-15
> Auditor: Principal Engineer review
> Scope: Full codebase — backend, frontend, infra, CI/CD, security

---

## Scores

| Dimension | Score | Verdict |
|---|---|---|
| **System Maturity** | 58 / 100 | Solid MVP, not enterprise-ready |
| **Production Readiness** | 42 / 100 | Deploy with caution, known blockers |
| **Scalability** | 35 / 100 | Single-user only, hard SQLite ceiling |
| **Security** | 30 / 100 | Critical credential exposure |
| **AI Orchestration Quality** | 68 / 100 | Well-designed pipeline, thin prompts |

---

## Critical Issues — Fix Before Any Public Deploy

### C1. Hardcoded credentials in compiled frontend bundle
**File:** `frontend/src/App.tsx` lines 62–63
```ts
const AUTH_USERNAME = 'Mansuri'
const AUTH_PASSWORD = '182012'
```
This ships verbatim into the Vite-built JavaScript bundle.
Any user who opens browser DevTools → Sources can read both values in 10 seconds.
The app even **teaches attackers the bypass**: line 234–238 shows `localStorage.setItem('agentic-ai:gate','1')`.
This is not an authentication system — it is a cosmetic gate.

**Fix:** Move credentials to environment variables (`VITE_GATE_USER` / `VITE_GATE_PASS`) and hash the password client-side. Or implement real backend authentication.

---

### C2. Backend has zero authentication
**File:** `app/core_system.py` line 1730
```python
_app_log.info("auth: DISABLED — all routes public")
```
Every route — `/upload`, `/query_stream`, `/cache/clear`, `/errors`, `/datasets` — is reachable with zero credentials by anyone who knows the backend URL.
On Render the URL is public. This means anyone can upload arbitrary files, drain the cache, read your error logs, and run unlimited LLM queries on your Groq key.

**Fix:** Implement Bearer token auth. The `backend/.env.example` already has `AUTH_TOKEN_SECRET` / `ADMIN_USERNAME` / `ADMIN_PASSWORD` fields — wire them into a `Depends(require_auth)` guard on sensitive routes.

---

### C3. LLM-generated SQL is executed without parameterization
**File:** `app/analytics_engine.py` — SqlWriter + SqlExecutor tools
The LLM generates raw SQL strings. `SqlValidator` checks for `DROP/DELETE/ALTER/INSERT` keywords, but this is a denylist — bypassable via comment injections, Unicode tricks, or clever sub-selects.
A sufficiently adversarial question could extract or corrupt data.

**Fix:** Run LLM SQL through `EXPLAIN QUERY PLAN` first. Use `aiosqlite` in read-only mode (`uri=True&mode=ro`) for all SELECT paths. Add parameterized query enforcement.

---

### C4. Response cache is an unbounded flat JSON file
**File:** `app/infrastructure.py` — `data/response_store.json`
Every unique question permanently appends to this file. No TTL, no size limit, no eviction.
On a busy deployment this file grows indefinitely.
The file is loaded entirely into memory on every read/write operation (atomic load → mutate → dump pattern).

**Fix:** Cap at N entries (LRU eviction). Or replace with Redis when `REDIS_URL` is set (the dependency is already in `requirements.txt`).

---

### C5. CORS was fully open (now partially fixed)
`allow_origin_regex=".*"` has been replaced with env-var-controlled `ALLOWED_ORIGINS`.
However the default is still `"*"`.
In production, set `ALLOWED_ORIGINS` to your exact Vercel URL before going live.

---

## High Priority Issues

### H1. analytics_engine.py is a 1,900-line monolith
All 14 tools, 7 sub-agents, coordinator, GroqClient, EventEmitter, CostGuard, and TurnState live in one file (57 KB).
This is the single most-touched file in the system. Every feature addition creates merge conflicts and makes code review difficult.

**Fix:** Split into `tools/`, `agents/`, `coordinator.py`, `groq_client.py`.

---

### H2. Tests are plain scripts — no pytest, no fixtures, no isolation
All 14 test files are `if __name__ == "__main__": sys.exit(main())` scripts.
- No parameterization
- No fixtures / setup / teardown
- No test discovery
- No coverage measurement
- Tests mutate real env vars (`os.environ.setdefault(...)`)
- CI runs them sequentially with `for t in ...; do python "$t"; done`

A single test failure prints to stdout and exits 1 — no diff, no line number, no context.

**Fix:** Migrate to pytest. Add `conftest.py` with DB fixtures and env mocking.

---

### H3. DuckDB is installed but unused
`requirements.txt` includes `duckdb>=1.0.0` but no file imports it.
This adds ~30 MB to the container image for zero benefit.

**Fix:** Either wire DuckDB for analytics acceleration (significant performance win on large datasets) or remove from requirements.

---

### H4. Vector module is unconnected to main pipeline
`app/vector/` has full implementations of:
- `embeddings.py` — text → float vector
- `vector_store.py` — in-memory store with cosine similarity
- `semantic_search.py` — top-K retrieval
- `retrieval.py` — document retrieval

None of these are called by any sub-agent or tool in `analytics_engine.py`.
Entity resolution uses a simple synonym dict lookup instead.

**Fix:** Wire semantic search into `EntityResolver` tool — would significantly improve entity disambiguation on messy party/product names.

---

### H5. Upload size limit inconsistency
`backend/.env.example` comment says `MAX_UPLOAD_BYTES=1073741824 # 1 GB`
but `infrastructure.py` line 85: `default=50 * 1024 * 1024` (50 MB).
These contradict each other.

**Fix:** Reconcile to a single documented limit. 50 MB is reasonable for MVP; update the comment.

---

### H6. No request correlation ID / distributed trace
Every request creates a `turn_id` for the query pipeline, but HTTP routes have no request ID.
When `/upload` crashes, the log entry and the Sentry event have no common identifier.

**Fix:** Add `X-Request-ID` middleware. Propagate to all log calls and Sentry breadcrumbs.

---

### H7. Rate limiting only on two endpoints
`/upload` (5/min) and `/query_stream` (30/min) are rate-limited.
`/dashboard`, `/kpi/**`, `/hierarchy/**`, `/enrichment/**`, `/errors/**` are unlimited.
The `/errors` endpoint in particular could be used to spam error logs.

**Fix:** Apply a global rate limiter middleware or extend `_rate_limit_check` to all routes.

---

### H8. No API versioning
All routes are at `/` root with no version prefix.
Breaking changes will hit existing clients immediately.

**Fix:** Prefix all routes with `/api/v1/`. Add deprecation headers when breaking changes ship.

---

## Missing Infrastructure

| Missing | Impact | Effort |
|---|---|---|
| Redis cache | Response cache unbounded, no TTL | Medium |
| Real auth (JWT or session) | Any public deploy is wide open | Medium |
| Background task queue (Celery/RQ) | Uploads block the event loop | High |
| Postgres in production | SQLite can't handle concurrent writes | High |
| CDN for frontend assets | Vercel handles this — already solved | Done |
| Log aggregation (Loki/Datadog) | Logs disappear on Render restart | Low |
| Health check dependencies | `/health` doesn't check Groq reachability | Low |
| Automated backups for SQLite | Data loss on disk failure | Medium |

---

## Missing Tooling

| Missing | Impact |
|---|---|
| pytest + coverage | No coverage measurement possible |
| pre-commit hooks | No lint/format gate before push |
| mypy / pyright | Python type errors caught at runtime |
| eslint strict config | TypeScript `any` leaks unchecked |
| Docker Compose for local dev | Onboarding requires manual env setup |
| Database migration tool (Alembic) | Schema changes via `ALTER TABLE` ad-hoc strings |
| Load testing (Locust/k6) | No performance baseline established |

---

## Missing Production Systems

| System | Why needed |
|---|---|
| Backup/restore for SQLite | Single-file DB on Render disk — one bad deploy = data loss |
| Graceful shutdown handler | `_shutdown()` is a no-op — in-flight requests are killed |
| Memory limit guard | No check on Python heap — OOM kills the process silently |
| SSE connection limit | No cap on concurrent streaming connections |
| Request timeout | No per-request timeout beyond Groq's 60s client timeout |
| Queue depth visibility | No way to see how many turns are queued |
| Admin panel | No way to inspect/manage data without hitting the API directly |

---

## Observability Assessment

| Aspect | Status |
|---|---|
| Structured logging | Partial — `logging.basicConfig` (unstructured text) |
| Sentry error tracking | Wired but optional — no-op without `SENTRY_DSN` |
| Request tracing | None — no OpenTelemetry |
| Metrics (Prometheus) | None |
| Turn audit trail | Good — `tool_calls` list in TurnState → `turn.end` SSE |
| Error log table | Good — SQLite-backed, queryable via `/errors` |
| Health endpoint | Good — `/health` returns DB status, row counts, cache size |
| Uptime monitoring | None — no external ping |

---

## Security Assessment

| Risk | Severity | Fixed? |
|---|---|---|
| Hardcoded credentials in JS bundle | CRITICAL | No |
| No backend authentication | CRITICAL | No |
| LLM SQL injection | HIGH | Partial (keyword denylist only) |
| Unlimited CORS (default `*`) | HIGH | Partial (env var, default still `*`) |
| Response cache path traversal | MEDIUM | No (path is hardcoded, low risk) |
| Error log exposes stack traces | MEDIUM | Partial (user_facing flag) |
| No HTTPS enforcement | MEDIUM | Platform-level (Render/Vercel handle it) |
| Groq key logged on startup | LOW | No (`settings.groq_api_key` not logged) |
| SQLite file accessible on disk | LOW | Render disk is not public |

---

## Performance Bottlenecks

| Bottleneck | Impact | Fix |
|---|---|---|
| SQLite concurrent writes | Blocks on single writer | Postgres or WAL tuning |
| Response cache full reload | O(N) on every cache read/write | LRU dict + TTL |
| `analytics_engine.py` import time | Cold start ~2-3s | Module splitting |
| Synchronous `insert_rows` via `asyncio.to_thread` | Thread pool contention on bulk uploads | Native async batch insert |
| Linear regression done in Python | Slow for large time series | NumPy vectorized (already using numpy — verify) |
| No query result pagination | Large result sets returned in full | Add `LIMIT`/`OFFSET` to SqlPlanner |

---

## Roadmap to Production-Grade Quality

### Phase 1 — Security & Auth (Week 1)
- [ ] Implement Bearer token auth on backend (`Depends(require_auth)`)
- [ ] Move frontend credentials to env vars, hash client-side
- [ ] Set `ALLOWED_ORIGINS` to exact Vercel URL in Render
- [ ] Add `EXPLAIN QUERY PLAN` pre-flight + read-only SQLite mode for SELECT
- [ ] Cap response cache at 500 entries (LRU)

### Phase 2 — Stability (Week 2)
- [ ] Add pytest + `conftest.py`, migrate all 14 tests
- [ ] Add `X-Request-ID` middleware + propagate to logs
- [ ] Wire DuckDB or remove from requirements
- [ ] Fix upload size limit discrepancy (pick 50 MB or 200 MB, document it)
- [ ] Add graceful shutdown handler (drain in-flight turns)
- [ ] Add mypy to CI

### Phase 3 — Observability (Week 3)
- [ ] Add OpenTelemetry traces (FastAPI + httpx)
- [ ] Structured JSON logging (replace basicConfig)
- [ ] Add Prometheus `/metrics` endpoint
- [ ] Set up Sentry DSN in production
- [ ] Add uptime monitor (UptimeRobot or Better Uptime)

### Phase 4 — Scalability (Month 2)
- [ ] Migrate from SQLite to Postgres (env var already supported)
- [ ] Replace file-based response cache with Redis
- [ ] Split `analytics_engine.py` into modules
- [ ] Add Alembic for migrations
- [ ] Wire vector module into EntityResolver
- [ ] Add Docker Compose for local dev
- [ ] Background task queue for uploads

### Phase 5 — Enterprise (Month 3+)
- [ ] Multi-tenant architecture (tenant_id in all queries)
- [ ] Full OAuth2/OIDC authentication
- [ ] Admin panel (read error logs, manage datasets, manage KPIs)
- [ ] API versioning (`/api/v1/`)
- [ ] Load testing baseline
- [ ] SLA monitoring + alerting
