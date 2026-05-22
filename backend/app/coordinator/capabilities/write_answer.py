"""
write_answer capability.

Wraps insightFmt — always the final capability the LLM calls.

The loop terminates after this returns successfully, so calling it signals
"I am done". The LLM must call this even when previous steps partially
failed, so the user always gets a human-readable response.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.sub_agents.insight_fmt import InsightFmtAgent


_fmt_agent = InsightFmtAgent()


class WriteAnswerCapability(Tool):
    name = "write_answer"
    description = (
        "ALWAYS call this last — it composes the final user-facing answer "
        "from everything collected in this turn (rows, insights, causal tree). "
        "Calling this signals that the turn is complete. "
        "Call it even when earlier steps partially failed — tell the user "
        "what you found and what you could not compute."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "sample_rows": {
                "type": "integer",
                "default": 25,
                "minimum": 1,
                "maximum": 100,
                "description": "How many data rows to feed into the answer prompt.",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        if ctx.llm is None:
            return ToolOutcome(ok=False, error="write_answer requires an LLM client")

        sample = max(1, min(100, int(args.get("sample_rows") or 25)))
        sub = ToolContext(state=ctx.state, llm=ctx.llm)
        outcome = await _fmt_agent.run({"sample_rows": sample}, sub)
        if not outcome.ok:
            return ToolOutcome(
                ok=False,
                error=f"insightFmt failed: {outcome.error}",
            )
        return ToolOutcome(
            ok=True,
            output=outcome.output,
            state_updates=outcome.state_updates,
        )


__all__ = ["WriteAnswerCapability"]
