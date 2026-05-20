"""Schema mapping — resolve canonical analytic concepts onto the columns
of arbitrarily-named uploaded workbooks, and build metric SQL from them.

See agentic-ai#1 (schema-portable margin & KPI computation).
"""
from app.schema_mapping.builder import MetricSqlBuilder
from app.schema_mapping.resolver import ColumnRef, ResolvedSchema, resolve_schema

__all__ = ["ColumnRef", "MetricSqlBuilder", "ResolvedSchema", "resolve_schema"]
