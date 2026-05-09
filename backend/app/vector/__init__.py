"""Vector / semantic retrieval module.

Public surface — call sites import from here, not the submodules.

Today's stack (in-process, zero external infra):
  - char-ngram TF-IDF embedder (deterministic, language-agnostic)
  - in-memory cosine-similarity vector store
  - hybrid retrieval: deterministic-first → semantic fallback → confidence merge

Future drop-ins (config-only swap via VECTOR_BACKEND env var):
  - pgvector — keep embeddings inside Postgres via the `pgvector` extension
  - Qdrant — dedicated vector DB

Both implement the same `VectorStore` Protocol; call sites don't change.
"""
from app.vector.embeddings import (
    Embedder,
    CharNgramTfidfEmbedder,
    get_default_embedder,
)
from app.vector.vector_store import (
    InMemoryVectorStore,
    VectorRecord,
    VectorStore,
    get_vector_store,
    set_vector_store,
)
from app.vector.semantic_search import (
    SemanticSearchResult,
    semantic_search,
)
from app.vector.retrieval import (
    HybridMatch,
    hybrid_retrieve,
    register_vocabulary,
    reset_vocabulary,
)

__all__ = [
    "CharNgramTfidfEmbedder",
    "Embedder",
    "HybridMatch",
    "InMemoryVectorStore",
    "SemanticSearchResult",
    "VectorRecord",
    "VectorStore",
    "get_default_embedder",
    "get_vector_store",
    "hybrid_retrieve",
    "register_vocabulary",
    "reset_vocabulary",
    "semantic_search",
    "set_vector_store",
]
