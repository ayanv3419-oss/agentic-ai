"""Centralized KPI registry + formula engine.

Public surface — call sites import from here, not the submodules.

Architecture:
    registry.py — SQLite-backed `kpi_registry` table; CRUD + seed catalog
    matcher.py  — natural-language alias matching against KPI names
    engine.py   — formula execution: validate columns, build SQL, run, format

Flow:
    user question
      → match_kpi(question)         → kpi row + confidence
      → execute_kpi(kpi row)        → validated SQL → SQLite → result dict
      → returned to caller (AI runner, dashboard, /kpi/calculate endpoint)
"""
from app.kpi.registry import (
    KPI_TABLE,
    KpiRow,
    init_kpi_table,
    list_kpis,
    get_kpi,
    upsert_kpi,
    disable_kpi,
    enable_kpi,
    seed_default_catalog,
    rebuild_catalog,
)
from app.kpi.matcher import KpiMatch, match_kpi
from app.kpi.engine import KpiResult, execute_kpi, calculate_by_name

__all__ = [
    "KPI_TABLE",
    "KpiMatch",
    "KpiResult",
    "KpiRow",
    "calculate_by_name",
    "disable_kpi",
    "enable_kpi",
    "execute_kpi",
    "get_kpi",
    "init_kpi_table",
    "list_kpis",
    "match_kpi",
    "rebuild_catalog",
    "seed_default_catalog",
    "upsert_kpi",
]
