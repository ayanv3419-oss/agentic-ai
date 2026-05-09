"""Single startup hook that swaps in Redis-backed stores when a Redis
backend is configured. Called once from FastAPI startup; no-op when no
backend is configured, so dev/test paths keep using in-memory stores.

Backend priority (first reachable wins):
  1. Upstash REST (UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN) —
     HTTPS-based, works behind firewalls + on serverless.
  2. Wire-protocol Redis (REDIS_URL) — standard rediss:// or redis://
     URLs, lower latency.
  3. In-memory fallback — process-local; loses state on restart.

Each backend is probed BEFORE swap so a misconfigured DSN doesn't
install stores that immediately fail. Probe failures are logged + the
system stays on the next backend.
"""
from __future__ import annotations

import logging

from app.infrastructure import (
    set_cache_store,
    set_conversation_store,
    settings,
)


_log = logging.getLogger("agentic_ai.cache.bootstrap")


async def _try_upstash() -> bool:
    """Probe Upstash REST. Returns True iff swap succeeded."""
    url = (settings.upstash_redis_rest_url or "").strip()
    token = (settings.upstash_redis_rest_token or "").strip()
    if not url or not token:
        return False
    from app.cache.upstash_rest import (
        UpstashCacheStore, UpstashConversationStore,
        UpstashRestUnavailableError, get_upstash,
    )
    try:
        client = await get_upstash()
        await client.ping()
    except UpstashRestUnavailableError as e:
        _log.warning(
            "upstash REST configured but probe failed; trying next backend: %s", e,
        )
        return False
    except Exception as e:
        _log.warning(
            "upstash REST probe unexpected error: %s: %s", type(e).__name__, e,
        )
        return False
    set_cache_store(UpstashCacheStore())
    set_conversation_store(UpstashConversationStore())
    _log.info("upstash REST stores wired: cache + conversation")
    return True


async def _try_wire_redis() -> bool:
    """Probe wire-protocol Redis. Returns True iff swap succeeded."""
    url = (settings.redis_url or "").strip()
    if not url:
        return False
    from app.cache.redis_client import RedisUnavailableError, get_redis
    try:
        await get_redis()
    except RedisUnavailableError as e:
        _log.warning(
            "REDIS_URL set but redis unreachable; trying next backend: %s", e,
        )
        return False
    except Exception as e:
        _log.warning("redis-py probe unexpected: %s: %s", type(e).__name__, e)
        return False
    from app.cache.redis_store import RedisCacheStore, RedisConversationStore
    set_cache_store(RedisCacheStore())
    set_conversation_store(RedisConversationStore())
    _log.info("redis-py stores wired: cache + conversation")
    return True


async def wire_redis_if_configured() -> dict[str, str]:
    """Probe each Redis backend in priority order; swap the first that
    succeeds. Returns a dict describing the active backends."""
    if await _try_upstash():
        return {"cache": "upstash_rest", "conversation": "upstash_rest"}
    if await _try_wire_redis():
        return {"cache": "redis", "conversation": "redis"}
    return {"cache": "unchanged", "conversation": "unchanged"}
