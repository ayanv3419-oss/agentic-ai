"""
Critic LLM client — symmetric to worker_client but role-configured for
evaluation. One JSON-mode call per reflection iteration.

Critic defaults to the same model as Worker initially (parity with v1);
swap to ``llama-3.1-8b-instant`` via ``CRITIC_MODEL`` env once accuracy
is verified post-launch.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.orchestrator_v2.llm.token_ledger import CallAccount

log = logging.getLogger("orchestrator_v2.llm.critic")


_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("prompt %s not found at %s; using empty fallback", name, path)
        return ""


CRITIC_SYSTEM_PROMPT = _load_prompt("critic_system")


@runtime_checkable
class CriticLLMClient(Protocol):
    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> tuple[dict[str, Any], CallAccount]:
        ...


class GroqCriticLLMClient:
    def __init__(self, api_key: str, model: str | None = None) -> None:
        from app.orchestrator_v2.llm.groq import GroqClient

        # Critic defaults to the same model as Worker; can be swapped via env.
        self.model = model or os.environ.get(
            "CRITIC_MODEL", os.environ.get("WORKER_MODEL", "llama-3.3-70b-versatile")
        )
        self._client = GroqClient(api_key=api_key, model=self.model)

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> tuple[dict[str, Any], CallAccount]:
        from app.orchestrator_v2.llm.groq import GroqMessage

        messages = [
            GroqMessage(role="system", content=system_prompt),
            GroqMessage(role="user", content=user_prompt),
        ]
        resp = await self._client.complete(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            force_json=True,
        )
        in_t = getattr(resp, "tokens_in", 0) or max(1, len(user_prompt) // 4)
        out_t = getattr(resp, "tokens_out", 0) or max(1, len(resp.content or "") // 4)
        try:
            payload = json.loads(resp.content or "{}")
        except json.JSONDecodeError:
            log.warning("critic JSON parse failed, returning is_acceptable=False")
            payload = {
                "is_acceptable": False,
                "confidence": 0.0,
                "summary": "critic LLM returned malformed JSON",
                "issues": [],
            }
        return payload, CallAccount.from_counts(in_t, out_t)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


class FakeCriticLLMClient:
    """
    Deterministic Critic — accepts by default. Tests can swap the verdict
    to drive the reflection loop.
    """

    ACCEPT = {
        "is_acceptable": True,
        "confidence": 0.95,
        "summary": "fake critic: answer is acceptable",
        "issues": [],
    }

    def __init__(self, verdict: dict[str, Any] | None = None) -> None:
        self.verdict = verdict or dict(self.ACCEPT)
        self.calls: list[str] = []

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> tuple[dict[str, Any], CallAccount]:
        self.calls.append(user_prompt[:80])
        in_t = max(1, (len(system_prompt) + len(user_prompt)) // 4)
        out_t = max(1, len(json.dumps(self.verdict)) // 4)
        return self.verdict, CallAccount.from_counts(in_t, out_t)

    async def aclose(self) -> None:
        return None


__all__ = [
    "CriticLLMClient",
    "GroqCriticLLMClient",
    "FakeCriticLLMClient",
    "CRITIC_SYSTEM_PROMPT",
]
