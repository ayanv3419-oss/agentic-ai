"""
validators/
===========

Deterministic post-execution checks. Run **before** the Critic on every
reflection iteration, in collect-all mode (every validator runs; all
failures aggregated into one ``ValidationReport``).

v1 validators (filled in during P3):

  - sql_validator           — denylist + parameterization (ports the regex
                              from analytics_engine.py:1863–1915)
  - schema_validator        — output shape matches expected Pydantic models
  - empty_result_validator  — rows == 0 must carry an explanation
  - timeframe_validator     — every data query has a resolved time window
  - chart_shape_validator   — chart payload matches the frontend's contract
  - aggregation_validator   — totals == sum(series) within tolerance
  - business_rule_validator — margin = revenue − cost; mock rows are flagged

Validators are auto-registered via the ``@register_validator`` decorator in
``base.py``. Adding a new validator is a one-file addition; no central
registry edits needed.
"""
