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

        # Resolve the value column: explicit arg → resolved revenue concept
        # column → first numeric-looking column in the state rows → fallback
        # to "Total Amount" (legacy). The old hard-coded "Total Amount" caused
        # all CausalTree values to be 0.0 on u_* data that uses different names.
        val_col_arg = args.get("value_column")
        if val_col_arg and str(val_col_arg).strip():
            val_col = str(val_col_arg).strip()
        else:
            # Try to get the revenue column from the resolved schema.
            resolved = ctx.state.resolved_schema
            revenue_col: str | None = None
            if resolved is not None:
                ref = resolved.ref("revenue")
                if ref is not None:
                    revenue_col = ref.column
            if revenue_col:
                val_col = revenue_col
            else:
                # Last resort: inspect the state rows and pick the first
                # column whose values look numeric (non-zero float).
                rows_sample = (ctx.state.rows or [])[:5]
                fallback_col = "Total Amount"
                for row in rows_sample:
                    for k, v in row.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            fallback_col = k
                            break
                    else:
                        continue
                    break
                val_col = fallback_col

        top_n = max(1, min(25, int(args.get("top_n") or 5)))
        current = args.get("current_rows")
        prior = args.get("prior_rows")
        if not isinstance(current, list):
            current = ctx.state.rows or []
        if not isinstance(prior, list):
            prior = []

        cur_groups = _group_sum(current, dim, val_col)
        cur_total = sum(r["value"] for r in cur_groups)

        # ---- No prior period supplied → return a ranked breakdown, not a
        # degenerate "everything moved from zero" comparison.
        # The old behaviour (prior=[]) caused every item to have prior=0.0
        # and delta=current_value, producing a nonsense RCA tree.
        if not prior:
            ranked = sorted(cur_groups, key=lambda x: x["value"], reverse=True)
            tree = {
                "dimension": dim,
                "value_column": val_col,
                "prior_available": False,
                "note": (
                    "No prior-period rows were supplied. This is a ranked "
                    "breakdown of the current period only, not a comparison."
                ),
                "totals": {"current": cur_total, "prior": None, "delta": None,
                           "pct_change": None},
                "top_contributors": [
                    {
                        "key": r["key"],
                        "value": r["value"],
                        "share_pct": round(r["value"] / cur_total * 100, 2)
                                     if cur_total else None,
                    }
                    for r in ranked[:top_n]
                ],
                "top_movers": [],
                "residual_delta": None,
            }
            return ToolOutcome(
                ok=True,
                output=tree,
                state_updates={"causal_tree": tree},
            )

        # ---- Full comparison when prior rows exist -------------------------
        pri_groups = _group_sum(prior, dim, val_col)
        pri_total = sum(r["value"] for r in pri_groups)
        movers = _movers(cur_groups, pri_groups, top_n=top_n)
        residual = (cur_total - pri_total) - sum(m["delta"] for m in movers)

        tree = {
            "dimension": dim,
            "value_column": val_col,
            "prior_available": True,
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
