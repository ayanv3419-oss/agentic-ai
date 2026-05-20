"""KPI execution engine — validates the KPI, runs its SQL, shapes the result.

Result shape:
    {
      "kpi": "profit_margin",
      "label": "Profit Margin",
      "category": "profit",
      "value": <number | None>,           # primary scalar (when applicable)
      "rows": [...]                       # full result set when distribution/list
      "output_type": "percent",
      "formula": "((Revenue - COGS) / Revenue) * 100",
      "explanation": "Gross profit as a % of revenue.",
      "sql_used": "SELECT ...",
      "required_columns": [...],
      "missing_columns": [...],           # empty list when valid
      "computed_at": "2025-...",
      "warnings": [...]                   # division-by-zero, null handling, etc.
    }

The engine never raises on a calculation error — it returns the dict with
`error` populated. Callers (AI runner, /kpi/calculate route) render the
error to the user.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.infrastructure import ALLOWED_TABLES, SCHEMA_COLUMNS, fetch_all
from app.kpi.registry import KpiRow, get_kpi
from app.time_engine import apply_date_tokens, resolve_dataset_date_tokens


_log = logging.getLogger("agentic_ai.kpi.engine")


@dataclass
class KpiResult:
    kpi: str
    label: str
    category: str
    value: float | int | None
    output_type: str
    formula: str
    explanation: str
    sql_used: str
    required_columns: list[str]
    missing_columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    computed_at: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    chart_supported: bool = False

    def to_dict(self) -> dict[str, Any]:
        """FULL developer payload — includes formula, sql_used, required_columns,
        stack/diagnostic fields. Used by /kpi/{id}/calculate (developer-facing)
        and by /errors. NEVER stream this to the chat frontend — use
        to_user_dict() instead."""
        return {
            "kpi":              self.kpi,
            "label":            self.label,
            "category":         self.category,
            "value":            self.value,
            "output_type":      self.output_type,
            "formula":          self.formula,
            "explanation":      self.explanation,
            "sql_used":         self.sql_used,
            "required_columns": self.required_columns,
            "missing_columns":  self.missing_columns,
            "rows":             self.rows,
            "computed_at":      self.computed_at,
            "warnings":         self.warnings,
            "error":            self.error,
            "chart_supported":  self.chart_supported,
        }

    def to_user_dict(self) -> dict[str, Any]:
        """USER-SAFE payload. Streamed to the chat bubble. Strips every
        technical / implementation detail:

          - No formula, no SQL, no required columns, no missing columns
          - No internal kpi id, no computed_at, no warnings text
          - No category internal name (kept business-friendly: "Sales", "Margin")
          - Rows reduced to {label, value} pairs only — chart-safe

        Intent: a non-technical executive viewing the chat sees a clean
        business answer and nothing more.
        """
        # Strip parenthetical suffixes like "(v2)", "(latest dataset day)",
        # "(dataset-relative)" that are internal jargon.
        clean_label = _strip_internal_suffix(self.label)

        # Reduce rows to label+value only, dropping any auxiliary columns.
        clean_rows: list[dict[str, Any]] = []
        for r in self.rows[:50]:    # cap to 50 so the chart payload stays small
            if "label" in r and "value" in r:
                clean_rows.append({"label": r["label"], "value": r["value"]})
            elif len(r) <= 5:
                # Distribution rows like payment-method breakdown — keep as-is
                # (already a small label→value map).
                clean_rows.append(dict(r))

        return {
            "metric":          clean_label,
            "value":           self.value,
            "format":          self.output_type,        # currency / percent / count / list
            "category_label":  _humanize_category(self.category),
            "rows":            clean_rows,
            "chartable":       self.chart_supported,
        }


def _strip_internal_suffix(label: str) -> str:
    """Remove any '(...)' parenthetical suffix so internal markers like
    '(v2)' / '(dataset-relative)' don't surface to the user."""
    if not label:
        return ""
    import re
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()
    return cleaned or label


_CATEGORY_LABELS = {
    "sales":           "Sales",
    "orders":          "Orders",
    "customers":       "Customers",
    "products":        "Products",
    "profit":          "Profit & Margin",
    "payments":        "Payments",
    "tax":             "Tax",
    "loyalty":         "Loyalty",
    "time":            "Time Trends",
    "hierarchy":       "Category Performance",
    "hierarchy_v2":    "Category Performance",
    "business_health": "Business Health",
    "dashboard":       "Headline Metrics",
}


def _humanize_category(category: str) -> str:
    return _CATEGORY_LABELS.get(category, "Analytics")


def _validate_required_columns(required: list[str]) -> list[str]:
    """Return the list of required canonical columns that are NOT in the
    sales/purchase schema. Empty list = all present."""
    available = set(SCHEMA_COLUMNS)
    missing = [c for c in required if c and c not in available]
    return missing


def _validate_sql_safety(sql: str) -> str | None:
    """Light sanity check. The SQL templates ship from us (not user input),
    but we still gate on:
      - must contain SELECT
      - must not mutate data
      - must reference only allowed tables
    """
    s = sql.strip().lower()
    if not s.startswith("select") and " select " not in s:
        return "SQL template must be a SELECT"
    bad_keywords = (
        " insert ", " update ", " delete ", " drop ", " alter ",
        " create ", " replace ", " attach ", " detach ", " pragma ",
    )
    padded = f" {s} "
    for kw in bad_keywords:
        if kw in padded:
            return f"SQL template contains disallowed keyword: {kw.strip()!r}"
    # Whitelist tables — the only tables we permit
    refs = {tbl for tbl in ALLOWED_TABLES if tbl in padded}
    if not refs:
        # Some templates use sub-queries / synthetic columns — that's OK as
        # long as no disallowed table is referenced.
        return None
    return None


def _friendly_kpi_error(exc: Exception, kpi_id: str) -> str:
    """Translate a raw SQL failure into a message that names the missing
    capability, instead of leaking 'no such table/column'.

    KPIs ship with SQL pinned to a specific uploaded-data shape
    (u_sales_transactions / u_inventory_master). When the current dataset
    does not match that shape the KPI cannot be computed — the user
    should see *why*, not a SQLite error string."""
    import re

    msg = str(exc)
    table = re.search(r"no such table:\s*([A-Za-z0-9_]+)", msg, re.I)
    if table:
        return (
            f"KPI '{kpi_id}' cannot be computed on the current data — it "
            f"requires a '{table.group(1)}' table, which this dataset does "
            f"not have. Upload data with that table to enable this metric."
        )
    column = re.search(r"no such column:\s*([A-Za-z0-9_.]+)", msg, re.I)
    if column:
        return (
            f"KPI '{kpi_id}' cannot be computed on the current data — it "
            f"requires a '{column.group(1)}' column, which this dataset "
            f"does not have. Upload data with that field to enable this "
            f"metric."
        )
    return f"{type(exc).__name__}: {exc}"


async def execute_kpi(kpi: KpiRow) -> KpiResult:
    """Run a single KPI. Never raises — errors land on result.error."""
    result = KpiResult(
        kpi=kpi.id,
        label=kpi.kpi_name,
        category=kpi.kpi_category,
        value=None,
        output_type=kpi.output_type,
        formula=kpi.formula_expression,
        explanation=kpi.description,
        sql_used=kpi.sql_template,
        required_columns=list(kpi.required_columns),
        chart_supported=kpi.chart_supported,
        computed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # 1. Column validation
    missing = _validate_required_columns(kpi.required_columns)
    if missing:
        result.missing_columns = missing
        result.error = (
            f"KPI '{kpi.id}' requires columns not in schema: {missing}"
        )
        return result

    # 2. SQL safety check
    safety = _validate_sql_safety(kpi.sql_template)
    if safety:
        result.error = f"SQL template rejected: {safety}"
        return result

    # 3. Dataset-relative time substitution.
    #    Templates may reference {dataset_today} / {dataset_month} / etc.
    #    apply_date_tokens replaces them with the current dataset values.
    #    If a template references a token and no data is uploaded yet, the
    #    placeholder remains and SQL execution fails — surfacing the issue.
    tokens = await resolve_dataset_date_tokens()
    sql_to_run = apply_date_tokens(kpi.sql_template, tokens)
    result.sql_used = sql_to_run

    # If template references tokens but no data exists yet, fail gracefully.
    if "{dataset_" in sql_to_run:
        result.error = (
            "KPI requires a dataset-relative date but no uploaded data is "
            "available yet."
        )
        return result

    # 4. Execute
    try:
        rows = await fetch_all(sql_to_run)
    except Exception as e:
        _log.exception("kpi execute failed: %s", kpi.id)
        result.error = _friendly_kpi_error(e, kpi.id)
        return result

    result.rows = rows

    # 4. Shape the scalar `value` when applicable
    if not rows:
        result.value = 0 if kpi.output_type in ("count", "currency", "ratio", "percent") else None
        result.warnings.append("No rows returned — dataset may be empty.")
        return result

    first = rows[0]
    # Single-scalar shape: `value` column directly
    if "value" in first and len(first) == 1:
        v = first["value"]
        result.value = _coerce_number(v)
    # Dashboard summary shape: pull total_sales as primary
    elif "total_sales" in first:
        result.value = _coerce_number(first.get("total_sales"))
    # Single-row label+value (top customer / top product): the one row's
    # value IS the scalar. A MULTI-row label+value set is a ranking /
    # distribution — leave result.value None; the data lives in rows.
    elif "value" in first and len(rows) == 1:
        result.value = _coerce_number(first["value"])
    else:
        result.value = None

    # 5. Heuristic warnings (division-by-zero / null surfaces)
    if result.value is not None and result.value == 0 and kpi.output_type == "percent":
        result.warnings.append("Result is 0 — denominator may be zero or no data matches.")

    return result


def _coerce_number(v: Any) -> float | int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        s = str(v).strip()
        if not s:
            return None
        if "." in s or "e" in s.lower():
            return float(s)
        return int(s)
    except (ValueError, TypeError):
        return None


async def calculate_by_name(name_or_id: str) -> KpiResult:
    """Convenience wrapper — looks up the KPI by id or display name, runs it."""
    kpi = await get_kpi(name_or_id)
    if kpi is None:
        return KpiResult(
            kpi=name_or_id,
            label=name_or_id,
            category="unknown",
            value=None,
            output_type="unknown",
            formula="",
            explanation="",
            sql_used="",
            required_columns=[],
            error=f"No KPI registered with id or name: {name_or_id!r}",
            computed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    return await execute_kpi(kpi)
