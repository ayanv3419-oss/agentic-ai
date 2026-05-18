"""
Local LLM client. Talks to a running Ollama instance via the OpenAI
SDK's compatibility endpoint.

Fail loudly: if Ollama isn't reachable, calls return an error response
(never raise). No Groq fallback, no cloud fallback.

Qwen 3 thinking-token workaround: every system prompt has '/no_think'
appended so the model skips its <think>...</think> reasoning block.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI, APIConnectionError, APIError, APITimeoutError
from pydantic import BaseModel, Field

from app.infrastructure import settings

_log = logging.getLogger("coordinator.llm")

NO_THINK = "/no_think"


# ---------------------------------------------------------------------------
# Wire types - shape compatible with the rest of the coordinator
# ---------------------------------------------------------------------------


class LLMMessage(BaseModel):
    role: str
    content: str


class LLMResponse(BaseModel):
    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str | None = None
    error: str | None = None
    error_kind: str | None = None     # "auth" | "upstream" | "network" | "parse" | "unknown"


class LLMToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMToolResponse(BaseModel):
    content: str = ""
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str | None = None
    error: str | None = None
    error_kind: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove any <think>...</think> blocks Qwen 3 might still emit."""
    if not text:
        return ""
    cleaned = _THINK_RE.sub("", text).strip()
    return cleaned


def _model_understands_no_think(model: str | None) -> bool:
    """`/no_think` is a Qwen3-specific control token. Sending it to any
    other model (e.g. llama-3.3 on Groq in production) is at best
    inert noise and at worst confuses the model. Gate strictly."""
    if not model:
        return False
    return model.lower().startswith("qwen")


def ensure_no_think(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Append the Qwen3 /no_think directive when the active model is a
    Qwen3 variant. For any other model (llama, mixtral, etc.) the
    messages are returned unchanged."""
    if not _model_understands_no_think(model):
        return [dict(m) for m in messages]
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            content = str(m.get("content") or "")
            if NO_THINK not in content:
                m["content"] = content.rstrip() + "\n\n" + NO_THINK
            return out
    out.insert(0, {"role": "system", "content": NO_THINK})
    return out


def _find_json_object(text: str) -> str | None:
    """Find the first balanced {...} block in ``text`` using a
    bracket-depth scan. The old greedy regex (`\\{.*\\}` with DOTALL)
    matched from the first `{` to the LAST `}`, so two separate JSON
    blocks in the same string were concatenated into garbage."""
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start : i + 1]
    return None


def parse_strict_json(content: str) -> dict[str, Any]:
    """Extract a JSON object from LLM output. Tolerant of fences + prose."""
    if not content or not content.strip():
        raise ValueError("Empty LLM output")
    text = strip_thinking(content).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    block = _find_json_object(text)
    if block:
        try:
            return json.loads(block)
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse JSON: {e}") from e
    raise ValueError("LLM output contained no parseable JSON")


def _err_response(msg: str, kind: str) -> LLMResponse:
    return LLMResponse(content="", tokens_in=0, tokens_out=0,
                       error=msg, error_kind=kind)


def _err_tool_response(msg: str, kind: str) -> LLMToolResponse:
    return LLMToolResponse(error=msg, error_kind=kind)


# ---------------------------------------------------------------------------
# LLMClient - wraps AsyncOpenAI pointed at Ollama
# ---------------------------------------------------------------------------


class LLMClient:
    """Local LLM client. Defaults pulled from infrastructure.settings."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key or settings.llm_api_key or "ollama"
        self.model = model or settings.llm_model
        self.temperature = settings.llm_temperature if temperature is None else temperature
        self.max_tokens = settings.llm_max_tokens if max_tokens is None else max_tokens
        self._client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=httpx.Timeout(600.0, connect=10.0),
            max_retries=0,
        )

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:
            _log.warning("LLM client close failed", exc_info=True)

    async def complete(
        self,
        messages: list[LLMMessage] | list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        force_json: bool = False,
    ) -> LLMResponse:
        msgs = [
            m.model_dump() if isinstance(m, LLMMessage) else dict(m)
            for m in messages
        ]
        msgs = ensure_no_think(msgs, model=self.model)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if force_json:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except APIConnectionError as e:
            return _err_response(
                f"Cannot reach Ollama at {self.base_url}: {e}",
                "network",
            )
        except APITimeoutError as e:
            return _err_response(f"Ollama timed out: {e}", "network")
        except APIError as e:
            return _err_response(f"Ollama API error: {e}", "upstream")
        except Exception as e:
            _log.exception("llm.complete unexpected failure")
            return _err_response(f"{type(e).__name__}: {e}", "unknown")

        try:
            choice = resp.choices[0]
            content = strip_thinking(choice.message.content or "")
            usage = resp.usage
            return LLMResponse(
                content=content,
                tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
                tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
                finish_reason=choice.finish_reason,
            )
        except (AttributeError, IndexError, ValueError, TypeError) as e:
            return _err_response(f"Malformed Ollama response: {e}", "parse")

    async def complete_stream(
        self,
        messages: list[LLMMessage] | list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        msgs = [
            m.model_dump() if isinstance(m, LLMMessage) else dict(m)
            for m in messages
        ]
        msgs = ensure_no_think(msgs, model=self.model)
        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=msgs,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=self.max_tokens if max_tokens is None else max_tokens,
                stream=True,
            )
            async for chunk in stream:
                try:
                    delta = chunk.choices[0].delta.content or ""
                except (AttributeError, IndexError):
                    continue
                if delta:
                    yield strip_thinking(delta)
        except (APIConnectionError, APITimeoutError, APIError) as e:
            _log.warning("llm.complete_stream failed: %s", e)
            return
        except Exception:
            _log.exception("llm.complete_stream unexpected failure")
            return

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMToolResponse:
        """Native tool-calling completion. When ``tools`` is empty falls
        back to a plain completion (used for cost-guard 'final answer')."""
        msgs = [dict(m) for m in messages]
        msgs = ensure_no_think(msgs, model=self.model)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except APIConnectionError as e:
            return _err_tool_response(
                f"Cannot reach Ollama at {self.base_url}: {e}",
                "network",
            )
        except APITimeoutError as e:
            return _err_tool_response(f"Ollama timed out: {e}", "network")
        except APIError as e:
            return _err_tool_response(f"Ollama API error: {e}", "upstream")
        except Exception as e:
            _log.exception("llm.complete_with_tools unexpected failure")
            return _err_tool_response(f"{type(e).__name__}: {e}", "unknown")

        try:
            choice = resp.choices[0]
            msg = choice.message
            usage = resp.usage
            raw_calls = list(msg.tool_calls or [])
            tool_calls: list[LLMToolCall] = []
            for i, rc in enumerate(raw_calls):
                fn = rc.function
                raw_args = fn.arguments
                if isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    try:
                        parsed_args = json.loads(raw_args) if raw_args else {}
                    except (json.JSONDecodeError, TypeError):
                        parsed_args = {}
                tool_calls.append(LLMToolCall(
                    id=str(getattr(rc, "id", None) or f"call_{i}"),
                    name=str(fn.name or ""),
                    arguments=parsed_args if isinstance(parsed_args, dict) else {},
                ))
            return LLMToolResponse(
                content=strip_thinking(msg.content or ""),
                tool_calls=tool_calls,
                tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
                tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
                finish_reason=choice.finish_reason,
            )
        except (AttributeError, IndexError, ValueError, TypeError) as e:
            return _err_tool_response(f"Malformed Ollama response: {e}", "parse")


# ---------------------------------------------------------------------------
# Health check - called once at startup so failures are loud
# ---------------------------------------------------------------------------


async def check_llm_health() -> tuple[bool, str]:
    """Reach the LLM endpoint, list models, confirm the configured model is present.

    Works with Ollama (no auth) and any OpenAI-compatible cloud API such as
    Groq (sends Bearer token when LLM_API_KEY is set to a non-Ollama value).

    Returns (ok, message). On failure the message is the human-readable
    reason, suitable for surfacing in startup logs / /health output.
    """
    url = settings.llm_base_url.rstrip("/")
    expected_model = settings.llm_model
    # Send auth header for cloud APIs; Ollama runs unauthed on localhost.
    headers: dict[str, str] = {}
    api_key = settings.llm_api_key or ""
    if api_key and api_key.lower() != "ollama":
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url}/models", headers=headers)
    except httpx.ConnectError as e:
        return False, (
            f"LLM endpoint unreachable at {url} ({e}). "
            f"Check LLM_BASE_URL and make sure the server is running."
        )
    except httpx.HTTPError as e:
        return False, f"LLM health check failed: {e}"

    if resp.status_code != 200:
        return False, f"LLM API returned HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        data = resp.json()
        ids = {m.get("id") for m in data.get("data", []) if isinstance(m, dict)}
    except Exception as e:
        return False, f"Could not parse LLM /models response: {e}"

    if expected_model not in ids:
        return False, (
            f"Model {expected_model!r} not found at {url}. "
            f"Available: {sorted(ids)}"
        )

    return True, f"LLM OK at {url}, model={expected_model}"


__all__ = [
    "LLMClient",
    "LLMMessage",
    "LLMResponse",
    "LLMToolCall",
    "LLMToolResponse",
    "check_llm_health",
    "ensure_no_think",
    "parse_strict_json",
    "strip_thinking",
]
