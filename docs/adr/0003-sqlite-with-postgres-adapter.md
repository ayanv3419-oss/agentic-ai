# ADR-0003 — SQLite default with Postgres adapter

**Status:** Accepted  
**Date:** 2025-05-15

## Context

The project targets small businesses doing local/solo analytics. A heavy database like Postgres adds operational burden for simple deployments. But the system also needs to scale to multi-tenant SaaS.

## Decision

Use **SQLite** (`aiosqlite`, WAL mode) as the default database. When `DATABASE_URL` is set in the environment, an `asyncpg`-based Postgres adapter activates automatically. The `database/engine.py` abstraction layer means application code never references SQLite or Postgres directly.

## Consequences

- ✅ Zero-config local dev and single-user deployments
- ✅ Same codebase runs on SQLite (dev/solo) and Postgres (production/SaaS)
- ✅ WAL mode enables concurrent reads without blocking writes
- ⚠️ SQL must stay compatible with both SQLite and Postgres dialects (no Postgres-specific features)
- ⚠️ `data/financial_records.db` must be excluded from git (already in .gitignore)
