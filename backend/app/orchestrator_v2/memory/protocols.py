"""
Memory layer protocols.

Three layers ship with real impls in P5:

  * ``ConversationMemory``       — rolling Q&A window per conversation_id
  * ``ExecutionMemoryWriter``    — audit log of ExecutionState snapshots
  * ``ContextTrimmer``           — per-call prompt token budget

Three layers ship as stubs:

  * ``SemanticMemory``           — vector retrieval (NoOp until v2.1)
  * ``ConversationSummarizer``   — LLM summary on overflow (NoOp)
  * ``BusinessContextMemory``    — dynamic per-user context (NoOp)

All exposed as protocols so test paths can inject NoOps without
constructing real DB connections.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ConversationMemory(Protocol):
    async def recent(self, conversation_id: str, *, limit: int) -> list[dict[str, Any]]: ...
    async def record(self, conversation_id: str, *, question: str, answer: str,
                     metadata: dict[str, Any] | None = None) -> None: ...


@runtime_checkable
class ExecutionMemoryWriter(Protocol):
    async def record(self, snapshot: dict[str, Any]) -> None: ...


@runtime_checkable
class SemanticMemory(Protocol):
    async def search(self, query: str, *, k: int = 5) -> list[dict[str, Any]]: ...
    async def upsert(self, doc_id: str, text: str,
                     metadata: dict[str, Any] | None = None) -> None: ...


@runtime_checkable
class ConversationSummarizer(Protocol):
    async def summarize(self, turns: list[dict[str, Any]]) -> str: ...


@runtime_checkable
class BusinessContextMemory(Protocol):
    async def get_context(self, conversation_id: str) -> dict[str, Any]: ...


__all__ = [
    "ConversationMemory",
    "ExecutionMemoryWriter",
    "SemanticMemory",
    "ConversationSummarizer",
    "BusinessContextMemory",
]
