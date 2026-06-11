"""
LLM client. Talks to any OpenAI-compatible endpoint — Ollama locally,
Together.ai / other cloud providers in production.

Fail loudly: if the LLM endpoint isn't reachable, calls return an error
response (never raise). No silent fallback.

Qwen 3 thinking-token workaround: every system prompt has '/no_think'
appended so the model skips its <think>...</think> reasoning block.
"""
from __future__ import annotations

import asyncio
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

# Error kinds that mean "the provider failed, not our request" — safe to
# retry the identical call on the fallback provider.
_RETRYABLE = {"upstream", "network"}

# Process-wide cap on concurrent outbound provider calls. Under load (7
# users × 8-16 sequential LLM calls each) an uncapped client floods the
# provider and trips its rate limiter. Acquired PER ATTEMPT around the
# actual network call only — backoff sleeps happen outside the semaphore
# so a sleeping retry never holds a slot.
_LLM_SEMAPHORE = asyncio.Semaphore(4)

# Same-provider retry policy for transient (429 / 5xx / network) failures:
# the first call plus this many retries, with exponential backoff sleeps
# between attempts. Only after these are exhausted do we fall back to the
# secondary provider (handled by complete / complete_with_tools).
_MAX_RETRIES = 2
_BACKOFF_SCHEDULE = (0.5, 1.5)  # seconds before retry 1, retry 2
_RETRY_AFTER_CAP = 10.0          # never honor a Retry-After longer than this

# Provider HTTP status codes that mean the REQUEST was bad, not the
# provider — retrying (here or on the fallback) fails identically, so
# these are NOT retryable. 401/403 surface as "auth"; the rest as
# "client". 429 (rate limit) and 5xx stay "upstream" → retryable.
_AUTH_STATUS = {401, 403}
_NON_RETRYABLE_STATUS = {400, 404, 405, 409, 410, 413, 414, 415, 422}


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
    error_kind: str | None = None     # "auth" | "client" | "upstream" | "network" | "parse" | "unknown"


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
    non-Qwen model is at best inert noise and at worst confuses the model.
    Gate strictly on model name."""
    if not model:
        return False
    return model.lower().startswith("qwen")


def _model_needs_thinking_flag(model: str | None) -> bool:
    """DashScope's Qwen3 reasoning models (qwen3-32b, qwen3.6-35b-a3b, ...)
    reject non-streaming calls unless `enable_thinking` is explicitly false.
    Commercial qwen-turbo/plus/flash/max don't need it; the Ollama
    'qwen3:Nb' tag format is excluded — it handles thinking via /no_think."""
    if not model:
        return False
    m = model.lower()
    return m.startswith("qwen3") and ":" not in m


def _floor_max_tokens(model: str | None, requested: int | None) -> int | None:
    """Gemini 2.5 models spend part of the max_tokens budget on internal
    'thinking'. A small cap (e.g. 900) gets fully consumed by thinking,
    leaving nothing for the answer / tool calls — the call then returns
    empty. Floor the cap generously for Gemini so both fit."""
    if requested is None:
        return None
    if model and "gemini" in model.lower() and requested < 8192:
        return 8192
    return requested


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


def _classify_api_error(exc: APIError) -> str:
    """Map an OpenAI APIError to an error_kind by HTTP status. 4xx request
    errors are permanent (retrying on the fallback fails the same way) so
    they land outside _RETRYABLE; 429 and 5xx are transient upstream
    failures the fallback provider may absorb."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in _AUTH_STATUS:
        return "auth"
    if status in _NON_RETRYABLE_STATUS:
        return "client"
    return "upstream"


def _retry_after_seconds(exc: Exception) -> float | None:
    """Best-effort extraction of a Retry-After hint from a provider error.

    Looks at the error's HTTP response headers for ``Retry-After`` (delta
    seconds form only; HTTP-date form is ignored). Returns the delay in
    seconds capped at ``_RETRY_AFTER_CAP``, or ``None`` when absent/unparsable.
    """
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if not raw:
        return None
    try:
        secs = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if secs < 0:
        return None
    return min(secs, _RETRY_AFTER_CAP)


async def _sleep_before_retry(attempt: int, exc: Exception | None) -> None:
    """Sleep before same-provider retry ``attempt`` (1-based).

    Honors a Retry-After header when the provider supplied one, otherwise
    falls back to the exponential backoff schedule. The semaphore is NOT
    held while we sleep — callers acquire it per attempt around the network
    call only."""
    idx = min(attempt - 1, len(_BACKOFF_SCHEDULE) - 1)
    delay = _BACKOFF_SCHEDULE[idx]
    if exc is not None:
        hinted = _retry_after_seconds(exc)
        if hinted is not None:
            delay = hinted
    await asyncio.sleep(delay)


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
        enable_fallback: bool = True,
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
        # Optional fallback provider — used automatically when the primary
        # is rate-limited / out of quota / unreachable. The fallback client
        # itself has no further fallback (enable_fallback=False).
        self._fallback: "LLMClient | None" = None
        if (enable_fallback and settings.llm_fallback_api_key
                and settings.llm_fallback_base_url):
            self._fallback = LLMClient(
                base_url=settings.llm_fallback_base_url,
                api_key=settings.llm_fallback_api_key,
                model=settings.llm_fallback_model or self.model,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_fallback=False,
            )

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:
            _log.warning("LLM client close failed", exc_info=True)
        if self._fallback is not None:
            await self._fallback.aclose()

    async def complete(
        self,
        messages: list[LLMMessage] | list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        force_json: bool = False,
    ) -> LLMResponse:
        """Plain completion WITH automatic failover: if the primary
        provider is rate-limited / out of quota / unreachable, the same
        request is retried once on the configured fallback provider."""
        resp = await self._complete_once(
            messages, temperature=temperature, max_tokens=max_tokens,
            force_json=force_json,
        )
        if (resp.error and resp.error_kind in _RETRYABLE
                and self._fallback is not None):
            _log.warning(
                "LLM primary failed (%s) — failing over to %s. detail: %s",
                resp.error_kind, self._fallback.model, (resp.error or "")[:300],
            )
            return await self._fallback._complete_once(
                messages, temperature=temperature, max_tokens=max_tokens,
                force_json=force_json,
            )
        return resp

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMToolResponse:
        """Tool-calling completion WITH automatic failover (see complete)."""
        resp = await self._complete_with_tools_once(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens,
        )
        if (resp.error and resp.error_kind in _RETRYABLE
                and self._fallback is not None):
            _log.warning(
                "LLM primary failed (%s) — failing over to %s. detail: %s",
                resp.error_kind, self._fallback.model, (resp.error or "")[:300],
            )
            # Single-attempt: don't re-run the fallback's full retry schedule
            # on top of the primary's retries (avoids up to 6 sequential calls
            # and ~4s of backoff sleeps under load).
            return await self._fallback._complete_with_tools_one_shot(
                messages, tools=tools, temperature=temperature,
                max_tokens=max_tokens,
            )
        return resp

    async def _complete_once(
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
            "max_tokens": _floor_max_tokens(
                self.model, self.max_tokens if max_tokens is None else max_tokens),
            "stream": False,
        }
        if force_json:
            kwargs["response_format"] = {"type": "json_object"}
        if _model_needs_thinking_flag(self.model):
            kwargs["extra_body"] = {"enable_thinking": False}

        resp = None
        err: LLMResponse | None = None
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                await _sleep_before_retry(attempt, last_exc)
            last_exc = None
            try:
                # Acquire PER ATTEMPT so backoff sleeps above don't hold a slot.
                async with _LLM_SEMAPHORE:
                    resp = await self._client.chat.completions.create(**kwargs)
                err = None
                break
            except APIConnectionError as e:
                last_exc = e
                err = _err_response(
                    f"Cannot reach LLM at {self.base_url}: {e}",
                    "network",
                )
            except APITimeoutError as e:
                last_exc = e
                err = _err_response(f"LLM request timed out at {self.base_url}: {e}", "network")
            except APIError as e:
                last_exc = e
                err = _err_response(
                    f"LLM API error from {self.base_url} ({self.model}): {e}",
                    _classify_api_error(e),
                )
            except Exception as e:
                _log.exception("llm.complete unexpected failure")
                return _err_response(f"{type(e).__name__}: {e}", "unknown")
            # Only transient (429 / 5xx / network) errors are retried here;
            # 4xx client/auth errors fail identically on retry → bail now.
            if err.error_kind not in _RETRYABLE or attempt == _MAX_RETRIES:
                break
            _log.warning(
                "LLM call to %s failed (%s), retrying %d/%d: %s",
                self.model, err.error_kind, attempt + 1, _MAX_RETRIES,
                (err.error or "")[:200],
            )
        if err is not None:
            return err

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
            return _err_response(f"Malformed LLM response from {self.base_url}: {e}", "parse")

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
        # Mirror the non-streaming path's provider fixes: Gemini needs a
        # max_tokens floor and DashScope Qwen3 needs enable_thinking=False.
        # Streaming has no failover — a fallback cannot un-yield chunks
        # already sent to the caller.
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": _floor_max_tokens(
                self.model, self.max_tokens if max_tokens is None else max_tokens),
            "stream": True,
        }
        if _model_needs_thinking_flag(self.model):
            kwargs["extra_body"] = {"enable_thinking": False}
        try:
            stream = await self._client.chat.completions.create(**kwargs)
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

    async def _complete_with_tools_once(
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
            "max_tokens": _floor_max_tokens(
                self.model, self.max_tokens if max_tokens is None else max_tokens),
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if _model_needs_thinking_flag(self.model):
            kwargs["extra_body"] = {"enable_thinking": False}

        resp = None
        err: LLMToolResponse | None = None
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                await _sleep_before_retry(attempt, last_exc)
            last_exc = None
            try:
                # Acquire PER ATTEMPT so backoff sleeps above don't hold a slot.
                async with _LLM_SEMAPHORE:
                    resp = await self._client.chat.completions.create(**kwargs)
                err = None
                break
            except APIConnectionError as e:
                last_exc = e
                err = _err_tool_response(
                    f"Cannot reach LLM at {self.base_url}: {e}",
                    "network",
                )
            except APITimeoutError as e:
                last_exc = e
                err = _err_tool_response(f"LLM request timed out at {self.base_url}: {e}", "network")
            except APIError as e:
                last_exc = e
                # Surface the real provider error — status code, body, etc.
                detail = str(e)
                status = getattr(e, "status_code", None) or getattr(e, "code", None)
                body = getattr(e, "body", None) or getattr(e, "response", None)
                err = _err_tool_response(
                    f"LLM API error from {self.base_url} (model={self.model}, status={status}): {detail} body={str(body)[:300]}",
                    _classify_api_error(e),
                )
            except Exception as e:
                _log.exception("llm.complete_with_tools unexpected failure")
                return _err_tool_response(f"{type(e).__name__}: {e}", "unknown")
            # Only transient (429 / 5xx / network) errors are retried here;
            # 4xx client/auth errors fail identically on retry → bail now.
            if err.error_kind not in _RETRYABLE or attempt == _MAX_RETRIES:
                break
            _log.warning(
                "LLM tool call to %s failed (%s), retrying %d/%d: %s",
                self.model, err.error_kind, attempt + 1, _MAX_RETRIES,
                (err.error or "")[:200],
            )
        if err is not None:
            return err

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
            return _err_tool_response(f"Malformed LLM response from {self.base_url}: {e}", "parse")

    async def _complete_with_tools_one_shot(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMToolResponse:
        """Single-attempt variant used for the fallback provider so we don't
        run the fallback's full retry schedule on top of the primary's retries."""
        msgs = [dict(m) for m in messages]
        msgs = ensure_no_think(msgs, model=self.model)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": _floor_max_tokens(
                self.model, self.max_tokens if max_tokens is None else max_tokens),
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if _model_needs_thinking_flag(self.model):
            kwargs["extra_body"] = {"enable_thinking": False}
        try:
            async with _LLM_SEMAPHORE:
                resp = await self._client.chat.completions.create(**kwargs)
        except APIConnectionError as e:
            return _err_tool_response(f"Cannot reach fallback LLM at {self.base_url}: {e}", "network")
        except APITimeoutError as e:
            return _err_tool_response(f"Fallback LLM timed out at {self.base_url}: {e}", "network")
        except APIError as e:
            status = getattr(e, "status_code", None) or getattr(e, "code", None)
            return _err_tool_response(
                f"Fallback LLM API error (model={self.model}, status={status}): {e}",
                _classify_api_error(e),
            )
        except Exception as e:
            return _err_tool_response(f"Fallback LLM unexpected: {type(e).__name__}: {e}", "unknown")
        try:
            choice = resp.choices[0]
            msg = choice.message
            usage = resp.usage
            raw_calls = list(msg.tool_calls or [])
            tool_calls: list[LLMToolCall] = []
            for i, rc in enumerate(raw_calls):
                fn = rc.function
                raw_args = fn.arguments
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
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
            return _err_tool_response(
                f"Malformed fallback LLM response from {self.base_url}: {e}", "parse"
            )


# ---------------------------------------------------------------------------
# Health check - called once at startup so failures are loud
# ---------------------------------------------------------------------------


async def check_llm_health() -> tuple[bool, str]:
    """Reach the LLM endpoint, list models, confirm the configured model is present.

    Works with Ollama (no auth) and any OpenAI-compatible cloud API such as
    Together.ai (sends Bearer token when LLM_API_KEY is set to a non-Ollama value).

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

    # Different providers return different shapes for /models:
    #   - OpenAI / Ollama : {"data": [{"id": "..."}, ...]}
    #   - Together.ai     : [{"id": "..."}, ...]   (bare list)
    #   - Some proxies    : {"models": [{"id": "..."}, ...]}
    # Be liberal in what we accept.
    try:
        payload = resp.json()
        if isinstance(payload, list):
            models = payload
        elif isinstance(payload, dict):
            models = payload.get("data") or payload.get("models") or []
        else:
            models = []
        ids = {m.get("id") for m in models if isinstance(m, dict) and m.get("id")}
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
