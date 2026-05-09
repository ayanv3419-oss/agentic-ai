"""Vector storage interface + in-memory cosine-similarity impl.

`VectorStore` is the contract every backend honours. Today's deployment
ships `InMemoryVectorStore`, which is suitable up to ~10^5 vectors at
`dim=128` (memory: ~50 MB, query latency: <1 ms). Beyond that, swap to
pgvector or Qdrant — same protocol.

Records carry:
  - `id`        — stable string identifier (the canonical entity name)
  - `text`      — the original text the embedding was made from
  - `kind`      — namespace tag ("product" / "customer" / "vendor" / ...)
  - `metadata`  — optional dict for tenant_id, created_at, etc.

Search returns `(record, similarity)` where similarity ∈ [0.0, 1.0]
because every embedder L2-normalises and cosine = dot of unit vectors.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

import numpy as np

from app.infrastructure import settings
from app.vector.embeddings import Embedder, get_default_embedder


_log = logging.getLogger("agentic_ai.vector.store")


@dataclass
class VectorRecord:
    id: str
    text: str
    kind: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    """Subset of the pgvector / Qdrant API we actually use. Keep small."""

    def kind_label(self) -> str: ...   # noqa: E704
    def dim(self) -> int: ...   # noqa: E704
    def size(self) -> int: ...   # noqa: E704

    def upsert(self, records: Iterable[VectorRecord]) -> int: ...   # noqa: E704

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[VectorRecord, float]]: ...   # noqa: E704

    def reset(self, kind: str | None = None) -> int: ...   # noqa: E704


class InMemoryVectorStore:
    """Numpy-backed cosine-similarity store. Thread-safe via a single lock
    (writes are rare; reads dominate)."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = embedder or get_default_embedder()
        self._dim = self._embedder.dim
        # Matrix is grown by chunks; we re-pack the slice [:n_records] for
        # search to avoid allocating per query.
        self._matrix = np.zeros((0, self._dim), dtype=np.float32)
        self._records: list[VectorRecord] = []
        self._id_to_idx: dict[tuple[str, str], int] = {}  # (kind, id) -> idx
        self._lock = threading.Lock()

    def kind_label(self) -> str:
        return "in_memory"

    def dim(self) -> int:
        return self._dim

    def size(self) -> int:
        with self._lock:
            return len(self._records)

    def upsert(self, records: Iterable[VectorRecord]) -> int:
        items = list(records)
        if not items:
            return 0
        new_vecs = self._embedder.embed_many([r.text for r in items])
        with self._lock:
            for r, vec in zip(items, new_vecs):
                key = (r.kind, r.id)
                if key in self._id_to_idx:
                    # Update in place: replace vector + record metadata.
                    idx = self._id_to_idx[key]
                    self._matrix[idx] = vec
                    self._records[idx] = r
                else:
                    self._id_to_idx[key] = len(self._records)
                    self._records.append(r)
                    self._matrix = np.vstack([self._matrix, vec[None, :]])
        _log.debug("vector_store upsert: %d records (total=%d)",
                   len(items), len(self._records))
        return len(items)

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        tenant_id: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[VectorRecord, float]]:
        """Cosine-similarity search.

        `tenant_id` filter — when set, only records whose
        `metadata["tenant_id"]` equals this value are eligible. Records
        with no tenant_id (the global vocabulary registered at boot from
        synonyms.json) are ALSO eligible — they're shared across tenants
        on purpose. Pass tenant_id=None to disable tenant filtering
        entirely (legacy single-admin path)."""
        with self._lock:
            if not self._records:
                return []
            n = len(self._records)
            matrix = self._matrix[:n]
            records = self._records
        q = self._embedder.embed(query)
        if q.shape != (self._dim,):
            return []
        # Cosine = dot when both unit-normalised.
        scores = matrix @ q  # shape (n,)
        # Apply kind filter at score time so we don't scan tracks of
        # irrelevant vectors when the namespace is small.
        if kind is not None:
            mask = np.array([r.kind == kind for r in records], dtype=bool)
            scores = np.where(mask, scores, -np.inf)
        # Apply tenant filter — global (no tenant_id) records always
        # match; tenant-stamped records match only when tenant_id equals.
        if tenant_id is not None:
            t_mask = np.array([
                (r.metadata or {}).get("tenant_id") in (None, "", tenant_id)
                for r in records
            ], dtype=bool)
            scores = np.where(t_mask, scores, -np.inf)
        # argpartition is O(n) and faster than full sort for small `limit`.
        k = max(1, min(limit, len(records)))
        top_idx = np.argpartition(-scores, kth=k - 1)[:k]
        # Sort the (small) top-k by descending score.
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        out: list[tuple[VectorRecord, float]] = []
        for idx in top_idx:
            score = float(scores[idx])
            if score == -np.inf or score < min_score:
                continue
            out.append((records[int(idx)], score))
        return out

    def reset(self, kind: str | None = None) -> int:
        with self._lock:
            if kind is None:
                n = len(self._records)
                self._matrix = np.zeros((0, self._dim), dtype=np.float32)
                self._records = []
                self._id_to_idx = {}
                return n
            keep_idxs = [i for i, r in enumerate(self._records) if r.kind != kind]
            removed = len(self._records) - len(keep_idxs)
            if removed == 0:
                return 0
            self._matrix = self._matrix[keep_idxs] if keep_idxs else np.zeros(
                (0, self._dim), dtype=np.float32,
            )
            self._records = [self._records[i] for i in keep_idxs]
            self._id_to_idx = {(r.kind, r.id): i for i, r in enumerate(self._records)}
            return removed


# ---- Singleton + swap hooks ----------------------------------------------

def _build_default_store() -> VectorStore:
    backend = (settings.vector_backend or "in_memory").lower()
    if backend == "in_memory":
        return InMemoryVectorStore()
    # `pgvector` and `qdrant` are future drop-ins. They MUST honour the same
    # VectorStore protocol; today we fall back to in-memory with a warning so
    # the system never refuses to boot for a backend that isn't installed.
    _log.warning(
        "vector_backend=%r requested but only 'in_memory' is implemented; "
        "falling back. Install pgvector / qdrant-client adapter to enable.",
        backend,
    )
    return InMemoryVectorStore()


_VECTOR_STORE: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _VECTOR_STORE
    if _VECTOR_STORE is None:
        _VECTOR_STORE = _build_default_store()
        _log.info("vector store initialized: %s dim=%d",
                  _VECTOR_STORE.kind_label(), _VECTOR_STORE.dim())
    return _VECTOR_STORE


def set_vector_store(store: VectorStore) -> None:
    """Swap the active backend. Tests use this to inject a fresh store
    between cases; production swaps in pgvector / Qdrant adapters."""
    global _VECTOR_STORE
    _VECTOR_STORE = store
    _log.info("vector store swapped: %s dim=%d",
              store.kind_label(), store.dim())
