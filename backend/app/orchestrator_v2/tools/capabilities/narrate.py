"""
Capability: narrate
===================

The Worker's narration step. Takes the aggregates produced by upstream
capabilities (``run_data_query``, ``compare_periods``,
``breakdown_by_hierarchy``, ``compute_kpi``) and composes a short
business-readable answer.

This is the **only** capability that consumes Groq for natural-language
generation in v2's hot path — the rest are deterministic. Internally
delegates to the existing ``InsightEngineTool`` primitive (and its
mode-specific prompts: trend, ranking, summary, comparison, RCA,
forecast, anomaly).

Status
------
P1 — typed contract + stub. Real body lands in P2 alongside the
``WorkerAgent`` (narrate is the second Worker LLM call per turn —
plan first, then narrate).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


NarrationMode = Literal[
    "summary",
    "trend",
    "ranking",
    "comparison",
    "rca",
    "forecast",
    "anomaly",
]


class NarrateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: NarrationMode = "summary"
    aggregates: dict[str, Any] = Field(
        ...,
        description=(
            "Stitched aggregate object the narrate prompt will consume. "
            "Built by the Worker from upstream step outputs."
        ),
    )
    metric_label: str | None = None
    user_question: str = Field(..., min_length=1, max_length=4000)


class NarrateOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    narrative: str = ""
    grounded_numbers: tuple[float, ...] = ()    # every number cited (for the
                                                # grounded-narrative validator)
    placeholder: bool = True
    note: str = "stub — body implementation lands in P2"


@register_capability
class Narrate(Capability[NarrateArgs, NarrateOutput]):
    name = "narrate"
    description = (
        "Produce a short business-readable answer from the aggregates. "
        "Mode-specific prompts: summary / trend / ranking / comparison / "
        "rca / forecast / anomaly. Every number cited MUST appear in the "
        "underlying aggregates (grounded-narrative validator enforces)."
    )
    args_model = NarrateArgs
    output_model = NarrateOutput
    requires: tuple[str, ...] = ()  # the Planner enforces upstream-data presence
    pure = False  # LLM call

    async def run(
        self,
        state: ExecutionState,
        args: NarrateArgs,
    ) -> NarrateOutput:
        # P2 body: one Worker LLM call using the narrator system prompt.
        # The grounded-narrative validator (P3) verifies every cited
        # number appears in the aggregates.
        import json
        import re

        from app.orchestrator_v2.llm.scope import get_worker_client
        from app.orchestrator_v2.llm.worker_client import NARRATOR_SYSTEM_PROMPT

        client = get_worker_client()

        user_prompt = json.dumps(
            {
                "user_question": args.user_question,
                "mode": args.mode,
                "metric_label": args.metric_label,
                "aggregates": args.aggregates,
            },
            default=str,
            ensure_ascii=False,
        )

        payload, _account = await client.complete_json(
            NARRATOR_SYSTEM_PROMPT,
            user_prompt,
            temperature=0.2,
            max_tokens=512,
        )

        narrative = str(payload.get("narrative", "")).strip()
        raw_numbers = payload.get("grounded_numbers") or []
        grounded: list[float] = []
        for n in raw_numbers:
            try:
                grounded.append(float(n))
            except (TypeError, ValueError):
                continue

        # If the model forgot to fill grounded_numbers, harvest from the
        # narrative text via a regex — the grounded-narrative validator
        # in P3 will still re-check.
        if not grounded and narrative:
            for m in re.findall(r"-?\d+(?:[\.,]\d+)*", narrative):
                try:
                    grounded.append(float(m.replace(",", "")))
                except ValueError:
                    continue

        return NarrateOutput(
            narrative=narrative,
            grounded_numbers=tuple(grounded),
            placeholder=False,
            note="",
        )
