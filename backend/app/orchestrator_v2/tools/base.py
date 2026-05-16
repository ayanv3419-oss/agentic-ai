"""
orchestrator_v2.tools.base
==========================

Abstract base classes for the v2 tool layer.

Two tiers:

* **Capability** — the Planner-visible operation. Strict-typed Pydantic
  args and output. The Planner emits a DAG of capability invocations;
  the Executor runs them. This is the only LLM-visible surface.

* **Primitive** — the existing 14 single-purpose tools from v1
  (``analytics_engine.Tool``). Reused inside capabilities; never exposed
  to the Planner. v2's ``primitives/`` package re-exports them so v2
  code never imports from ``app.analytics_engine`` directly.

Design notes (plan Q6 + Q7):

* Args and output schemas are real Pydantic models — they're serialized
  to JSON-schema for the Planner's prompt template via
  ``Capability.json_schema()``. That schema is the contract the LLM is
  trained to fill, so it must be deterministic.
* ``Capability.execute()`` mirrors v1's ``Tool.execute()`` — validate
  args, run, never raise, attach ``duration_ms``. This way the Executor
  can treat every capability uniformly.
* ``StepStatus`` and ``StepResult`` are imported from ``state`` so the
  Executor records uniform outcome records on the central state.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState

log = logging.getLogger("orchestrator_v2.tools")


# Generic args/output type vars so each Capability subclass can declare its
# own concrete Pydantic models and IDEs/mypy get full type inference.
A = TypeVar("A", bound=BaseModel)
O = TypeVar("O", bound=BaseModel)  # noqa: E741 — single-letter name matches generic convention.


# ===========================================================================
# Result envelope
# ===========================================================================


class CapabilityResult(BaseModel):
    """
    What a Capability returns from ``execute()`` — the Executor unpacks
    this into a ``StepResult`` for the central state.

    ``output`` carries the capability's typed output as a dict (after
    Pydantic ``.model_dump()``) so downstream validators and the Critic
    can inspect it without knowing the concrete output class.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0
    # Diagnostic breadcrumbs the Critic / monitoring may surface; not
    # passed to the Planner's next iteration directly.
    notes: tuple[str, ...] = ()


# ===========================================================================
# Capability ABC
# ===========================================================================


class Capability(ABC, Generic[A, O]):
    """
    Abstract Planner-visible operation.

    Subclasses declare:

      * ``name``         — stable identifier (e.g., ``"compute_kpi"``).
                           Must match the prompt vocabulary.
      * ``description``  — one-sentence summary for the Planner prompt.
      * ``args_model``   — Pydantic model for typed input.
      * ``output_model`` — Pydantic model for typed output.
      * ``run(state, args) -> O`` — the actual work.

    Subclasses MUST NOT raise from ``run``; if a runtime error is
    unavoidable, return a typed output that carries the error inside it
    (e.g., ``ok=False``) or let ``execute()`` capture the exception via
    its try/except wrapper.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    args_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    # Some capabilities have hard prerequisites (e.g., narrate requires
    # run_data_query). Declaring them here lets the Planner validate the
    # DAG before execution starts.
    requires: ClassVar[tuple[str, ...]] = ()

    # Capabilities that are pure (no DB / network / LLM) are safe to
    # parallelise without isolation concerns. The Executor uses this hint
    # when fanning out independent DAG branches.
    pure: ClassVar[bool] = False

    @abstractmethod
    async def run(self, state: ExecutionState, args: A) -> O:
        """Subclasses implement the actual work. See class docstring."""
        ...

    async def execute(
        self,
        state: ExecutionState,
        raw_args: dict[str, Any],
    ) -> CapabilityResult:
        """
        Validate args, run, never raise. Identical idiom to v1's
        ``Tool.execute`` so the Executor + monitoring code can treat
        both layers uniformly during the v1 → v2 transition.
        """
        start = time.perf_counter()

        # 1. Validate args against the declared schema.
        try:
            args = self.args_model(**(raw_args or {}))
        except Exception as e:
            log.warning(
                "capability %s arg validation failed: %s",
                self.name, e,
            )
            return CapabilityResult(
                ok=False,
                error=f"Invalid args for {self.name}: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
            )

        # 2. Run the body.
        try:
            output = await self.run(state, args)
        except NotImplementedError as e:
            # NotImplementedError is the legitimate signal from a P1 stub.
            # Treat it as a clean failure with a clear marker so the
            # Executor can record it without flagging an unhandled
            # exception in monitoring.
            log.info("capability %s is a stub: %s", self.name, e)
            return CapabilityResult(
                ok=False,
                error=f"not_implemented: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
                notes=("stub_capability",),
            )
        except Exception as e:
            log.exception(
                "capability %s crashed for turn %s",
                self.name, getattr(state, "turn_id", "<unknown>"),
            )
            return CapabilityResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
            )

        # 3. Pack the typed output as a dict.
        try:
            output_dict = output.model_dump()  # type: ignore[attr-defined]
        except Exception as e:
            log.exception(
                "capability %s returned a non-Pydantic output: %s",
                self.name, e,
            )
            return CapabilityResult(
                ok=False,
                error=f"output serialization failed: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
            )

        return CapabilityResult(
            ok=True,
            output=output_dict,
            duration_ms=(time.perf_counter() - start) * 1000,
        )

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """
        Schema entry for the Planner prompt — name, description, args
        JSON-schema. ``output_model`` is intentionally NOT exposed: the
        Planner cares about WHAT to call, not how the result is shaped
        (the Worker's narrate call gets the concrete result later).
        """
        try:
            args_schema = cls.args_model.model_json_schema()
        except Exception:
            args_schema = {"type": "object", "properties": {}}
        return {
            "name": cls.name,
            "description": cls.description,
            "args_schema": args_schema,
            "requires": list(cls.requires),
        }


# ===========================================================================
# Shared field types — kept in one place so capabilities reuse them and
# JSON-schema output stays consistent.
# ===========================================================================


class EmptyArgs(BaseModel):
    """For capabilities that take no inputs (rare; mostly diagnostic)."""

    model_config = ConfigDict(extra="forbid")


class StubOutput(BaseModel):
    """
    Placeholder output for capabilities whose body lands in a later phase.
    Carries enough metadata that the executor + Critic can recognise it
    without special-casing every stub.
    """

    model_config = ConfigDict(frozen=True)

    capability: str
    placeholder: bool = True
    note: str = Field(
        default="stub — implementation lands in a subsequent phase",
        max_length=200,
    )
    target_phase: str = Field(default="P2", max_length=8)


__all__ = [
    "Capability",
    "CapabilityResult",
    "EmptyArgs",
    "StubOutput",
]
