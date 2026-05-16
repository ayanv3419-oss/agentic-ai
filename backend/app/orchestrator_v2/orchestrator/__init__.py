"""
orchestrator/
=============

The control plane for a turn. Houses:

* ``planner.py``        — produces a typed Plan + delta plans
* ``executor.py``       — runs the Plan DAG (sequential + parallel branches)
* ``reflection_loop.py`` — bounded iteration controller (MAX = 3)
* ``confidence.py``     — confidence scoring + accept/reflect/escalate gate

These modules are filled in by phases P2 (executor + planner), P4 (reflection
loop), and P5 (confidence). For P0 (skeleton) they are present as imports
only.
"""
