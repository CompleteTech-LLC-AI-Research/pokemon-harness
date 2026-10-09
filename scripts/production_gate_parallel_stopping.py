"""Broadcast owned stop requests and share the frozen absolute cleanup bound."""

import signal
from typing import Any


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
