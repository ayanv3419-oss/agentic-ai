"""
Golden harness — the curated v1-vs-v2 acceptance gate for flipping the
``ORCHESTRATOR_VERSION`` default.

A ``GoldenCase`` describes the expected SHAPE of a successful answer
(route, capabilities used, chart presence, latency ceiling, etc.). The
runner in ``harness.py`` evaluates each case against both v1 and v2 on
the same frozen DB snapshot and produces a Markdown diff report.

Phase status
------------
P6 ships the **scaffolding + 6 starter cases**. The full 30-50 case set
is curated incrementally as v2 capability bodies (compare_periods,
breakdown_by_hierarchy) come online in P3/P4.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GoldenCase:
    """One curated test case."""

    case_id: str                                # stable identifier, e.g. "g001_revenue"
    question: str

    # Routing expectations -----------------------------------------------------
    expected_front_door_tier: str | None = None
    """One of {"cache", "clarification", "kpi_fast_path", "miss"} or None."""

    expected_route_v2: str | None = None
    """When None, any v2 route accepted. Use "v2_agentic" to enforce
    the LLM path (most non-fast-path cases)."""

    # Capability expectations --------------------------------------------------
    expected_capabilities_used: tuple[str, ...] = ()
    """Capabilities the v2 plan MUST include (any order)."""

    # Output shape expectations -----------------------------------------------
    expected_chart: bool = False
    expected_chart_kind: str | None = None
    expected_answer_contains_number: bool = True
    expected_ranking_items: int | None = None

    # Performance ceilings -----------------------------------------------------
    max_latency_ms: int = 15000
    max_reflection_iterations: int = 2
    max_total_tokens: int = 60_000

    # Grounded-narrative — when True, the harness asserts every number in
    # the v2 narrative appears in the underlying aggregates within
    # tolerance.
    enforce_grounded_narrative: bool = True

    # Soft expectations (warnings only, not failures) --------------------------
    notes: tuple[str, ...] = field(default_factory=tuple)


# ===========================================================================
# Starter cases — illustrative coverage of the major shapes. Expand to 30-50
# during P7 shadow-test instrumentation.
# ===========================================================================

GOLDEN_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        case_id="g001_revenue_total",
        question="what was my total revenue?",
        expected_front_door_tier="kpi_fast_path",
        expected_capabilities_used=(),  # short-circuits at front door
        expected_answer_contains_number=True,
        expected_chart=False,
        max_latency_ms=1500,
        max_total_tokens=0,
    ),
    GoldenCase(
        case_id="g002_orders_total",
        question="how many orders did I receive?",
        expected_front_door_tier="kpi_fast_path",
        expected_answer_contains_number=True,
        max_latency_ms=1500,
        max_total_tokens=0,
    ),
    GoldenCase(
        case_id="g003_revenue_last_month",
        question="what was my revenue last month?",
        expected_route_v2="v2_agentic",
        expected_capabilities_used=(
            "resolve_time_window", "run_data_query", "narrate", "format_response",
        ),
        expected_chart=True,
        max_latency_ms=15000,
    ),
    GoldenCase(
        case_id="g004_yoy_comparison",
        question="compare this month's revenue with last year",
        expected_route_v2="v2_agentic",
        expected_capabilities_used=("compare_periods", "narrate", "format_response"),
        expected_chart=True,
        expected_chart_kind="comparison",
        notes=("requires P3 compare_periods body",),
    ),
    GoldenCase(
        case_id="g005_top_products",
        question="what were my top 5 selling products?",
        expected_route_v2="v2_agentic",
        expected_capabilities_used=(
            "run_data_query", "narrate", "format_response",
        ),
        expected_chart=True,
        expected_ranking_items=5,
    ),
    GoldenCase(
        case_id="g006_branch_clarification",
        question="how is my store doing this month?",
        expected_front_door_tier="clarification",
        expected_answer_contains_number=False,
        max_latency_ms=500,
        max_total_tokens=0,
        notes=("only fires when ≥2 enabled branches exist",),
    ),
)


# Helpers for the harness runner ============================================


def by_id(case_id: str) -> GoldenCase | None:
    for c in GOLDEN_CASES:
        if c.case_id == case_id:
            return c
    return None


def with_tier(tier: str) -> tuple[GoldenCase, ...]:
    return tuple(c for c in GOLDEN_CASES if c.expected_front_door_tier == tier)


__all__ = ["GoldenCase", "GOLDEN_CASES", "by_id", "with_tier"]
