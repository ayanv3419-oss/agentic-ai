"""
Side-effect import of every validator module so the
``@register_validator`` decorators run.
"""

from app.orchestrator_v2.validators import (  # noqa: F401
    aggregation_validator,
    business_rule_validator,
    chart_shape_validator,
    empty_result_validator,
    grounded_narrative_validator,
    schema_validator,
    sql_validator,
    timeframe_validator,
)

REGISTERED_VALIDATORS: tuple[str, ...] = (
    "sql_validator",
    "schema_validator",
    "empty_result_validator",
    "timeframe_validator",
    "chart_shape_validator",
    "aggregation_validator",
    "business_rule_validator",
    "grounded_narrative_validator",
)
