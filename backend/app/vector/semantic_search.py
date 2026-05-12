"""Top-level semantic-search helper."""
from __future__ import annotations

from dataclasses import dataclass

from app.vector.vector_store import VectorRecord, get_vector_store


@dataclass(frozen=True)
class SemanticSearchResult:
    canonical: str
    text: str
    kind: str
    score: float
    metadata: dict | None = None

    @classmethod
    def from_record(cls, rec: VectorRecord, score: float) -> "SemanticSearchResult":
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
    limit: int = 5,
    min_score: float = 0.4,
) -> list[SemanticSearchResult]:
    if not query or not query.strip():
        return []
    store = get_vector_store()
    raw = store.search(query, kind=kind, limit=limit, min_score=min_score)
    return [SemanticSearchResult.from_record(r, s) for r, s in raw]
