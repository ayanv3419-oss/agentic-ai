# Planner delta prompt

You previously produced a plan that was executed but the Critic and/or
the deterministic Validators flagged issues. Emit a **delta plan** — a
small list of ADDITIONAL steps that fix the flagged issues, without
re-running anything that already succeeded.

## Output contract

Same JSON shape as the original planner output:

```json
{
  "reasoning": "<short paragraph (≤300 chars) explaining the delta>",
  "steps": [ ... ]
}
```

## Rules

1. The `steps` list contains **only new** steps to add to the plan. Do
   NOT repeat steps that already executed successfully.
2. New step IDs MUST NOT collide with existing executed step IDs.
3. New steps MAY depend on existing executed step IDs — the Executor
   re-uses the cached outputs from the original execution.
4. The delta MUST address every blocking issue listed in the
   `<critic_feedback>` and `<validation_report>` sections appended to
   this prompt.
5. Use the same aspect→capability routing the orchestrator expects:
   - `missing_timeframe`  → add `resolve_time_window`
   - `missing_comparison` → add `compare_periods`
   - `missing_hierarchy_breakdown` → add `breakdown_by_hierarchy`
   - `missing_kpi`        → add `compute_kpi`
   - `chart_data_invalid` → add a new `format_response` with corrected args
   - `weak_reasoning` / `vague_answer` → add a new `narrate` (mode may differ)
   - `empty_result_unexplained` → re-run `run_data_query` with broader filters
6. If a delta step replaces a narrate or format_response, also include
   the replacement step so the final answer is regenerated.
7. Keep the delta minimal. The reflection budget is 3 iterations total.
