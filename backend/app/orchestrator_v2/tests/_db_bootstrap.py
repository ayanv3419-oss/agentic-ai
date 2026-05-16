"""
DB schema bootstrap for the v2 test suites.

In production these run as part of ``core_system._startup`` (during
FastAPI app boot). Tests don't go through that boot path, so on a fresh
DB (CI's `/tmp/_ci_v2_test.db`, or any newly-set `FINANCIAL_DB_PATH`)
the v2 capability tests would hit:

    OperationalError: no such table: kpi_registry

This module idempotently creates the v1 schema + seeds the default KPI
catalog so capabilities like ``compute_kpi`` and ``run_data_query``
have something to work with. Safe to call any number of times.

Usage:
  - CLI:      python -m app.orchestrator_v2.tests._db_bootstrap
  - In code:  asyncio.run(bootstrap_v2_test_db())
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("orchestrator_v2.tests.bootstrap")


async def bootstrap_v2_test_db() -> None:
    """Mirror the relevant subset of core_system._startup so v2 tests
    can run against a fresh ``FINANCIAL_DB_PATH``. Never raises — logs
    + continues on any single failure so a partial bootstrap still
    leaves the DB more usable than an empty one."""

    # 1. Core schema (sales, purchase, uploads, hierarchy tables, ...).
    try:
        from app.infrastructure import init_database
        await init_database()
        log.info("bootstrap: init_database OK")
    except Exception:
        log.exception("bootstrap: init_database failed (continuing)")

    # 2. KPI registry table + seed the default catalog.
    try:
        from app.kpi import init_kpi_table, seed_default_catalog
        await init_kpi_table()
        await seed_default_catalog()
        log.info("bootstrap: KPI registry seeded")
    except Exception:
        log.exception("bootstrap: KPI seeding failed (continuing)")

    # 3. Hierarchy defaults (branch_master + product hierarchy v2 nodes).
    #    Optional for v2 tests but cheap and avoids surprises in
    #    breakdown_by_hierarchy.
    try:
        from app.hierarchy import seed_default_business
        await seed_default_business()
        log.info("bootstrap: hierarchy defaults seeded")
    except Exception:
        log.exception("bootstrap: hierarchy seed failed (continuing)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(bootstrap_v2_test_db())
    print("v2 test DB bootstrap complete")
