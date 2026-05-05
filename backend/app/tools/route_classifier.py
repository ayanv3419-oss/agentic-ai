"""RouteClassifier — keyword routing of the question into a route label.

Sets state.route ∈ { SALES_QUERY, PURCHASE_QUERY, RCA, FORECAST,
ANALYTICS, UNKNOWN }. Does NOT pick the sub-agent — that's the
coordinator's job. The route is metadata used by SqlPlanner / SqlWriter.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.state import TurnState
from app.tools.base import Tool, ToolResult


_RCA_KW       = ("why ", "why did", "what caused", "what's driving", "drop", "decline",
                 "fell", "decreased", "down ", "root cause", "rca")
_FORECAST_KW  = ("forecast", "predict", "projection", "next month", "next 30",
                 "next 7", "future", "upcoming")
_ANALYTICS_KW = ("trend", "compare", "comparison", "vs ", "versus", "growth",
                 "change over", "movement")
_PURCHASE_KW  = ("purchase", "purchases", "supplier", "vendor", "bought", "buy ")
_SALES_KW     = ("sale", "sales", "revenue", "income", "earned", "earnings",
                 "turnover", "order", "orders", "transaction", "customer",
                 "buyer", "product", "best seller", "top selling",
                 "how much", "how many", "total ", "this month", "last month",
                 "this week", "last week", "today", "yesterday")


class RouteClassifierArgs(BaseModel):
    pass


class RouteClassifierTool(Tool):
    name = "RouteClassifier"
    description = "Classifies the question into a coarse route label."
    args_model = RouteClassifierArgs
    independent = True

    async def run(self, state: TurnState, args: RouteClassifierArgs) -> ToolResult:
        q = (state.question or "").lower().strip()
        if not q:
            return ToolResult(ok=False, error="empty question")

        if any(k in q for k in _RCA_KW):
            route = "RCA"
        elif any(k in q for k in _FORECAST_KW):
            route = "FORECAST"
        elif any(k in q for k in _ANALYTICS_KW):
            route = "ANALYTICS"
        elif any(k in q for k in _PURCHASE_KW):
            route = "PURCHASE_QUERY"
        elif any(k in q for k in _SALES_KW):
            route = "SALES_QUERY"
        else:
            route = "UNKNOWN"

        return ToolResult(
            ok=True,
            output={"route": route},
            state_updates={"route": route},
        )
