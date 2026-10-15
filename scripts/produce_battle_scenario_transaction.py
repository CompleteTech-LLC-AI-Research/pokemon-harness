"""Synchronous producer operation scopes; cooperative admission, not hard timeout.

Private contexts never enter the report schema. Capture borrows run's time origin
and transfers resource actions; a reentrant run owns a separate resource scope.
Cleanup may outlive the original deadline but cannot grant a new success window.
"""

from __future__ import annotations

import functools
import json
import math
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.produce_battle_scenario_model import CaptureBoundsExceeded, ScenarioRefusal


@dataclass
class _ObservationWatermark:
    last: float | None = None


@dataclass
class _OperationClock:
    clock: Callable[[], float]
    started: float = field(init=False)
    last: float = field(init=False)
    deadline: float | None = None
    observations: _ObservationWatermark = field(default_factory=_ObservationWatermark)

    def __post_init__(self) -> None:
        value = self.clock()
        if isinstance(value, bool) or not math.isfinite(value):
            raise ScenarioRefusal("operation clock origin must be finite")
        self._observe(value, "operation origin")
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

    def _observe(self, value: float, phase: str) -> None:
        previous = self.observations.last
        if (isinstance(value, bool) or not math.isfinite(value)
                or (previous is not None and value < previous)):
            raise ScenarioRefusal(f"nonfinite or backward operation clock during {phase}")
        # Shared by same-clock related operations, even when an inner failure is caught.
        self.observations.last = value

    def sample(self, phase: str) -> float:
        value = self.clock()
        self._observe(value, phase)
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
    recovery: Callable[[], Any] | None = None
    state: str = "registered"
    result: Any = None


@dataclass
class _OperationScope:
    timing: _OperationClock | None
    actions: list[_Action] = field(default_factory=list)
    cleanup: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def action(self, key, path, phase, callback, recovery=None) -> _Action:
        for action in self.actions:
            if action.key == key:
                return action
        action = _Action(key, path, phase, callback, recovery)
        self.actions.append(action)
        return action

    def publication(self, token: Any) -> _Action:
        return self.action(("publication", id(token)), token.path,
                           "publication rollback", token.withdraw_result,
                           token.recovery_metadata)

    def disarm(self, action: _Action) -> None:
        action.state = "disarmed"

    def perform(self, action: _Action) -> Any:
        """Claim before invoking, including an invocation that raises BaseException."""
        if action.state != "registered":
            return action.result
        action.state = "attempted"
        row = {"path": safe_text(action.path), "phase": action.phase,
               "disposition": "unresolved", "attempted": True}
        self.evidence.append(row)
        try:
            action.result = action.callback()
            result = action.result
            row["disposition"] = getattr(result, "disposition", "completed")
            row["observation"] = getattr(result, "observation", None)
            row["exception_type"] = getattr(result, "exception_type", None)
            row["cleanup"] = getattr(result, "cleanup", ())
            error = getattr(result, "error", None)
            if error is not None:
                row["error"] = safe_text(error)
            return result
        except BaseException as secondary:
            row["exception_type"] = type(secondary).__name__
            row["error"] = safe_text(secondary)
            raise
        finally:
            if action.recovery is not None:
                try:
                    row["recovery"] = action.recovery()
                except BaseException as diagnostic:  # noqa: BLE001 - independent diagnostics
                    row["recovery_diagnostic_failure"] = type(diagnostic).__name__

    def attempt(self, action: _Action, primary: BaseException) -> None:
        try:
            result = self.perform(action)
        except BaseException as secondary:  # noqa: BLE001 - remaining actions still run
            label = ("session.close() also failed" if action.phase == "session close"
                     else "producer cleanup action raised")
            safe_note(primary, label, secondary)
        else:
            try:
                error = getattr(result, "error", None)
                if error is not None:
                    safe_note(primary, "producer cleanup unresolved", error)
            except BaseException:  # noqa: BLE001 - result diagnostics cannot stop cleanup
                safe_note(primary, "producer cleanup result diagnostics failed")

    def rollback(self, primary: BaseException) -> None:
        with failure_cleanup(self):
            for action in reversed(self.actions):
                if action.state == "registered":
                    self.attempt(action, primary)
            try:
                self.failure_ledger(primary)
            except BaseException as diagnostic:  # noqa: BLE001 - ledger failure stays secondary
                self.evidence.append({"phase": "failure ledger", "disposition": "unresolved",
                                      "error": safe_text(diagnostic)})
                attach_evidence(primary, self.evidence)
                safe_note(primary, "producer failure ledger could not be attempted", diagnostic)

    def failure_ledger(self, primary: BaseException) -> None:
        """Best effort only, using an existing private prior-report directory."""
        directories = set()
        for row in self.evidence:
            recovery = row.get("recovery")
            if isinstance(recovery, dict) and isinstance(recovery.get("manifest"), str):
                directories.add(Path(recovery["manifest"]).parent)
        for directory in directories:
            ledger = None
            try:
                # Exclusive private sibling; canonical preparation manifest is never rewritten.
                ledger = directory / f"cleanup-{os.getpid()}-{time.monotonic_ns()}.json"
                descriptor = os.open(ledger, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"schema": 1, "scope": "failure cleanup attempt; not completion",
                               "deadline": self.timing.deadline if self.timing else None,
                               "actions": self.evidence}, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                # No power-loss guarantee; this is not a synced transaction journal.
                safe_note(primary, "producer failure ledger retained", str(ledger))
            except BaseException as secondary:  # noqa: BLE001 - retain primary/snapshot
                self.evidence.append({"phase": "failure ledger", "disposition": "unresolved",
                                      "path": safe_text(ledger),
                                      "exception_type": type(secondary).__name__,
                                      "error": safe_text(secondary)})
                safe_note(primary, "producer failure ledger incomplete", secondary)
        attach_evidence(primary, self.evidence)


def safe_text(value: Any) -> str:
    try:
        return str(value)
    except BaseException:  # noqa: BLE001 - hostile diagnostic objects are secondary
        return "<producer diagnostic unavailable>"


def nested_evidence(secondary: BaseException) -> tuple[dict[str, Any], ...]:
    try:
        return tuple(dict(row) for row in getattr(secondary, "producer_cleanup", ()))
    except BaseException as diagnostic:  # noqa: BLE001 - retain uncertainty, never mask cause
        return ({"phase": "nested cleanup evidence", "disposition": "unresolved",
                 "exception_type": type(diagnostic).__name__},)


def attach_evidence(primary: BaseException, rows: list[dict[str, Any]]) -> None:
    try:
        previous = getattr(primary, "producer_cleanup", ())
        primary.producer_cleanup = tuple(previous) + tuple(dict(row) for row in rows)
    except BaseException:  # noqa: BLE001 - no durable/in-memory evidence guarantee on failure
        safe_note(primary, "producer structured cleanup evidence could not be attached")


@contextmanager
def resource_scope():
    """A lower helper owns interruption-before-return and transfers on success."""
    parent = active_scope()
    scope = _OperationScope(parent.timing if parent is not None else None,
                            cleanup=parent.cleanup if parent is not None else False)
    try:
        token = _ACTIVE.set(scope)
        failure = None
        try:
            yield scope
        except BaseException as primary:
            failure = primary
            raise
        finally:
            reset_context(token, failure)
        if parent is not None:
            parent.actions.extend(scope.actions)
    except BaseException as primary:
        rollback_preserving(scope, primary)
        raise


def rollback_preserving(scope: _OperationScope, primary: BaseException) -> None:
    try:
        scope.rollback(primary)
    except BaseException as secondary:  # noqa: BLE001 - no diagnostic replaces the primary
        safe_note(primary, "producer rollback collector incomplete", secondary)
        attach_evidence(primary, scope.evidence)


_ACTIVE: ContextVar[_OperationScope | None] = ContextVar("producer_operation", default=None)


def active_scope() -> _OperationScope | None:
    return _ACTIVE.get()


def safe_note(primary: BaseException, label: str, detail: Any = None) -> None:
    """Diagnostic failures stay explicit without replacing the primary."""
    failure = None
    try:
        text = label if detail is None else f"{label}: {detail!r}"
    except BaseException as exc:  # noqa: BLE001 - even broken repr is secondary
        text = "producer diagnostic formatting failed"
        failure = type(exc).__name__
    try:
        primary.add_note(text)
    except BaseException as exc:  # noqa: BLE001 - continue remaining actions
        failure = type(exc).__name__
    if failure is not None:
        scope = active_scope()
        if scope is not None:
            scope.evidence.append({"phase": "diagnostic attachment", "disposition": "unresolved",
                                   "exception_type": failure,
                                   "error": "producer diagnostic could not be completed"})


@contextmanager
def failure_cleanup(scope: _OperationScope | None = None):
    owner = scope or active_scope()
    if owner is None:
        yield
        return
    previous = owner.cleanup
    token = _ACTIVE.set(owner)
    owner.cleanup = True
    failure = None
    try:
        yield
    except BaseException as primary:
        failure = primary
        raise
    finally:
        owner.cleanup = previous
        reset_context(token, failure)


def reset_context(token: Any, primary: BaseException | None = None) -> None:
    try:
        _ACTIVE.reset(token)
    except BaseException as secondary:
        if primary is None:
            raise
        safe_note(primary, "producer context reset failed", secondary)


def normal_guard(phase: str) -> float | None:
    scope = active_scope()
    if scope is None or scope.cleanup or scope.timing is None:
        return None
    return scope.timing.admit(phase)


def bind_operation(maximum: float, deadline: float | None = None) -> None:
    scope = active_scope()
    if scope is None or scope.timing is None:
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
        defaults = function.__kwdefaults__ or {}
        default_clock = defaults.get("clock", time.monotonic)

        @functools.wraps(function)
        def invoke(**kwargs):
            parent = active_scope()
            clock = kwargs.get("clock", default_clock)
            if parent is not None and parent.timing is not None and clock is not parent.timing.clock:
                raise ScenarioRefusal("nested operation clock domain differs from its owner")
            borrowed = kind == "capture" and parent is not None and parent.timing is not None
            timing = parent.timing if borrowed else _OperationClock(
                clock,
                deadline=parent.timing.deadline if parent is not None and parent.timing is not None else None,
                observations=parent.timing.observations if parent is not None and parent.timing is not None else _ObservationWatermark(),
            )
            if parent is not None and parent.timing is not None and not borrowed:
                if timing.started < parent.timing.last:
                    raise ScenarioRefusal("backward clock entering reentrant operation")
            scope = _OperationScope(timing)
            try:
                token = _ACTIVE.set(scope)
                failure = None
                try:
                    result = function(**kwargs)
                except BaseException as primary:
                    failure = primary
                    raise
                finally:
                    reset_context(token, failure)
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
                rollback_preserving(scope, primary)
                raise
        return invoke
    return decorate
