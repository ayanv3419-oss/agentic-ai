"""Response cache — `data/response_store.json`.

Structure: dict keyed by cache_key (sha256 of the normalized question).
Persistence is atomic (tmp + replace) and serialized by a threading.Lock.

Caching rules (from architecture):
  * Cache HIT  → return stored answer; do NOT call the LLM or any tools.
  * Cache MISS → tools run, ResponseStored writes the entry on success.
  * /upload    → invalidates the entire cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.config import settings

log = logging.getLogger("agentic_ai.cache")

_LOCK = Lock()


def _path() -> Path:
    return Path(settings.response_store_path)


def _load() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        log.warning("response_store load failed; treating as empty", exc_info=True)
        return {}


def _save(data: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(p)


def cache_key_for(question: str) -> str:
    """sha256 over the lowercased / stripped question. Deterministic + global."""
    norm = (question or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def get_cached(key: str) -> dict[str, Any] | None:
    with _LOCK:
        data = _load()
        entry = data.get(key)
    return entry if isinstance(entry, dict) else None


def put_cached(key: str, record: dict[str, Any]) -> None:
    """Store / replace a cache entry. `record` must be the full ResponseStored
    payload (query, sub_agent, sql, rows, final_answer, chart, ...)."""
    record = dict(record)
    record.setdefault("stored_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with _LOCK:
        data = _load()
        data[key] = record
        _save(data)


def invalidate_all() -> int:
    """Clear the cache. Returns count of removed entries."""
    with _LOCK:
        data = _load()
        n = len(data)
        _save({})
    log.info("cache invalidated (%d entries removed)", n)
    return n


def cache_size() -> int:
    with _LOCK:
        data = _load()
    return len(data)
