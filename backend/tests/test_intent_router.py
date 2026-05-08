"""Regression tests for `classify_query_kind` — the analytics intent router.

Runs as a plain script (no pytest dependency to install on Render):

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_intent_router.py

Exits with code 0 on success, 1 on the first failed expectation. Prints a
verdict line per case so a green run is one screen of `OK` lines.

The four kinds produced by `classify_query_kind`:
    data_query        → routes to the analytics sub-agent pipeline
    missing_data      → "we don't track that" template (no SQL, no LLM)
    general_knowledge → LLM with a knowledge-permissive prompt
    chat              → LLM with the small-talk prompt
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# Make `app.*` importable without requiring the backend to be installed.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Set safe env defaults so importing config doesn't crash on missing values.
os.environ.setdefault("ADMIN_USERNAME", "test")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")

from app.agents import (  # noqa: E402
    classify_query_kind,
    _reset_last_kind,
)


# --- Test cases -----------------------------------------------------------

# (question, expected_kind, has_data) — has_data defaults to True.
ANALYTICS_CASES: list[tuple[str, str, bool]] = [
    # ---- exact strings the user said MUST resolve to data_query ----
    ("sales?",                              "data_query", True),
    ("last week",                           "data_query", True),
    ("last 8 days",                         "data_query", True),
    ("how much sales",                      "data_query", True),
    ("tell me revenue",                     "data_query", True),
    ("orders?",                             "data_query", True),
    ("today sales",                         "data_query", True),
    ("yesterday report",                    "data_query", True),
    ("monthly trend",                       "data_query", True),
    ("compare sales",                       "data_query", True),

    # ---- the originally-failing examples ----
    ("tell me about my sales",              "data_query", True),
    ("tell me about my last 2 sales data",  "data_query", True),
    ("tell me about last 8 days sales",     "data_query", True),
    ("what is my last 8 days data",         "data_query", True),
    ("what is my last month sales",         "data_query", True),

    # ---- broader natural-language analytics queries ----
    ("last 2 month revenue",                "data_query", True),
    ("how much did i sell yesterday",       "data_query", True),
    ("sales trend",                         "data_query", True),
    ("revenue trend",                       "data_query", True),
    ("orders this week",                    "data_query", True),
    ("compare this month vs last month",    "data_query", True),
    ("root cause of drop",                  "data_query", True),
    ("top products",                        "data_query", True),
    ("sales summary",                       "data_query", True),
    ("how many orders",                     "data_query", True),
    ("average order value",                 "data_query", True),
    ("total revenue",                       "data_query", True),
    ("sales performance",                   "data_query", True),
    ("daily sales",                         "data_query", True),
    ("weekly sales",                        "data_query", True),
    ("monthly sales",                       "data_query", True),

    # ---- has_data biased queries (analytics signal alone is weak) ----
    ("today",                               "data_query", True),
    ("this week",                           "data_query", True),
    ("last 5 days",                         "data_query", True),
    ("last 30 days",                        "data_query", True),
]


CHAT_CASES: list[tuple[str, str, bool]] = [
    # Pure greetings / small talk
    ("hi",                                  "chat", True),
    ("hii",                                 "chat", True),
    ("hello",                               "chat", True),
    ("hey there",                           "chat", True),
    ("how are you?",                        "chat", True),
    ("who are you?",                        "chat", True),
    ("what is your name",                   "chat", True),
    ("what can you do",                     "chat", True),
    ("thanks",                              "chat", True),
    ("thank you",                           "chat", True),
    ("good morning",                        "chat", True),
    ("bye",                                 "chat", True),
    ("ok",                                  "chat", True),
]


KNOWLEDGE_CASES: list[tuple[str, str, bool]] = [
    # Definitions / general knowledge — no possessive, no business signal.
    ("what is profit margin",               "general_knowledge", True),
    ("what is aov",                         "general_knowledge", True),
    ("explain net revenue",                 "general_knowledge", True),
    ("define gmv",                          "general_knowledge", True),
    ("how can i improve my conversion rate", "general_knowledge", True),
    ("how do retail margins work",          "general_knowledge", True),
    ("tell me about customer retention",    "general_knowledge", True),
]


MISSING_DATA_CASES: list[tuple[str, str, bool]] = [
    # Out-of-scope dimensions — the dataset doesn't carry these.
    ("what is my employee headcount",       "missing_data", True),
    ("show me my employee salary",          "missing_data", True),
    ("sales by city",                       "missing_data", True),
    ("inventory by warehouse",              "missing_data", True),
    ("what is my profit margin",            "missing_data", True),
    ("what is my net profit",               "missing_data", True),
    ("ad spend last month",                 "missing_data", True),
]


# --- Continuity tests (multi-turn) ----------------------------------------

# (question, expected_kind, previous_kind) — exercises conversational
# continuity: a short ambiguous follow-up after a data turn should stay
# in agentic mode.
CONTINUITY_CASES: list[tuple[str, str, str | None]] = [
    ("and what about last week",            "data_query", "data_query"),
    ("what about october",                  "data_query", "data_query"),
    ("and may",                             "data_query", "data_query"),
    # Same query without prior context defaults to chat (too short, no data signal):
    ("and may",                             "chat",       None),
]


# --- has_data=False (no upload yet) ---------------------------------------

NO_DATA_CASES: list[tuple[str, str, bool]] = [
    # Strong analytics signal still wins even without data — we WANT to
    # route to the agentic pipeline so it can produce a clean
    # "No data uploaded yet" response instead of the chat LLM
    # hallucinating numbers.
    ("show me last month sales",            "data_query", False),
    ("revenue trend",                       "data_query", False),
    # Short ambiguous queries with no signal default to chat when there's
    # nothing on file — no point routing to SQL.
    ("hi",                                  "chat",       False),
    ("today",                               "chat",       False),
]


# --- Runner ---------------------------------------------------------------

def _run(group_label: str, cases: list[tuple], use_continuity: bool = False) -> tuple[int, int]:
    print(f"\n=== {group_label} ===")
    passed = failed = 0
    for case in cases:
        if use_continuity:
            question, expected, prev_kind = case
            _reset_last_kind()
            kind, conf, hints = classify_query_kind(
                question, has_data=True, previous_kind=prev_kind,
            )
        else:
            question, expected, has_data = case
            _reset_last_kind()
            kind, conf, hints = classify_query_kind(
                question, has_data=has_data,
            )
        ok = kind == expected
        passed += int(ok); failed += int(not ok)
        marker = "OK " if ok else "BAD"
        if ok:
            print(f"  [{marker}] {expected:18} got={kind:18} :: {question!r}")
        else:
            patterns = ",".join(hints.get("matched_patterns", [])) or "-"
            scores = (
                f"a={hints.get('analytics_score'):.2f} "
                f"c={hints.get('chat_score'):.2f} "
                f"k={hints.get('knowledge')} m={bool(hints.get('missing'))}"
            )
            print(f"  [{marker}] {expected:18} got={kind:18} :: {question!r}")
            print(f"        patterns=[{patterns}] {scores}")
            print(f"        reason={hints.get('reason')!r}")
    return passed, failed


def main() -> int:
    total_pass = total_fail = 0
    p, f = _run("ANALYTICS — must route to data_query", ANALYTICS_CASES); total_pass += p; total_fail += f
    p, f = _run("CHAT — must route to chat",            CHAT_CASES);      total_pass += p; total_fail += f
    p, f = _run("KNOWLEDGE — must route to general_knowledge", KNOWLEDGE_CASES); total_pass += p; total_fail += f
    p, f = _run("MISSING DATA — must route to missing_data", MISSING_DATA_CASES); total_pass += p; total_fail += f
    p, f = _run("CONTINUITY — short follow-up after data turn",
                CONTINUITY_CASES, use_continuity=True); total_pass += p; total_fail += f
    p, f = _run("HAS_DATA=False — no upload yet",       NO_DATA_CASES);   total_pass += p; total_fail += f

    total = total_pass + total_fail
    print(f"\nTOTAL: {total_pass}/{total} passed, {total_fail} failed")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
