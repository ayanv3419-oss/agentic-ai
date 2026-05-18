"""
coordinator/
============

Single Coordinator orchestration system powered by a local Qwen 3 model
served through Ollama.

Public surface (the only symbols core_system.py touches):

    run_query_turn   - the entry point /query_stream calls
    TurnState        - immutable per-turn state
    EventEmitter     - SSE event queue (re-exported from .emitter)
    format_sse       - SSE wire-format helper
    PresentationEmitter - sanitizes events before they hit the wire

Everything else is internal.
"""
from __future__ import annotations

from app.coordinator.emitter import (
    EventEmitter,
    PresentationEmitter,
    format_sse,
)
from app.coordinator.runner import run_query_turn
from app.coordinator.state import TurnState

__all__ = [
    "EventEmitter",
    "PresentationEmitter",
    "TurnState",
    "format_sse",
    "run_query_turn",
]
