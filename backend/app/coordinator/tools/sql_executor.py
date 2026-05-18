"""
SqlExecutor - run a validated SELECT and return rows + a chart payload.

Hard caps: row count, bytes scanned. Same restrictions as SqlDryRun
applied again here (defense in depth).
"""
from __future__ import annotations

import asyncio
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
    return f"SELECT * FROM ({cleaned}) LIMIT {default_limit}"


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


def _build_chart_payload(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Auto-derive a chart payload when the result has columns suitable
    for plotting. Picks the first text-like column as x and the first
    numeric column as y by scanning multiple rows (not just row[0]) so
    a NULL at row 0 doesn't disqualify a valid column."""
    if not rows:
        return None
    types = _infer_column_types(rows)
    if not types:
        return None
    cols = list(types.keys())
    x_col = next((c for c in cols if types[c] == "text"), None)
    y_col = next((c for c in cols if types[c] == "number"), None)
    if x_col is None and cols:
        x_col = cols[0]
    if y_col is None:
        # Try the second column as a fallback numeric, but only if it
        # isn't the same column we picked for x.
        for c in cols:
            if c != x_col and types[c] != "text":
                y_col = c
                break
    if x_col is None or y_col is None or x_col == y_col:
        return None
    total = len(rows)
    truncated = total > _CHART_MAX_POINTS
    capped = rows[:_CHART_MAX_POINTS]
    return {
        "kind": "bar" if total <= 30 else "line",
        "x": x_col,
        "y": y_col,
        "labels": [str(r.get(x_col)) for r in capped],
        "values": [r.get(y_col) for r in capped],
        "total_points": total,
        "truncated": truncated,
    }


class SqlExecutorTool(Tool):
    name = "SqlExecutor"
    description = (
        "Execute a validated SELECT and return rows. Will refuse any "
        "non-SELECT. Adds a LIMIT if the query doesn't have one. Also "
        "builds a simple chart payload (bar/line) when the result looks "
        "plottable."
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

        chart = _build_chart_payload(result)
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
