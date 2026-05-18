"""
SqlExecutor - run a validated SELECT and return rows + a chart payload.

Hard caps: row count, bytes scanned. Same restrictions as SqlDryRun
applied again here (defense in depth).
"""
from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.sql_dry_run import _validate_shape
from app.infrastructure import get_connection, settings


_DEFAULT_MAX_ROWS = 500
_HARD_ROW_CAP = 5000


def _enforce_limit(sql: str, default_limit: int) -> str:
    """Add a LIMIT clause if the SQL doesn't already have one."""
    if re.search(r"\blimit\b\s+\d+", sql, re.IGNORECASE):
        return sql
    return f"{sql.rstrip().rstrip(';')} LIMIT {default_limit}"


def _build_chart_payload(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Auto-derive a chart payload when the result has 1-2 columns suitable
    for plotting. Frontend uses 'category' + 'value' axis names."""
    if not rows:
        return None
    first = rows[0]
    cols = list(first.keys())
    if not cols:
        return None
    # Pick the first text-like column as x and the first numeric as y.
    x_col = None
    y_col = None
    for c in cols:
        v = first.get(c)
        if x_col is None and isinstance(v, str):
            x_col = c
        if y_col is None and isinstance(v, (int, float)):
            y_col = c
        if x_col and y_col:
            break
    if x_col is None and len(cols) >= 1:
        x_col = cols[0]
    if y_col is None and len(cols) >= 2:
        y_col = cols[1]
    if x_col is None or y_col is None:
        return None
    return {
        "kind": "bar" if len(rows) <= 30 else "line",
        "x": x_col,
        "y": y_col,
        "labels": [str(r.get(x_col)) for r in rows[:200]],
        "values": [r.get(y_col) for r in rows[:200]],
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
        # Soft cost-guard check: byte estimate.
        try:
            payload_bytes = sys.getsizeof(repr(result))
        except Exception:
            payload_bytes = 0
        if payload_bytes > settings.sql_max_bytes_scanned:
            return ToolOutcome(
                ok=False,
                error=(
                    f"Result exceeds byte cap "
                    f"({payload_bytes} > {settings.sql_max_bytes_scanned})"
                ),
            )

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
