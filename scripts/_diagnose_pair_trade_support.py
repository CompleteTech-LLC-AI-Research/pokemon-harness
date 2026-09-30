"""Generic bounded-diagnostic support split out of ``diagnose_pair_trade``.

Extracted verbatim from ``scripts/diagnose_pair_trade.py`` for #158 so the
driver's script stays under the 1000-line bound.  This module holds only the
exception hierarchy, value-coercion helpers, and the deadline/ cancellation
building blocks; it has no dependency on the driver module, so the driver can
import it without a cycle.
"""

from __future__ import annotations

import math
import signal
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any


class DiagnosticError(RuntimeError):
    """Base class for bounded diagnostic failures."""


class DeadlineExceeded(DiagnosticError):
    """The diagnostic exceeded its wall-clock deadline."""


class DiagnosticCancelled(DiagnosticError):
    """The operator requested cancellation with SIGINT or SIGTERM."""


class CaptureConfigurationError(DiagnosticError, ValueError):
    """The requested checkpoint selection cannot satisfy its bound."""


def _json_safe(value: object) -> object:
    """Return a conservative JSON representation for diagnostic values."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "repr": repr(value)[:400],
    }


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _finite_positive_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _normalise_capture_frames(values: Iterable[int] | None) -> tuple[int, ...]:
    """Validate and order requested post-frame ordinals."""

    if values is None:
        return ()
    normalised: set[int] = set()
    for value in values:
        ordinal = _nonnegative_int(value, "capture frame ordinal")
        # Frame zero is always captured explicitly, so treating a repeated
        # ``--capture-frame 0`` as an idempotent request is least surprising.
        if ordinal:
            normalised.add(ordinal)
    return tuple(sorted(normalised))


class _Deadline:
    """Monotonic wall deadline checked at every owned operation boundary."""

    def __init__(
        self,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds = _finite_positive_float(seconds, "deadline_seconds")
        self._clock = clock
        self.started = clock()
        self.deadline = self.started + self.seconds

    @property
    def elapsed(self) -> float:
        return max(0.0, self._clock() - self.started)

    def check(self, stage: str) -> None:
        now = self._clock()
        if now >= self.deadline:
            raise DeadlineExceeded(
                f"wall deadline of {self.seconds:g}s exceeded during {stage}"
            )


class _SignalCancellation:
    """Install temporary main-thread cancellation handlers.

    Python cannot forcibly interrupt a native extension that holds control for
    an unbounded time.  The handlers therefore provide bounded cooperative
    cancellation at Python owner boundaries; an external hard kill may still
    preempt both cleanup and final report emission.
    """

    def __init__(self) -> None:
        self.requested = False
        self.signum: int | None = None
        self.cleanup_started = False
        self._previous: dict[int, Any] = {}
        self._installed = False

    def _handler(self, signum: int, _frame: object) -> None:
        self.requested = True
        self.signum = signum
        if self.cleanup_started:
            return
        raise self._cancellation_error()

    def _cancellation_error(self) -> DiagnosticCancelled:
        if self.signum is None:
            return DiagnosticCancelled("diagnostic cancellation requested")
        try:
            name = signal.Signals(self.signum).name
        except ValueError:
            name = f"signal {self.signum}"
        return DiagnosticCancelled(f"diagnostic cancelled by {name}")

    def raise_if_requested(self) -> None:
        if self.requested and not self.cleanup_started:
            raise self._cancellation_error()

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        self._installed = True
        for candidate in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if candidate is None:
                continue
            self._previous[int(candidate)] = signal.getsignal(candidate)
            signal.signal(candidate, self._handler)

    def restore(self) -> None:
        if not self._installed:
            return
        try:
            for signum, handler in self._previous.items():
                signal.signal(signal.Signals(signum), handler)
        finally:
            self._installed = False
