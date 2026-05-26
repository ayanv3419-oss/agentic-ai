"""Schema mapping — resolve canonical analytic concepts onto the columns
of arbitrarily-named uploaded workbooks, and build metric SQL from them.

See agentic-ai#1 (schema-portable margin & KPI computation).
"""
from app.schema_mapping.builder import MetricNotComputable, MetricSqlBuilder
from app.schema_mapping.resolver import (
    CANONICAL_CONCEPTS,
    ColumnRef,
    Concept,
    Resolution,
    ResolvedSchema,
    concepts_of_kind,
    resolve_schema,
)

__all__ = [
    "CANONICAL_CONCEPTS",
    "ColumnRef",
    "Concept",
    "MetricNotComputable",
    "MetricSqlBuilder",
    "Resolution",
    "ResolvedSchema",
    "concepts_of_kind",
    "resolve_schema",
]
