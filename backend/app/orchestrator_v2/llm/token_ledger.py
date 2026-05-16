"""
Token ledger — counts input + output tokens across all LLM calls in a turn.

Used by the WorkerLLMClient and CriticLLMClient to feed the ``TokenLedger``
on ``ExecutionState`` so the reflection loop can enforce the
``PER_TURN_TOKEN_BUDGET`` and ``PER_TURN_USD_BUDGET`` constants from
``run.py``.

Pricing constants below are rough estimates for Groq's ``llama-3.3-70b-versatile``
and ``llama-3.1-8b-instant`` (Groq's published rates as of 2025-Q1). They are
env-overridable so accounting can be tuned without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# USD per 1M tokens — approximate; tune via env if Groq pricing changes.
DEFAULT_INPUT_USD_PER_M = _env_float("V2_INPUT_USD_PER_M", 0.59)
DEFAULT_OUTPUT_USD_PER_M = _env_float("V2_OUTPUT_USD_PER_M", 0.79)


@dataclass(frozen=True)
class CallAccount:
    """Per-call accounting result returned to the WorkerAgent / CriticAgent."""

    input_tokens: int
    output_tokens: int
    cost_usd: float

    @classmethod
    def from_counts(
        cls,
        input_tokens: int,
        output_tokens: int,
        input_usd_per_m: float = DEFAULT_INPUT_USD_PER_M,
        output_usd_per_m: float = DEFAULT_OUTPUT_USD_PER_M,
    ) -> "CallAccount":
        cost = (
            (input_tokens / 1_000_000) * input_usd_per_m
            + (output_tokens / 1_000_000) * output_usd_per_m
        )
        return cls(
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cost_usd=max(0.0, cost),
        )


__all__ = [
    "CallAccount",
    "DEFAULT_INPUT_USD_PER_M",
    "DEFAULT_OUTPUT_USD_PER_M",
]
