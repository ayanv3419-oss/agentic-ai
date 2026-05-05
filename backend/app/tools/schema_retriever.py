"""SchemaRetriever — single source: app.database.schema.

No duplicated schema literal. SqlValidator and downstream consumers use
state.db_schema set here.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.database.schema import schema_dict
from app.state import TurnState
from app.tools.base import Tool, ToolResult


class SchemaRetrieverArgs(BaseModel):
    pass


class SchemaRetrieverTool(Tool):
    name = "SchemaRetriever"
    description = "Returns the canonical financial DB schema as a dict."
    args_model = SchemaRetrieverArgs
    independent = True

    async def run(self, state: TurnState, args: SchemaRetrieverArgs) -> ToolResult:
        schema = schema_dict()
        return ToolResult(ok=True, output=schema, state_updates={"db_schema": schema})
