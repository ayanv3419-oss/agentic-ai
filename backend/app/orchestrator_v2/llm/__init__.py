"""
llm/
====

Worker and Critic LLM clients plus prompt assets.

  - ``worker_client.py`` — wraps ``GroqClient`` with Worker-role config
                           (default model, temperature, max tokens, retry).
                           Used for Plan + Narrate calls.
  - ``critic_client.py`` — wraps ``GroqClient`` with Critic-role config.
                           Defaults to the same model as Worker for v2
                           launch; can be swapped to a cheaper/smaller
                           model post-launch via env (``CRITIC_MODEL``).
  - ``token_ledger.py``  — counts input + output tokens per call, feeds
                           the per-turn budget guard.
  - ``prompts/``         — system prompts as Markdown files
                           (planner_system, planner_delta, narrator_system,
                           critic_system).

Provider abstraction (Anthropic / OpenAI / Bedrock) is deferred. The
existing ``app.analytics_engine.GroqClient`` is reused verbatim until
the sunset phase moves it into this package.
"""
