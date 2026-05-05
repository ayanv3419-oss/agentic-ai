"""ResponseStored — atomic write of state.response_record into response_store.json.

Cache structure: dict keyed by state.cache_key (sha256 of normalized question).
"""
from __future__ import annotations

from pydantic import BaseModel

from app.cache import put_cached
from app.state import TurnState
from app.tools.base import Tool, ToolResult, require


class ResponseStoredArgs(BaseModel):
    pass


class ResponseStoredTool(Tool):
    name = "ResponseStored"
    description = "Persists the formatted response record into data/response_store.json."
    args_model = ResponseStoredArgs
    independent = False

    async def run(self, state: TurnState, args: ResponseStoredArgs) -> ToolResult:
        miss = require(state, "response_record", "cache_key")
        if miss:
            return miss
        try:
            put_cached(state.cache_key or "", state.response_record or {})
        except Exception as e:
            return ToolResult(ok=False, error=f"persist failed: {type(e).__name__}: {e}")
        return ToolResult(
            ok=True,
            output={"stored": True, "cache_key": state.cache_key},
        )
