"""Dispatcher — calls Groq once with the system prompt + user question and
returns the chosen sub-agent name. Strict validation; no fallback guessing.
"""
from __future__ import annotations

import logging

from app.agents import ANALYTIC_AGENTS
from app.coordinator.prompts import SYSTEM_PROMPT
from app.llm import GroqMessage, get_groq, parse_strict_json

log = logging.getLogger("agentic_ai.coordinator.dispatcher")


async def select_sub_agent(question: str) -> tuple[str, str, dict]:
    """Returns (sub_agent_name, reason, llm_metrics).

    Raises ValueError on any dispatch failure (LLM error, invalid JSON,
    invalid sub-agent name). The caller emits the structured error event.
    """
    if not question or not question.strip():
        raise ValueError("empty question")

    groq = get_groq()
    resp = await groq.complete(
        [
            GroqMessage(role="system", content=SYSTEM_PROMPT),
            GroqMessage(role="user", content=question.strip()),
        ],
        temperature=0.0,
        max_tokens=200,
        force_json=True,
    )
    metrics = {"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out}
    if resp.error:
        raise ValueError(f"LLM dispatch failed: {resp.error_kind}: {resp.error}")
    try:
        parsed = parse_strict_json(resp.content)
    except ValueError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}") from e
    name = str(parsed.get("sub_agent") or "").strip()
    reason = str(parsed.get("reason") or "")
    if name not in ANALYTIC_AGENTS:
        raise ValueError(
            f"LLM picked unknown sub-agent {name!r}; allowed: {list(ANALYTIC_AGENTS)}"
        )
    return name, reason, metrics
