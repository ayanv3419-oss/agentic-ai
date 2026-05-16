"""
orchestrator_v2.state
=====================

Frozen Pydantic data models that define the v2 contract.

This module is **pure data** — no I/O, no LLM calls, no SQL. Every other
v2 module imports types from here, and every state transition during a
turn returns a NEW ``ExecutionState`` via ``state.apply(**updates)``.

Design choices (see plan Q5 + Q7 + Q9):

* All models are frozen. State changes always allocate a new instance.
  This matches the idiom used by the legacy ``TurnState`` in
  ``analytics_engine.py:58–115`` and gives us an audit-friendly history.
* Enum-typed fields (``CriticIssue.aspect``, ``StepStatus``, ``Severity``,
  ``TimeWindow.granularity``) are ``Literal`` unions so JSON-mode LLM
  output is strictly validated.
* Tuple-of-models (not list-of-models) for collections, so the frozen
  guarantee is real — lists are mutable, tuples are not.
* ``ConfidenceScores.overall`` is a computed property (not a stored
  field) so it can never drift from the underlying components.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ===========================================================================
# Type aliases — Literal enums kept in one place so JSON-mode prompts and
# Pydantic validation share the same vocabulary.
# ===========================================================================

Severity = Literal["blocking", "warning"]
"""Used by both ``CriticIssue`` and ``ValidationFailure``."""

StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
"""Status of an individual capability invocation in the plan DAG."""

Granularity = Literal["hour", "day", "week", "month", "quarter", "year"]
"""Time-series bucket granularity carried on ``TimeWindow``."""

CriticAspect = Literal[
    "missing_timeframe",
    "missing_kpi",
    "missing_comparison",
    "missing_hierarchy_breakdown",
    "weak_reasoning",
    "vague_answer",
    "failed_tool",
    "contradictory_metrics",
    "empty_result_unexplained",
    "no_supporting_evidence",
    "inconsistent_business_logic",
    "chart_data_invalid",
]
"""
Closed enum of every issue the Critic may raise. The Planner has a
routing table from aspect → capability to add in a delta plan, so
introducing a new aspect requires updating both the prompt template AND
the routing table. The closed enum keeps that contract explicit.
"""


# ===========================================================================
# Small value objects
# ===========================================================================


class TimeWindow(BaseModel):
    """A resolved time range used by data-query / comparison capabilities."""

    model_config = ConfigDict(frozen=True)

    start_date: str          # ISO date 'YYYY-MM-DD'
    end_date: str            # ISO date 'YYYY-MM-DD' (inclusive)
    granularity: Granularity = "day"
    label: str | None = None  # human label like "last 30 days" for narration


class TokenLedger(BaseModel):
    """
    Running tally of LLM token consumption + estimated USD spend across
    all LLM calls in a turn. The reflection loop refuses to start a new
    iteration once ``cost_usd`` or ``input_tokens + output_tokens`` exceeds
    the per-turn budget defined in ``run.py``.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def with_addition(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> "TokenLedger":
        """Return a NEW ledger with one more call recorded (frozen idiom)."""
        return TokenLedger(
            input_tokens=self.input_tokens + max(0, input_tokens),
            output_tokens=self.output_tokens + max(0, output_tokens),
            cost_usd=self.cost_usd + max(0.0, cost_usd),
            calls=self.calls + 1,
        )


# ===========================================================================
# Plan — the Planner's typed output
# ===========================================================================


class PlanStep(BaseModel):
    """
    One node in the plan DAG.

    * ``step_id`` is a stable identifier the Critic and ValidationReport
      can refer back to ("the timeframe-resolve step failed").
    * ``depends_on`` lists step_ids that must complete before this step
      runs. Empty list = root step.
    * ``parallel_group`` is an optional tag the Executor uses to fan out
      independent branches concurrently (capped by
      ``MAX_PARALLEL_CAPABILITIES``).
    """

    model_config = ConfigDict(frozen=True)

    step_id: str
    capability: str                       # must be a registered capability name
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = None
    rationale: str | None = None          # the Planner's reason for picking this step


class Plan(BaseModel):
    """
    The Planner's output. Stable across reflection iterations EXCEPT for
    new steps appended by a delta plan; existing step_ids are never
    rewritten — the Executor's idempotent cache keys depend on it.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(default_factory=lambda: f"plan-{uuid.uuid4().hex[:12]}")
    root_question: str
    steps: tuple[PlanStep, ...] = ()
    created_at: float = Field(default_factory=time.time)
    iteration: int = 0                    # 0 for first plan, 1+ for delta plans

    def step(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None


# ===========================================================================
# Step execution result
# ===========================================================================


class StepResult(BaseModel):
    """
    What the Executor recorded after running one ``PlanStep``.

    The Executor is responsible for filling every field (even on failure
    paths) so downstream validators and the Critic can inspect a
    well-shaped record without ``None``-guards.
    """

    model_config = ConfigDict(frozen=True)

    step_id: str
    capability: str
    status: StepStatus
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: float = 0.0
    duration_ms: float = 0.0
    attempt: int = 1                      # bumped by retry_manager on transient retries


# ===========================================================================
# Validators — deterministic, pre-Critic
# ===========================================================================


class ValidationFailure(BaseModel):
    """One issue raised by a single validator."""

    model_config = ConfigDict(frozen=True)

    validator: str                        # e.g., "sql_validator"
    severity: Severity
    aspect: str                           # free string here so validators can
                                          # surface issues outside the Critic
                                          # enum; the Planner aspect→capability
                                          # routing table merges both sources
    description: str
    step_id: str | None = None            # the offending plan step, if applicable
    target_capability: str | None = None  # capability to add/rerun


class ValidationReport(BaseModel):
    """Aggregate result of running the full validator chain."""

    model_config = ConfigDict(frozen=True)

    passed: bool                          # True iff zero blocking failures
    failures: tuple[ValidationFailure, ...] = ()

    @property
    def blocking_failures(self) -> tuple[ValidationFailure, ...]:
        return tuple(f for f in self.failures if f.severity == "blocking")


# ===========================================================================
# Critic
# ===========================================================================


class CriticIssue(BaseModel):
    """One concern raised by the Critic AI."""

    model_config = ConfigDict(frozen=True)

    aspect: CriticAspect
    severity: Severity
    description: str = Field(..., max_length=240)
    target_capability: str | None = None


class CriticFeedback(BaseModel):
    """
    The Critic's structured verdict, enforced via JSON-mode + Pydantic
    validation. Critic NEVER produces the final business answer — only
    this object.
    """

    model_config = ConfigDict(frozen=True)

    is_acceptable: bool
    confidence: float = Field(..., ge=0.0, le=1.0)
    issues: tuple[CriticIssue, ...] = ()
    summary: str = Field(..., max_length=400)

    @property
    def blocking_issues(self) -> tuple[CriticIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "blocking")


# ===========================================================================
# Confidence scoring
# ===========================================================================


class ConfidenceScores(BaseModel):
    """
    Vector of 5 confidence dimensions plus a weighted aggregate. All five
    components are computed deterministically from ``ExecutionState``;
    no extra LLM call is made.

    Default weights (overridable in run.py from env / config):

      completeness 0.30 — Critic's view of "did we answer it?"
      data         0.20 — was the underlying data adequate?
      tool         0.20 — did the capabilities succeed?
      validation   0.20 — did deterministic checks pass?
      reasoning    0.10 — Critic's self-confidence (least trustworthy)
    """

    model_config = ConfigDict(frozen=True)

    completeness: float = Field(..., ge=0.0, le=1.0)
    data: float = Field(..., ge=0.0, le=1.0)
    tool: float = Field(..., ge=0.0, le=1.0)
    validation: float = Field(..., ge=0.0, le=1.0)
    reasoning: float = Field(..., ge=0.0, le=1.0)

    weight_completeness: float = 0.30
    weight_data: float = 0.20
    weight_tool: float = 0.20
    weight_validation: float = 0.20
    weight_reasoning: float = 0.10

    @property
    def overall(self) -> float:
        """Weighted average. Always in [0, 1] given component constraints."""
        total = (
            self.completeness * self.weight_completeness
            + self.data * self.weight_data
            + self.tool * self.weight_tool
            + self.validation * self.weight_validation
            + self.reasoning * self.weight_reasoning
        )
        # Normalise in case the weights don't sum to 1 (config edit safety).
        weight_sum = (
            self.weight_completeness + self.weight_data + self.weight_tool
            + self.weight_validation + self.weight_reasoning
        )
        return total / weight_sum if weight_sum > 0 else 0.0


# ===========================================================================
# Request context — handoff from core_system to v2
# ===========================================================================


class RequestContext(BaseModel):
    """
    Minimal envelope produced by core_system.py for any orchestrator
    version. v1 builds a ``TurnState`` from this; v2 builds an
    ``ExecutionState``.

    Deliberately small — anything bigger than this belongs INSIDE the
    orchestrator, not at the dispatch seam.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    request_id: str
    question: str
    conversation_id: str | None = None
    groq_api_key: str                     # already validated by core_system


# ===========================================================================
# ExecutionState — the v2 master state
# ===========================================================================


class ExecutionState(BaseModel):
    """
    The single source of truth for a turn under orchestrator_v2.

    Lifecycle:
      1. ``from_request_context()`` is called by ``run_query_turn_v2`` to
         build the initial state.
      2. Every stage (Planner, Executor, Validator chain, Critic) returns
         a NEW ``ExecutionState`` via ``apply(**updates)``.
      3. ``audit.py`` snapshots the final state into the ``execution_log``
         SQLite table for replay/debugging.

    The shape is intentionally rich — observability is more valuable than
    payload-size pruning at this stage. ``monitoring.audit`` strips fields
    that don't need to be persisted before INSERT.
    """

    model_config = ConfigDict(frozen=True)

    # ----- Identity ---------------------------------------------------------
    turn_id: str = Field(default_factory=lambda: f"turn-{uuid.uuid4().hex[:12]}")
    request_id: str
    conversation_id: str | None = None
    question: str
    normalized_question: str = ""

    # ----- Plan + execution -------------------------------------------------
    plan: Plan | None = None
    executed_steps: tuple[StepResult, ...] = ()

    # ----- Reflection -------------------------------------------------------
    reflection_iteration: int = 0
    validation_history: tuple[ValidationReport, ...] = ()
    critic_history: tuple[CriticFeedback, ...] = ()
    confidence: ConfidenceScores | None = None

    # ----- LLM accounting ---------------------------------------------------
    token_usage: TokenLedger = Field(default_factory=TokenLedger)

    # ----- Narration --------------------------------------------------------
    draft_answer: str | None = None
    final_answer: str | None = None
    chart_payload: dict[str, Any] | None = None

    # ----- Outcome ----------------------------------------------------------
    outcome: Literal["pending", "accepted", "escalated", "error"] = "pending"
    error_message: str | None = None

    # ----- Wall-clock -------------------------------------------------------
    started_at: float = Field(default_factory=time.time)

    # ----- Idioms -----------------------------------------------------------

    def apply(self, **updates: Any) -> "ExecutionState":
        """Frozen update — returns a NEW state. Matches TurnState's idiom."""
        return self.model_copy(update=updates)

    def append_step_result(self, result: StepResult) -> "ExecutionState":
        """Convenience for the Executor."""
        return self.apply(executed_steps=self.executed_steps + (result,))

    def append_validation(self, report: ValidationReport) -> "ExecutionState":
        return self.apply(validation_history=self.validation_history + (report,))

    def append_critic(self, feedback: CriticFeedback) -> "ExecutionState":
        return self.apply(critic_history=self.critic_history + (feedback,))

    @property
    def latest_validation(self) -> ValidationReport | None:
        return self.validation_history[-1] if self.validation_history else None

    @property
    def latest_critic(self) -> CriticFeedback | None:
        return self.critic_history[-1] if self.critic_history else None

    @classmethod
    def from_request_context(cls, ctx: RequestContext) -> "ExecutionState":
        return cls(
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            question=ctx.question,
            normalized_question=ctx.question.strip().lower(),
        )


__all__ = [
    # Type aliases
    "Severity",
    "StepStatus",
    "Granularity",
    "CriticAspect",
    # Value objects
    "TimeWindow",
    "TokenLedger",
    # Plan
    "PlanStep",
    "Plan",
    "StepResult",
    # Validation
    "ValidationFailure",
    "ValidationReport",
    # Critic
    "CriticIssue",
    "CriticFeedback",
    # Confidence
    "ConfidenceScores",
    # Top-level state
    "RequestContext",
    "ExecutionState",
]
