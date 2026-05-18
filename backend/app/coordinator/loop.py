"""
Coordinator loop. Drives the agentic cycle:

    1. Build a system prompt describing the 8 tools + 3 sub-agents.
    2. Ask the LLM what to do (tool-calling completion).
    3. For each tool call: dispatch() through hooks.
    4. Feed the tool result back into the conversation.
    5. Reflect, repeat. Stop when the LLM emits no more tool calls or
       the iteration cap is hit, then emit final + turn.end.

Caps: 10 LLM rounds (MAX_ITERATIONS) AND 20 total tool calls
(MAX_TOOL_CALLS). Either trips, the loop ends.
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


SYSTEM_PROMPT = """You are the Coordinator for MetricAi, an analytics
agent that answers natural-language questions about uploaded sales /
purchase / financial data stored in SQLite.

You have access to 8 tools and 3 sub-agents. The 8 tools are
deterministic helpers; the 3 sub-agents wrap LLM calls.

Tools:
  - Schema       (always call this first if you need to write SQL)
  - RouteClass   (classify the question)
  - Granularity  (pick the time bucket)
  - TimeKPI      (resolve the time window + KPI hints)
  - EntityLoc    (resolve named entities)
  - SqlDryRun    (validate SQL before running it)
  - SqlExecutor  (run a validated SELECT)
  - CausalTree   (decompose a metric into movers for RCA)

Sub-agents:
  - sqlWriter    (write a SELECT from intent + schema)
  - rcaReasoner  (narrate a causal_tree as plain English)
  - insightFmt   (compose the final user-facing answer)

Process every question as follows:
  1. RouteClass + TimeKPI early so you know intent + window.
  2. Schema before sqlWriter.
  3. sqlWriter -> SqlDryRun -> SqlExecutor.
  4. For RCA routes: also run SqlExecutor for the prior period, then
     CausalTree, then rcaReasoner.
  5. Always finish with insightFmt to produce the final answer.

Strict rules:
  - You MUST end the turn by calling insightFmt and then producing no
    further tool calls.
  - You MUST NOT call SqlExecutor without first calling SqlDryRun on the
    same SQL.
  - Hard caps: 10 LLM rounds AND 20 total tool calls per turn. Either
    trips, the turn ends - so plan ahead.
  - When in doubt, ask for fewer rows (smaller LIMIT) rather than more."""


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
            _log.warning("llm error in loop: %s", resp.error)
            state = state.with_error(f"llm:{resp.error_kind}:{resp.error}")
            if not state.final_answer:
                state = state.apply(
                    final_answer=(
                        "I couldn't reach the local model. "
                        "Please check that Ollama is running and the model is pulled."
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
            if call.name == "insightFmt" and result.status == "ok":
                state = state.apply(finished=True)

    return state


__all__ = ["SYSTEM_PROMPT", "run_loop"]
