"""
Worker LLM client — wraps the existing ``app.analytics_engine.GroqClient``
with Worker-role config and per-role env overrides.

The Worker makes two LLM calls per turn (plan + narrate) on the default
configuration. Both calls are JSON-mode and routed through the same
``GroqClient``; the Worker just sets different system prompts and
temperatures.

For offline testing (no Groq key), use ``FakeWorkerLLMClient`` — it
returns canned responses sufficient for golden harness + smoke tests.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.orchestrator_v2.llm.token_ledger import CallAccount

log = logging.getLogger("orchestrator_v2.llm.worker")


_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """Read a prompt template from llm/prompts/<name>.md."""
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("prompt %s not found at %s; using empty fallback", name, path)
        return ""


PLANNER_SYSTEM_PROMPT = _load_prompt("planner_system")
PLANNER_DELTA_PROMPT = _load_prompt("planner_delta")
NARRATOR_SYSTEM_PROMPT = _load_prompt("narrator_system")


@runtime_checkable
class WorkerLLMClient(Protocol):
    """The Worker only needs JSON-mode completion."""

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> tuple[dict[str, Any], CallAccount]:
        ...


class GroqWorkerLLMClient:
    """Production Worker client backed by the existing GroqClient."""

    def __init__(self, api_key: str, model: str | None = None) -> None:
        from app.orchestrator_v2.llm.groq import GroqClient

        self.model = model or os.environ.get(
            "WORKER_MODEL", "llama-3.3-70b-versatile"
        )
        # Model is configured at GroqClient construction (one httpx.AsyncClient
        # per role) — per-call model override is not exposed.
        self._client = GroqClient(api_key=api_key, model=self.model)

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> tuple[dict[str, Any], CallAccount]:
        from app.orchestrator_v2.llm.groq import GroqMessage

        messages = [
            GroqMessage(role="system", content=system_prompt),
            GroqMessage(role="user", content=user_prompt),
        ]
        resp = await self._client.complete(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            force_json=True,
        )
        # Token counts are best-effort: GroqResponse carries tokens_in/out
        # but downstream may not always set them. Fall back to char/4.
        in_t = getattr(resp, "tokens_in", 0) or max(1, len(user_prompt) // 4)
        out_t = getattr(resp, "tokens_out", 0) or max(1, len(resp.content or "") // 4)
        try:
            payload = json.loads(resp.content or "{}")
        except json.JSONDecodeError:
            log.warning("worker JSON parse failed, returning empty dict")
            payload = {}
        return payload, CallAccount.from_counts(in_t, out_t)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


class FakeWorkerLLMClient:
    """
    Deterministic Worker client for tests + offline development.

    Returns canned plans + narratives the smoke tests + golden harness can
    rely on. Behaviour driven by ``responses`` (a dict keyed by a coarse
    intent slug) so individual tests can override specific cases.
    """

    DEFAULT_PLAN = {
        "reasoning": "fake: compute total revenue then narrate",
        "steps": [
            {"step_id": "s1", "capability": "compute_kpi",
             "args": {"kpi_id": "total_revenue"},
             "depends_on": [], "parallel_group": None,
             "rationale": "user asked about revenue"},
            {"step_id": "s2", "capability": "narrate",
             "args": {"mode": "summary",
                      "aggregates": "$s1",
                      "user_question": "<replaced at runtime>"},
             "depends_on": ["s1"], "parallel_group": None,
             "rationale": "summarise the metric"},
            {"step_id": "s3", "capability": "format_response",
             "args": {"narrative": "$s2.narrative",
                      "aggregates": "$s1",
                      "mode": "summary"},
             "depends_on": ["s2"], "parallel_group": None,
             "rationale": "package the final response"},
        ],
    }

    DEFAULT_NARRATIVE = {
        "narrative": "Total revenue is captured in the linked aggregate.",
        "grounded_numbers": [],
    }

    # Delta plan used when the loop re-invokes the Planner after a
    # Critic rejection. Uses only REAL capabilities so the fake test
    # doesn't trip on the deferred-stub capabilities. Re-runs
    # compute_kpi + narrate + format_response with fresh step IDs.
    DEFAULT_DELTA_PLAN = {
        "reasoning": "fake delta: re-narrate with sharper figures",
        "steps": [
            {"step_id": "s4", "capability": "compute_kpi",
             "args": {"kpi_id": "total_revenue"},
             "depends_on": [], "parallel_group": None,
             "rationale": "recompute kpi for delta narration"},
            {"step_id": "s5", "capability": "narrate",
             "args": {"mode": "summary",
                      "aggregates": "$s4",
                      "user_question": "<replaced at runtime>"},
             "depends_on": ["s4"], "parallel_group": None,
             "rationale": "re-narrate with refreshed aggregates"},
            {"step_id": "s6", "capability": "format_response",
             "args": {"narrative": "$s5.narrative",
                      "aggregates": "$s4",
                      "mode": "summary"},
             "depends_on": ["s5"], "parallel_group": None,
             "rationale": "re-package final response"},
        ],
    }

    def __init__(
        self,
        plan: dict[str, Any] | None = None,
        narrative: dict[str, Any] | None = None,
        responses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.plan = plan or dict(self.DEFAULT_PLAN)
        self.narrative = narrative or dict(self.DEFAULT_NARRATIVE)
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []
        # Each delta plan call uses fresh step IDs so re-invocations
        # don't collide with the prior plan's steps.
        self._delta_counter = 0

    def _bump_step_ids(self, plan: dict[str, Any], offset: int) -> dict[str, Any]:
        """Return a copy of plan with step_ids offset by N. Used to keep
        successive delta-plan invocations distinct."""
        old_to_new = {
            s["step_id"]: f"s{int(s['step_id'][1:]) + offset}"
            for s in plan.get("steps", [])
        }
        new_steps = []
        for s in plan.get("steps", []):
            new_step = dict(s)
            new_step["step_id"] = old_to_new[s["step_id"]]
            new_step["depends_on"] = [old_to_new.get(d, d) for d in s.get("depends_on", [])]
            # Rewrite $-refs in args from old → new step IDs.
            new_args: dict[str, Any] = {}
            for k, v in (s.get("args") or {}).items():
                if isinstance(v, str) and v.startswith("$"):
                    body = v[1:]
                    head, dot, rest = body.partition(".")
                    if head in old_to_new:
                        new_args[k] = f"${old_to_new[head]}{dot}{rest}"
                        continue
                new_args[k] = v
            new_step["args"] = new_args
            new_steps.append(new_step)
        return {"reasoning": plan.get("reasoning", ""), "steps": new_steps}

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> tuple[dict[str, Any], CallAccount]:
        # Record the call so tests can introspect.
        self.calls.append((system_prompt[:60], user_prompt[:60]))

        # Three payload paths: initial plan, delta plan, narrative.
        # The delta prompt is identified by the "delta plan" / "previously
        # produced" phrasing in its system text (see planner_delta.md).
        is_planner = "Planner" in system_prompt or "delta plan" in system_prompt.lower()
        is_delta = is_planner and (
            "delta plan" in system_prompt.lower()
            or "previously produced" in system_prompt.lower()
        )

        if is_delta:
            self._delta_counter += 1
            base = self.responses.get("delta_plan", self.DEFAULT_DELTA_PLAN)
            # Successive deltas get fresh step IDs (s4-s6, s7-s9, ...).
            payload = self._bump_step_ids(base, 3 * (self._delta_counter - 1))
        elif is_planner:
            payload = self.responses.get("plan", self.plan)
        else:
            payload = self.responses.get("narrative", self.narrative)

        in_t = max(1, (len(system_prompt) + len(user_prompt)) // 4)
        out_t = max(1, len(json.dumps(payload)) // 4)
        return payload, CallAccount.from_counts(in_t, out_t)

    async def aclose(self) -> None:
        return None


__all__ = [
    "WorkerLLMClient",
    "GroqWorkerLLMClient",
    "FakeWorkerLLMClient",
    "PLANNER_SYSTEM_PROMPT",
    "PLANNER_DELTA_PROMPT",
    "NARRATOR_SYSTEM_PROMPT",
]
