"""Broadcast owned stop requests and share the frozen absolute cleanup bound."""

import signal
from typing import Any


class DeadlineWatch:
    """Keep every sibling's original deadline live during bounded cleanup sleeps.

    Requests are broadcast without blocking on another job. Readiness checks recur
    during each wait; no timeout receives a cleanup allowance after its sibling's
    cleanup has already consumed that allowance.
    """

    def __init__(
        self,
        running: list[Any],
        hub: Any,
        cleanup_seconds: float,
        may_cancel: Any,
        *,
        grace_seconds: float,
        clock: Any,
        sleeper: Any,
        problems: list[str],
    ) -> None:
        self.running, self.hub = running, hub
        self.cleanup_seconds, self.may_cancel = cleanup_seconds, may_cancel
        self.grace_seconds, self.clock, self.sleeper = grace_seconds, clock, sleeper
        self.problems = problems
        self.expired: set[str] = set()
        self.notified: set[str] = set()
        self.readiness: dict[str, float] = {}
        self.fail_fast_reason = ""

    def pulse(self) -> None:
        now = self.clock()
        expired = [
            item
            for item in self.running
            if not item.stop_requested and item.process.poll() is None and now >= item.deadline
        ]
        if expired:
            self.expired.update(item.job_id for item in expired)
            if any(self.may_cancel(item) for item in expired):
                self.fail_fast_reason = (
                    self.fail_fast_reason or f"fail_fast: {expired[0].job_id} timed out"
                )
                selected = self.running[:]
            else:
                selected = expired
            deadline = min(item.deadline + self.cleanup_seconds for item in expired)
            for item in selected:
                item.cleanup_deadline = (
                    min(item.cleanup_deadline, deadline)
                    if item.cleanup_deadline is not None
                    else deadline
                )
                if item.process.poll() is None:
                    item.stop_requested = True
                    self.hub.expect_drop(item.job_id)
        for item in tuple(self.running):
            if not item.stop_requested or item.job_id in self.notified:
                continue
            if item.process.poll() is not None:
                self.notified.add(item.job_id)
                continue
            limit = self.readiness.setdefault(
                item.job_id, min(item.cleanup_deadline, now + self.grace_seconds + 2.0)
            )
            try:
                view = item.process.guardian_status
                view.drain()
                ready = view.ready or view.eof or now >= limit
            except Exception as exc:  # noqa: BLE001 - failed observation cannot defer owned cleanup
                self.problems.append(
                    f"{item.job_id}: stop readiness unproven: {type(exc).__name__}"
                )
                ready = True
            if ready:
                try:
                    item.owned.signal_root(signal.SIGTERM)
                except Exception as exc:  # noqa: BLE001 - keep trying other owned siblings
                    self.problems.append(
                        f"{item.job_id}: deadline stop failed: {type(exc).__name__}"
                    )
                self.notified.add(item.job_id)

    def sleep(self, seconds: float) -> None:
        self.pulse()
        now = self.clock()
        upcoming = [
            item.deadline
            for item in self.running
            if not item.stop_requested and item.process.poll() is None
        ]
        duration = (
            min(seconds, 0.01, max(0.0, min(upcoming) - now)) if upcoming else min(seconds, 0.01)
        )
        self.sleeper(duration)
        self.pulse()


def prepare_stops(
    items: list[Any], hub: Any, deadline: float, *, grace_seconds: float, clock: Any, sleeper: Any
) -> None:
    """Ask every live job to stop before waiting on any one job's teardown.

    Already completed roots remain unsignalled and are harvested with their observed
    terminal statuses. A job selected for cancellation is kept for the interrupted
    row even when its stop completes during another job's cleanup.
    """

    pending = []
    for item in tuple(items):
        item.cleanup_deadline = (
            min(item.cleanup_deadline, deadline) if item.cleanup_deadline is not None else deadline
        )
        if item.process.poll() is None:
            item.stop_requested = True
            hub.expect_drop(item.job_id)
            pending.append(item)
    deadline = min([deadline] + [item.cleanup_deadline for item in pending])
    ready_limit = min(deadline, clock() + grace_seconds + 2.0)
    while pending:
        for item in pending[:]:
            view = item.process.guardian_status
            view.drain()
            if item.process.poll() is not None or view.ready or view.eof or clock() >= ready_limit:
                item.owned.signal_root(signal.SIGTERM)
                pending.remove(item)
        if pending:
            sleeper(min(0.01, max(0.0, ready_limit - clock())))


def stop_process(
    item: Any, view: Any, deadline: float, *, grace_seconds: float, clock: Any, sleeper: Any
) -> None:
    """Readiness, cooperative stop and subsequent sweep share one absolute deadline."""

    ready_limit = min(deadline, clock() + grace_seconds + 2.0)
    while item.process.poll() is None and clock() < ready_limit:
        view.drain()
        if view.ready or view.eof:
            break
        sleeper(min(0.01, max(0.0, ready_limit - clock())))
    item.owned.signal_root(signal.SIGTERM)
    scale = 6 if item.owned.holder_mode else 3
    limit = min(deadline, clock() + scale * (grace_seconds + 1.0))
    while item.process.poll() is None and clock() < limit:
        sleeper(min(0.02, max(0.0, limit - clock())))
