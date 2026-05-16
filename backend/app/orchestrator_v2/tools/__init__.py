"""
tools/
======

Two-tier tool layer.

* ``capabilities/``  — coarse, Planner-visible operations (~8). The Planner
                       emits a DAG of capability invocations; nothing else.
* ``primitives/``    — fine-grained deterministic tools (~14, ported from
                       ``app.analytics_engine``). Capabilities chain
                       primitives internally. Primitives are NEVER exposed
                       to the LLM.
* ``registry.py``    — singleton catalog + ``@register_capability`` decorator.
* ``base.py``        — abstract ``Capability`` and ``Tool`` classes.

The split is the central abstraction of v2: the LLM picks capabilities;
deterministic Python composes primitives. The LLM never sees primitives.
"""
