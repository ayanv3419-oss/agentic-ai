"""
Validator base class + registry + chain runner.

Validators are deterministic post-execution checks. They run **before**
the Critic on every reflection iteration, in collect-all mode (every
validator runs; all failures aggregated into one ``ValidationReport``).

A validator is a plain class with one synchronous ``validate(state)``
method returning a list of ``ValidationFailure``. Synchronous because
validators only read the in-memory state — no I/O, no LLM. The chain
runner is async-friendly so it can run in the same context as the
Executor + Critic.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import ClassVar, TypeVar

from app.orchestrator_v2.state import (
    ExecutionState,
    ValidationFailure,
    ValidationReport,
)

log = logging.getLogger("orchestrator_v2.validators")


class Validator(ABC):
    name: ClassVar[str] = ""

    @abstractmethod
    def validate(self, state: ExecutionState) -> list[ValidationFailure]:
        ...


T = TypeVar("T", bound=Validator)


class _ValidatorRegistry:
    def __init__(self) -> None:
        self._validators: list[Validator] = []
        self._lock = threading.Lock()
        self._names: set[str] = set()

    def register(self, validator: Validator) -> None:
        if not validator.name:
            raise ValueError(f"Validator {type(validator).__name__} has no .name")
        with self._lock:
            if validator.name in self._names:
                raise ValueError(f"Validator already registered: {validator.name!r}")
            self._validators.append(validator)
            self._names.add(validator.name)

    def all(self) -> tuple[Validator, ...]:
        return tuple(self._validators)

    @property
    def names(self) -> list[str]:
        return [v.name for v in self._validators]


_REGISTRY = _ValidatorRegistry()


def register_validator(cls: type[T]) -> type[T]:
    """Decorator. Instantiates + registers on the module-level registry."""
    _REGISTRY.register(cls())
    return cls


def get_validator_registry() -> _ValidatorRegistry:
    # Trigger side-effect imports of every validator module.
    import importlib
    if not _REGISTRY.names:
        try:
            importlib.import_module("app.orchestrator_v2.validators._bootstrap")
        except Exception:
            log.exception("validator bootstrap failed")
            raise
    return _REGISTRY


def run_validators(state: ExecutionState) -> ValidationReport:
    """
    Run every registered validator against the state. Aggregate all
    failures into one report. Never raises — a crashing validator
    emits a synthetic ``failed_tool`` ValidationFailure tagged with the
    validator's name.
    """
    registry = get_validator_registry()
    all_failures: list[ValidationFailure] = []
    for v in registry.all():
        try:
            failures = v.validate(state) or []
        except Exception as e:
            log.exception("validator %s crashed", v.name)
            failures = [
                ValidationFailure(
                    validator=v.name,
                    severity="warning",
                    aspect="failed_tool",
                    description=f"validator crashed: {type(e).__name__}: {e}"[:200],
                )
            ]
        all_failures.extend(failures)
    blocking = [f for f in all_failures if f.severity == "blocking"]
    return ValidationReport(
        passed=len(blocking) == 0,
        failures=tuple(all_failures),
    )


__all__ = [
    "Validator",
    "register_validator",
    "get_validator_registry",
    "run_validators",
]
