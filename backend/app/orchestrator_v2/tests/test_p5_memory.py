"""
P5 memory + audit smoke test.

  * Context trimmer keeps recent + system message; drops oldest first.
  * SqliteConversationMemory.record/recent round-trip.
  * SqliteExecutionMemoryWriter.record persists snapshot; subsequent
    SELECT returns the same turn_id.
"""

from __future__ import annotations

import asyncio
import sys

from app.orchestrator_v2.memory.context_trimmer import estimate_tokens, trim_messages
from app.orchestrator_v2.memory.conversation_memory import SqliteConversationMemory
from app.orchestrator_v2.memory.execution_memory import SqliteExecutionMemoryWriter


def _heading(t: str) -> None:
    print("=" * 70); print(t); print("=" * 70)


def test_estimate_tokens_basic() -> None:
    _heading("test_estimate_tokens_basic")
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 100) == 25
    print(f"  [OK]estimate_tokens: empty=0, 4 chars=1 tok, 100 chars=25 tok")


def test_trim_messages_drops_oldest() -> None:
    _heading("test_trim_messages_drops_oldest")
    msgs = [
        {"role": "system", "content": "sys-instructions"},
        {"role": "user", "content": "Q1 " + "x" * 400},      # ~101 tokens
        {"role": "assistant", "content": "A1 " + "y" * 400}, # ~101 tokens
        {"role": "user", "content": "Q2 " + "x" * 400},      # ~101 tokens
        {"role": "assistant", "content": "A2 " + "y" * 400}, # ~101 tokens
        {"role": "user", "content": "Q3 latest"},            # ~3 tokens
    ]
    trimmed = trim_messages(msgs, budget_tokens=200)
    # System always preserved; latest user preserved; oldest U/A dropped.
    roles = [m["role"] for m in trimmed]
    assert roles[0] == "system"
    assert trimmed[-1]["content"].startswith("Q3"), trimmed[-1]["content"][:20]
    assert len(trimmed) < len(msgs), "trimmer dropped no messages"
    print(f"  [OK]trim_messages: {len(msgs)} -> {len(trimmed)} (system+latest preserved)")


async def test_conversation_memory_roundtrip() -> None:
    _heading("test_conversation_memory_roundtrip")
    mem = SqliteConversationMemory()
    conv_id = "p5-smoke-" + str(asyncio.get_event_loop().time())
    await mem.record(conv_id, question="Q1", answer="A1", metadata={"x": 1})
    await mem.record(conv_id, question="Q2", answer="A2", metadata={"x": 2})
    recent = await mem.recent(conv_id, limit=5)
    assert len(recent) == 2
    assert recent[0]["question"] == "Q1"
    assert recent[1]["question"] == "Q2"
    assert recent[1]["metadata"].get("x") == 2
    print(f"  [OK]conversation memory: 2 turns recorded + retrieved in order")


async def test_execution_memory_roundtrip() -> None:
    _heading("test_execution_memory_roundtrip")
    writer = SqliteExecutionMemoryWriter()
    snapshot = {
        "turn_id": "p5-turn-" + str(asyncio.get_event_loop().time()),
        "request_id": "p5-req",
        "conversation_id": "p5-conv",
        "question": "what is X?",
        "plan": {"plan_id": "p1", "steps": []},
        "executed_steps": [{"step_id": "s1"}],
        "validation_history": [],
        "critic_history": [],
        "confidence": {"overall": 0.9},
        "token_usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.0001},
        "outcome": "accepted",
        "error_message": None,
        "final_answer": "the answer",
        "duration_ms": 1234.5,
    }
    await writer.record(snapshot)

    # Read it back directly.
    from app.infrastructure import get_connection
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT outcome, final_answer FROM v2_execution_log WHERE turn_id = ?",
            (snapshot["turn_id"],),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "accepted"
    assert row[1] == "the answer"
    print(f"  [OK]execution_log row persisted; outcome={row[0]} answer={row[1]!r}")


def main() -> int:
    tests = [
        ("sync", test_estimate_tokens_basic),
        ("sync", test_trim_messages_drops_oldest),
        ("async", test_conversation_memory_roundtrip),
        ("async", test_execution_memory_roundtrip),
    ]
    failures: list[str] = []
    for kind, t in tests:
        try:
            if kind == "sync":
                t()
            else:
                asyncio.run(t())
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
            print(f"  [FAIL]{t.__name__}: {e}")
        except Exception as e:
            import traceback; traceback.print_exc()
            failures.append(f"{t.__name__}: {type(e).__name__}: {e}")
        print()
    _heading("SUMMARY")
    if failures:
        for f in failures: print(f"    - {f}")
        return 1
    print(f"  all {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
