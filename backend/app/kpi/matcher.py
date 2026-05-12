"""KPI matcher — resolves a natural-language question to a registered KPI.

Strategy (deterministic, zero-LLM):
  1. Normalize question (lowercase, collapse whitespace, strip punctuation).
  2. For every enabled KPI, score the question against:
       - exact alias match     → confidence 1.0  (decisive)
       - alias-substring match → confidence 0.85 (high)
       - id-substring match    → confidence 0.70 (medium)
       - name-substring match  → confidence 0.65 (medium)
  3. Return the highest-scoring match. Ties broken by alias length (longer
     alias = more specific = wins).

The confidence floor for the AI fast-path lives in the caller; this module
just emits scores. 0.85 is a sensible default — anything below that should
fall through to the regular SQL pipeline.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.kpi.registry import KpiRow, list_kpis


_log = logging.getLogger("agentic_ai.kpi.matcher")

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize(s: str) -> str:
    if not s:
        return ""
    return " ".join(_WORD_RE.findall(s.lower()))


@dataclass(frozen=True)
class KpiMatch:
    kpi: KpiRow
    confidence: float
    matched_alias: str
    reason: str


async def match_kpi(question: str) -> KpiMatch | None:
    """Find the best KPI for a question. Returns None if nothing scores."""
    if not question or not question.strip():
        return None
    q_norm = _normalize(question)
    if not q_norm:
        return None

    kpis = await list_kpis(enabled_only=True)
    if not kpis:
        return None

    best: KpiMatch | None = None
    for kpi in kpis:
        # 1. Exact alias match (question == alias)
        for alias in kpi.aliases:
            alias_norm = _normalize(alias)
            if alias_norm and q_norm == alias_norm:
                cand = KpiMatch(kpi=kpi, confidence=1.0, matched_alias=alias, reason="exact_alias")
                if _better(cand, best):
                    best = cand
                break

        # 2. Alias-substring match (alias is contained in question)
        for alias in kpi.aliases:
            alias_norm = _normalize(alias)
            if not alias_norm:
                continue
            if alias_norm in q_norm:
                cand = KpiMatch(kpi=kpi, confidence=0.85, matched_alias=alias, reason="alias_substring")
                if _better(cand, best):
                    best = cand
                break

        # 3. id-substring match (kpi.id phrase contained in question)
        id_phrase = _normalize(kpi.id.replace("_", " "))
        if id_phrase and id_phrase in q_norm:
            cand = KpiMatch(kpi=kpi, confidence=0.70, matched_alias=kpi.id, reason="id_substring")
            if _better(cand, best):
                best = cand

        # 4. Name-substring match
        name_norm = _normalize(kpi.kpi_name)
        if name_norm and name_norm in q_norm:
            cand = KpiMatch(kpi=kpi, confidence=0.65, matched_alias=kpi.kpi_name, reason="name_substring")
            if _better(cand, best):
                best = cand

    if best:
        _log.debug(
            "kpi match: q=%r → %s (conf=%.2f, alias=%r, reason=%s)",
            question, best.kpi.id, best.confidence, best.matched_alias, best.reason,
        )
    return best


def _better(a: KpiMatch, b: KpiMatch | None) -> bool:
    """`a` beats `b` if its confidence is higher, or if equal confidence
    but `a`'s matched alias is longer (more specific)."""
    if b is None:
        return True
    if a.confidence > b.confidence:
        return True
    if a.confidence < b.confidence:
        return False
    return len(a.matched_alias) > len(b.matched_alias)
