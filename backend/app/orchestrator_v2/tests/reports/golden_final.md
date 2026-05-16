# Golden harness report - 1/6 passed

| Case | Passed | Outcome | Duration | Capabilities | Failures |
|---|---|---|---|---|---|
| `g001_revenue_total` | [FAIL] | accepted | 130ms | compute_kpi,narrate,format_response | answer contains no digit |
| `g002_orders_total` | [FAIL] | accepted | 28ms | compute_kpi,narrate,format_response | answer contains no digit |
| `g003_revenue_last_month` | [FAIL] | accepted | 24ms | compute_kpi,narrate,format_response | missing expected capabilities: ['resolve_time_window', 'run_data_query']; answer contains no digit |
| `g004_yoy_comparison` | [FAIL] | accepted | 30ms | compute_kpi,narrate,format_response | missing expected capabilities: ['compare_periods']; answer contains no digit |
| `g005_top_products` | [FAIL] | accepted | 21ms | compute_kpi,narrate,format_response | missing expected capabilities: ['run_data_query']; answer contains no digit |
| `g006_branch_clarification` | [OK] | accepted | 27ms | compute_kpi,narrate,format_response | - |

## Notes
- `g004_yoy_comparison`: requires P3 compare_periods body
- `g006_branch_clarification`: only fires when ≥2 enabled branches exist