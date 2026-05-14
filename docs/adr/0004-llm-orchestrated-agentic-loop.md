# ADR-0004 — LLM-orchestrated agentic loop

**Status:** Accepted
**Date:** 2026-05-15
**Supersedes:** [ADR-0002](0002-deterministic-sub-agent-pipelines.md)

## Context

ADR-0002 made every analytic turn run a **fixed, hardcoded tool pipeline**.
The LLM only picked *which* sub-agent ran (the Dispatcher); after that the 14
tools fired in a set order with no branching. This bought traceability,
testability, and cost-predictability — but it also meant the system could
only ever do what a pipeline was hand-written for. Adding a capability meant
adding a sub-agent; a question that needed a slightly different combination
of steps had no path through the system.

We want the LLM to actually *reason* about a request: decide which tools it
needs, run one, inspect the result, decide whether that's enough or whether
another tool is needed, and only answer once it is satisfied.

## Decision

Replace the fixed sub-agent pipelines (`QueryAgent`, `AnalyticsAgent`,
`RCAAgent`, `ForecastAgent`, `ResponseAgent`) and the Dispatcher with a single
**`AgenticLoop`**: a native tool-calling loop where the LLM orchestrates the
turn directly.

Key boundaries that keep the loop from being a black box:

1. **Hybrid front door.** The cheap deterministic shortcuts — response cache
   and KPI fast-path — still sit in front in `run_query_turn`. Only requests
   that aren't an instant shortcut reach the loop.
2. **Coarse capabilities, not raw tools.** The LLM picks from ~4 coarse
   capabilities (`resolve_time_window`, `resolve_entities`, `run_data_query`,
   `generate_narrative`), not the 14 fine-grained tools. The rigid SQL
   micro-chain (`SchemaRetriever → SqlPlanner → SqlWriter → SqlValidator →
   SqlExecutor → ResultAggregator`) stays internally deterministic, collapsed
   inside `run_data_query`.
3. **Unified call.** One LLM call per iteration: the tool result is fed back
   and the same call either requests another capability or emits the final
   answer. There is no separate "evaluator" call.
4. **Cost-guard backstop.** The existing `CostGuard` (iteration + spend caps)
   wraps the loop. On the ceiling, one final tool-free call produces a
   best-effort answer flagged incomplete — the turn never errors out or loops
   forever.
5. **Non-binding deterministic hints.** `RouteClassifier` + `IntentAnalyzer`
   still run as a free pre-pass, but their output is a *hint* in the loop's
   opening context, not a routing decision.
6. **Question path only.** `DashboardAgent` and `DataCleanAgent` are
   untouched — they are UI-triggered actions with no tool-choice to make.

## Consequences

- ✅ The LLM can compose capabilities for requests no one wrote a pipeline
  for, and can self-correct: a failed tool result is fed back so it can retry
  with different arguments.
- ✅ Still observable — every iteration emits `loop.iteration` (the chosen
  capability + the LLM's reasoning) plus the existing `tool.call` /
  `tool.result` events.
- ✅ Cost stays bounded by the same `CostGuard`, and the cheap fast-paths mean
  repeat / bare-KPI questions still cost zero LLM calls.
- ⚠️ A turn is no longer perfectly reproducible — the same question can take a
  different path. Tests that asserted an exact pipeline order no longer apply.
- ⚠️ Every non-shortcut turn now needs a reachable Groq endpoint; there is no
  deterministic fallback pipeline if the LLM is unavailable (the loop returns
  a turn-level error instead).
- ⚠️ Worst-case spend per turn is now "iteration cap × per-call cost" rather
  than a statically known pipeline length.
