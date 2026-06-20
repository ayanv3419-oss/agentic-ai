"""Regression: insightFmt must NOT fabricate numbers when there is no data.

Bug (2026-06-21): a tenant who had uploaded NOTHING asked "what is the sales
trend" and the model invented a full report (₹1.85M revenue, ₹520k peak week).
Root cause: insightFmt had no precondition on having retrieved any rows, the
loop let the model jump straight to it, and the validator only inspects SQL
rows — never the final prose — so fabricated figures sailed through.

Fix: a code-level grounding guard in insightFmt.run() — for any non-CHAT
question that never pulled a row, return a deterministic "no data yet" refusal
instead of calling the LLM (so hallucination is structurally impossible).

Run:
    cd backend && python -m pytest tests/test_no_data_refusal.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.coordinator.state import ToolResult, TurnState
from app.coordinator.tools.base import ToolContext
from app.coordinator.sub_agents.insight_fmt import (
    InsightFmtAgent,
    _has_grounding,
    _needs_data,
    _no_data_answer,
)


# --- pure helpers ------------------------------------------------------------

def test_needs_data_true_for_data_routes_and_unknown():
    for route in ("TREND", "KPI", "RANKING", "RCA", "ANALYTICS", "INVENTORY"):
        assert _needs_data(TurnState(question="q", route=route)) is True
    # Missing route (model skipped RouteClass) is treated as a data question.
    assert _needs_data(TurnState(question="q")) is True


def test_needs_data_false_for_chat():
    assert _needs_data(TurnState(question="hi", route="CHAT")) is False
    assert _needs_data(TurnState(question="hi", route="chat")) is False  # case-insensitive


def test_has_grounding_false_when_nothing_ran():
    assert _has_grounding(TurnState(question="q", route="TREND")) is False


def test_has_grounding_true_with_rows():
    st = TurnState(question="q", route="TREND", rows=[{"x": 1}], row_count=1)
    assert _has_grounding(st) is True


def test_has_grounding_true_when_sqlexecutor_ran_even_with_zero_rows():
    # Legitimate "no data for that period" path: a query DID run, just matched
    # nothing. insightFmt should answer normally, not show the upload refusal.
    st = TurnState(
        question="sales in 1999",
        route="TREND",
        tool_results=[ToolResult(call_id="c1", name="SqlExecutor", status="ok")],
    )
    assert _has_grounding(st) is True


def test_no_data_answer_english_and_roman_hindi():
    en = _no_data_answer("en")
    assert "Upload Data" in en and "don't have any data" in en
    hi = _no_data_answer("hi")
    assert "Upload Data" in hi and "data nahi mila" in hi
    # Roman Hindi must stay in the Latin alphabet (no Devanagari).
    assert all(ord(ch) < 0x0900 or ord(ch) > 0x097F for ch in hi)


# --- the guard in run() ------------------------------------------------------

class _BoomLLM:
    """Any call means the guard failed to short-circuit — fail loudly."""

    async def complete(self, *a, **k):  # pragma: no cover - must not be hit
        raise AssertionError("insightFmt called the LLM despite having no data")


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content

    async def complete(self, *a, **k):
        return SimpleNamespace(error=None, content=self._content)


@pytest.mark.asyncio
async def test_run_refuses_data_question_with_no_data_without_calling_llm():
    state = TurnState(question="what is the sales trend", route="TREND")
    out = await InsightFmtAgent().run({}, ToolContext(state=state, llm=_BoomLLM()))
    assert out.ok is True
    assert out.state_updates["final_answer"] == _no_data_answer("en")
    assert out.output.get("grounded") is False
    # The fabricated figures from the bug must never appear.
    assert "1.85" not in out.state_updates["final_answer"]


@pytest.mark.asyncio
async def test_run_refusal_honours_answer_language():
    state = TurnState(
        question="pichle 30 din ka sales trend",
        route="TREND",
        answer_language="hi",
    )
    out = await InsightFmtAgent().run({}, ToolContext(state=state, llm=_BoomLLM()))
    assert out.ok is True
    assert out.state_updates["final_answer"] == _no_data_answer("hi")


@pytest.mark.asyncio
async def test_run_proceeds_to_llm_when_grounded():
    state = TurnState(
        question="what is the sales trend",
        route="TREND",
        rows=[{"month": "2026-05", "revenue": 100}],
        row_count=1,
    )
    out = await InsightFmtAgent().run(
        {}, ToolContext(state=state, llm=_FakeLLM("REAL ANALYST ANSWER"))
    )
    assert out.ok is True
    assert out.state_updates["final_answer"] == "REAL ANALYST ANSWER"


@pytest.mark.asyncio
async def test_run_allows_chat_without_data():
    # A greeting needs no data — the guard must not block it.
    state = TurnState(question="hello", route="CHAT")
    out = await InsightFmtAgent().run(
        {}, ToolContext(state=state, llm=_FakeLLM("Hi! How can I help?"))
    )
    assert out.ok is True
    assert out.state_updates["final_answer"] == "Hi! How can I help?"
