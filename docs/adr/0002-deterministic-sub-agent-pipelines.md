# ADR-0002 — Deterministic sub-agent pipelines

**Status:** Accepted  
**Date:** 2025-05-15

## Context

Agentic systems that let the LLM choose tools dynamically are hard to debug, test, and cost-control. Every turn becomes a black box.

## Decision

Each sub-agent declares a **fixed, ordered pipeline** of `(tool_name, args)` steps at definition time. The LLM is used only at two points:

1. **Dispatcher** — one LLM call picks *which* sub-agent to run
2. **InsightEngine / SqlPlanner** — LLM-assisted steps inside the pipeline

Once a sub-agent is selected, its pipeline runs deterministically — no dynamic tool selection, no branching based on LLM output mid-pipeline.

## Consequences

- ✅ Fully traceable — every turn emits `tool.call` + `tool.result` SSE events in a predictable order
- ✅ Testable — pipelines can be unit-tested without an LLM
- ✅ Cost-controllable — the cost guard can calculate worst-case spend before starting
- ⚠️ Less flexible — adding a new analytic capability requires writing a new sub-agent
- ⚠️ Dispatcher is a single point of sub-agent routing; misclassification sends the whole turn down the wrong path
