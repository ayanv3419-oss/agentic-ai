"""Async Redis client singleton.

Lazy-initialized when REDIS_URL is set. The same `get_redis()` function
is consumed by every Redis-backed store (cache, conversation, rate
limiter) so we share a single connection pool.

Raises `RedisUnavailableError` cleanly when REDIS_URL is unset OR the
connection fails — call sites catch this and fall back to in-memory.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from app.infrastructure import settings


_log = logging.getLogger("agentic_ai.cache.redis")

_CLIENT: Optional[aioredis.Redis] = None
_CLIENT_LOCK = asyncio.Lock()
_CONNECT_FAILED: bool = False


class RedisUnavailableError(RuntimeError):
    """Either REDIS_URL is unset OR the client failed to connect.
    Catch in stores that have an in-memory fallback."""


async def get_redis() -> aioredis.Redis:
    """Lazy connect. First call creates a connection pool; subsequent
    calls reuse it. After a single connect failure we set a circuit
    breaker — operators must `redis_close()` to retry."""
    global _CLIENT, _CONNECT_FAILED
    url = (settings.redis_url or "").strip()
    if not url:
        raise RedisUnavailableError("REDIS_URL is not set; in-memory fallback active")
    if _CONNECT_FAILED:
        raise RedisUnavailableError("Redis connect failed earlier; redis_close() to retry")
    if _CLIENT is not None:
        return _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            client = aioredis.from_url(
                url,
                encoding="utf-8",
                decode_responses=True,
                # Reasonable defaults for a request-handler use case.
                socket_timeout=5.0,
                socket_connect_timeout=2.0,
                health_check_interval=30,
            )
            # Surface a connection error eagerly so the caller can fall
            # back instead of failing on first put.
            await client.ping()
        except Exception as e:
            _CONNECT_FAILED = True
            _log.exception("redis connect failed: %s", e)
            raise RedisUnavailableError(f"redis connect failed: {e}")
        _CLIENT = client
        _log.info("redis client connected")
        return _CLIENT


async def redis_close() -> None:
    """Close the client + reset circuit breaker. Used on shutdown +
    by tests."""
    global _CLIENT, _CONNECT_FAILED
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                await _CLIENT.aclose()
            except Exception:
                _log.exception("redis close failed")
        _CLIENT = None
        _CONNECT_FAILED = False


def redis_status() -> dict[str, Any]:
    """Cheap status. Doesn't open a connection."""
    return {
        "configured":   bool((settings.redis_url or "").strip()),
        "client_open":  _CLIENT is not None,
        "init_failed":  _CONNECT_FAILED,
    }
