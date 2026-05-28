"""
Coordinator loop. Drives the agentic cycle:

    1. Build a system prompt describing the 8 tools + 3 sub-agents.
    2. Ask the LLM what to do (tool-calling completion).
    3. For each tool call: dispatch() through hooks.
    4. Feed the tool result back into the conversation.
    5. Reflect, repeat. Stop when the LLM emits no more tool calls or
       the iteration cap is hit, then emit final + turn.end.

Caps: MAX_ITERATIONS LLM rounds AND MAX_TOOL_CALLS total tool calls
(both defined in hooks.py - the single source of truth). Either trips,
the loop ends.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.coordinator.dispatcher import dispatch
from app.coordinator.hooks import MAX_ITERATIONS, MAX_TOOL_CALLS
from app.coordinator.llm import LLMClient
from app.coordinator.memory import render_context
from app.coordinator.state import ToolCall, TurnState
from app.coordinator.tools.registry import ToolRegistry


_log = logging.getLogger("coordinator.loop")


SYSTEM_PROMPT = f"""You are the Coordinator for MetricAi, an analytics
agent that answers natural-language questions about the user's uploaded
business data. You orchestrate tools yourself — call one at a time,
inspect each result, then decide the next call.

You have ELEVEN tools. Pick what you need:

PERCEPTION (figure out what the user is asking):
  - RouteClass()
      Classify intent: data_query | chat | knowledge | missing_data.
  - TimeKPI(phrase)
      Resolve a time phrase like "last month" / "2025 Q3" / "yesterday"
      into a concrete ISO date range. No-op if the question has no time.
  - Granularity()
      Decide chart bucket: day / week / month / year. Reads time window.
  - EntityLoc()
      Find brand/product/customer names mentioned in the question and
      verify they exist in the data.

SCHEMA (look up tables and columns):
  - Schema()
      Return the list of tables (u_* dynamic tables hold the real data),
      their columns + types, row counts, date ranges, and any
      METRIC DEFINITIONS or DATA CAPABILITY notes for margin / profit /
      inventory. CALL THIS BEFORE WRITING ANY SQL.

SQL PIPELINE (fetch the numbers):
  - sqlWriter(question, schema_summary?)
      LLM-backed: given the question + Schema output, returns a single
      SELECT statement plus a one-sentence rationale.
  - SqlDryRun(sql)
      Parse and validate the SQL: rejects non-SELECT, dangerous keywords,
      references to unknown tables/columns. MUST pass before SqlExecutor.
  - SqlExecutor(sql)
      Run the validated SQL against the database. Returns row data plus
      a chart payload. The hook layer blocks this tool unless SqlDryRun
      passed on the EXACT same SQL this turn.

CAUSAL / RCA (for "why" questions):
  - CausalTree(dimension)
      Build a causal contribution tree for a "why did X change" question.
  - rcaReasoner(prior_rows, current_rows, dimension)
      LLM-backed: narrate the root cause in plain English.

ANSWER (terminal — turn ends when this succeeds):
  - insightFmt()
      Compose and stream the final user-facing answer from everything
      collected in state (rows, chart, time window, narrative).
      ALWAYS call this last. The turn ends as soon as it succeeds.

TYPICAL SEQUENCES:

  Simple data question ("top 5 products by sales"):
    RouteClass → TimeKPI → Schema → sqlWriter → SqlDryRun
    → SqlExecutor → insightFmt

  Trend / time question ("monthly sales 2025"):
    RouteClass → TimeKPI → Granularity → Schema → sqlWriter
    → SqlDryRun → SqlExecutor → insightFmt

  RCA / "why" question ("why did Brand X drop last month"):
    RouteClass → TimeKPI → EntityLoc → Schema
    → sqlWriter → SqlDryRun → SqlExecutor   (current period)
    → sqlWriter → SqlDryRun → SqlExecutor   (prior period — repeat)
    → CausalTree → rcaReasoner → insightFmt

  Chat / knowledge question (no data needed):
    RouteClass → insightFmt

HARD RULES:
  - Schema FIRST, sqlWriter AFTER. Never invent column names.
  - SqlDryRun BEFORE SqlExecutor, every time. The hook layer enforces it.
  - insightFmt is the ONLY way to finish — call it once, last.
  - ZERO ROWS = STOP. If SqlExecutor returns row_count=0, do NOT retry
    with different filters. Call insightFmt and tell the user there is
    no data for that period.
  - For margin / profit / inventory: read the METRIC DEFINITIONS block
    that Schema returns. Use those EXACT formulas in sqlWriter. If the
    Schema output says margin CANNOT be computed for this dataset,
    state that to the user — do not improvise.
  - NEVER invent customer/brand filters unless the user named one
    explicitly. For "WHICH brand performs best" you GROUP BY brand,
    you do NOT filter.
  - CHART RULE (MANDATORY): The frontend renders a chart from the rows
    SqlExecutor returns. To make a chart visible to the user, your SQL
    must return MULTIPLE rows with one label column (date / brand /
    category) and one numeric column. ONE row = NO chart. Apply this
    EVERY turn that isn't pure CHAT:
      * Granularity = month/week/day → intent MUST say "GROUP BY <bucket>
        ORDER BY <bucket> ASC". Example: "what is sales of last 7 months"
        → intent = "total Net_Sales grouped by month ORDER BY month ASC".
      * Extremum questions ("lowest/highest day", "best/worst week",
        "which month had the most sales") → return the TOP / BOTTOM 10
        of that dimension, NOT LIMIT 1. The chart highlights the answer
        in context. NEVER write LIMIT 1 for any extremum question.
      * Ranking ("which brand/category leads") → LIMIT 10-15, never 1.
  - CHART / PLOT REQUESTS (MANDATORY): If the user message contains any
    of "chart", "plot", "graph", "visualize", "show me", "compare",
    "trend", "distribution", "breakdown" — even as a follow-up like
    "make a chart on this" or "plot this and compare" — you MUST run
    the full pipeline (Schema → sqlWriter → SqlDryRun → SqlExecutor →
    insightFmt) to produce fresh rows. NEVER answer a chart request
    from memory alone. Re-run the SQL with an intent that yields the
    multi-row shape the user asked for.
  - NEVER tell the user "I cannot generate visual plots" or "I cannot
    create charts" — you can, and the chart panel renders automatically
    from your SqlExecutor result.
  - Hard caps: {MAX_ITERATIONS} LLM rounds, {MAX_TOOL_CALLS} total tool
    calls per turn. Plan 5-8 calls for a typical question."""


def _initial_messages(state: TurnState) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    context = render_context(state.conversation_id)
    if context:
        msgs.append({
            "role": "system",
            "content": f"Recent conversation:\n{context}",
        })
    msgs.append({"role": "user", "content": state.question})
    return msgs


_TOOL_MSG_CAP = 24000


def _tool_message(call_id: str, name: str, content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        body = content
    else:
        try:
            body = json.dumps(content, default=str, ensure_ascii=False)
        except Exception:
            body = str(content)
    if len(body) > _TOOL_MSG_CAP:
        # Tell the LLM the result was truncated so it can adapt
        # (e.g. ask Schema for fewer tables, narrow a query, etc.).
        # Otherwise it silently bases SQL on a sliced schema.
        body = (
            body[:_TOOL_MSG_CAP]
            + f"\n...[TRUNCATED, full size {len(body)} bytes]"
        )
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": body,
    }


def _assistant_with_tool_calls(content: str, raw_calls: list) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, default=str),
                },
            }
            for tc in raw_calls
        ],
    }


async def run_loop(
    state: TurnState,
    *,
    llm: LLMClient,
    registry: ToolRegistry,
    emit,
) -> TurnState:
    """Drive the agentic loop until the LLM stops emitting tool calls,
    the iteration cap is hit, or insightFmt has produced a final answer."""
    messages = _initial_messages(state)
    tool_specs = registry.tool_specs()

    while True:
        if state.cost.iterations >= MAX_ITERATIONS:
            _log.info("iteration cap reached for turn=%s", state.turn_id)
            break
        if state.iteration >= MAX_TOOL_CALLS:
            _log.info("tool-call cap reached for turn=%s", state.turn_id)
            break
        if state.finished and state.final_answer:
            break

        resp = await llm.complete_with_tools(
            messages,
            tools=tool_specs,
            temperature=0.0,
            max_tokens=900,
        )

        if resp.error:
            # Don't count an erroring round toward the iteration cap.
            _log.warning("llm error in loop: %s (kind=%s)", resp.error, resp.error_kind)
            state = state.with_error(f"llm:{resp.error_kind}:{resp.error}")
            if not state.final_answer:
                # Surface the real reason so we can diagnose — don't hide it
                # behind a hard-coded "check Ollama" string (we're not always
                # on Ollama; production uses OpenRouter).
                short = (resp.error or "unknown error").splitlines()[0][:300]
                state = state.apply(
                    final_answer=(
                        f"The AI provider returned an error ({resp.error_kind or 'error'}): "
                        f"{short}"
                    ),
                )
            break

        # Only successful rounds count toward the round budget.
        state = state.apply(
            cost=state.cost.with_iter(
                tokens_in=resp.tokens_in,
                tokens_out=resp.tokens_out,
            ),
        )

        # No tool calls = the LLM is done; treat its content as the
        # final answer if insightFmt hasn't already produced one.
        if not resp.tool_calls:
            if not state.final_answer and resp.content:
                state = state.apply(final_answer=resp.content.strip())
            break

        # Add assistant message + dispatch every tool call in the batch.
        # Dispatching all of them (rather than break-on-insightFmt) keeps
        # the OpenAI tool-calling protocol satisfied: every tool_calls[i]
        # in the assistant message MUST have a matching tool message.
        # Loop termination after insightFmt is handled by the outer while
        # check on state.finished + state.final_answer.
        messages.append(_assistant_with_tool_calls(resp.content, resp.tool_calls))
        for tc in resp.tool_calls:
            state = state.apply(iteration=state.iteration + 1)
            call = ToolCall(
                call_id=tc.id or f"call_{uuid.uuid4().hex[:8]}",
                name=tc.name,
                arguments=tc.arguments,
                iteration=state.iteration,
                reasoning=(resp.content or "").strip()[:400],
            )
            state, result = await dispatch(
                state, call,
                registry=registry, llm=llm, emit=emit,
            )
            messages.append(_tool_message(
                call.call_id,
                call.name,
                {
                    "ok": result.status == "ok",
                    "output": result.output,
                    "error": result.error,
                },
            ))
            # insightFmt is the terminal tool — once it succeeds the
            # turn is done (no more LLM rounds).
            if call.name == "insightFmt" and result.status == "ok":
                state = state.apply(finished=True)

    return state


__all__ = ["SYSTEM_PROMPT", "run_loop"]
