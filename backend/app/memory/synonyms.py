"""Entity synonyms — backing store for EntityResolver."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import settings

log = logging.getLogger("agentic_ai.memory.synonyms")


_DEFAULT: dict[str, list[str]] = {
    "swiggy":      ["swiggy ltd", "bundl technologies", "swiggy app"],
    "zomato":      ["zomato ltd", "zomato app"],
    "groceries":   ["grocery", "kirana", "fmcg"],
    "electronics": ["consumer electronics", "appliances"],
    "fashion":     ["apparel", "clothing", "lifestyle"],
}


def _path() -> Path:
    return Path(settings.synonyms_path)


def load_synonyms() -> dict[str, list[str]]:
    p = _path()
    if not p.exists():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_DEFAULT, indent=2), encoding="utf-8")
        except Exception:
            log.warning("could not write default synonyms file", exc_info=True)
        return dict(_DEFAULT)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.warning("synonyms.json unreadable; using defaults", exc_info=True)
        return dict(_DEFAULT)


def resolve_entities(question: str) -> list[dict]:
    """Return canonical entities matched in the question.

    Returns a list of dicts: {canonical, matched_aliases}.
    """
    if not question:
        return []
    q = question.lower()
    syns = load_synonyms()
    out: list[dict] = []
    for canonical, aliases in syns.items():
        hits = [a for a in [canonical, *aliases] if a.lower() in q]
        if hits:
            out.append({"canonical": canonical, "matched_aliases": hits})
    return out
