"""Hybrid retrieval — deterministic match first, semantic fallback second.

The system's analytics design is deterministic-first: rule-based
EntityResolver / IntentClassifier hits at high confidence avoid LLM calls
entirely. The vector layer is a FALLBACK that fires only when the
deterministic path produces nothing (or low-confidence) results.

This module owns:

  - `register_vocabulary(kind, items)` — bootstrap helper. Pass an iterable
    of canonical entity names + (optional) aliases; the embeddings are
    generated and indexed.

  - `hybrid_retrieve(query, kind=...)` — single entry point that the
    EntityResolver tool calls. Returns a list of `HybridMatch` records
    each tagged by source ("exact" / "alias" / "semantic"), so callers
    can apply different downstream behaviour by source.

The fallback chain is:

  1. exact-match against canonical names
  2. alias-match (substring / synonym lookup)
  3. semantic search via vector store (cosine ≥ 0.55)

Confidence merge: exact = 1.0, alias = 0.85, semantic = the cosine score.
The first non-empty stage's results are returned — semantic only fires
when stages 1 + 2 are both empty.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from app.vector.semantic_search import semantic_search
from app.vector.vector_store import VectorRecord, get_vector_store


_log = logging.getLogger("agentic_ai.vector.retrieval")

_EXACT_SCORE = 1.0
_ALIAS_SCORE = 0.85
# 0.30 looks loose, but it's the LAST stage — exact + alias always win
# first. Char-ngram cosine for typo pairs ("snkrs" vs "Sneakers") lands
# in the 0.30-0.45 range, so a higher floor would discard the very cases
# this stage exists to catch. Fewer false positives matter less here than
# false negatives, since downstream tooling (SqlPlanner) treats the
# matched entity as a filter HINT, not a hard predicate. When you swap to
# a learned embedder (sentence-transformers etc.) raise this to 0.5+.
_SEMANTIC_FLOOR = 0.30


@dataclass(frozen=True)
class HybridMatch:
    """One retrieval result, tagged by which stage produced it.

    `source` ∈ {"exact", "alias", "semantic"} — call sites can branch on
    this to (e.g.) skip a confidence prompt when source=='exact'.
    """
    canonical: str
    matched_text: str
    kind: str
    score: float
    source: str


# ---- Vocabulary registry --------------------------------------------------

# An in-memory shadow of the deterministic vocabulary so we can do exact +
# alias matching without hitting the vector store. Populated by
# `register_vocabulary`. Maps `kind -> {canonical: [aliases...]}`.
_VOCAB: dict[str, dict[str, list[str]]] = {}


def register_vocabulary(
    kind: str,
    items: Iterable[tuple[str, list[str]]] | dict[str, list[str]],
) -> int:
    """Register canonical names + aliases for a namespace.

    Generates embeddings for each (canonical + alias) variant and indexes
    them in the vector store under namespace `kind`. The exact + alias
    layers are kept in `_VOCAB` for cheap lookup.

    Returns the number of canonical entries registered."""
    if isinstance(items, dict):
        item_list = list(items.items())
    else:
        item_list = list(items)
    if not item_list:
        return 0

    vocab = _VOCAB.setdefault(kind, {})
    records: list[VectorRecord] = []
    for canonical, aliases in item_list:
        canonical_norm = canonical.strip()
        if not canonical_norm:
            continue
        aliases_norm = [a.strip() for a in (aliases or []) if a and a.strip()]
        vocab[canonical_norm.lower()] = [a.lower() for a in aliases_norm]
        # Index canonical AND each alias as separate vector-store records.
        # We need DIFFERENT `id`s for each (the vector store dedupes by
        # (kind, id)) but they all carry the same `metadata["canonical"]`
        # so retrieval can map any matched alias back to the canonical
        # name. The id encodes the variant index for stability across
        # re-indexes.
        records.append(VectorRecord(
            id=canonical_norm, text=canonical_norm, kind=kind,
            metadata={"role": "canonical", "canonical": canonical_norm},
        ))
        for i, alias in enumerate(aliases_norm):
            records.append(VectorRecord(
                id=f"{canonical_norm}#alias={i}",
                text=alias, kind=kind,
                metadata={"role": "alias", "canonical": canonical_norm},
            ))

    if records:
        get_vector_store().upsert(records)
    _log.info(
        "vocabulary registered: kind=%s canonicals=%d total_records=%d",
        kind, len(item_list), len(records),
    )
    return len(item_list)


def reset_vocabulary(kind: str | None = None) -> None:
    """Clear the vocabulary (and its vectors) — used by tests + by the
    upload pipeline when a re-index is required."""
    if kind is None:
        _VOCAB.clear()
        get_vector_store().reset()
    else:
        _VOCAB.pop(kind, None)
        get_vector_store().reset(kind=kind)


# ---- Hybrid retrieval -----------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)


def _exact_matches(query_lower: str, kind: str) -> list[HybridMatch]:
    out: list[HybridMatch] = []
    vocab = _VOCAB.get(kind, {})
    for canonical_low in vocab:
        if canonical_low == query_lower or canonical_low in query_lower.split():
            out.append(HybridMatch(
                canonical=canonical_low,
                matched_text=canonical_low,
                kind=kind, score=_EXACT_SCORE, source="exact",
            ))
    return out


def _alias_matches(query_lower: str, kind: str) -> list[HybridMatch]:
    out: list[HybridMatch] = []
    vocab = _VOCAB.get(kind, {})
    for canonical_low, aliases in vocab.items():
        for alias in aliases:
            if alias and alias in query_lower:
                out.append(HybridMatch(
                    canonical=canonical_low,
                    matched_text=alias,
                    kind=kind, score=_ALIAS_SCORE, source="alias",
                ))
                break  # one alias hit per canonical is enough
    return out


def hybrid_retrieve(
    query: str,
    *,
    kind: str | None = None,
    limit: int = 5,
    semantic_floor: float = _SEMANTIC_FLOOR,
) -> list[HybridMatch]:
    """Deterministic-first retrieval with semantic fallback.

    Order:
      1. exact match against canonical names (score=1.0, source='exact')
      2. alias match (score=0.85, source='alias')
      3. semantic search via vector store (score=cosine, source='semantic')
    """
    if not query or not query.strip():
        return []
    query_norm = query.strip()
    query_lower = query_norm.lower()

    # Stage 1: exact -------------------------------------------------------
    if kind is not None:
        exact = _exact_matches(query_lower, kind)
    else:
        exact = []
        for k in _VOCAB.keys():
            exact.extend(_exact_matches(query_lower, k))
    if exact:
        return exact[:limit]

    # Stage 2: alias -------------------------------------------------------
    if kind is not None:
        alias = _alias_matches(query_lower, kind)
    else:
        alias = []
        for k in _VOCAB.keys():
            alias.extend(_alias_matches(query_lower, k))
    if alias:
        # Dedupe by canonical (one alias hit is enough).
        seen: set[str] = set()
        deduped: list[HybridMatch] = []
        for m in alias:
            if m.canonical in seen:
                continue
            seen.add(m.canonical)
            deduped.append(m)
        return deduped[:limit]

    # Stage 3: semantic fallback -------------------------------------------
    sem = semantic_search(
        query_norm, kind=kind, limit=limit, min_score=semantic_floor,
    )
    if not sem:
        return []
    return [
        HybridMatch(
            canonical=r.canonical.lower(),
            matched_text=r.text,
            kind=r.kind,
            score=r.score,
            source="semantic",
        )
        for r in sem
    ]
