"""Enrichment layers derived from the real sales data.

Two layers shipped here:

  • inventory.py — stock / velocity / status per SKU
  • forecast.py  — 14-day forward projection per SKU

Both are deterministic functions of the real `sales` table. The
transactional tables are read-only inputs; enrichment writes only to
`sku_inventory` and `sku_forecast`. Re-running the refresh is idempotent
— the tables are wiped + repopulated each time.
"""
from app.enrichment.inventory import (
    refresh_inventory,
    list_inventory,
    inventory_snapshot,
)
from app.enrichment.forecast import (
    refresh_forecast,
    list_forecast_for_sku,
    forecast_summary,
)
from app.enrichment.mock_products import (
    MOCK_FOOTWEAR_POOL,
    backfill_missing_product_names,
    mock_backfill_stats,
)
from app.enrichment.mock_costs import (
    backfill_quantities,
    cost_master_snapshot,
    list_product_costs,
    refresh_product_costs,
)

__all__ = [
    "MOCK_FOOTWEAR_POOL",
    "backfill_missing_product_names",
    "backfill_quantities",
    "cost_master_snapshot",
    "forecast_summary",
    "inventory_snapshot",
    "list_forecast_for_sku",
    "list_inventory",
    "list_product_costs",
    "mock_backfill_stats",
    "refresh_forecast",
    "refresh_inventory",
    "refresh_product_costs",
]
