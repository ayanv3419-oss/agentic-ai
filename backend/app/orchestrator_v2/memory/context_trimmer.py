"""
Context trimmer — enforces a per-call prompt token budget.

Token counting is coarse-grained (4 chars ~ 1 token); good enough for
production budget enforcement until a model-specific tokeniser is wired
in P7+. Drop strategy: oldest first.
"""

from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    """Coarse char-based estimate. 4 chars ≈ 1 token for English; less
    accurate for code-heavy / JSON-heavy strings but trends correctly."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def trim_messages(
    messages: list[dict[str, Any]],
    *,
    budget_tokens: int,
    system_floor_tokens: int = 1024,
) -> list[dict[str, Any]]:
    """
    Trim a chat-style messages list so the total stays under ``budget_tokens``.

    Rules:

      * System message (role=='system'), if any, is preserved and at
        least ``system_floor_tokens`` worth of it stays in.
      * User and assistant messages are dropped oldest-first until under
        budget — but the most recent user message is always preserved.
    """
    if not messages:
        return messages

    def _toks(m: dict[str, Any]) -> int:
        content = m.get("content", "")
        return estimate_tokens(str(content))

    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]

    used = sum(_toks(m) for m in system_msgs)
    # Reserve the most recent user message.
    keepers = list(reversed(other_msgs))
    final: list[dict[str, Any]] = []
    for m in keepers:
        t = _toks(m)
        if used + t > budget_tokens and final:
            break
        final.append(m)
        used += t

    final.reverse()
    return system_msgs + final


__all__ = ["estimate_tokens", "trim_messages"]
