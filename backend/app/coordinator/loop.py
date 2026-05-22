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
business data stored in SQLite.

You have exactly FOUR capabilities. Use them in order:

  1. understand_question  — ALWAYS call this first, no arguments needed.
       Internally runs: intent classification, time-window resolution,
       chart granularity, and entity resolution (brand/product names).
       Returns: route, time_window, granularity, matched entities.

  2. run_data_query(intent)  — fetch data from the database.
       Pass a plain-English description of the query you want.
       Internally runs: schema lookup, SQL writing, validation, execution.
       Returns: result rows + chart payload.
       Call this ONCE per distinct query. For RCA you may call it twice
       (current period first, then prior period).

  3. explain_change(dimension)  — for RCA / "why did X drop" questions.
       Call AFTER run_data_query for both periods.
       Pass the prior-period rows as prior_rows.
       Returns: causal tree + plain-English root-cause explanation.

  4. write_answer()  — ALWAYS call this last.
       Composes the final user-facing answer from everything collected.
       Calling this ends the turn — do NOT call anything after it.

Standard flows:

  KPI / RANKING / TREND question:
    understand_question → run_data_query(intent) → write_answer

  RCA / "why" question:
    understand_question
    → run_data_query(intent="current period data by <dimension>")
    → run_data_query(intent="prior period data by <dimension>")
    → explain_change(dimension=..., prior_rows=<prior period rows>)
    → write_answer

  Conversational / CHAT question (no data needed):
    understand_question → write_answer

Rules:
  - ALWAYS call understand_question first. ALWAYS call write_answer last.
  - run_data_query handles SQL writing, validation and execution for you —
    just describe what you want in plain English. SQL retry is automatic.
  - For margin / profit: run_data_query will use the correct formula
    automatically if the data supports it. If it says margin cannot be
    computed, trust that — do not retry with improvised math.
  - ZERO ROWS = STOP. If run_data_query returns row_count=0 or contains
    a "NO_DATA" field, the table has no data for that period. Do NOT
    call run_data_query again with different filters or entity names.
    Call write_answer immediately and tell the user no data is available.
  - NEVER invent or add party/customer name filters unless the user
    explicitly named a specific customer in their question.
  - Hard caps: {MAX_ITERATIONS} LLM rounds AND {MAX_TOOL_CALLS} total
    capability calls per turn. Plan for 3-5 calls on typical questions."""


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
            # write_answer is the terminal capability — it calls insightFmt
            # internally. Once it succeeds the turn is done.
            if call.name == "write_answer" and result.status == "ok":
                state = state.apply(finished=True)

    return state


__all__ = ["SYSTEM_PROMPT", "run_loop"]
