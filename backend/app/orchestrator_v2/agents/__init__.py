"""
agents/
=======

Two-role AI system.

* ``base.py``         — shared agent contract (system prompt, LLM client,
                        structured-output validation).
* ``worker_agent.py`` — plans + narrates. Two LLM calls per turn:
                        ``plan(question, context) → Plan`` and
                        ``narrate(state) → str``.
* ``critic_agent.py`` — evaluates Worker output. Returns a structured
                        ``CriticFeedback``. **Never** produces the final
                        business answer.

The Critic / Worker split is enforced by their respective LLM clients
in ``orchestrator_v2.llm``; agents themselves are stateless adapters.
"""
