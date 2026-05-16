"""
monitoring/
===========

Observability surface for v2.

  - ``tracing.py``    — span helpers (compatible with the existing
                        ``app.monitoring.instrumentation.instrument_tool``).
  - ``telemetry.py``  — SSE event emission helpers; centralises the v2
                        event taxonomy so wire-format changes happen in
                        one place.
  - ``metrics.py``    — counters + histograms (reflection iterations per
                        turn, capability latencies, confidence distribution).
  - ``audit.py``      — writes ``ExecutionState`` snapshots to the
                        ``execution_log`` SQLite table after every turn.

These compose existing infrastructure (Sentry init from
``app.monitoring.sentry_config``) rather than reinventing it.
"""
