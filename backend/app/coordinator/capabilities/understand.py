"""
understand_question capability.

Collapses the four question-understanding tools into one LLM call:
  RouteClass → TimeKPI → Granularity → EntityLoc

The LLM calls this ONCE at the start of every turn. The internal sequence
is guaranteed by code, not by the LLM — none of the four steps can be
skipped or mis-ordered.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.route_class import RouteClassTool
from app.coordinator.tools.time_kpi import TimeKPITool
from app.coordinator.tools.granularity import GranularityTool
from app.coordinator.tools.entity_loc import EntityLocTool


_route_tool    = RouteClassTool()
_time_tool     = TimeKPITool()
_gran_tool     = GranularityTool()
_entity_tool   = EntityLocTool()


class UnderstandQuestionCapability(Tool):
    name = "understand_question"
    description = (
        "ALWAYS call this first — before any data query. "
        "Classifies intent, resolves the time window, picks the chart "
        "granularity, and finds exact entity values (brand names, product "
        "names, customer names) to use as SQL WHERE filters. "
        "Returns route, time_window, granularity, and matched entities."
    )
    parameters_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        updates: dict[str, Any] = {}
        state = ctx.state
        errors: list[str] = []

        # ── Step 1: RouteClass ──────────────────────────────────────────
        sub = ToolContext(state=state, llm=ctx.llm)
        outcome = await _route_tool.run({}, sub)
        if outcome.ok and outcome.state_updates:
            updates.update(outcome.state_updates)
            state = state.apply(**outcome.state_updates)
        elif not outcome.ok:
            errors.append(f"RouteClass: {outcome.error}")

        # ── Step 2: TimeKPI ─────────────────────────────────────────────
        sub = ToolContext(state=state, llm=ctx.llm)
        outcome = await _time_tool.run({}, sub)
        if outcome.ok and outcome.state_updates:
            updates.update(outcome.state_updates)
            state = state.apply(**outcome.state_updates)
        elif not outcome.ok:
            errors.append(f"TimeKPI: {outcome.error}")

        # ── Step 3: Granularity (skip for CHAT) ─────────────────────────
        if updates.get("route") != "CHAT":
            sub = ToolContext(state=state, llm=ctx.llm)
            outcome = await _gran_tool.run({}, sub)
            if outcome.ok and outcome.state_updates:
                updates.update(outcome.state_updates)
                state = state.apply(**outcome.state_updates)

        # ── Step 4: EntityLoc ───────────────────────────────────────────
        sub = ToolContext(state=state, llm=ctx.llm)
        outcome = await _entity_tool.run({}, sub)
        if outcome.ok and outcome.state_updates:
            updates.update(outcome.state_updates)
            state = state.apply(**outcome.state_updates)

        # ── Build output summary for LLM ────────────────────────────────
        tw = updates.get("time_window") or {}
        output = {
            "route":       updates.get("route", "ANALYTICS"),
            "time_window": tw,
            "granularity": updates.get("granularity", "month"),
            "kpi_hints":   updates.get("kpi_hints", []),
            "entities":    updates.get("entities", []),
        }
        if errors:
            output["warnings"] = errors

        return ToolOutcome(ok=True, output=output, state_updates=updates)


__all__ = ["UnderstandQuestionCapability"]
