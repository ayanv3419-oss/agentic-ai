"""Hard limits — iterations, USD spend, SQL scan size."""
from __future__ import annotations

from app.config import settings
from app.state import TurnState


class CostGuardError(Exception):
    """Raised when a per-turn budget is exceeded."""


def check_loop_iteration(state: TurnState) -> None:
    if state.iteration >= settings.max_loop_iterations:
        raise CostGuardError(
            f"Loop iteration limit reached ({settings.max_loop_iterations})"
        )


def check_cost(state: TurnState) -> None:
    if state.cost_usd > settings.cost_limit_usd:
        raise CostGuardError(
            f"Cost limit exceeded: ${state.cost_usd:.4f} > ${settings.cost_limit_usd}"
        )


def check_sql_scan_estimate(estimated_bytes: int) -> None:
    if estimated_bytes > settings.sql_max_bytes_scanned:
        raise CostGuardError(
            f"SQL scan estimate too large: {estimated_bytes} bytes "
            f"> {settings.sql_max_bytes_scanned}"
        )
