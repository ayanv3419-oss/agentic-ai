"""
v2 tests + golden harness.

Unit tests live alongside the module they cover (`test_planner.py`,
`test_executor.py`, `test_critic.py`, `test_reflection_loop.py`,
`test_validators.py`).

The golden harness (`golden_questions.py` + `harness.py`) runs a curated
set of representative questions through both v1 and v2 against a frozen
DB snapshot, diffs the outputs, and produces a Markdown report. This
harness is the acceptance gate for flipping ``ORCHESTRATOR_VERSION`` to
``v2`` in production.
"""
