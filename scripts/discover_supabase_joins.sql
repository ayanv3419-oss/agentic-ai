-- Run this whole file in the Supabase SQL editor (one shot, no params).
-- It returns FOUR result sets the assistant will use to design FK DDL.
--
-- 1) Every u_* table and its columns + data types + row counts.
-- 2) The current contents of `_relationships` (what the auto-detector found).
-- 3) Any FOREIGN KEY constraints that already exist on u_* tables.
-- 4) Candidate join-key pairs: columns whose normalised name appears in 2+
--    u_* tables, with distinct/overlap counts so we can spot the real keys.
--
-- Paste the result of EACH block back to the assistant.

-- =========================================================================
-- [1] Tables + columns + row counts
-- =========================================================================
WITH t AS (
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema = 'public'
    AND table_name LIKE 'u\_%' ESCAPE '\'
)
SELECT
  c.table_name,
  c.column_name,
  c.data_type,
  (SELECT n_live_tup FROM pg_stat_user_tables s
     WHERE s.relname = c.table_name) AS approx_rows
FROM information_schema.columns c
JOIN t ON t.table_name = c.table_name
WHERE c.column_name NOT IN ('_id','_batch_id','_source_sheet','_inserted_at')
ORDER BY c.table_name, c.ordinal_position;

-- =========================================================================
-- [2] Current contents of _relationships (auto-detector output)
-- =========================================================================
SELECT *
FROM _relationships
ORDER BY from_table, from_column;

-- =========================================================================
-- [3] Existing FOREIGN KEY constraints on u_* tables
-- =========================================================================
SELECT
  tc.table_name      AS from_table,
  kcu.column_name    AS from_column,
  ccu.table_name     AS to_table,
  ccu.column_name    AS to_column,
  tc.constraint_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema    = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON tc.constraint_name = ccu.constraint_name
 AND tc.table_schema    = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema    = 'public'
  AND (tc.table_name LIKE 'u\_%' ESCAPE '\'
       OR ccu.table_name LIKE 'u\_%' ESCAPE '\')
ORDER BY tc.table_name, kcu.column_name;

-- =========================================================================
-- [4] Candidate join-key pairs — same NORMALISED column name in 2+ u_* tables
--     Excludes obvious non-keys (id, name, date, qty, ...).
--     Returns each side's distinct-value count so we can pick a "dimension"
--     side (smaller, likely 1 row per key) vs "fact" side (larger, repeats).
-- =========================================================================
WITH u_cols AS (
  SELECT
    c.table_name,
    c.column_name,
    regexp_replace(lower(c.column_name), '[\s\-]+', '_', 'g') AS norm
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON c.table_schema = t.table_schema AND c.table_name = t.table_name
  WHERE t.table_schema = 'public'
    AND t.table_name LIKE 'u\_%' ESCAPE '\'
    AND c.column_name NOT IN ('_id','_batch_id','_source_sheet','_inserted_at')
    AND lower(c.column_name) NOT IN (
      'id','no','num','number','name','description','desc',
      'date','day','month','year','time','timestamp',
      'status','type','kind','category','label',
      'amount','value','qty','quantity','price','cost',
      'total','subtotal','tax','discount','rate',
      'notes','note','remarks','comment'
    )
),
shared AS (
  SELECT norm
  FROM u_cols
  GROUP BY norm
  HAVING COUNT(DISTINCT table_name) >= 2
)
SELECT
  u.table_name,
  u.column_name,
  u.norm AS normalised_key
FROM u_cols u
JOIN shared s USING (norm)
ORDER BY u.norm, u.table_name;
