"""Unit tests for the TRUST guardrail validator (validate_result).

The validator is the deterministic post-execution sanity check: garbage
results (NaN/Inf, negative totals, absurd percentages, bad counts) drop
confidence to 'low'; normal results stay 'high'; a legit zero / empty
result stays 'high' (it's noted, not flagged).

Run:
    cd backend && python -m pytest tests/test_validation.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.coordinator.validation import _metric_kind, validate_result


# ---------------------------------------------------------------------------
# Garbage → low confidence
# ---------------------------------------------------------------------------


class TestGarbageIsLow:
    def test_nan_flags_low(self):
        rows = [{"month": "2025-01", "net_sales": math.nan}]
        v = validate_result(rows)
        assert v["confidence"] == "low"
        assert any("NaN" in r or "non-finite" in r for r in v["reasons"])

    def test_infinity_flags_low(self):
        rows = [{"brand": "A", "revenue": math.inf}]
        v = validate_result(rows)
        assert v["confidence"] == "low"

    def test_negative_revenue_flags_low(self):
        rows = [{"brand": "Acme", "total_sales": -42000.0}]
        v = validate_result(rows)
        assert v["confidence"] == "low"
        assert any("negative" in r.lower() for r in v["reasons"])

    def test_absurd_percentage_flags_low(self):
        # The classic broken-aggregation banner: -7,843% margin.
        rows = [{"brand": "Acme", "margin_pct": -7843.0}]
        v = validate_result(rows)
        assert v["confidence"] == "low"
        assert any("ratio" in r.lower() or "percent" in r.lower() for r in v["reasons"])

    def test_negative_count_flags_low(self):
        rows = [{"day": "2025-01-01", "order_count": -3}]
        v = validate_result(rows)
        assert v["confidence"] == "low"

    def test_non_integer_count_flags_low(self):
        rows = [{"day": "2025-01-01", "units_qty": 12.5}]
        v = validate_result(rows)
        assert v["confidence"] == "low"


# ---------------------------------------------------------------------------
# Normal → high confidence
# ---------------------------------------------------------------------------


class TestNormalIsHigh:
    def test_normal_money_series_is_high(self):
        rows = [
            {"month": "2025-01", "net_sales": 409123.0},
            {"month": "2025-02", "net_sales": 388900.0},
            {"month": "2025-03", "net_sales": 412004.0},
        ]
        v = validate_result(rows)
        assert v["confidence"] == "high"

    def test_plausible_percentage_is_high(self):
        rows = [
            {"brand": "A", "margin_pct": 23.4},
            {"brand": "B", "margin_pct": 41.8},
        ]
        v = validate_result(rows)
        assert v["confidence"] == "high"

    def test_fractional_ratio_is_high(self):
        # Ratio stored as a fraction (0.0-1.0) must not false-flag.
        rows = [{"brand": "A", "conversion_ratio": 0.32}]
        v = validate_result(rows)
        assert v["confidence"] == "high"

    def test_numeric_string_aggregate_is_high(self):
        # asyncpg renders Decimal aggregates as text like '238694.00'.
        rows = [{"brand": "A", "total_amount": "238694.00"}]
        v = validate_result(rows)
        assert v["confidence"] == "high"

    def test_valid_count_is_high(self):
        rows = [{"day": "2025-01-01", "order_count": 312}]
        v = validate_result(rows)
        assert v["confidence"] == "high"


# ---------------------------------------------------------------------------
# Legit zero / empty → high confidence (noted, not flagged)
# ---------------------------------------------------------------------------


class TestZeroAndEmptyAreHigh:
    def test_empty_result_is_high(self):
        v = validate_result([])
        assert v["confidence"] == "high"
        assert any("empty" in r.lower() for r in v["reasons"])

    def test_none_rows_is_high(self):
        v = validate_result(None)
        assert v["confidence"] == "high"

    def test_legit_zero_value_is_high(self):
        # A real zero (no sales that period) is NOT garbage.
        rows = [{"month": "2025-07", "net_sales": 0.0}]
        v = validate_result(rows)
        assert v["confidence"] == "high"

    def test_zero_count_is_high(self):
        rows = [{"day": "2025-07-01", "order_count": 0}]
        v = validate_result(rows)
        assert v["confidence"] == "high"


# ---------------------------------------------------------------------------
# Metric-kind heuristic
# ---------------------------------------------------------------------------


class TestMetricKind:
    @pytest.mark.parametrize(
        "col,expected",
        [
            ("margin_pct", "ratio"),
            ("growth_rate", "ratio"),
            ("gross_margin", "ratio"),
            ("net_sales", "money"),
            ("total_revenue", "money"),
            ("profit", "money"),
            ("order_count", "count"),
            ("units_qty", "count"),
            ("brand", "other"),
            ("month", "other"),
        ],
    )
    def test_classification(self, col, expected):
        assert _metric_kind(col) == expected


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
