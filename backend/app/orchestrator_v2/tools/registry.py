"""
orchestrator_v2.tools.registry
==============================

Singleton catalog of v2 ``Capability`` instances + decorator-style
registration.

Usage in a capability module::

    from app.orchestrator_v2.tools.base import Capability
    from app.orchestrator_v2.tools.registry import register_capability

    @register_capability
    class ResolveTimeWindow(Capability):
        name = "resolve_time_window"
        ...

Then anywhere::

    from app.orchestrator_v2.tools.registry import get_capability_registry
    registry = get_capability_registry()
    registry.get("resolve_time_window")              # → Capability instance
    registry.json_schema_for_planner_prompt()        # → list[dict] for prompt

Design:

* The registry is **lazily bootstrapped** — the first call to
  ``get_capability_registry()`` imports
  ``app.orchestrator_v2.tools.capabilities`` to trigger each module's
  ``@register_capability`` side effect. This avoids ordering bugs
  between v2's package imports.
* Re-registering a name raises — capability names are stable identifiers
  in prompts and the cache key for partial-retry; duplication is a bug.
* The registry holds **instances**, not classes, so capabilities can
  cache per-process state (e.g., a prompt-template object).
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any, TypeVar

from app.orchestrator_v2.tools.base import Capability

log = logging.getLogger("orchestrator_v2.tools.registry")

T = TypeVar("T", bound=Capability)


class CapabilityRegistry:
    """In-memory catalog. One instance per process; not thread-stateful."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._lock = threading.Lock()

    def register(self, capability: Capability) -> None:
        if not capability.name:
            raise ValueError(
                f"Capability {type(capability).__name__} has no .name"
            )
        with self._lock:
            if capability.name in self._capabilities:
                raise ValueError(
                    f"Capability already registered: {capability.name!r}"
                )
            self._capabilities[capability.name] = capability
        log.debug("registered capability: %s", capability.name)

    def get(self, name: str) -> Capability:
        try:
            return self._capabilities[name]
        except KeyError:
            raise KeyError(f"Unknown capability: {name!r}") from None

    def has(self, name: str) -> bool:
        return name in self._capabilities

    @property
    def names(self) -> list[str]:
        return sorted(self._capabilities.keys())

    def all(self) -> tuple[Capability, ...]:
        return tuple(self._capabilities.values())

    def json_schema_for_planner_prompt(self) -> list[dict[str, Any]]:
        """
        Materialise the JSON-schema view the Planner prompt embeds.
        Stable ordering by name so prompt cache keys (when caches arrive
        in P5) hit reliably.
        """
        return [
            type(c).json_schema()
            for c in sorted(self._capabilities.values(), key=lambda c: c.name)
        ]


_REGISTRY: CapabilityRegistry | None = None
_REGISTRY_BOOT_LOCK = threading.Lock()


def register_capability(cls: type[T]) -> type[T]:
    """
    Decorator. Instantiates the capability class and registers it on the
    process-wide registry.

    Used at import-time inside ``tools/capabilities/<name>.py`` modules.
    """
    instance = cls()
    _get_registry_unbootstrapped().register(instance)
    return cls


def _get_registry_unbootstrapped() -> CapabilityRegistry:
    """
    Internal accessor that DOES NOT trigger bootstrap. Used by the
    decorator itself, since bootstrap imports the capabilities package
    (which would recurse).
    """
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_BOOT_LOCK:
            if _REGISTRY is None:
                _REGISTRY = CapabilityRegistry()
    return _REGISTRY


def get_capability_registry() -> CapabilityRegistry:
    """
    Public accessor — lazily bootstraps by importing the capabilities
    package. Safe to call repeatedly from any thread.
    """
    registry = _get_registry_unbootstrapped()
    if not registry.names:
        # Trigger side-effect imports of every capability module so the
        # @register_capability decorators run.
        try:
            importlib.import_module(
                "app.orchestrator_v2.tools.capabilities"
            )
        except Exception:
            log.exception("capability bootstrap failed")
            raise
    return registry


__all__ = [
    "CapabilityRegistry",
    "register_capability",
    "get_capability_registry",
]
