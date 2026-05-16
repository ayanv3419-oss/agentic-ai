"""
Per-request LLM-client scope.

The Planner, narrate capability, and Critic all need access to the
Worker / Critic LLM clients that were constructed for THIS turn (using
THIS user's per-request Groq key). State is frozen, so we use
contextvars — the same pattern v1 uses for ``request_groq``.

The runner sets the clients before calling the pipeline and resets them
in a finally block. Capabilities call ``get_worker_client()`` /
``get_critic_client()`` synchronously; missing clients raise
``RuntimeError`` rather than silently falling through, so misconfigured
test paths fail loudly.
"""

from __future__ import annotations

from contextvars import ContextVar

from app.orchestrator_v2.llm.critic_client import CriticLLMClient
from app.orchestrator_v2.llm.worker_client import WorkerLLMClient


_WORKER_LLM: ContextVar[WorkerLLMClient | None] = ContextVar(
    "v2_worker_llm", default=None
)
_CRITIC_LLM: ContextVar[CriticLLMClient | None] = ContextVar(
    "v2_critic_llm", default=None
)


def set_worker_client(client: WorkerLLMClient):
    return _WORKER_LLM.set(client)


def reset_worker_client(token) -> None:
    try:
        _WORKER_LLM.reset(token)
    except Exception:
        pass


def get_worker_client() -> WorkerLLMClient:
    client = _WORKER_LLM.get()
    if client is None:
        raise RuntimeError(
            "v2 worker LLM client not set — call set_worker_client() "
            "before invoking capabilities that need an LLM"
        )
    return client


def set_critic_client(client: CriticLLMClient):
    return _CRITIC_LLM.set(client)


def reset_critic_client(token) -> None:
    try:
        _CRITIC_LLM.reset(token)
    except Exception:
        pass


def get_critic_client() -> CriticLLMClient:
    client = _CRITIC_LLM.get()
    if client is None:
        raise RuntimeError(
            "v2 critic LLM client not set — call set_critic_client() "
            "before invoking the Critic agent"
        )
    return client


__all__ = [
    "set_worker_client",
    "reset_worker_client",
    "get_worker_client",
    "set_critic_client",
    "reset_critic_client",
    "get_critic_client",
]
