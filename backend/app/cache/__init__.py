"""Cache + ephemeral-state adapters.

Public surface re-exports the Redis-aware variants of the three stores
(CacheStore, ConversationStore, rate-limit counter). Each adapter:

  - returns an in-memory or JSON-file impl when `REDIS_URL` is unset
    (today's default)
  - swaps to Redis when `REDIS_URL` is set
  - is exposed via a single `wire_redis_if_configured()` startup hook
    that the FastAPI app calls once

The fallback path is what the existing 146 tests run against. The Redis
path is exercised separately when REDIS_URL is set.
"""
from app.cache.redis_client import (
    RedisUnavailableError,
    get_redis,
    redis_close,
    redis_status,
)
from app.cache.redis_store import (
    RedisCacheStore,
    RedisConversationStore,
)
from app.cache.upstash_rest import (
    UpstashCacheStore,
    UpstashConversationStore,
    UpstashRestUnavailableError,
    get_upstash,
    upstash_close,
    upstash_status,
)
from app.cache.bootstrap import wire_redis_if_configured

__all__ = [
    "RedisCacheStore",
    "RedisConversationStore",
    "RedisUnavailableError",
    "UpstashCacheStore",
    "UpstashConversationStore",
    "UpstashRestUnavailableError",
    "get_redis",
    "get_upstash",
    "redis_close",
    "redis_status",
    "upstash_close",
    "upstash_status",
    "wire_redis_if_configured",
]
