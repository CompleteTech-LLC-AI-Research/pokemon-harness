"""Synchronous producer operation scopes; cooperative admission, not hard timeout.

Private contexts never enter the report schema. Capture borrows run's time origin
and transfers resource actions; a reentrant run owns a separate resource scope.
Cleanup may outlive the original deadline but cannot grant a new success window.
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from scripts.produce_battle_scenario_model import CaptureBoundsExceeded, ScenarioRefusal


@dataclass
class _OperationClock:
    clock: Callable[[], float]
    started: float = field(init=False)
    last: float = field(init=False)
    deadline: float | None = None

    def __post_init__(self) -> None:
        value = self.clock()
        if isinstance(value, bool) or not math.isfinite(value):
            raise ScenarioRefusal("operation clock origin must be finite")
        self.started = self.last = value
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ScenarioRefusal("operation deadline must be finite")

    def bind(self, maximum: float, deadline: float | None = None) -> None:
        if isinstance(maximum, bool) or not math.isfinite(maximum) or maximum <= 0:
            raise ScenarioRefusal("max_wall_seconds must be positive and finite")
        limit = self.started + maximum
        if not math.isfinite(limit):
            raise ScenarioRefusal("operation deadline arithmetic must be finite")
        for candidate in (deadline, self.deadline):
            if candidate is not None:
                if isinstance(candidate, bool) or not math.isfinite(candidate):
                    raise ScenarioRefusal("operation deadline must be finite")
                limit = min(limit, candidate)
        self.deadline = limit

    def sample(self, phase: str) -> float:
        value = self.clock()
        if isinstance(value, bool) or not math.isfinite(value) or value < self.last:
            raise ScenarioRefusal(f"nonfinite or backward operation clock during {phase}")
        self.last = value
        return value

    def admit(self, phase: str) -> float:
        now = self.sample(phase)
        if self.deadline is None:
            raise ScenarioRefusal("operation deadline has not been bound")
        if now >= self.deadline:
            raise CaptureBoundsExceeded(f"capture exceeded max_wall_seconds during {phase}")
        return now


@dataclass
class _Action:
    key: Any
    path: Any
    phase: str
    callback: Callable[[], Any]
    attempted: bool = False
    result: Any = None


@dataclass
class _OperationScope:
    timing: _OperationClock
    actions: list[_Action] = field(default_factory=list)
    cleanup: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def publication(self, token: Any) -> _Action:
        key = ("publication", id(token))
        for action in self.actions:
            if action.key == key:
                return action
        action = _Action(key, token.path, "publication rollback", token.withdraw)
        self.actions.append(action)
        return action

    def rollback(self, primary: BaseException) -> None:
        with failure_cleanup(self):
            for action in reversed(self.actions):
                if action.attempted:
                    continue
                action.attempted = True
                try:
                    action.result = action.callback()
                except BaseException as secondary:  # noqa: BLE001 - independent rollback
                    safe_note(primary, "producer cleanup failed", secondary)
                else:
                    if action.result is not None:
                        safe_note(primary, "producer cleanup result", action.result)


_ACTIVE: ContextVar[_OperationScope | None] = ContextVar("producer_operation", default=None)


def active_scope() -> _OperationScope | None:
    return _ACTIVE.get()


def safe_note(primary: BaseException, label: str, detail: Any = None) -> None:
    """Diagnostics must never replace the operational failure."""
    try:
        text = label if detail is None else f"{label}: {detail!r}"
    except BaseException:  # noqa: BLE001 - even broken exception repr is secondary
        text = "producer diagnostic formatting failed"
    try:
        primary.add_note(text)
    except BaseException:  # noqa: BLE001 - preserve the primary and remaining actions
        pass


@contextmanager
def failure_cleanup(scope: _OperationScope | None = None):
    owner = scope or active_scope()
    if owner is None:
        yield
        return
    previous = owner.cleanup
    token = _ACTIVE.set(owner)
    owner.cleanup = True
    try:
        yield
    finally:
        owner.cleanup = previous
        _ACTIVE.reset(token)


def normal_guard(phase: str) -> float | None:
    scope = active_scope()
    if scope is None or scope.cleanup:
        return None
    return scope.timing.admit(phase)


def bind_operation(maximum: float, deadline: float | None = None) -> None:
    scope = active_scope()
    if scope is None:
        raise ScenarioRefusal("capture operation scope is missing")
    scope.timing.bind(maximum, deadline)
    normal_guard("operation preparation")


def track_publication(token: Any) -> None:
    scope = active_scope()
    if scope is not None and token is not None:
        scope.publication(token)


def operation_entry(kind: str):
    """Keep facade signatures/call shapes while binding synchronous nested work."""
    def decorate(function):
        @functools.wraps(function)
        def invoke(**kwargs):
            parent = active_scope()
            clock = kwargs.get("clock", time.monotonic)
            if parent is not None and clock is not parent.timing.clock:
                raise ScenarioRefusal("nested operation clock domain differs from its owner")
            borrowed = kind == "capture" and parent is not None
            timing = parent.timing if borrowed else _OperationClock(
                clock, deadline=parent.timing.deadline if parent is not None else None
            )
            scope = _OperationScope(timing)
            try:
                token = _ACTIVE.set(scope)
                try:
                    result = function(**kwargs)
                finally:
                    _ACTIVE.reset(token)
                # No normal blocking work or context teardown follows this sample.
                now = timing.admit(f"{kind} final admission")
                elapsed = round(now - timing.started, 6)
                if kind == "capture":
                    result = {**result, "wall_seconds": elapsed,
                              "wall_seconds_scope": "operation origin through fixture admission"}
                    if borrowed:
                        parent.actions.extend(scope.actions)
                else:
                    result = {**result, "operation_timing": {
                        "wall_seconds": elapsed,
                        "scope": "operation origin through final admission",
                        "report_finalized": kwargs.get("report") is not None,
                    }}
                return result
            except BaseException as primary:
                scope.rollback(primary)
                raise
        return invoke
    return decorate
