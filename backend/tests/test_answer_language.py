"""Unit tests for multi-language answers (English / Roman Hindi).

"hi" means ROMANISED Hindi — Hindi words in the Latin alphabet ("Hinglish"),
never Devanagari. Covers the deterministic seams (no LLM needed):
  * cache_key_for() includes the answer language, so the same question asked
    in English vs Roman Hindi never collides on one cached answer.
  * insight_fmt._language_directive() emits a Roman-Hindi instruction for "hi"
    and stays empty (English default) otherwise.
  * insight_fmt._build_user() reinforces the language at the end of the prompt.

Run:
    cd backend && python -m pytest tests/test_answer_language.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.coordinator.state import TurnState
from app.coordinator.sub_agents.insight_fmt import (
    _build_user,
    _language_directive,
)
from app.infrastructure import cache_key_for


# --- cache key is language-scoped -------------------------------------------

def test_cache_key_differs_by_language():
    q = "total sales last month"
    en = cache_key_for(q, tenant_id="t1", language="en")
    hi = cache_key_for(q, tenant_id="t1", language="hi")
    assert en != hi, "English and Roman Hindi must get distinct cache keys"


def test_cache_key_stable_for_same_language():
    q = "top 5 products"
    assert cache_key_for(q, tenant_id="t1", language="hi") == cache_key_for(
        q, tenant_id="t1", language="hi"
    )


def test_cache_key_default_is_english():
    q = "revenue by month"
    assert cache_key_for(q, tenant_id="t1") == cache_key_for(
        q, tenant_id="t1", language="en"
    )


# --- language directive ------------------------------------------------------

def test_directive_empty_for_english_and_unknown():
    assert _language_directive("en") == ""
    assert _language_directive("") == ""
    assert _language_directive("gu") == ""  # Gujarati removed → no directive
    assert _language_directive("fr") == ""  # unsupported → English


def test_directive_asks_for_roman_not_devanagari():
    d = _language_directive("hi")
    assert "HINDI" in d.upper()
    assert "Roman" in d
    assert "Devanagari" in d  # explicitly tells the model NOT to use it
    # The directive itself must stay in ASCII/Latin (it must not contain
    # Devanagari example characters that could confuse a small model).
    assert all(ord(ch) < 0x0900 or ord(ch) > 0x097F for ch in d)


def test_directive_preserves_hard_data_instruction():
    d = _language_directive("hi")
    assert "₹" in d and "number" in d.lower() and "markdown" in d.lower()


# --- build_user reinforces the language at the prompt tail -------------------

def _ctx(language: str):
    state = TurnState(question="how much did I sell?", answer_language=language)
    return SimpleNamespace(state=state)


def test_build_user_reinforces_roman_hindi():
    out = _build_user(_ctx("hi"), {})
    assert out.rstrip().endswith("entirely in Roman Hindi (Hindi in the Latin alphabet).")


def test_build_user_plain_for_english():
    out = _build_user(_ctx("en"), {})
    assert out.rstrip().endswith("Write the final answer now.")
