"""
CausalTree - structured root-cause decomposition for a metric that
dropped, spiked, or otherwise needs an explanation. Pure data-driven
heuristic: breaks the metric down by (category, segment, time bucket)
using already-fetched rows, finds the largest movers, and returns a
tree the rcaReasoner sub-agent narrates.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _group_sum(rows: list[dict[str, Any]], key: str, value: str) -> list[dict[str, Any]]:
    bucket: dict[str, float] = {}
    for r in rows:
        k = str(r.get(key) or "(unknown)")
        bucket[k] = bucket.get(k, 0.0) + _to_float(r.get(value))
    out = [{"key": k, "value": v} for k, v in bucket.items()]
    out.sort(key=lambda x: x["value"], reverse=True)
    return out


def _movers(current: list[dict[str, Any]], prior: list[dict[str, Any]], top_n: int = 5):
    cur_map = {r["key"]: r["value"] for r in current}
    pri_map = {r["key"]: r["value"] for r in prior}
    keys = set(cur_map) | set(pri_map)
    deltas = []
    for k in keys:
        c = cur_map.get(k, 0.0)
        p = pri_map.get(k, 0.0)
        delta = c - p
        deltas.append({
            "key": k,
            "current": c,
            "prior": p,
            "delta": delta,
            "pct_change": (delta / p * 100.0) if p else None,
        })
    deltas.sort(key=lambda d: abs(d["delta"]), reverse=True)
    return deltas[:top_n]


class CausalTreeTool(Tool):
    name = "CausalTree"
    description = (
        "Decompose a metric (e.g. revenue) into a structured tree of "
        "movers so an answer can explain WHY it changed. Inputs: current "
        "and prior-period rows plus the dimension to split by. Returns "
        "the top movers + the residual. Use this after the data has been "
        "fetched for both periods."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "dimension": {
                "type": "string",
                "description": (
                    "Column to decompose by, e.g. 'Product Name', "
                    "'Party Name', 'Payment Type'."
                ),
            },
            "value_column": {
                "type": "string",
                "default": "Total Amount",
                "description": "Numeric column to aggregate (sum).",
            },
            "current_rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Rows from the current/target period.",
            },
            "prior_rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Rows from the comparison period.",
            },
            "top_n": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": ["dimension"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        dim = str(args.get("dimension") or "").strip()
        if not dim:
            return ToolOutcome(ok=False, error="dimension is required")
        val_col = str(args.get("value_column") or "Total Amount")
        top_n = max(1, min(25, int(args.get("top_n") or 5)))
        current = args.get("current_rows")
        prior = args.get("prior_rows")
        if not isinstance(current, list):
            current = ctx.state.rows or []
        if not isinstance(prior, list):
            prior = []

        cur_groups = _group_sum(current, dim, val_col)
        pri_groups = _group_sum(prior, dim, val_col)
        cur_total = sum(r["value"] for r in cur_groups)
        pri_total = sum(r["value"] for r in pri_groups)
        movers = _movers(cur_groups, pri_groups, top_n=top_n)
        residual = (cur_total - pri_total) - sum(m["delta"] for m in movers)

        tree = {
            "dimension": dim,
            "value_column": val_col,
            "totals": {
                "current": cur_total,
                "prior": pri_total,
                "delta": cur_total - pri_total,
                "pct_change": ((cur_total - pri_total) / pri_total * 100.0)
                              if pri_total else None,
            },
            "top_movers": movers,
            "residual_delta": residual,
        }
        return ToolOutcome(
            ok=True,
            output=tree,
            state_updates={"causal_tree": tree},
        )


__all__ = ["CausalTreeTool"]
