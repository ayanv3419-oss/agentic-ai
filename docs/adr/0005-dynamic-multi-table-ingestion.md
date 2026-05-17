# ADR-0005 — Dynamic multi-table ingestion + always-on charts

**Status:** Accepted
**Date:** 2026-05-17

## Context

The system shipped assuming users would upload data that fits the legacy
**sales / purchase** schema — POS-export columns like `Date`, `Total Amount`,
`Party Name`, `Payment Type`, `BHARAT PAY`, `HDFC BANK`. `ALLOWED_TABLES` was
a hardcoded `("sales", "purchase")` tuple; `SCHEMA_SPEC` declared 20 fixed
columns; `HEADER_ALIASES` mapped raw POS headers onto those canonicals; and
the upload route accepted ONE file → ONE sheet → ONE of those two tables.

That assumption broke on the first user who uploaded a real multi-domain
workbook (`gpt data all in one.xlsx`):

- **6 sheets**: `Sale Report`, `Item Details`, `Inventory_Master`,
  `Sales_Transactions`, `Product_Hierarchy`, `Transaction_Hierarchy`.
- **Each sheet has a totally different shape** — the inventory sheet has 14
  stock-management columns (`sku_id`, `stock_qty`, `unit_cost`, `warehouse`,
  `supplier`, `launch_season` …); the transactions sheet has 24 SKU-level
  columns (`region`, `state`, `brand`, `gst`, `net_sales` …); the hierarchy
  sheet has 7 catalog columns.
- The xlsx parser was already capable of multi-sheet workbooks but
  silently picked ONE sheet (whichever scored best against the legacy
  required-columns list) and dropped the other 5.
- ~30 of the user's 50 test questions are about inventory, hierarchy,
  warehouse, brand, category — dimensions that physically did not exist in
  the legacy schema and got refused by the `missing_data` classifier with a
  "we don't track that" template (and no chart).
- Even questions that DID match a registered KPI returned `chart: null`
  because the KPI fast-path was wired without a chart payload.

## Decision

Introduce a **parallel dynamic-tables pipeline** that runs alongside the
legacy sales/purchase path, plus a hard guarantee that every data-bearing
response carries a chart.

### 1. Dynamic ingestion (`app/dynamic_ingest.py`)

A new module that ingests an entire xlsx workbook — every sheet becomes its
own SQLite table:

- **Naming**: sheet `Sales_Transactions` → table `u_sales_transactions`. The
  `u_` prefix is mandatory and guarantees no collision with any system
  table (`sales`, `purchase`, `product_hierarchy`, `sku_inventory`, …).
- **Header detection** is liberal — any row with ≥3 string-looking cells
  followed (across blank rows) by a row with at least one non-string value
  is treated as a header. Handles real-world junk-row exports (the user's
  workbook has "Generated on Apr 25, 2026", a blank, then "Banks" on the
  first three rows of `Sale Report`).
- **Column types** are inferred from the first 200 data rows (REAL >
  INTEGER > TEXT). Date strings stay TEXT, normalized to ISO `YYYY-MM-DD`
  on insert.
- **Re-upload semantics** = REPLACE (chosen by the user during planning):
  each upload drops + recreates the matching `u_*` tables. Tables from
  past uploads with non-matching sheet names are left alone.
- **Persistence**: the table catalog is written to `data/dynamic_tables.json`
  so the LLM prompt and SQL validator see the same set across restarts.

### 2. New endpoint `POST /upload_workbook`

Accepts a single multi-sheet xlsx, loops every sheet through dynamic
ingestion, bumps `data_version` (invalidating the response cache), records
one audit row in `uploads`, and returns a per-sheet summary
(`{ingested: [...], skipped: [...], tables: [...], total_rows}`). The
legacy `POST /upload` with `target=sales|purchase` is unchanged.

### 3. New LLM capability `query_user_table`

Registered as the 6th coarse capability the agentic loop exposes
(`resolve_time_window`, `resolve_entities`, `run_data_query`,
**`query_user_table`**, `generate_narrative`, `google_drive`). The LLM
writes a `SELECT` against a `u_*` table directly — the tool validates
(SELECT-only, no semicolons, identifiers must come from the dynamic
registry, scan estimate counted toward the cost guard) and executes.
Crucially, it AUTO-BUILDS a chart from the result using the LLM-supplied
`chart_x_column` + `chart_y_column` aliases, in the same `SalesChart`
shape the frontend `ChatChart` already renders.

### 4. LLM context

`_schema_summary_text()` now appends every dynamic table with its column
names so the LLM can write correct SQL without round-tripping through a
schema-discovery tool. The system prompt is rewritten to tell the LLM:
"use `run_data_query` for the legacy sales/purchase tables, use
`query_user_table` for ANY u_* table" and gives explicit routing examples
("low stock" → `u_inventory_master`, "by region" → `u_sales_transactions`,
…). Format is column-names-only (no types) so the prompt stays small enough
for the 12k TPM rate limit.

### 5. Charts are always emitted when data exists

Three gates that previously emitted `chart: null` are fixed:

- **KPI fast-path** now builds a chart from `kpi_result.to_user_dict()` —
  a time-series if a `series`/`breakdown` field exists, a single-bar
  ranking otherwise.
- **Agentic loop final emit** falls back to a chart derived from
  `state.rows` (via `_build_dynamic_chart`) when the loop ended without
  populating `state.chart_data`.
- **`missing_data` classifier** suppresses the inventory / location /
  category / cost_profit / supplier_cost labels whenever any dynamic
  table is registered. With u_* tables present, the LLM + raw-SQL
  capability are far better judges of feasibility than a static keyword
  list, so those questions reach the loop instead of a hardcoded refusal.

### 6. KPI fast-path skipped when dynamic tables exist

The shipped KPI registry queries the legacy schema. With dynamic tables
present, the right data lives in `u_*` and the KPI fast-path would return
stale or empty answers. The fast-path is now bypassed (one LLM call cost
instead of zero) whenever `list_dynamic_tables()` is non-empty.

## Consequences

- ✅ A single `POST /upload_workbook` ingests every sheet of a real
  multi-domain xlsx. The user can ask about inventory, hierarchy,
  warehouses, brands, categories — all powered by their own data.
- ✅ Every data-bearing answer carries a chart payload the frontend
  renders (ranking / trend / summary).
- ✅ No collision with the legacy sales/purchase pipeline — the `u_`
  prefix is a hard separator.
- ✅ Re-upload is predictable (REPLACE the per-sheet tables, keep others).
- ⚠️ The legacy v2 orchestrator (`app/orchestrator_v2/`) does NOT yet
  understand `u_*` tables; clients that route to v2 (default for
  `/query_stream` without `X-Orchestrator-Version: v1`) still hit the v2
  KPI fast-path and miss dynamic data. Follow-up work: port the
  `query_user_table` capability into `orchestrator_v2/tools/capabilities/`
  and apply the same KPI-fast-path skip in `front_door.py`.
- ⚠️ Every non-shortcut answer is now an LLM call (no KPI fast-path when
  dynamic tables exist). Token budget per question rises from ~0 to ~7-10k
  on Groq's free tier — needs rate-limit handling on the client side until
  the user upgrades to dev tier.
- ⚠️ Raw-SQL by the LLM is whitelist-validated against the dynamic
  registry but does not parameterize string literals the LLM may include
  (e.g. `WHERE category = 'Footwear'`). Treat as MVP — no production
  multi-tenant deployment without further SQL hardening.

## Out of scope (deliberately deferred)

- Removing the `Sales_Transactions` data duplication between
  `u_sales_transactions` and the legacy `sales` table (the user can still
  load that sheet via the legacy `/upload?target=sales` path if they want
  the polished KPI engine over it, with the existing column-aliasing).
- Cross-table joins via the LLM (single-table SELECTs only for now —
  the schema prompt does not encourage joins, and the validator's
  identifier whitelist is per-table not per-join).
- Frontend UI for the `/upload_workbook` endpoint. The existing Upload
  page still calls `/upload?target=...`; callers wanting dynamic
  ingestion currently use `/upload_workbook` directly (curl / scripted
  tests).
