"""Embedding generators.

Today's default is `CharNgramTfidfEmbedder` — a deterministic, in-process,
typo-friendly embedder that doesn't require any model download or external
service. It's the right choice for entity matching and typo recovery
because:

  - **Typo robustness**: char n-grams overlap heavily across edit-distance-1
    pairs ("Sneaker" and "snkr" share several 2- and 3-grams), so cosine
    similarity is high even without a learned model.
  - **Deterministic**: same input → same vector forever. No drift across
    library versions, no API key, no GPU.
  - **Cheap**: 128-dim vector built via hash → mod, O(n) where n=chars.

When you swap to a real semantic model (sentence-transformers via Qdrant,
or pgvector with OpenAI-embeddings), implement the same `Embedder` interface
and call `set_default_embedder(...)` at startup.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Iterable, Protocol

import numpy as np

from app.infrastructure import settings


_log = logging.getLogger("agentic_ai.vector.embeddings")


# ---- Embedder protocol ----------------------------------------------------

class Embedder(Protocol):
    """Anything that can turn a string into a fixed-length float vector.

    Implementations MUST:
      - return vectors of length `self.dim` (called once + cached by callers)
      - normalize to unit length (so cosine = dot product)
      - be deterministic (same string -> same vector)
    """

    @property
    def dim(self) -> int: ...   # noqa: E704

    def embed(self, text: str) -> np.ndarray: ...   # noqa: E704

    def embed_many(self, texts: Iterable[str]) -> np.ndarray: ...   # noqa: E704


# ---- Char-ngram TF-IDF impl ----------------------------------------------

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+", re.I)


def _normalize(text: str) -> str:
    """Lowercase + collapse non-alphanumerics to single spaces. Strips
    boundary spaces so n-grams at word edges still align between
    'Sneaker A' and 'sneakers' once normalized."""
    return _NORMALIZE_RE.sub(" ", (text or "").lower()).strip()


def _ngrams(text: str, n_min: int = 2, n_max: int = 4) -> list[str]:
    """Generate character n-grams, padding word boundaries with `_` so
    'snkr' and 'sneakers' both contain '_s' at the start. Empty input
    returns an empty list."""
    norm = _normalize(text)
    if not norm:
        return []
    out: list[str] = []
    for word in norm.split():
        padded = f"_{word}_"
        for n in range(n_min, n_max + 1):
            if len(padded) < n:
                continue
            for i in range(len(padded) - n + 1):
                out.append(padded[i : i + n])
    return out


class CharNgramTfidfEmbedder:
    """Hashing-trick TF (sub-linear) embedder over 2..4-character n-grams.

    `dim` is small (default 128) — enough for entity-space discrimination
    without burning memory in the in-memory store. Increase via
    settings.vector_dim if you index a much larger corpus."""

    def __init__(self, dim: int | None = None, n_min: int = 2, n_max: int = 4) -> None:
        self._dim = int(dim or settings.vector_dim or 128)
        self._n_min = n_min
        self._n_max = n_max

    @property
    def dim(self) -> int:
        return self._dim

    def _hash_index(self, ngram: str) -> int:
        """Stable integer in [0, dim). md5 is overkill but fast + deterministic
        across Python versions (Python's built-in hash() is randomized by
        PYTHONHASHSEED)."""
        h = hashlib.md5(ngram.encode("utf-8")).digest()
        return int.from_bytes(h[:8], "big") % self._dim

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype=np.float32)
        ngs = _ngrams(text, self._n_min, self._n_max)
        if not ngs:
            return vec
        # Sub-linear TF: 1 + log(count). Pinned to int counters first so
        # ties between common ngrams ("_a", "ne", ...) don't dominate.
        counts: dict[int, int] = {}
        for ng in ngs:
            idx = self._hash_index(ng)
            counts[idx] = counts.get(idx, 0) + 1
        for idx, c in counts.items():
            vec[idx] = 1.0 + math.log(c)
        # L2 normalise — cosine similarity becomes a dot product, which
        # the in-memory vector store relies on for fast batched scoring.
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def embed_many(self, texts: Iterable[str]) -> np.ndarray:
        items = list(texts)
        if not items:
            return np.zeros((0, self._dim), dtype=np.float32)
        out = np.zeros((len(items), self._dim), dtype=np.float32)
        for i, t in enumerate(items):
            out[i] = self.embed(t)
        return out


# ---- Default-embedder singleton -------------------------------------------

_DEFAULT_EMBEDDER: Embedder | None = None


def get_default_embedder() -> Embedder:
    """Lazily-initialised singleton. Cheap to construct, but caching avoids
    re-reading settings on every call site."""
    global _DEFAULT_EMBEDDER
    if _DEFAULT_EMBEDDER is None:
        _DEFAULT_EMBEDDER = CharNgramTfidfEmbedder()
        _log.info(
            "embedder initialized: %s dim=%d",
            type(_DEFAULT_EMBEDDER).__name__, _DEFAULT_EMBEDDER.dim,
        )
    return _DEFAULT_EMBEDDER


def set_default_embedder(embedder: Embedder) -> None:
    """Replace the active embedder (tests + production swap to a learned
    model). Re-indexing the vector store after swapping is the caller's
    responsibility — different embedders produce incompatible vectors."""
    global _DEFAULT_EMBEDDER
    _DEFAULT_EMBEDDER = embedder
    _log.info("embedder swapped: %s dim=%d", type(embedder).__name__, embedder.dim)
