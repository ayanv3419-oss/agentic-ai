"""Upstash Redis REST adapter.

Why this exists alongside the wire-protocol `RedisCacheStore`:
  - Upstash exposes Redis commands over HTTPS at port 443. That works
    from serverless platforms / firewalled networks where outbound on
    6379 is blocked.
  - The REST API is one HTTP call per Redis command. Latency is higher
    (~10-30ms vs <1ms wire-protocol) but availability is much better.
  - Same `CacheStore` / `ConversationStore` protocols, so call sites
    don't change. The bootstrap chooses the best available backend at
    startup; everything below it is unaware.

API shape (Upstash REST):
    POST {url}/{command}/{arg1}/{arg2}/...
    Authorization: Bearer {token}

  The body for SET-with-value is the value itself in the URL or body —
  we use POST with body for any value that contains special characters
  (json blobs, etc.) so URL-encoding edge cases don't bite.

Error handling: every failed call returns "graceful miss" — get returns
None, put no-ops, invalidate_all returns 0. Upstash being down is a
cache miss, not a fatal error. The probe at startup is the only place
we actually care about the result.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from app.infrastructure import (
    CacheStore,
    ConversationStore,
    settings,
)


_log = logging.getLogger("agentic_ai.cache.upstash_rest")


_CACHE_PREFIX = "agentic:cache:"
_CONVO_PREFIX = "agentic:convo:"
_CACHE_TTL    = 24 * 60 * 60         # 24h
_CONVO_TTL    = 7 * 24 * 60 * 60     # 7d


def _mask_token(token: str | None) -> str:
    """Mask the bearer token for safe logging. Symmetric with
    integrations/supabase.py. Returns `<unset>` for empty input."""
    if not token:
        return "<unset>"
    if len(token) < 16:
        return "<short_token_masked>"
    return f"{token[:8]}...***...{token[-4:]}"


class UpstashRestUnavailableError(RuntimeError):
    """Raised when Upstash REST is unconfigured OR unreachable.
    Bootstrap catches this + falls through to the next backend."""


class _UpstashRestClient:
    """Thin async wrapper over the Upstash REST HTTP API.

    All commands serialize to a path-like URL. Responses are JSON of
    the form `{"result": <value>}` on success, or `{"error": "..."}`
    on failure. We surface failures by raising — the cache stores
    catch + treat as miss.
    """

    def __init__(self, url: str, token: str) -> None:
        if not url or not token:
            raise ValueError("Upstash REST requires url + token")
        self._url = url.rstrip("/")
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=self._url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent":    "agentic-ai-backend/1.0",
            },
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=2.0),
        )

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception:
            _log.exception("upstash REST close failed")

    async def _call(self, *parts: str, body: Any = None) -> Any:
        """Issue a Redis command via REST. POST with optional JSON body
        (for SET-with-special-value); GET otherwise. The first path
        element is the Redis command name (set/get/del/...)."""
        path = "/" + "/".join(parts)
        try:
            if body is not None:
                r = await self._http.post(path, json=body)
            else:
                r = await self._http.get(path)
        except Exception as e:
            raise UpstashRestUnavailableError(
                f"upstash REST network error: {type(e).__name__}: {e}"
            )
        if r.status_code != 200:
            raise UpstashRestUnavailableError(
                f"upstash REST HTTP {r.status_code}"
            )
        try:
            payload = r.json()
        except Exception:
            raise UpstashRestUnavailableError("upstash REST: response not JSON")
        if "error" in payload:
            raise UpstashRestUnavailableError(
                f"upstash REST: {payload['error']}"
            )
        return payload.get("result")

    # ---- Probe ---------------------------------------------------------

    async def ping(self) -> str:
        """Cheap reachability check — Upstash supports the PING command
        over REST and returns "PONG" on success."""
        out = await self._call("ping")
        if isinstance(out, str) and out.upper() == "PONG":
            return out
        raise UpstashRestUnavailableError(
            f"upstash ping returned unexpected result: {out!r}"
        )

    # ---- Key/value ----------------------------------------------------

    async def get(self, key: str) -> Optional[str]:
        return await self._call("get", key)

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        # SET key value EX <ttl>. Upstash REST accepts the value via
        # POST body when sending a JSON request.
        await self._call("set", key, value, "ex", str(int(ttl_seconds)))

    async def delete(self, key: str) -> None:
        await self._call("del", key)

    # ---- Bulk delete (for invalidate_all) -----------------------------

    async def delete_by_prefix(self, prefix: str) -> int:
        """Walk all keys matching `<prefix>*` and DEL them. Uses SCAN
        so we don't block Upstash on a giant KEYS sweep. Returns the
        count of deleted keys."""
        deleted = 0
        cursor = "0"
        while True:
            # SCAN cursor MATCH pattern COUNT 200
            res = await self._call(
                "scan", cursor, "match", f"{prefix}*", "count", "200",
            )
            if not isinstance(res, list) or len(res) != 2:
                break
            cursor, keys = res[0], res[1]
            if isinstance(keys, list) and keys:
                # DEL accepts variadic args via the path.
                await self._call("del", *[str(k) for k in keys])
                deleted += len(keys)
            if cursor == "0":
                break
        return deleted

    async def count_by_prefix(self, prefix: str) -> int:
        """Count keys under prefix. SCAN-based — O(n) over the matching
        slice but doesn't block other clients."""
        total = 0
        cursor = "0"
        while True:
            res = await self._call(
                "scan", cursor, "match", f"{prefix}*", "count", "200",
            )
            if not isinstance(res, list) or len(res) != 2:
                break
            cursor, keys = res[0], res[1]
            if isinstance(keys, list):
                total += len(keys)
            if cursor == "0":
                break
        return total


# ---------------- Singleton ------------------------------------------------

_CLIENT: Optional[_UpstashRestClient] = None
_CLIENT_LOCK = asyncio.Lock()
_INIT_FAILED: bool = False


async def get_upstash() -> _UpstashRestClient:
    """Lazy singleton — raises UpstashRestUnavailableError when env
    vars are unset OR the previous init failed."""
    global _CLIENT, _INIT_FAILED
    url = (settings.upstash_redis_rest_url or "").strip()
    token = (settings.upstash_redis_rest_token or "").strip()
    if not url or not token:
        raise UpstashRestUnavailableError(
            "UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN must both be set"
        )
    if _INIT_FAILED:
        raise UpstashRestUnavailableError(
            "upstash REST init failed earlier; call upstash_close() to retry"
        )
    if _CLIENT is not None:
        return _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            client = _UpstashRestClient(url=url, token=token)
        except Exception as e:
            _INIT_FAILED = True
            _log.exception("upstash REST client construction failed: %s", e)
            raise UpstashRestUnavailableError(f"client construction failed: {e}")
        _CLIENT = client
        _log.info("upstash REST client initialized url=%s token=%s",
                  url, _mask_token(token))
        return _CLIENT


async def upstash_close() -> None:
    """Close the underlying httpx pool + reset the circuit breaker."""
    global _CLIENT, _INIT_FAILED
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            await _CLIENT.close()
        _CLIENT = None
        _INIT_FAILED = False


def upstash_status() -> dict[str, Any]:
    """Cheap inspection for /health. Token is MASKED."""
    url = (settings.upstash_redis_rest_url or "").strip()
    token = (settings.upstash_redis_rest_token or "").strip()
    return {
        "configured":     bool(url and token),
        "client_open":    _CLIENT is not None,
        "init_failed":    _INIT_FAILED,
        "url":            url or "<unset>",
        "token_fingerprint": _mask_token(token),
    }


# ---------------- Sync bridge for the legacy CacheStore protocol ----------

def _run_async(coro: Any) -> Any:
    """Bridge the sync CacheStore methods to async HTTP. See the
    explanation in `redis_store.py` — same pattern."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=10.0)


# ---------------- CacheStore impl -----------------------------------------

class UpstashCacheStore(CacheStore):
    """Drop-in for JsonFileCacheStore / RedisCacheStore. Uses Upstash REST.
    Errors degrade to "miss" so a transient Upstash blip never breaks
    an analytics turn."""

    def __init__(self, key_prefix: str = _CACHE_PREFIX, ttl_seconds: int = _CACHE_TTL):
        self._prefix = key_prefix
        self._ttl = max(1, int(ttl_seconds))

    def kind(self) -> str:
        return "upstash_rest"

    async def _aget(self, key: str) -> dict[str, Any] | None:
        try:
            client = await get_upstash()
            raw = await client.get(self._prefix + key)
        except UpstashRestUnavailableError:
            return None
        except Exception:
            _log.exception("upstash cache get failed")
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def _aput(self, key: str, value: dict[str, Any]) -> None:
        try:
            client = await get_upstash()
            await client.setex(
                self._prefix + key, self._ttl,
                json.dumps(value, default=str),
            )
        except UpstashRestUnavailableError:
            return
        except Exception:
            _log.exception("upstash cache put failed")

    async def _ainvalidate_all(self) -> int:
        try:
            client = await get_upstash()
            return await client.delete_by_prefix(self._prefix)
        except UpstashRestUnavailableError:
            return 0
        except Exception:
            _log.exception("upstash cache invalidate failed")
            return 0

    async def _asize(self) -> int:
        try:
            client = await get_upstash()
            return await client.count_by_prefix(self._prefix)
        except UpstashRestUnavailableError:
            return 0
        except Exception:
            _log.exception("upstash cache size failed")
            return 0

    # Sync wrappers for the existing CacheStore protocol.
    def get(self, key: str) -> dict[str, Any] | None:
        return _run_async(self._aget(key))

    def put(self, key: str, value: dict[str, Any]) -> None:
        _run_async(self._aput(key, value))

    def invalidate_all(self) -> int:
        return _run_async(self._ainvalidate_all())

    def size(self) -> int:
        return _run_async(self._asize())


# ---------------- ConversationStore impl ---------------------------------

class UpstashConversationStore(ConversationStore):
    """Drop-in for InMemoryConversationStore / RedisConversationStore.
    Stores the last query kind as a flat string per (user, conversation)
    composite key."""

    def __init__(self, key_prefix: str = _CONVO_PREFIX, ttl_seconds: int = _CONVO_TTL):
        self._prefix = key_prefix
        self._ttl = max(1, int(ttl_seconds))

    def kind(self) -> str:
        return "upstash_rest"

    def _key(self, user_key: str) -> str:
        return self._prefix + user_key

    async def _aget(self, user_key: str) -> str | None:
        try:
            client = await get_upstash()
            return await client.get(self._key(user_key))
        except UpstashRestUnavailableError:
            return None
        except Exception:
            _log.exception("upstash convo get failed")
            return None

    async def _aset(self, user_key: str, kind: str) -> None:
        try:
            client = await get_upstash()
            await client.setex(self._key(user_key), self._ttl, kind)
        except UpstashRestUnavailableError:
            return
        except Exception:
            _log.exception("upstash convo set failed")

    async def _areset(self, user_key: str | None) -> None:
        try:
            client = await get_upstash()
            if user_key is None:
                await client.delete_by_prefix(self._prefix)
            else:
                await client.delete(self._key(user_key))
        except UpstashRestUnavailableError:
            return
        except Exception:
            _log.exception("upstash convo reset failed")

    def get_last_kind(self, user_key: str) -> str | None:
        return _run_async(self._aget(user_key))

    def set_last_kind(self, user_key: str, kind: str) -> None:
        _run_async(self._aset(user_key, kind))

    def reset(self, user_key: str | None = None) -> None:
        _run_async(self._areset(user_key))
