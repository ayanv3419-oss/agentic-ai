"""Redis-backed adapters for the existing CacheStore + ConversationStore
protocols.

These are drop-in replacements for the in-memory / JSON-file impls in
`infrastructure.py`. The startup hook in `cache.bootstrap` swaps them in
when REDIS_URL is set; otherwise the existing in-memory path keeps
running. No call-site changes anywhere.

Key namespacing:
  - `agentic:cache:<sha256>`           — response cache
  - `agentic:convo:<user_id>:<convo>`  — conversation memory
  - `agentic:ratelimit:<user_id>`      — rate-limit sliding window

TTLs are conservative (24h for cache, 7d for conversation memory) so a
forgotten Redis instance doesn't grow forever. Tune via env later if
needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.infrastructure import CacheStore, ConversationStore


_log = logging.getLogger("agentic_ai.cache.redis_store")


# Prefixes — keep them short (Redis charges memory by key length).
_CACHE_PREFIX = "agentic:cache:"
_CONVO_PREFIX = "agentic:convo:"

# Default TTLs in seconds.
_CACHE_TTL = 24 * 60 * 60         # 24 hours — short enough that stale
                                  # entries age out, long enough that a
                                  # working day's questions get cache hits.
_CONVO_TTL = 7 * 24 * 60 * 60     # 7 days — preserves continuity across
                                  # a typical work week.


def _run_async(coro: Any) -> Any:
    """Synchronous wrapper for the existing CacheStore protocol. The
    legacy CacheStore is sync (because the JSON-file impl is sync). We
    bridge to async-redis via `asyncio.run` when not in an event loop,
    or `loop.run_until_complete` when inside one but on a different
    task. Production callers should use the async store directly when
    they're in an async context — this bridge is for the legacy
    sync-call sites."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already in a running loop — schedule + wait.
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=10.0)


class RedisCacheStore(CacheStore):
    """Drop-in for the JsonFileCacheStore. Same 5 methods, same protocol.
    Errors fall back silently to "miss" / "no-op put" so a Redis blip
    never breaks an analytics turn."""

    def __init__(self, key_prefix: str = _CACHE_PREFIX, ttl_seconds: int = _CACHE_TTL):
        self._prefix = key_prefix
        self._ttl = max(1, int(ttl_seconds))

    def kind(self) -> str:
        return "redis"

    async def _aget(self, key: str) -> dict[str, Any] | None:
        from app.cache.redis_client import RedisUnavailableError, get_redis
        try:
            r = await get_redis()
            raw = await r.get(self._prefix + key)
        except RedisUnavailableError:
            return None
        except Exception:
            _log.exception("redis cache get failed")
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def _aput(self, key: str, value: dict[str, Any]) -> None:
        from app.cache.redis_client import RedisUnavailableError, get_redis
        try:
            r = await get_redis()
            await r.set(self._prefix + key, json.dumps(value, default=str), ex=self._ttl)
        except RedisUnavailableError:
            return
        except Exception:
            _log.exception("redis cache put failed")

    async def _ainvalidate_all(self) -> int:
        """Drop EVERY key under our prefix. Uses SCAN so we don't block
        Redis; a flushdb would be wrong (other apps share Redis)."""
        from app.cache.redis_client import RedisUnavailableError, get_redis
        try:
            r = await get_redis()
        except RedisUnavailableError:
            return 0
        n = 0
        try:
            async for key in r.scan_iter(match=self._prefix + "*", count=200):
                await r.delete(key)
                n += 1
        except Exception:
            _log.exception("redis cache invalidate failed")
        return n

    async def _asize(self) -> int:
        from app.cache.redis_client import RedisUnavailableError, get_redis
        try:
            r = await get_redis()
        except RedisUnavailableError:
            return 0
        n = 0
        try:
            async for _ in r.scan_iter(match=self._prefix + "*", count=200):
                n += 1
        except Exception:
            _log.exception("redis cache size failed")
        return n

    # ---- Sync wrappers for the legacy CacheStore protocol -----------

    def get(self, key: str) -> dict[str, Any] | None:
        return _run_async(self._aget(key))

    def put(self, key: str, value: dict[str, Any]) -> None:
        _run_async(self._aput(key, value))

    def invalidate_all(self) -> int:
        return _run_async(self._ainvalidate_all())

    def size(self) -> int:
        return _run_async(self._asize())


class RedisConversationStore(ConversationStore):
    """Drop-in for InMemoryConversationStore.

    Stored value is a flat string (the last query kind) — no JSON
    serialization needed for the small payload today. Future expansion
    to a hash with `last_kind`, `last_intent`, `last_entity` etc. is
    just `r.hset(key, mapping={...})`."""

    def __init__(self, key_prefix: str = _CONVO_PREFIX, ttl_seconds: int = _CONVO_TTL):
        self._prefix = key_prefix
        self._ttl = max(1, int(ttl_seconds))

    def kind(self) -> str:
        return "redis"

    def _key(self, user_key: str) -> str:
        return self._prefix + user_key

    async def _aget(self, user_key: str) -> str | None:
        from app.cache.redis_client import RedisUnavailableError, get_redis
        try:
            r = await get_redis()
            return await r.get(self._key(user_key))
        except RedisUnavailableError:
            return None
        except Exception:
            _log.exception("redis convo get failed")
            return None

    async def _aset(self, user_key: str, kind: str) -> None:
        from app.cache.redis_client import RedisUnavailableError, get_redis
        try:
            r = await get_redis()
            await r.set(self._key(user_key), kind, ex=self._ttl)
        except RedisUnavailableError:
            return
        except Exception:
            _log.exception("redis convo set failed")

    async def _areset(self, user_key: str | None) -> None:
        from app.cache.redis_client import RedisUnavailableError, get_redis
        try:
            r = await get_redis()
        except RedisUnavailableError:
            return
        try:
            if user_key is None:
                async for k in r.scan_iter(match=self._prefix + "*", count=200):
                    await r.delete(k)
            else:
                await r.delete(self._key(user_key))
        except Exception:
            _log.exception("redis convo reset failed")

    # Sync wrappers ----------------------------------------------------

    def get_last_kind(self, user_key: str) -> str | None:
        return _run_async(self._aget(user_key))

    def set_last_kind(self, user_key: str, kind: str) -> None:
        _run_async(self._aset(user_key, kind))

    def reset(self, user_key: str | None = None) -> None:
        _run_async(self._areset(user_key))
