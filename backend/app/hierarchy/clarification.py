"""Ambiguity detection — surfaces "I don't know which one you mean" cases.

For the MVP we only activate clarification on BRANCH ambiguity. Adding
product/category clarification is risky (asking too often kills UX); the
existing entity-resolver + KPI matcher already disambiguate at high
confidence, so we let those flows handle product naming.

A clarification fires when ALL of:
  1. Question references a branch/store/shop term.
  2. branch_master has ≥ 2 enabled rows.
  3. None of the configured branch names appears in the question.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.hierarchy.location import list_branches


_BRANCH_TRIGGERS = (
    "store", "stores", "branch", "branches", "shop", "shops",
    "outlet", "outlets", "location", "locations",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize(s: str) -> str:
    return " ".join(_TOKEN_RE.findall((s or "").lower()))


@dataclass
class ClarificationRequest:
    field: str                          # 'branch' (extensible)
    reason: str
    question: str
    options: list[str] = field(default_factory=list)
    prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field":    self.field,
            "reason":   self.reason,
            "question": self.question,
            "options":  self.options,
            "prompt":   self.prompt,
        }


async def detect_ambiguity(question: str) -> ClarificationRequest | None:
    """Return a ClarificationRequest if the question is ambiguous, else None."""
    q_norm = _normalize(question)
    if not q_norm:
        return None

    # Branch ambiguity check
    has_branch_term = any(t in q_norm.split() for t in _BRANCH_TRIGGERS)
    if has_branch_term:
        branches = await list_branches(enabled_only=True)
        if len(branches) >= 2:
            # Does the question already name one of the branches?
            names_lower = [b["branch_name"].lower() for b in branches]
            mentions = [n for n in names_lower if n and n in q_norm]
            if not mentions:
                return ClarificationRequest(
                    field="branch",
                    reason="multiple_branches_no_specific_mention",
                    question=question,
                    options=[b["branch_name"] for b in branches],
                    prompt=(
                        f"You have {len(branches)} stores: "
                        f"{', '.join(b['branch_name'] for b in branches)}. "
                        f"Which one did you mean?"
                    ),
                )

    return None
