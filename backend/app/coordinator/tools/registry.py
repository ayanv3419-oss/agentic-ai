"""
Tool registry. Holds the 8 Coordinator tools and exposes them as
OpenAI tool-calling specs.
"""
from __future__ import annotations

import threading
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.Lock()

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool.name must be set")
        with self._lock:
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def tool_specs(self) -> list[dict[str, Any]]:
        """OpenAI tool-calling function-schemas, one per registered tool."""
        return [t.openai_spec() for t in self._tools.values()]

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome(
                ok=False,
                error=f"unknown tool: {name!r}. Available: {self.names()}",
            )
        return await tool(args, ctx)


_singleton: ToolRegistry | None = None
_lock = threading.Lock()


def get_registry() -> ToolRegistry:
    """Lazy singleton - built on first access."""
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                from app.coordinator.tools import build_default_registry
                _singleton = build_default_registry()
    return _singleton


__all__ = ["ToolRegistry", "get_registry"]
