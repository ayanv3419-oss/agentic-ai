# Metric AI

**Ask your business data questions in plain English — get real numbers, narrative insight, and live charts back.**

Metric AI is an LLM-orchestrated analytics platform for small businesses. Upload your
sales and inventory spreadsheets (CSV / XLSX), then ask things like *"why did Brand X
drop last month?"* or *"show me monthly revenue for 2025"*. An autonomous agent figures
out what you're asking, writes and validates the SQL itself, runs it against your data,
and streams back a grounded answer with a chart — no dashboards to build, no SQL to write.

<p align="left">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5.6-3178C6?logo=typescript&logoColor=white">
  <img alt="Postgres" src="https://img.shields.io/badge/Postgres-Supabase-4169E1?logo=postgresql&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

> **Live demo:** https://agentic-ai-livid-eta.vercel.app
> **Backend:** FastAPI on Render · **Database:** Supabase Postgres · **LLM:** Qwen 3 via OpenRouter

---

## Screenshots

> _Add a screenshot or short GIF here — drop the image in `docs/img/` and reference it.
> A 10-second clip of typing a question and watching the answer + chart stream in is the
> single highest-impact thing you can put in this README._

```
docs/img/demo.gif        ← the money shot: question in → chart out
docs/img/dashboard.png   ← the auto-generated KPI dashboard
```

---

## What this project demonstrates

This is a full, end-to-end product — not a tutorial clone. It shows:

- **Agentic AI orchestration** — a real [ReAct-style loop](#architecture) where the LLM picks
  tools, inspects results, and iterates, with hard cost/iteration guards.
- **LLM-to-SQL with safety rails** — the model proposes SQL; a deterministic validator
  (`SqlDryRun`) must approve it (SELECT-only, known tables/columns, no dangerous keywords)
  *before* it ever runs.
- **Schema portability** — a semantic resolver maps *any* uploaded spreadsheet's column
  names onto canonical business concepts, so the analytics work on data it has never seen.
- **Production thinking** — JWT auth, multi-tenant data isolation (schema-per-tenant),
  a SQLite→Postgres migration path, streaming (SSE), monitoring (Sentry), rate limiting,
  CI on every push, and honest self-audited limitations.

---

## Features

| | |
|---|---|
| 💬 **Natural-language Q&A** | Ask in plain English; the agent plans, queries, and answers. |
| 📊 **Auto-charts** | Trend / ranking / breakdown questions render a chart automatically (Recharts). |
| ⚡ **KPI fast-path** | Common metrics (total sales, orders, margin) skip the LLM and run a pre-validated SQL template — instant + free. |
| 🔎 **Root-cause analysis** | *"Why did X change?"* builds a causal contribution tree across dimensions and narrates it. |
| 🧩 **Works on any schema** | The concept resolver maps your column names (`sales_value`, `net_amount`, `turnover`…) onto canonical metrics. |
| 📈 **Forecasting** | Linear-regression projection of future sales from history. |
| 🔐 **Auth + multi-tenant** | JWT + bcrypt login; schema-per-tenant isolation on Postgres. |
| 🌊 **Live streaming** | Every tool call and the final answer stream to the UI over Server-Sent Events. |
| 💸 **Cost guards** | Hard per-turn caps on LLM iterations and spend — the agent can't run away. |

---

## Architecture

Metric AI's core is an **agentic loop**: the LLM is given a set of tools and decides,
one step at a time, which to call. After each tool result it reflects and chooses the
next move, until it has enough to answer.

```
                   ┌─────────────────────────────────────────────┐
   user question   │              COORDINATOR LOOP               │
   ──────────────► │   (LLM picks a tool → runs it → reflects)    │
                   │                                             │
                   │   while not done and under cost cap:        │
                   │     1. ask LLM: what next?                  │
                   │     2. dispatch the tool it chose           │
                   │     3. feed the result back in              │
                   └───────────────────┬─────────────────────────┘
                                       │ chooses from 11 tools
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
  PERCEPTION                      SQL PIPELINE                   ANSWER / RCA
  ─────────────                   ─────────────                  ─────────────
  RouteClass   (intent)           Schema      (tables/cols)      CausalTree   (why-tree)
  TimeKPI      (date ranges)      sqlWriter   (LLM → SELECT)     rcaReasoner  (narrate)
  Granularity  (day/week/month)   SqlDryRun   (validate!)        insightFmt   (final answer)
  EntityLoc    (brands/products)  SqlExecutor (run + chart)
```

**Why it's built this way**

- **The LLM orchestrates; the tools are deterministic.** The model decides *what* to do,
  but each tool does *exactly* one thing the same way every time. This keeps behavior
  debuggable — you can replay any turn and see precisely which tool produced which result.
- **`SqlDryRun` is a hard gate.** The executor refuses to run any SQL the validator
  hasn't approved on the same turn. LLM-written SQL never touches the database unchecked.
- **Immutable state.** A single `TurnState` is passed between tools and never mutated —
  every change produces a new state via `.apply()`. No hidden cross-tool side effects.
- **Cheap front door.** Before the loop even starts, a response cache and a KPI fast-path
  answer common questions with zero LLM calls.

Two short-circuits sit in front of the loop:
1. **Response cache** — SHA-256-keyed; identical question on unchanged data returns instantly.
2. **KPI fast-path** — matches the question against a registry of named metrics and runs a
   pre-validated SQL template, skipping the LLM entirely.

### Request lifecycle (a single question)

```
POST /query_stream
   → response cache?  ── hit ─→ stream cached answer ─→ done
   → KPI fast-path?   ── hit ─→ run template SQL ─→ stream ─→ done
   → agentic loop:
        RouteClass → TimeKPI → Schema → sqlWriter → SqlDryRun
        → SqlExecutor → insightFmt → turn.end
```

---

## Tech stack

**Backend** — Python 3.11 · FastAPI · Pydantic v2 · `asyncpg` (Postgres) / `aiosqlite` (SQLite) ·
DuckDB (analytics) · OpenAI-compatible LLM client (Ollama locally, OpenRouter in prod) ·
bcrypt + PyJWT (auth) · NumPy · Sentry · FastMCP (MCP server)

**Frontend** — React 18 · TypeScript 5.6 · Vite 5 · Tailwind CSS 3 · Recharts (charts) ·
Zustand (state) · Server-Sent Events (streaming)

**Infra** — Vercel (frontend) · Render (backend) · Supabase (Postgres) · GitHub Actions (CI)

---

## Repository structure

```
backend/
  app/
    coordinator/            # the agentic loop
      loop.py               #   drives the ReAct cycle
      dispatcher.py         #   routes tool calls through hooks
      hooks.py              #   cost caps + the SqlDryRun→SqlExecutor gate
      state.py              #   immutable TurnState
      llm.py                #   OpenAI-compatible LLM client
      tools/                #   8 deterministic tools (RouteClass, Schema, SqlExecutor…)
      sub_agents/           #   3 LLM-backed tools (sqlWriter, rcaReasoner, insightFmt)
      capabilities/         #   coarse capability wrappers
    schema_mapping/         # concept resolver + metric SQL builder (schema portability)
    kpi/                    # KPI registry, matcher, formula engine (the fast-path)
    enrichment/             # forecasting, inventory snapshots, cost master
    hierarchy/              # product + location trees
    vector/                 # embeddings + semantic retrieval
    monitoring/             # Sentry, tracing, instrumentation
    database/               # SQLite ↔ Postgres engine abstraction
    core_system.py          # FastAPI app — every HTTP route, CORS, startup
  tests/                    # pytest suites (schema mapping, enrichment, time engine…)
  main.py                   # backend entry point

frontend/
  src/
    App.tsx                 # layout shell, auth gate, view switcher
    ui_system.tsx           # all pages: Dashboard, AI Assistant, Upload, Shop Info
    client_core.ts          # API client (REST + SSE) + Zustand store + types

docs/adr/                   # Architectural Decision Records
render.yaml                 # backend deploy config (Render)
main.py                     # single-command launcher (runs backend + frontend together)
```

---

## Getting started (local)

### Prerequisites

- **Python 3.11+**
- **Node.js 20+**
- **[Ollama](https://ollama.com/download)** for the local LLM (no GPU required):
  ```bash
  ollama pull qwen3:8b
  ```

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # defaults point at local Ollama — works out of the box
```

### 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env        # VITE_BACKEND_URL defaults to localhost:8000 for dev
```

### 3. Run

One command from the repo root starts **both** servers:

```bash
python main.py
#  backend  → http://localhost:8000
#  frontend → http://localhost:5173
```

Or run them separately:

```bash
# terminal 1
uvicorn backend.main:app --port 8000 --reload
# terminal 2
cd frontend && npm run dev
```

Open **http://localhost:5173**, upload a spreadsheet (a sample lives in `data/`), and ask away.

---

## Configuration

All config is via environment variables (`backend/.env`). The defaults run a fully local,
single-user setup. Key switches:

| Variable | Purpose | Default |
|---|---|---|
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | Any OpenAI-compatible endpoint | local Ollama + `qwen3:8b` |
| `DATABASE_URL` | `postgres://…` switches from SQLite to Postgres | unset (SQLite) |
| `AUTH_ENABLED` | Master switch for JWT auth | `false` |
| `MAX_LOOP_ITERATIONS` / `COST_LIMIT_USD` | Per-turn agent budgets | `8` / `$1.00` |
| `ALLOWED_ORIGINS` | CORS allow-list (lock down in prod) | `*` |

See [`backend/.env.example`](backend/.env.example) for the full annotated list.

---

## Testing & CI

```bash
cd backend
python -m pytest tests/ -v
```

Every push and PR runs [GitHub Actions](.github/workflows/ci.yml):

- **Boot smoke** — the FastAPI app imports cleanly and core routes are present.
- **Backend suites** — schema mapping, enrichment, time engine, hierarchy, dedup, and more.
- **Frontend** — `tsc --noEmit` type-check + a full `vite build`.

---

## Deployment

The app is deployed as three managed services:

- **Frontend** → Vercel (static Vite build), env: `VITE_BACKEND_URL`
- **Backend** → Render (see [`render.yaml`](render.yaml)), Postgres via `DATABASE_URL`
- **LLM** → OpenRouter (free Qwen tier), OpenAI-compatible — works from anywhere

Auth, CORS lock-down, and Postgres all activate purely from environment variables —
the same codebase runs as a local single-user MVP or a deployed multi-tenant service.

---

## Project status & roadmap

**Status: MVP / actively developed.** Honest about what's done and what isn't — see the
self-audit in [`PRODUCTION_READINESS_REPORT.md`](PRODUCTION_READINESS_REPORT.md) and
[`TECHNICAL_DEBT_REPORT.md`](TECHNICAL_DEBT_REPORT.md).

**Working today:** NL Q&A, auto-charts, KPI fast-path, schema mapping, RCA, forecasting,
auth, multi-tenant isolation, streaming, Postgres + SQLite, CI.

**Known limitations / next up:**
- LLM-generated SQL is validated by an allow/deny gate, not parameterized — full
  parameterization is the next hardening step.
- Margin/profit/inventory metrics assume a sales + cost schema; other shapes degrade
  gracefully (the agent says so) rather than guessing.
- Vector/semantic retrieval is built but not yet wired into the main query path.

---

## License

[MIT](LICENSE) — free to use, learn from, and build on.

## Author

Built by **Ayan Mansuri** — [@ayanv3419-oss](https://github.com/ayanv3419-oss).
A from-scratch exploration of agentic AI systems, LLM-to-SQL, and full-stack product
engineering. Questions and feedback welcome via GitHub Issues.
