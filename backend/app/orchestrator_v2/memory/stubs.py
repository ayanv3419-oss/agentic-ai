"""
NoOp stubs for the deferred memory layers (SemanticMemory,
ConversationSummarizer, BusinessContextMemory). v2.1 swaps these for
real implementations without changing call sites.
"""

from __future__ import annotations

from typing import Any


class NoOpSemanticMemory:
    async def search(self, query: str, *, k: int = 5) -> list[dict[str, Any]]:
        return []
    async def upsert(self, doc_id: str, text: str,
                     metadata: dict[str, Any] | None = None) -> None:
        return None


class NoOpConversationSummarizer:
    async def summarize(self, turns: list[dict[str, Any]]) -> str:
        return ""


class NoOpBusinessContextMemory:
    async def get_context(self, conversation_id: str) -> dict[str, Any]:
        return {}


__all__ = [
    "NoOpSemanticMemory",
    "NoOpConversationSummarizer",
    "NoOpBusinessContextMemory",
]
