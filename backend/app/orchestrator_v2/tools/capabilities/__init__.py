"""
Planner-visible capabilities.

Importing this package triggers the ``@register_capability`` decorators
in each submodule, populating the singleton registry. Order is alphabetical
for deterministic registration; the registry does its own dedup check so
re-imports are safe.
"""

# Side-effect imports: each module's @register_capability runs at import
# time, populating the singleton registry.
from app.orchestrator_v2.tools.capabilities import (  # noqa: F401
    breakdown_by_hierarchy,
    compare_periods,
    compute_kpi,
    format_response,
    narrate,
    resolve_entities,
    resolve_time_window,
    run_data_query,
)

CAPABILITY_MODULES: tuple[str, ...] = (
    "resolve_time_window",
    "resolve_entities",
    "compute_kpi",
    "run_data_query",
    "compare_periods",
    "breakdown_by_hierarchy",
    "narrate",
    "format_response",
)
