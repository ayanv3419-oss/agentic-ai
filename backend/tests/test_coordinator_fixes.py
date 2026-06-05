"""Regression tests for the coordinator quality-pass fixes.

Covers the three bugs whose fixes are subtle enough to be easy to
regress: #1 protocol violation, #3 LIMIT wrapping, #6 state corruption
on tool failure. Other fixes are validated by the live smoke run.

Run:
    cd backend && python -m pytest tests/test_coordinator_fixes.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.coordinator.dispatcher import dispatch
from app.coordinator.loop import run_loop
from app.coordinator.state import ToolCall, TurnState
from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.registry import ToolRegistry
from app.coordinator.tools.sql_executor import _enforce_limit


# ---------------------------------------------------------------------------
# #3 _enforce_limit always wraps as `SELECT * FROM (<sql>) LIMIT N`
# ---------------------------------------------------------------------------


class TestEnforceLimit:
    def test_simple_select_gets_outer_limit(self):
        out = _enforce_limit("SELECT * FROM sale", 500)
        assert out == "SELECT * FROM (\nSELECT * FROM sale\n) AS _capped LIMIT 500"

    def test_inner_cte_limit_does_not_skip_outer_cap(self):
        # The pre-fix regex matched the inner LIMIT and returned the SQL
        # unwrapped — leaving the outer SELECT unbounded.
        sql = (
            "WITH t AS (SELECT product, SUM(amt) s FROM sale GROUP BY product "
            "ORDER BY s DESC LIMIT 10) SELECT * FROM sale s JOIN t ON s.product=t.product"
        )
        out = _enforce_limit(sql, 500)
        assert out.startswith("SELECT * FROM (")
        assert out.endswith(") AS _capped LIMIT 500")

    def test_subquery_limit_does_not_skip_outer_cap(self):
        sql = "SELECT * FROM (SELECT * FROM sale LIMIT 5) sub"
        out = _enforce_limit(sql, 500)
        assert out == "SELECT * FROM (\nSELECT * FROM (SELECT * FROM sale LIMIT 5) sub\n) AS _capped LIMIT 500"

    def test_trailing_semicolon_stripped(self):
        out = _enforce_limit("SELECT 1;", 100)
        assert ";" not in out.rstrip("0123456789 LIMIT")
        assert out.endswith(" LIMIT 100")

    def test_inner_order_by_preserved_under_sqlite(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE p(n INT, v INT)")
        db.executemany("INSERT INTO p VALUES (?, ?)", [(1, 10), (2, 20), (3, 5)])
        wrapped = _enforce_limit("SELECT * FROM p ORDER BY v DESC", 2)
        rows = db.execute(wrapped).fetchall()
        assert rows == [(2, 20), (1, 10)]


# ---------------------------------------------------------------------------
# #6 dispatcher does NOT apply state_updates when the tool failed
# ---------------------------------------------------------------------------


class _FailingButUpdatingTool(Tool):
    name = "FailingTool"
    description = "Always fails but tries to leave a sql_draft behind."
    parameters_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, args, ctx):
        return ToolOutcome(
            ok=False,
            error="planned failure",
            state_updates={"sql_draft": "SELECT * FROM injected"},
        )


class _SucceedingUpdatingTool(Tool):
    name = "SucceedingTool"
    description = "Succeeds and writes sql_draft."
    parameters_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, args, ctx):
        return ToolOutcome(
            ok=True,
            output={"k": "v"},
            state_updates={"sql_draft": "SELECT 1"},
        )


@pytest.mark.asyncio
async def test_failed_tool_does_not_apply_state_updates():
    # Pre-fix: dispatcher applied state_updates unconditionally, so this
    # failing tool would leak `sql_draft` into state and the next tool
    # (e.g. SqlDryRun) would consume it as the SQL to validate.
    reg = ToolRegistry()
    reg.register(_FailingButUpdatingTool())
    # restriction_guard whitelist would reject "FailingTool" - skip pre-hooks
    # by going through registry.execute directly via dispatch but allow the
    # name. We patch the allowed set for the duration of the test.
    import app.coordinator.hooks as hooks_mod
    original_allowed = hooks_mod.restriction_guard.__globals__["__builtins__"]
    # Easier: temporarily add the test tool to the whitelist by monkeypatching
    # the function.
    real_restriction_guard = hooks_mod.restriction_guard

    def permissive(state, call):
        return hooks_mod.HookOutcome()

    hooks_mod.PRE_HOOKS[0] = permissive
    try:
        state = TurnState(question="x", sql_draft="ORIGINAL")
        call = ToolCall(call_id="c1", name="FailingTool", arguments={})
        new_state, result = await dispatch(state, call, registry=reg, llm=None, emit=None)
        assert result.status == "error"
        assert new_state.sql_draft == "ORIGINAL"  # NOT overwritten
    finally:
        hooks_mod.PRE_HOOKS[0] = real_restriction_guard


@pytest.mark.asyncio
async def test_succeeding_tool_does_apply_state_updates():
    reg = ToolRegistry()
    reg.register(_SucceedingUpdatingTool())
    import app.coordinator.hooks as hooks_mod
    real_restriction_guard = hooks_mod.restriction_guard

    def permissive(state, call):
        return hooks_mod.HookOutcome()

    hooks_mod.PRE_HOOKS[0] = permissive
    try:
        state = TurnState(question="x", sql_draft="ORIGINAL")
        call = ToolCall(call_id="c2", name="SucceedingTool", arguments={})
        new_state, result = await dispatch(state, call, registry=reg, llm=None, emit=None)
        assert result.status == "ok"
        assert new_state.sql_draft == "SELECT 1"  # overwritten on success
    finally:
        hooks_mod.PRE_HOOKS[0] = real_restriction_guard


# ---------------------------------------------------------------------------
# #1 Protocol: every tool_call in a batch gets a matching tool message,
# even when insightFmt is NOT the last item in the batch.
# ---------------------------------------------------------------------------


class _RecordingTool(Tool):
    """Echoes its name; tracks every call. Used to stand in for any
    coordinator tool during loop testing."""

    parameters_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, name: str, *, sets_final: bool = False):
        self.name = name
        self.description = f"recording stub for {name}"
        self.calls: list[dict] = []
        self._sets_final = sets_final

    async def run(self, args, ctx):
        self.calls.append(args or {})
        updates: dict = {}
        if self._sets_final:
            updates["final_answer"] = "synthetic answer"
        return ToolOutcome(ok=True, output={"name": self.name}, state_updates=updates)


class _ScriptedLLM:
    """Returns canned LLMToolResponse objects in order. After the script
    is exhausted, returns an empty tool_calls response so the loop exits."""

    def __init__(self, script):
        self._script = list(script)
        self._idx = 0
        self.captured_requests: list[list[dict]] = []

    async def complete_with_tools(self, messages, *, tools, temperature, max_tokens):
        from app.coordinator.llm import LLMToolResponse
        self.captured_requests.append([dict(m) for m in messages])
        if self._idx < len(self._script):
            resp = self._script[self._idx]
            self._idx += 1
            return resp
        return LLMToolResponse(content="done", tool_calls=[], tokens_in=1, tokens_out=1)


@pytest.mark.asyncio
async def test_protocol_satisfied_when_insightfmt_is_mid_batch():
    """Pre-fix: when the LLM emitted [Schema, insightFmt, SqlExecutor] in
    one response, the loop dispatched Schema + insightFmt then broke,
    leaving SqlExecutor without a matching tool message. Next LLM round
    saw a malformed message list."""
    from app.coordinator.llm import LLMToolCall, LLMToolResponse

    schema_tool = _RecordingTool("Schema")
    insight_tool = _RecordingTool("insightFmt", sets_final=True)
    dryrun_tool = _RecordingTool("SqlDryRun")
    sql_tool = _RecordingTool("SqlExecutor")

    reg = ToolRegistry()
    for t in (schema_tool, insight_tool, dryrun_tool, sql_tool):
        reg.register(t)

    # Allow the test tool names through restriction_guard.
    import app.coordinator.hooks as hooks_mod
    real = hooks_mod.restriction_guard
    hooks_mod.PRE_HOOKS[0] = lambda s, c: hooks_mod.HookOutcome()

    try:
        scripted = LLMToolResponse(
            content="ok",
            tool_calls=[
                LLMToolCall(id="a", name="Schema", arguments={}),
                LLMToolCall(id="b", name="insightFmt", arguments={}),
                # SqlDryRun before SqlExecutor — the sql_dryrun_guard
                # pre-hook now blocks SqlExecutor without a passing dry-run.
                LLMToolCall(id="c", name="SqlDryRun", arguments={}),
                LLMToolCall(id="d", name="SqlExecutor", arguments={"sql": "SELECT 1"}),
            ],
            tokens_in=10,
            tokens_out=5,
        )
        llm = _ScriptedLLM([scripted])

        state = TurnState(question="x")
        events: list = []

        async def emit(name, payload):
            events.append((name, payload))

        new_state = await run_loop(state, llm=llm, registry=reg, emit=emit)

        # All three tools were dispatched (no early break dropping the
        # trailing SqlExecutor).
        assert len(schema_tool.calls) == 1
        assert len(insight_tool.calls) == 1
        assert len(sql_tool.calls) == 1

        # The final answer set by insightFmt survived and the loop
        # exited cleanly on the next iteration via the
        # `state.finished and state.final_answer` check.
        assert new_state.final_answer == "synthetic answer"
        assert new_state.finished is True
    finally:
        hooks_mod.PRE_HOOKS[0] = real
