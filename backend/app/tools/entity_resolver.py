"""EntityResolver — looks up canonical entities via memory/synonyms.json."""
from __future__ import annotations

from pydantic import BaseModel

from app.memory import resolve_entities
from app.state import TurnState
from app.tools.base import Tool, ToolResult


class EntityResolverArgs(BaseModel):
    pass


class EntityResolverTool(Tool):
    name = "EntityResolver"
    description = (
        "Resolves merchant / category / product mentions in the question "
        "against the synonyms memory store. Sets state.entities."
    )
    args_model = EntityResolverArgs
    independent = True

    async def run(self, state: TurnState, args: EntityResolverArgs) -> ToolResult:
        if not state.question:
            return ToolResult(ok=False, error="empty question")
        entities = resolve_entities(state.question)
        return ToolResult(
            ok=True,
            output={"entities": entities},
            state_updates={"entities": entities},
        )
