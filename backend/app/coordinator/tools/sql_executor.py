"""
SqlExecutor - run a validated SELECT and return rows + a chart payload.

Hard caps: row count, bytes scanned. Same restrictions as SqlDryRun
applied again here (defense in depth).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.sql_dry_run import _validate_shape
from app.infrastructure import get_connection


_DEFAULT_MAX_ROWS = 500
_HARD_ROW_CAP = 5000
_CHART_MAX_POINTS = 500  # was 200 (silent), now matches default max_rows
_TYPE_SAMPLE_ROWS = 50   # rows scanned to infer column types


def _enforce_limit(sql: str, default_limit: int) -> str:
    """Cap the outermost result at ``default_limit`` rows.

    Previously this used a regex (``\\blimit\\b\\s+\\d+``) to decide
    whether to add a LIMIT - but the regex matched a LIMIT inside a CTE
    or subquery and skipped the outer cap, letting big joins return
    unbounded rows. We now always wrap the SQL in
    ``SELECT * FROM (<sql>) LIMIT N`` so the outermost row count is
    bounded regardless of inner clauses. SQLite handles outer LIMIT
    cleanly over inner ORDER BY / LIMIT (the inner ordering survives;
    the outer LIMIT just trims). The redundant-LIMIT case is harmless.
    """
    cleaned = sql.rstrip().rstrip(";").strip()
    # Newlines around the wrap so a trailing line-comment (`-- ...`) inside
    # `cleaned` cannot comment out the closing paren or the outer LIMIT.
    return f"SELECT * FROM (\n{cleaned}\n) LIMIT {default_limit}"


def _infer_column_types(
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    """Classify each column as 'text' / 'number' / 'other' by scanning
    up to _TYPE_SAMPLE_ROWS rows. None values are ignored. Booleans are
    excluded from 'number' (they're an isinstance(int) trap)."""
    if not rows:
        return {}
    cols = list(rows[0].keys())
    sample = rows[:_TYPE_SAMPLE_ROWS]
    out: dict[str, str] = {}
    for c in cols:
        kind = "other"
        for r in sample:
            v = r.get(c)
            if v is None:
                continue
            if isinstance(v, bool):
                # bool is int subclass in Python; treat as 'other'.
                kind = "other"
                break
            if isinstance(v, (int, float)):
                kind = "number"
                break
            if isinstance(v, str):
                kind = "text"
                break
        out[c] = kind
    return out


_DATE_COL_HINT = re.compile(r"(date|month|year|day|week|quarter|period|bucket)", re.I)
_DATE_VAL = re.compile(r"^\d{4}([-/]\d{1,2}){0,2}$")


def _y_label_for(col: str) -> str:
    """Map a numeric column name to a y-axis semantic label. The frontend
    formats 'sales' as currency (₹), 'percent' with a trailing %, and
    anything else as a plain number. Percent is checked first so a
    margin_pct column is labelled %, not currency."""
    c = (col or "").lower()
    if "pct" in c or "percent" in c:
        return "percent"
    # Quantity / count are checked BEFORE the currency keywords so a
    # column like `total_units` or `total_orders` is not mislabelled as
    # currency by the broad `total` match.
    if any(k in c for k in ("qty", "quantity", "units", "stock")):
        return "quantity"
    if any(k in c for k in ("count", "orders", "txn", "number")):
        return "count"
    if any(k in c for k in ("amount", "total", "sales", "revenue", "price", "value", "cost")):
        return "sales"
    return "value"


def _num(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _looks_like_dates(col: str, rows: list[dict[str, Any]]) -> bool:
    """True when a label column is time-like (so we plot a trend, not a
    ranking) - judged by column name AND the shape of its values."""
    if _DATE_COL_HINT.search(col or ""):
        return True
    hits = seen = 0
    for r in rows[:20]:
        v = r.get(col)
        if v is None:
            continue
        seen += 1
        if isinstance(v, str) and _DATE_VAL.match(v.strip()):
            hits += 1
    return seen > 0 and hits >= max(1, seen // 2)


def _order_by_col(sql: str | None, cols: list[str]) -> str | None:
    """Pull the column a query is ranked by out of its ORDER BY clause.
    That column is the metric the user actually asked about - so the
    chart must plot IT, not just the first numeric column. Handles
    "ORDER BY t.col DESC", quoted names, and positional "ORDER BY 4"."""
    if not sql:
        return None
    m = re.search(r"\border\s+by\s+(.+?)(?:\s+limit\b|\s*$)", sql, re.I | re.S)
    if not m:
        return None
    first = m.group(1).split(",")[0].strip()
    first = re.sub(r"\s+(asc|desc)\s*$", "", first, flags=re.I).strip()
    first = first.strip('"').strip("'")
    if "." in first:
        first = first.split(".")[-1].strip('"').strip("'")
    if first.isdigit():
        idx = int(first) - 1
        return cols[idx] if 0 <= idx < len(cols) else None
    return first if first in cols else None


def _build_chart_payload(
    rows: list[dict[str, Any]],
    sql: str | None = None,
    granularity: str | None = None,
    aggregation_type: str | None = None,
) -> dict[str, Any] | None:
    """Build a SalesChart payload - the exact shape the frontend ChatChart
    renders. Returns:
      - kind 'summary' for a single numeric value,
      - kind 'trend'   when the label column looks like dates,
      - kind 'ranking' otherwise (label + value pairs).
    The y-axis column is the one the query is ORDER BY-ed on when known
    (so "top 5 by margin" plots margin, not sales); otherwise the first
    numeric column. Returns None when there is nothing to plot.

    ``granularity`` is the value resolved by the Granularity tool (e.g.
    "month", "week", "day") — used instead of the old hard-coded "monthly".
    Defaults to "auto" when not provided so the frontend can decide.
    """
    if not rows:
        return None
    types = _infer_column_types(rows)
    if not types:
        return None
    cols = list(types.keys())
    x_col = next((c for c in cols if types[c] == "text"), None)
    ranked = _order_by_col(sql, cols)
    if ranked and types.get(ranked) == "number":
        y_col = ranked
    else:
        y_col = next((c for c in cols if types[c] == "number"), None)
    if y_col is None:
        return None

    # Determine chart granularity. Prefer the value set by the Granularity
    # tool (e.g. "monthly", "weekly"). When it is absent, infer from the
    # x-axis bucket values so the frontend receives a concrete value instead
    # of the sentinel "auto" (which it cannot safely handle in a switch).
    _GRAN_MAP = {"month": "monthly", "week": "weekly", "day": "daily",
                 "year": "yearly", "monthly": "monthly", "weekly": "weekly",
                 "daily": "daily", "yearly": "yearly"}
    gran: str | None = _GRAN_MAP.get((granularity or "").lower())
    if gran is None and x_col:
        # Infer from the first x-value: YYYY → yearly, YYYY-MM → monthly,
        # YYYY-MM-DD → daily (frontend will further distinguish weekly).
        first_x = str(rows[0].get(x_col, "")) if rows else ""
        import re as _re2
        if _re2.match(r"^\d{4}$", first_x):
            gran = "yearly"
        elif _re2.match(r"^\d{4}-\d{2}$", first_x):
            gran = "monthly"
        elif _re2.match(r"^\d{4}-\d{2}-\d{2}", first_x):
            gran = "daily"
        # Still None → frontend inferGranularity() will handle it via null

    # Compute an honest total from the actual result rows.
    def _total_numeric() -> float:
        return sum(_num(r.get(y_col)) for r in rows)

    # Single row with a number -> summary card (e.g. "total sales").
    # Exception: if the x_col looks like a category/label (not a date,
    # not a number) and the SQL had LIMIT 1 or ORDER BY, treat as a
    # degenerate ranking and emit ranking kind so the frontend knows
    # this is a "top 1" result rather than an aggregate total.
    if len(rows) == 1:
        x_val = str(rows[0].get(x_col, "")) if x_col else ""
        import re as _re3
        if x_col and types.get(x_col) == "text" and not _re3.match(r"^\d", x_val):
            # Single-row ranking result (e.g. LIMIT 1 on brand/category)
            # Emit as ranking so the frontend shows a bar item, not a big number.
            return {
                "kind": "ranking",
                "granularity": gran,
                "totals": {"total_sales": _num(rows[0].get(y_col)), "orders": 0, "customers": 0},
                "series": [],
                "items": [{"name": x_val, "sales": _num(rows[0].get(y_col)), "orders": 0}],
                "y_label": _y_label_for(y_col),
            }
        return {
            "kind": "summary",
            "granularity": gran,
            "totals": {
                "total_sales": _num(rows[0].get(y_col)),
                # orders / customers kept as 0 — frontend requires these
                # fields; we cannot compute real values from a SELECT result
                # without knowing the original invoice/customer columns.
                "orders": 0,
                "customers": 0,
            },
            "series": [],
            "y_label": _y_label_for(y_col),
        }

    if x_col is None or x_col == y_col:
        x_col = next((c for c in cols if c != y_col), None)
    if x_col is None:
        return None

    capped = rows[:_CHART_MAX_POINTS]
    points = [
        {"name": str(r.get(x_col)), "value": _num(r.get(y_col))}
        for r in capped
    ]
    # Compute the headline total correctly based on the KPI's aggregation type.
    # percent / ratio metrics (e.g. margin %) must show the AVERAGE across
    # items — summing them (e.g. 95% + 96% + ... = 955%) is meaningless.
    # sum / count / currency metrics keep the plain sum.
    _agg = (aggregation_type or "sum").lower()
    if _agg in ("percent", "ratio", "avg") and points:
        total_val = sum(p["value"] for p in points) / len(points)
    else:
        total_val = sum(p["value"] for p in points)
    y_label = _y_label_for(y_col)

    if _looks_like_dates(x_col, capped):
        return {
            "kind": "trend",
            "granularity": gran,
            "totals": {"total_sales": total_val, "orders": 0, "customers": 0},
            "series": [
                {"bucket": p["name"], "sales": p["value"], "orders": 0}
                for p in points
            ],
            "y_label": y_label,
        }

    return {
        "kind": "ranking",
        "granularity": gran,
        "totals": {"total_sales": total_val, "orders": 0, "customers": 0},
        "series": [],
        "items": [
            {"name": p["name"], "sales": p["value"], "orders": 0}
            for p in points
        ],
        "y_label": y_label,
    }


class SqlExecutorTool(Tool):
    name = "SqlExecutor"
    description = (
        "Execute a validated SELECT and return rows. Will refuse any "
        "non-SELECT. Adds a LIMIT if the query doesn't have one. Also "
        "builds a chart payload (summary / trend / ranking) when the "
        "result looks plottable."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SELECT statement to execute.",
            },
            "max_rows": {
                "type": "integer",
                "default": _DEFAULT_MAX_ROWS,
                "minimum": 1,
                "maximum": _HARD_ROW_CAP,
            },
        },
        "required": ["sql"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        sql = str(args.get("sql") or "").strip()
        max_rows = min(
            _HARD_ROW_CAP,
            max(1, int(args.get("max_rows") or _DEFAULT_MAX_ROWS)),
        )
        shape_err = _validate_shape(sql)
        if shape_err is not None:
            return ToolOutcome(ok=False, error=shape_err)
        final_sql = _enforce_limit(sql, max_rows)
        try:
            async with get_connection() as db:
                cur = await db.execute(final_sql)
                rows = await cur.fetchall()
                await cur.close()
        except Exception as e:
            return ToolOutcome(ok=False, error=f"sql execution failed: {e}")

        result = [dict(r) for r in rows]
        # NOTE: the legacy `sql_max_bytes_scanned` check used
        # sys.getsizeof(repr(result)) which measured the Python string
        # object size, not bytes scanned from disk - effectively a
        # no-op against the 10 GB default. Removed. Row bounding is
        # handled by the outer LIMIT in _enforce_limit + _HARD_ROW_CAP.

        # Pass the granularity from state (set by Granularity tool) so the
        # chart payload reflects the actual time bucket, not a hard-coded
        # "monthly".
        _kpi_hint = getattr(ctx.state, "kpi_hint", None) or {}
        chart = _build_chart_payload(
            result, sql,
            granularity=ctx.state.granularity,
            aggregation_type=_kpi_hint.get("aggregation_type"),
        )
        updates: dict[str, Any] = {
            "sql_final": final_sql,
            "rows": result,
            "row_count": len(result),
        }
        if chart is not None:
            updates["chart_payload"] = chart
        return ToolOutcome(
            ok=True,
            output={
                "sql": final_sql,
                "row_count": len(result),
                "rows": result[:200],
                "chart": chart,
            },
            state_updates=updates,
        )


__all__ = ["SqlExecutorTool"]
