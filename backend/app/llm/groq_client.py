"""Async Groq client.

Failure contract: `complete()` and `complete_stream()` NEVER raise. They return
a result whose `error` / `error_kind` are set when the upstream call failed.
Per-request key scoping uses `contextvars` so the in-flight key from the
`X-Groq-Api-Key` header doesn't leak between concurrent requests.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from typing import Any, AsyncIterator

import httpx
from pydantic import BaseModel

from app.config import settings

log = logging.getLogger("agentic_ai.groq")


class GroqMessage(BaseModel):
    role: str
    content: str


class GroqResponse(BaseModel):
    content: str
    tokens_in: int
    tokens_out: int
    finish_reason: str | None = None
    error: str | None = None
    error_kind: str | None = None  # "auth" | "upstream" | "network" | "parse" | "unknown"


class GroqStreamChunk(BaseModel):
    delta: str
    finish_reason: str | None = None
    error: str | None = None
    error_kind: str | None = None


def _err_response(msg: str, kind: str) -> GroqResponse:
    return GroqResponse(content="", tokens_in=0, tokens_out=0, error=msg, error_kind=kind)


def _err_chunk(msg: str, kind: str) -> GroqStreamChunk:
    return GroqStreamChunk(delta="", finish_reason="error", error=msg, error_kind=kind)


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model or settings.groq_model
        self.base_url = settings.groq_base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            log.warning("groq client close failed", exc_info=True)

    def _headers_or_error(self) -> tuple[dict[str, str] | None, GroqResponse | None]:
        if not self.api_key:
            return None, _err_response("GROQ_API_KEY not configured", "auth")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }, None

    def _payload(
        self,
        messages: list[GroqMessage],
        *,
        temperature: float,
        max_tokens: int,
        force_json: bool,
        stream: bool,
    ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if force_json:
            p["response_format"] = {"type": "json_object"}
        return p

    async def complete(
        self,
        messages: list[GroqMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        force_json: bool = True,
    ) -> GroqResponse:
        try:
            return await self._complete_inner(messages, temperature, max_tokens, force_json)
        except Exception as e:
            log.exception("groq.complete unexpected failure")
            return _err_response(f"{type(e).__name__}: {e}", "unknown")

    async def _complete_inner(
        self,
        messages: list[GroqMessage],
        temperature: float,
        max_tokens: int,
        force_json: bool,
    ) -> GroqResponse:
        headers, err = self._headers_or_error()
        if err is not None:
            return err
        payload = self._payload(messages, temperature=temperature, max_tokens=max_tokens,
                                force_json=force_json, stream=False)
        url = f"{self.base_url}/chat/completions"
        last_status: int | None = None
        last_body = ""
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(url, headers=headers, json=payload)
            except httpx.RequestError as e:
                last_err = e
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            if resp.status_code in (401, 403):
                return _err_response(f"Groq auth rejected: HTTP {resp.status_code}", "auth")
            if resp.status_code >= 400:
                last_status = resp.status_code
                try:
                    last_body = (resp.text or "")[:300]
                except Exception:
                    last_body = ""
                last_err = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                if resp.status_code >= 500 or resp.status_code == 429:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                return _err_response(
                    f"Groq error: HTTP {resp.status_code} {last_body}", "upstream"
                )
            try:
                data = resp.json()
                choice = data["choices"][0]
                usage = data.get("usage", {}) or {}
                return GroqResponse(
                    content=choice["message"].get("content") or "",
                    tokens_in=int(usage.get("prompt_tokens", 0)),
                    tokens_out=int(usage.get("completion_tokens", 0)),
                    finish_reason=choice.get("finish_reason"),
                )
            except (KeyError, IndexError, ValueError, TypeError) as e:
                return _err_response(f"Malformed Groq response: {e}", "parse")
        if last_status is not None:
            return _err_response(
                f"Groq retries exhausted (last HTTP {last_status}): {last_body}", "upstream"
            )
        return _err_response(f"Groq retries exhausted: {last_err}", "network")

    async def complete_stream(
        self,
        messages: list[GroqMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        force_json: bool = True,
    ) -> AsyncIterator[GroqStreamChunk]:
        headers, err = self._headers_or_error()
        if err is not None:
            yield _err_chunk(err.error or "auth error", err.error_kind or "auth")
            return
        payload = self._payload(messages, temperature=temperature, max_tokens=max_tokens,
                                force_json=force_json, stream=True)
        url = f"{self.base_url}/chat/completions"
        try:
            async with self._client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    try:
                        body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                    except Exception:
                        body = ""
                    kind = "auth" if resp.status_code in (401, 403) else "upstream"
                    yield _err_chunk(f"HTTP {resp.status_code}: {body}", kind)
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        choice = chunk["choices"][0]
                        delta = (choice.get("delta") or {}).get("content") or ""
                        finish = choice.get("finish_reason")
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        log.debug("dropping malformed stream chunk: %r", raw[:120])
                        continue
                    if delta or finish:
                        yield GroqStreamChunk(delta=delta, finish_reason=finish)
        except httpx.RequestError as e:
            yield _err_chunk(f"Network error: {e}", "network")
        except Exception as e:
            log.exception("groq.complete_stream unexpected failure")
            yield _err_chunk(f"{type(e).__name__}: {e}", "unknown")


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_strict_json(content: str) -> dict[str, Any]:
    """Extract a JSON object from LLM output, tolerant of fences and prose."""
    if not content or not content.strip():
        raise ValueError("Empty LLM output")
    text = content.strip()
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
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse JSON: {e}") from e
    raise ValueError("LLM output contained no parseable JSON")


_singleton: GroqClient | None = None
_request_groq: contextvars.ContextVar[GroqClient | None] = contextvars.ContextVar(
    "request_groq", default=None
)


def set_request_groq(client: GroqClient | None) -> contextvars.Token:
    return _request_groq.set(client)


def reset_request_groq(token: contextvars.Token) -> None:
    try:
        _request_groq.reset(token)
    except Exception:
        log.warning("reset_request_groq failed", exc_info=True)


def get_groq() -> GroqClient:
    cur = _request_groq.get()
    if cur is not None:
        return cur
    global _singleton
    if _singleton is None:
        _singleton = GroqClient()
    return _singleton
