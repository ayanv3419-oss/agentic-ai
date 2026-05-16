"""
memory/
=======

Per-conversation and per-turn memory layers, gated behind protocols so the
NoOp stubs that ship in v2 launch can be swapped for real implementations
without changing call sites.

Phase P5 ships three real impls:

  - ``conversation_memory.py`` — SQLite-backed rolling window of recent
                                  Q&A pairs per conversation_id.
  - ``execution_memory.py``    — SQLite-backed audit log of plans,
                                  validator reports, Critic feedback,
                                  confidence scores, durations.
  - ``context_trimmer.py``     — pre-prompt token budget enforcer.

Phase P5 ships three NoOp stubs (interfaces only):

  - ``SemanticMemory``          (vector retrieval — wired in v2.1)
  - ``ConversationSummarizer``  (LLM summary of overflow — v2.1)
  - ``BusinessContextMemory``   (dynamic per-user context — v2.x)

The cache lives in ``front_door.py``, not here — it's a request-lifecycle
short-circuit, not a per-turn memory layer.
"""
