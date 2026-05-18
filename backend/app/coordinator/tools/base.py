"""
Tool ABC. Every tool implements:

  * name              - unique identifier the LLM picks
  * description       - one-line purpose shown to the LLM
  * parameters_schema - JSON schema for arguments
  * run(args, ctx)    - async execution returning a ToolOutcome
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

from app.coordinator.state import TurnState


@dataclass
class ToolContext:
    """Runtime context handed to every Tool.run()."""

    state: TurnState
    # Optional LLM client - some tools (e.g. CausalTree) may want it.
    llm: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolOutcome:
    """The structured result of one tool invocation."""

    ok: bool
    output: Any = None
    error: str | None = None
    # Updates merged into TurnState via state.apply(**state_updates)
    state_updates: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0


class Tool(abc.ABC):
    """Concrete tools subclass this and register with ToolRegistry."""

    name: str = ""
    description: str = ""
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def openai_spec(self) -> dict[str, Any]:
        """OpenAI/Ollama tool-calling function-schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        started = time.perf_counter()
        try:
            outcome = await self.run(args or {}, ctx)
        except Exception as e:
            outcome = ToolOutcome(ok=False, error=f"{type(e).__name__}: {e}")
        outcome.duration_ms = (time.perf_counter() - started) * 1000.0
        return outcome

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        ...


__all__ = ["Tool", "ToolContext", "ToolOutcome"]
