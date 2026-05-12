"""Product & Location hierarchy module.

Public surface for callers (KPI engine, AI runner, HTTP routes).
"""
from app.hierarchy.product import (
    classify_product,
    list_product_hierarchy,
    list_product_master,
    sync_product_master_from_data,
    PRODUCT_LEVELS,
)
from app.hierarchy.location import (
    create_branch,
    get_default_branch,
    list_branches,
    list_location_hierarchy,
    seed_default_business,
    LOCATION_LEVELS,
)
from app.hierarchy.clarification import (
    ClarificationRequest,
    detect_ambiguity,
)
from app.hierarchy.v2 import (
    V2_LEVELS,
    classify_to_v2,
    drilldown as v2_drilldown,
    list_sku_master,
    list_v2_tree,
    sync_product_sku_master,
)

__all__ = [
    "ClarificationRequest",
    "LOCATION_LEVELS",
    "PRODUCT_LEVELS",
    "V2_LEVELS",
    "classify_product",
    "classify_to_v2",
    "create_branch",
    "detect_ambiguity",
    "get_default_branch",
    "list_branches",
    "list_location_hierarchy",
    "list_product_hierarchy",
    "list_product_master",
    "list_sku_master",
    "list_v2_tree",
    "seed_default_business",
    "sync_product_master_from_data",
    "sync_product_sku_master",
    "v2_drilldown",
]
