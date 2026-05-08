"""Regression suite for entity-aware analytics intent routing.

Runs as a plain script:

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_analytics_intent.py

Covers:
  - the 12 query patterns the operator wants to work permanently
  - top vs bottom ranking direction
  - typo robustness via the deterministic TYPO_MAP + difflib fuzzy layer
  - missing-dimension routing (city, category)
  - end-to-end SQL plan kind / group_by / ORDER BY direction

Exits with code 0 if every assertion passes, else 1.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("ADMIN_USERNAME", "test")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")

from app.analytics_engine import (  # noqa: E402
    _build_intent_diagnostics,
    _normalize_typos,
    _ranking_direction,
    _ranking_subject,
    _reset_last_kind,
    classify_intent,
    classify_query_kind,
)


# ---- 1. The 12 patterns the operator listed ------------------------------

# Each row: (question, expected_kind, expected_intent, expected_subject, expected_direction)
PATTERN_CASES: list[tuple[str, str, str, str | None, str | None]] = [
    ("which product sold most",                "data_query", "product_performance", "product",  "desc"),
    ("which product sold this month most",     "data_query", "product_performance", "product",  "desc"),
    ("top product this month",                 "data_query", "product_performance", "product",  "desc"),
    ("highest selling item",                   "data_query", "product_performance", "product",  "desc"),
    ("best selling product",                   "data_query", "product_performance", "product",  "desc"),
    ("top 5 products",                         "data_query", "product_performance", "product",  "desc"),
    ("top customers",                          "data_query", "product_performance", "customer", "desc"),
    ("best performing product",                "data_query", "product_performance", "product",  "desc"),
    ("which item generated most revenue",      "data_query", "product_performance", "product",  "desc"),
    ("top selling SKU",                        "data_query", "product_performance", "product",  "desc"),
    ("lowest selling products",                "data_query", "product_performance", "product",  "asc"),
    ("worst performing products",              "data_query", "product_performance", "product",  "asc"),
    ("bottom 5 products",                      "data_query", "product_performance", "product",  "asc"),
    ("least selling items",                    "data_query", "product_performance", "product",  "asc"),
]


# ---- 2. Typo-heavy variants ---------------------------------------------

TYPO_CASES: list[tuple[str, str, str, str | None, str | None]] = [
    ("topest selling produt this mounth",      "data_query", "product_performance", "product",  "desc"),
    ("highest reveneu produt",                 "data_query", "product_performance", "product",  "desc"),
    ("top sellling product",                   "data_query", "product_performance", "product",  "desc"),
    ("loweset selling produkt",                "data_query", "product_performance", "product",  "asc"),
    ("worst custmer this mounth",              "data_query", "product_performance", "customer", "asc"),
]


# ---- 3. Missing-dimension routing ---------------------------------------

# Schema has Product Name + Party Name but no Category, City, or other
# extra dimensions. Queries that NAME those dimensions must route to
# missing_data — never to a generic sales_summary that hallucinates numbers.
MISSING_CASES: list[tuple[str, str]] = [
    ("most profitable category",         "missing_data"),
    ("category breakdown",               "missing_data"),
    ("sales by city",                    "missing_data"),
    ("highest revenue city",             "missing_data"),
    ("best performing categories",       "missing_data"),
]


# ---- 4. Diagnostics record shape ----------------------------------------

DIAGNOSTICS_CASES: list[tuple[str, dict]] = [
    ("which product sold most", {
        "intent_type": "ranking", "entity": "product",
        "direction": "desc", "chart": "horizontal_bar",
        "sql_type": "grouped_ranking",
    }),
    ("worst selling products", {
        "intent_type": "ranking", "entity": "product",
        "direction": "asc", "chart": "horizontal_bar",
        "sql_type": "grouped_ranking",
    }),
    ("top customers this month", {
        "intent_type": "ranking", "entity": "customer",
        "direction": "desc", "chart": "horizontal_bar",
        "sql_type": "grouped_ranking",
    }),
]


# ---- 5. SQL plan shape (end-to-end through SqlPlanner) -------------------
#
# SqlPlanner needs state.intent + state.time_window to be populated. We
# stub both manually since this test doesn't exercise the full coordinator
# loop — it asserts the planner emits the right SQL shape for a given
# intent + direction combo.

import asyncio  # noqa: E402

from app.analytics_engine import SqlPlannerArgs, SqlPlannerTool, TurnState  # noqa: E402


async def _plan_for(question: str) -> dict:
    intent_type, _, hints = classify_intent(question)
    state = TurnState(question=question)
    state = state.apply(
        intent={"type": intent_type, "confidence": 0.96, "hints": hints,
                "metric": "total_amount", "top_n": hints.get("top_n")},
        time_window={"start_date": "2025-01-01", "end_date": "2025-12-31"},
    )
    res = await SqlPlannerTool().run(state, SqlPlannerArgs())
    return res.output or {}


PLAN_CASES: list[tuple[str, dict]] = [
    ("top 5 products", {
        "kind": "ranking", "ranking_subject": "product",
        "rank_direction": "desc", "group_by": ["Product Name"],
        "order_by_dir": "DESC", "limit": 5,
    }),
    ("bottom 3 products", {
        "kind": "ranking", "ranking_subject": "product",
        "rank_direction": "asc", "group_by": ["Product Name"],
        "order_by_dir": "ASC", "limit": 3,
    }),
    ("top customers", {
        "kind": "ranking", "ranking_subject": "customer",
        "rank_direction": "desc", "group_by": ["Party Name"],
        "order_by_dir": "DESC",
    }),
    ("worst selling products", {
        "kind": "ranking", "ranking_subject": "product",
        "rank_direction": "asc", "group_by": ["Product Name"],
        "order_by_dir": "ASC",
    }),
]


# ---- Runner --------------------------------------------------------------

def _run_pattern(label: str, cases: list) -> tuple[int, int]:
    print(f"\n=== {label} ===")
    passed = failed = 0
    for q, exp_kind, exp_intent, exp_subj, exp_dir in cases:
        _reset_last_kind()
        kind, _, _ = classify_query_kind(q)
        intent, conf, hints = classify_intent(q)
        ok = (kind == exp_kind and intent == exp_intent)
        if exp_subj is not None:
            ok = ok and hints.get("ranking_subject") == exp_subj
        if exp_dir is not None:
            ok = ok and hints.get("rank_direction") == exp_dir
        marker = "OK " if ok else "BAD"
        passed += int(ok); failed += int(not ok)
        if ok:
            print(f"  [{marker}] {q!r:48} intent={intent:22} subj={hints.get('ranking_subject')} dir={hints.get('rank_direction')}")
        else:
            print(f"  [{marker}] {q!r:48} got kind={kind} intent={intent} subj={hints.get('ranking_subject')} dir={hints.get('rank_direction')}")
            print(f"        expected kind={exp_kind} intent={exp_intent} subj={exp_subj} dir={exp_dir}")
    return passed, failed


def _run_missing(label: str) -> tuple[int, int]:
    print(f"\n=== {label} ===")
    passed = failed = 0
    for q, exp in MISSING_CASES:
        _reset_last_kind()
        kind, _, _ = classify_query_kind(q)
        ok = kind == exp
        marker = "OK " if ok else "BAD"
        passed += int(ok); failed += int(not ok)
        print(f"  [{marker}] {q!r:48} got={kind} expected={exp}")
    return passed, failed


def _run_diagnostics() -> tuple[int, int]:
    print("\n=== DIAGNOSTICS — structured per-turn record ===")
    passed = failed = 0
    for q, expected_subset in DIAGNOSTICS_CASES:
        intent, _, hints = classify_intent(q)
        diag = _build_intent_diagnostics(q, intent, hints)
        ok = all(diag.get(k) == v for k, v in expected_subset.items())
        marker = "OK " if ok else "BAD"
        passed += int(ok); failed += int(not ok)
        if ok:
            print(f"  [{marker}] {q!r:42} -> {diag}")
        else:
            missing_keys = {k: (diag.get(k), v) for k, v in expected_subset.items() if diag.get(k) != v}
            print(f"  [{marker}] {q!r:42} missing/mismatched: {missing_keys}")
            print(f"        full diagnostics: {diag}")
    return passed, failed


def _run_plans() -> tuple[int, int]:
    print("\n=== SQL PLAN SHAPE (end-to-end through SqlPlanner) ===")
    passed = failed = 0
    for q, expected in PLAN_CASES:
        plan = asyncio.run(_plan_for(q))
        order_dir = (plan.get("order_by") or [{}])[0].get("dir")
        actual = {
            "kind":            plan.get("kind"),
            "ranking_subject": plan.get("ranking_subject"),
            "rank_direction":  plan.get("rank_direction"),
            "group_by":        plan.get("group_by"),
            "order_by_dir":    order_dir,
        }
        if "limit" in expected:
            actual["limit"] = plan.get("limit")
        ok = all(actual.get(k) == v for k, v in expected.items())
        marker = "OK " if ok else "BAD"
        passed += int(ok); failed += int(not ok)
        print(f"  [{marker}] {q!r:42} {actual}")
        if not ok:
            print(f"        expected: {expected}")
    return passed, failed


def _run_normalization() -> tuple[int, int]:
    print("\n=== TYPO NORMALIZATION (unit) ===")
    passed = failed = 0
    cases = [
        ("topest selling produt this mounth", "top selling product this month"),
        ("highest reveneu",                   "highest revenue"),
        ("worst custmer",                     "worst customer"),
        ("loweset selling produkt",           "lowest selling product"),
        ("which product sold most",           "which product sold most"),  # already clean
    ]
    for raw, expected in cases:
        got = _normalize_typos(raw)
        ok = got.lower() == expected.lower()
        marker = "OK " if ok else "BAD"
        passed += int(ok); failed += int(not ok)
        print(f"  [{marker}] {raw!r:48} -> {got!r}")
        if not ok:
            print(f"        expected: {expected!r}")
    return passed, failed


def main() -> int:
    total_pass = total_fail = 0
    for label, cases in [
        ("PATTERN — 12 supported phrasings", PATTERN_CASES),
        ("TYPO — typo-tolerant variants",    TYPO_CASES),
    ]:
        p, f = _run_pattern(label, cases); total_pass += p; total_fail += f
    p, f = _run_missing("MISSING DIMENSION — schema doesn't carry these"); total_pass += p; total_fail += f
    p, f = _run_diagnostics();                                              total_pass += p; total_fail += f
    p, f = _run_plans();                                                    total_pass += p; total_fail += f
    p, f = _run_normalization();                                            total_pass += p; total_fail += f

    total = total_pass + total_fail
    print(f"\nTOTAL: {total_pass}/{total} passed, {total_fail} failed")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
