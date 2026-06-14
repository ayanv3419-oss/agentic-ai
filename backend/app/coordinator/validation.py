"""
TRUST guardrail - deterministic post-execution sanity check on SQL results.

After the LLM writes SQL and SqlExecutor runs it, NOTHING here calls an LLM.
``validate_result`` inspects the raw result rows + columns and returns a
confidence verdict the formatter (insightFmt) uses to decide whether to add a
brief caveat. The whole point is to stop a wrong-but-nonzero number from
becoming an authoritative, confident ₹-denominated headline.

Design rules:
  * Pure + deterministic + fast. No I/O, no LLM, no DB.
  * Conservative: prefer a false NEGATIVE (stay 'high') over crying wolf.
    A legit zero / legit empty result is NOT flagged - it's just noted.
  * Only the obvious garbage (NaN, Inf, impossible negatives, absurd
    percentages, negative counts) drops confidence to 'low'.

API:
    validate_result(rows, columns=None, query_kind=None)
        -> {"confidence": "high"|"low", "reasons": [str, ...]}
"""
from __future__ import annotations

import math
import re
from typing import Any

# Column-name heuristics → metric kind. Conservative, checked in order:
# ratio first (a "margin_pct" column is a ratio, not money), then money,
# then count. Anything unmatched is treated as 'other' and only checked for
# NaN/Inf.
_RATIO_RE = re.compile(
    r"(margin|pct|percent|ratio|rate|share|growth|yoy|mom|cagr)", re.I
)
_MONEY_RE = re.compile(
    r"(revenue|sales|amount|total|profit|gross|net|turnover|gmv|price|value|cost|spend)",
    re.I,
)
_COUNT_RE = re.compile(
    r"(count|qty|quantity|units|orders|txn|transactions|number|num_|_num)", re.I
)

# A ratio expressed as a percent is "suspicious" past this magnitude. 1000%
# (10x) is the band: legit margins / growth rarely exceed it, and a value
# like -7843% is the classic broken-aggregation banner we want to catch.
_PCT_SUSPICIOUS = 1000.0
# Same band when the ratio is stored as a fraction (0.5 == 50%): |value| > 10.
_FRACTION_SUSPICIOUS = 10.0


def _metric_kind(col: str | None) -> str:
    """Heuristically classify a column as 'ratio' | 'money' | 'count' |
    'other' from its name. Order matters: ratio wins over money so a
    'margin_pct' column isn't treated as currency."""
    c = col or ""
    if _RATIO_RE.search(c):
        return "ratio"
    if _MONEY_RE.search(c):
        return "money"
    if _COUNT_RE.search(c):
        return "count"
    return "other"


def _as_number(v: Any) -> float | None:
    """Coerce a cell to float, or None if it isn't numeric. Mirrors the
    SqlExecutor convention where DB drivers may render numerics as text
    (asyncpg returns Decimal aggregates as strings like '238694.00').
    Booleans are NOT numbers here (they're an isinstance(int) trap)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    # Decimal and other float()-able numerics.
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_nonneg_int(v: float) -> bool:
    return v >= 0 and float(v).is_integer()


def validate_result(
    rows: list[dict[str, Any]] | None,
    columns: list[str] | None = None,
    query_kind: str | None = None,
) -> dict[str, Any]:
    """Inspect the latest SQL result and return a confidence verdict.

    Returns ``{"confidence": "high"|"low", "reasons": [str, ...]}``.

    ``columns`` may be omitted - it's inferred from the first row's keys.
    ``query_kind`` is the route (e.g. "data_query") - currently advisory
    only; the checks below run regardless of it so non-data turns with no
    numeric rows simply pass.

    Checks (any failure → 'low'):
      * NaN / Infinity in any numeric cell.
      * money-type metric is negative where it shouldn't be.
      * ratio/percent/margin value outside a plausible band (|v| > 1000%).
      * count-type metric is negative or non-integer.
    An empty/zero result is NOT a failure - it's noted in ``reasons`` but
    keeps confidence 'high'.
    """
    reasons: list[str] = []
    rows = rows or []

    # Empty result: distinct from a zero result. Note it, don't flag it.
    if not rows:
        reasons.append("empty result (no rows) - distinct from a zero value")
        return {"confidence": "high", "reasons": reasons}

    cols = list(columns) if columns else list(rows[0].keys())
    kinds = {c: _metric_kind(c) for c in cols}

    low = False
    # De-dupe reason strings so one bad column doesn't spam N identical lines.
    seen: set[str] = set()

    def _flag(msg: str) -> None:
        nonlocal low
        low = True
        if msg not in seen:
            seen.add(msg)
            reasons.append(msg)

    for r in rows:
        for c in cols:
            num = _as_number(r.get(c))
            if num is None:
                continue  # text / null / non-numeric → nothing to check

            # 1. NaN / Infinity anywhere is always garbage.
            if math.isnan(num) or math.isinf(num):
                _flag(f"non-finite value (NaN/Infinity) in column '{c}'")
                continue

            kind = kinds.get(c, "other")

            if kind == "ratio":
                # Heuristic: values with |v| > 1.5 are almost certainly stored
                # as percentages (e.g. 23.4 == 23.4%); smaller ones as a
                # fraction (0.23). Use the matching band so we don't false-flag
                # a legit 0.95 fraction or a legit 95% percentage.
                if abs(num) > 1.5:
                    if abs(num) > _PCT_SUSPICIOUS:
                        _flag(
                            f"implausible ratio/percentage in column '{c}': "
                            f"{num:g}% exceeds ±{_PCT_SUSPICIOUS:g}%"
                        )
                elif abs(num) > _FRACTION_SUSPICIOUS:
                    _flag(
                        f"implausible ratio in column '{c}': {num:g} "
                        f"exceeds ±{_FRACTION_SUSPICIOUS:g}"
                    )

            elif kind == "money":
                if num < 0:
                    _flag(
                        f"negative value in money/total column '{c}': {num:g} "
                        f"(revenue/sales/total should not be negative)"
                    )

            elif kind == "count":
                if num < 0:
                    _flag(f"negative count in column '{c}': {num:g}")
                elif not _is_nonneg_int(num):
                    _flag(f"non-integer count in column '{c}': {num:g}")

    if low:
        return {"confidence": "low", "reasons": reasons}

    # All numeric cells passed. Note a pure-zero result (not a failure).
    if not reasons:
        reasons.append("all numeric cells within plausible bounds")
    return {"confidence": "high", "reasons": reasons}


__all__ = ["validate_result"]
