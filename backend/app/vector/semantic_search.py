"""Top-level semantic-search helper.

Thin wrapper over `get_vector_store().search(...)` that returns a small
result-record dataclass instead of (record, score) tuples — friendlier for
call sites that just want "the matched canonical name + a score".
"""
from __future__ import annotations

from dataclasses import dataclass

from app.vector.vector_store import VectorRecord, get_vector_store


@dataclass(frozen=True)
class SemanticSearchResult:
    """One semantic match, ranked by `score` ∈ [0, 1] (cosine similarity)."""
    canonical: str          # the entity's canonical name (record.id)
    text: str               # the original text the embedding was made from
    kind: str               # namespace ("product", "customer", ...)
    score: float            # cosine similarity
    metadata: dict | None = None

    @classmethod
    def from_record(cls, rec: VectorRecord, score: float) -> "SemanticSearchResult":
        # When a record was indexed as an alias, `metadata["canonical"]`
        # carries the user-facing canonical name (record.id is a synthetic
        # `<canonical>#alias=N` so the vector store can keep multiple alias
        # texts under one logical entity). Honour it.
        meta = dict(rec.metadata) if rec.metadata else None
        canonical = (meta or {}).get("canonical") or rec.id
        return cls(
            canonical=canonical,
            text=rec.text,
            kind=rec.kind,
            score=float(score),
            metadata=meta,
        )


def semantic_search(
    query: str,
    *,
    kind: str | None = None,
    tenant_id: str | None = None,
    limit: int = 5,
    min_score: float = 0.4,
) -> list[SemanticSearchResult]:
    """Find up to `limit` semantic matches for `query` in the active vector
    store, optionally restricted to a single namespace + a single tenant.

    `tenant_id` filters out records stamped with a DIFFERENT tenant
    (records with no tenant_id are global — always match). Pass None to
    disable tenant filtering (legacy single-admin path).

    `min_score=0.4` is a deliberately conservative default — a 0.4 cosine
    similarity in this char-ngram TF-IDF space is roughly "shares a few
    n-grams". Callers that need higher precision should bump it (the
    EntityResolver hybrid path uses 0.55)."""
    if not query or not query.strip():
        return []
    store = get_vector_store()
    raw = store.search(
        query, kind=kind, tenant_id=tenant_id, limit=limit, min_score=min_score,
    )
    return [SemanticSearchResult.from_record(r, s) for r, s in raw]
