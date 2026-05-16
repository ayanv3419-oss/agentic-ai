# v1 Sunset / Migration Plan (P9)

> **Status:** Documented, not executed. Awaits explicit go-ahead — the
> deletion of `analytics_engine.py` is the only genuinely destructive
> change left in the rebuild, and v2 still depends on it via the
> primitives re-export shim.

This document is the surgical recipe for retiring v1 (`analytics_engine.py`)
once you decide to flip the irreversible switch. Following it
mechanically lands the rebuild at its intended end-state: one
orchestrator, one set of primitives, no legacy monolith.

---

## Current dependency graph (as of P2-P8)

```
core_system.py
  ├─ v1 imports (KEEP for the 30-day shadow window):
  │    DashboardAgent, DataCleanAgent, EventEmitter, GroqClient, TurnState,
  │    format_sse, get_registry, reset/set_request_groq, run_query_turn
  │
  └─ v2 imports (the new world):
       app.orchestrator_v2.run_query_turn_v2
       app.orchestrator_v2.state.RequestContext

orchestrator_v2/
  ├─ run.py             → emits SSE, owns reflection loop
  ├─ front_door.py      → imports `_narrate_kpi` from analytics_engine
  ├─ tools/primitives/  → RE-EXPORTS 14 v1 Tool classes from analytics_engine
  ├─ tools/_v1_bridge.py → imports TurnState from analytics_engine
  └─ llm/{worker_client, critic_client}.py → import GroqClient + GroqMessage
                                              from analytics_engine
```

**Conclusion:** `analytics_engine.py` is still load-bearing for v2.
Deleting it today breaks v2 too.

---

## Pre-conditions before deletion

1. **Shadow mode results acceptable.** Run `SHADOW_V2=true` against
   real traffic for 1-2 weeks (per plan Q13). Use the offline diff
   helper:

       python -c "from app.orchestrator_v2.monitoring import shadow; \
           import sqlite3; c=sqlite3.connect(r'ai/data/financial_records.db'); \
           rows=c.execute('SELECT diff_json FROM v2_shadow_log').fetchall(); \
           ..."

   Acceptance criterion (plan Q13): <1% numerical divergence, zero
   hallucinations across ≥10k turns.

2. **v2 is the default in production.** Already shipped via P8
   (`ORCHESTRATOR_VERSION` default = `v2`). `FORCE_V1=1` is still
   honoured as an emergency rollback. Once shadow approves, drop
   `FORCE_V1` handling too.

3. **Frontend regression check.** Spin up the SPA, exercise:
   chat (KPI + non-KPI + comparison), dashboard load, upload, errors
   page, drive sync. Frontend should show no difference whether
   `ORCHESTRATOR_VERSION` is v2 or `FORCE_V1=1`.

4. **Tag the commit** as `v1-archive` so the monolith is one
   `git checkout` away if a forensic need ever arises:

       git tag -a v1-archive -m "last commit before analytics_engine.py removal"
       git push origin v1-archive

---

## Migration recipe — one PR, ~600 LOC moved

### Step 1: Move shared utilities to `orchestrator_v2/llm/`

Create `orchestrator_v2/llm/groq.py` and move these symbols
into it verbatim:

| Symbol | From line in `analytics_engine.py` |
|---|---|
| `GroqMessage` | 224-226 |
| `GroqResponse` | 229-235 |
| `GroqStreamChunk` | 238-242 |
| `GroqToolCall` | 245-249 |
| `GroqToolResponse` | 252-261 |
| `_err_response`, `_err_tool_response`, `_err_chunk` | 264-273 |
| `GroqClient` | 276-541 |
| `set_request_groq`, `reset_request_groq`, `get_request_groq` | (search file) |

Update `orchestrator_v2/llm/worker_client.py` and
`orchestrator_v2/llm/critic_client.py` to import from the new path:

    from app.orchestrator_v2.llm.groq import GroqClient, GroqMessage

### Step 2: Move EventEmitter + SSE helpers to `orchestrator_v2/monitoring/sse.py`

| Symbol | From line |
|---|---|
| `EventEmitter` | 134-184 |
| `format_sse`, `format_comment`, `_COMMENT_MARKER` | 121-131 |

Update `core_system.py` import:

    from app.orchestrator_v2.monitoring.sse import EventEmitter, format_sse

### Step 3: Move primitives into per-file modules

For each of the 14 Tool classes, move it into its own file under
`orchestrator_v2/tools/primitives/`:

| Class | Source lines | Target file |
|---|---|---|
| `DatabaseTool`           | 682-742   | `primitives/database.py` |
| `RouteClassifierTool`    | 749-795   | `primitives/route_classifier.py` |
| `IntentAnalyzerTool`     | 1208-1299 | `primitives/intent_analyzer.py` |
| `TimeKPITool`            | 1341-1429 | `primitives/time_kpi.py` |
| `EntityResolverTool`     | 1450-1502 | `primitives/entity_resolver.py` |
| `SchemaRetrieverTool`    | 1508-1531 | `primitives/schema_retriever.py` |
| `SqlPlannerTool`         | 1537-1751 | `primitives/sql_planner.py` |
| `SqlWriterTool`          | 1757-1857 | `primitives/sql_writer.py` |
| `SqlValidatorTool`       | 1863-1915 | `primitives/sql_validator.py` |
| `SqlExecutorTool`        | 1929-1973 | `primitives/sql_executor.py` |
| `ResultAggregatorTool`   | 2120-2251 | `primitives/result_aggregator.py` |
| `InsightEngineTool`      | 2292-2611 | `primitives/insight_engine.py` |
| `ResponseFormatterTool`  | 2617-2665 | `primitives/response_formatter.py` |
| `ResponseStoredTool`     | 2670-2752 | `primitives/response_stored.py` |

Carry along their helpers (e.g., `_DANGEROUS` regex, `QUERY_TYPES`,
narration mode helpers) into the file that uses them — or to a shared
`primitives/_shared.py` if used by several.

Update `orchestrator_v2/tools/primitives/__init__.py` to import from
the new per-file locations instead of `analytics_engine`. The public
import surface stays identical.

### Step 4: Move TurnState into `orchestrator_v2/state_v1.py` (deprecated)

Keep `TurnState` reachable for the bridge code in `tools/_v1_bridge.py`
during the deprecation period. Plan to delete `state_v1.py` + the
bridge once all capability bodies are rewritten to consume
`ExecutionState` directly (their args already are typed; the bridge is
just a convenience wrapper).

### Step 5: Move `_narrate_kpi` to `front_door.py`

It's a pure helper (~10 LOC). Inline it directly into the front-door
module; drop the late import.

### Step 6: Remaining v1 surface area

After steps 1-5 the only things left in `analytics_engine.py` are:

- `Tool`, `ToolResult`, `ToolRegistry`, `require()`           → move to `tools/base.py` / `tools/registry.py` (overload existing files; keep the same shape)
- `_run_internal`, `_bootstrap`                                → delete (v2 has its own Executor)
- `DataCleanAgent`, `DashboardAgent`                           → move to top-level `app/agents/` (they're domain-level, not orchestration)
- `run_query_turn`, `_force_final`, `_INTENT_RULES`, `_TYPO_MAP`, etc. → delete (replaced by v2's planner + executor + critic)

### Step 7: Delete the file

    git rm ai/backend/app/analytics_engine.py

### Step 8: Update `core_system.py` imports

    # OLD
    from app.analytics_engine import (
        DashboardAgent, DataCleanAgent, EventEmitter, GroqClient,
        TurnState, format_sse, get_registry, run_query_turn, ...)

    # NEW
    from app.agents.dashboard import DashboardAgent
    from app.agents.data_clean import DataCleanAgent
    from app.orchestrator_v2.monitoring.sse import EventEmitter, format_sse
    # GroqClient/TurnState/run_query_turn references removed entirely

Also: rip out `_runner`, `_resolve_orchestrator_version`'s v1 branch,
the `FORCE_V1` env check, and the `X-Orchestrator-Version: v1` path.

### Step 9: Update the version string

    app = FastAPI(title="Agentic AI", version="4.0.0", ...)

(Drop the `-v2` suffix; v2 is just "the orchestrator" now.)

### Step 10: Run the full test sweep

    cd ai/backend
    python -m tests.test_business_intelligence
    # ... all 14 v1 scripts
    python -m app.orchestrator_v2.tests.test_p1_capability_layer
    # ... all 5 v2 suites
    python -m app.orchestrator_v2.tests.harness --report=reports/golden.md

Every one of the existing 14 v1 test scripts continues to work
because they exercise `app.kpi`, `app.hierarchy`, `app.time_engine`,
`app.dedup`, `app.errors`, `app.enrichment` — none of which depend on
`analytics_engine.py`.

---

## Rollback

If step 7 reveals an import we missed:

    git revert <deletion-commit>

…and re-run tests. The `v1-archive` tag is still available if a
forensic comparison is ever needed.

---

## Estimated effort

Mechanical no-logic refactor: ~3-4 engineering hours.
The risky bit is step 6 — confirming `_INTENT_RULES`, `QUERY_TYPES`,
`_TYPO_MAP`, and friends aren't referenced by tests or by the
primitives we moved. Grep before deleting.
