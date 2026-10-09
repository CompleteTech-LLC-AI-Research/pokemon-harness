"""Original success/cleanup bounds shared across every identity measurement stage.

This is a budget context, not a subprocess supervisor or containment proof. Probe
admission, output polling and cleanup must use these same bounds when implemented.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

PRE_DISPATCH_SECONDS = 1800.0
PRE_DISPATCH_CLEANUP_SECONDS = 60.0


class IdentityDeadline(RuntimeError):
    """Measurement exhausted its original budget; never a measured identity."""


def noop_checkpoint() -> None:
    """A utility callback without an active sibling supervisor."""


@dataclass(frozen=True)
class MeasurementBudget:
    deadline: float
    cleanup_deadline: float
    clock: Callable[[], float] = time.monotonic
    service: Callable[[], None] = noop_checkpoint
    cleanup_limit: Callable[[], float | None] = lambda: None
    _cleanup_seen: list[float] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.deadline) or not math.isfinite(self.cleanup_deadline):
            raise ValueError("measurement deadlines must be finite")
        if not self._cleanup_seen:
            self._cleanup_seen.append(self.cleanup_deadline)
        else:
            self._cleanup_seen[0] = min(self._cleanup_seen[0], self.cleanup_deadline)

    def cleanup_bound(self) -> float:
        tightened = self.cleanup_limit()
        if tightened is not None and not math.isfinite(tightened):
            raise IdentityDeadline("the shared cleanup bound is unknown")
        if tightened is not None:
            self._cleanup_seen[0] = min(self._cleanup_seen[0], tightened)
        return self._cleanup_seen[0]

    def now(self) -> float:
        now = self.clock()
        if not math.isfinite(now):
            raise IdentityDeadline("the measurement clock is unknown")
        return now

    def checkpoint(self) -> None:
        # Service the active owner before refusing this measurement's success.
        self.service()
        if self.now() >= min(self.deadline, self.cleanup_bound()):
            raise IdentityDeadline("identity measurement exceeded its frozen deadline")

    def cleanup_open(self) -> bool:
        """Cleanup may continue after success expires, within the same cleanup bound."""

        self.service()
        return self.now() < self.cleanup_bound()

    def cleanup_checkpoint(self) -> None:
        if not self.cleanup_open():
            raise IdentityDeadline("failure diagnostics exceeded the frozen cleanup deadline")

    def tighten(self, *, deadline: float, cleanup_deadline: float) -> MeasurementBudget:
        if not math.isfinite(deadline) or not math.isfinite(cleanup_deadline):
            raise ValueError("measurement deadlines must be finite")
        return replace(
            self,
            deadline=min(self.deadline, deadline),
            cleanup_deadline=min(self.cleanup_bound(), cleanup_deadline),
        )

    def with_checkpoint(self, checkpoint: Callable[[], None]) -> MeasurementBudget:
        if checkpoint in (noop_checkpoint, self.checkpoint, self.service):
            return self

        def service() -> None:
            self.service()
            checkpoint()

        return replace(self, service=service)


def pre_dispatch_budget(*, service: Callable[[], None] = noop_checkpoint) -> MeasurementBudget:
    """Explicit separate bound; callers share this one instance across pre-dispatch work."""

    started = time.monotonic()
    return MeasurementBudget(
        started + PRE_DISPATCH_SECONDS,
        started + PRE_DISPATCH_SECONDS + PRE_DISPATCH_CLEANUP_SECONDS,
        service=service,
    )
