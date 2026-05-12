"""Vector storage — in-memory cosine-similarity (single-user MVP)."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

import numpy as np

from app.vector.embeddings import Embedder, get_default_embedder


_log = logging.getLogger("agentic_ai.vector.store")


@dataclass
class VectorRecord:
    id: str
    text: str
    kind: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
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
    """Numpy-backed cosine-similarity store. Thread-safe via a single lock."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = embedder or get_default_embedder()
        self._dim = self._embedder.dim
        self._matrix = np.zeros((0, self._dim), dtype=np.float32)
        self._records: list[VectorRecord] = []
        self._id_to_idx: dict[tuple[str, str], int] = {}
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
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[VectorRecord, float]]:
        with self._lock:
            if not self._records:
                return []
            n = len(self._records)
            matrix = self._matrix[:n]
            records = self._records
        q = self._embedder.embed(query)
        if q.shape != (self._dim,):
            return []
        scores = matrix @ q
        if kind is not None:
            mask = np.array([r.kind == kind for r in records], dtype=bool)
            scores = np.where(mask, scores, -np.inf)
        k = max(1, min(limit, len(records)))
        top_idx = np.argpartition(-scores, kth=k - 1)[:k]
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


_VECTOR_STORE: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _VECTOR_STORE
    if _VECTOR_STORE is None:
        _VECTOR_STORE = InMemoryVectorStore()
        _log.info("vector store initialized: in_memory dim=%d", _VECTOR_STORE.dim())
    return _VECTOR_STORE


def set_vector_store(store: VectorStore) -> None:
    global _VECTOR_STORE
    _VECTOR_STORE = store
    _log.info("vector store swapped: %s dim=%d",
              store.kind_label(), store.dim())
