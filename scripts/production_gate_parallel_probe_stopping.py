"""Nonblocking probe-scope escalation through exact retained instance handles.

Source only, not execution-admitted. Escalation does not prove adoption/reaping or
authorize releasing unresolved scope resources. No PID lookup, numeric fallback,
new cleanup budget, sleep, wait loop or foreign process discovery occurs here.
"""

from __future__ import annotations

import math
import signal
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.production_gate_parallel_probe_owner import RootProbeOwner


@dataclass
class ProbeStopState:
    grace_seconds: float
    started: float | None = None
    kill_bound: float | None = None
    term_sent: set[tuple[int, int]] = field(default_factory=set)
    kill_sent: set[tuple[int, int]] = field(default_factory=set)
    unknown_clock: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.grace_seconds) not in (int, float)
            or not math.isfinite(self.grace_seconds)
            or not 0 <= self.grace_seconds <= 5
        ):
            raise ValueError("probe stop grace must be finite and within the admitted range")


def advance_probe_stop(owner: RootProbeOwner) -> None:
    """One bounded signal attempt per held instance per pulse; retry refusals.

    The holder's six-stage grace matches the admitted node's own sweep allowance.
    Its first absolute kill bound is only tightened, including by sibling cleanup
    loss. Unknown clock/budget evidence fails the operation and refuses further
    success; it cannot postpone signalling or manufacture another allowance.
    """

    state = owner.stop_state
    try:
        now, cleanup = owner.budget.now(), owner.budget.cleanup_bound()
    except Exception as error:  # noqa: BLE001 - unknown clock cannot defer owned stop indefinitely
        owner.custody.refuse("probe stop budget unknown: " + type(error).__name__)
        state.unknown_clock = True
        now, cleanup = None, None
    if state.started is None and not state.unknown_clock:
        if not owner.custody.failures and now < min(owner.budget.deadline, cleanup):
            return
        state.started = min(now, owner.budget.deadline, cleanup)
        state.kill_bound = min(cleanup, state.started + 6 * (state.grace_seconds + 1))
        owner.custody.refuse("probe scope stop requested; success permanently refused")
    if not state.unknown_clock:
        state.kill_bound = min(state.kill_bound, cleanup)
    killing = state.unknown_clock or now >= state.kill_bound
    # Duplicate H pins share one exact instance. A broken binding may be replaced
    # only by an independently held, validated duplicate of that same instance.
    candidates = [owner.holder_pin, *owner.custody.roles.values()]
    groups = {}
    for record in candidates:
        if record.validated and not record.closed and record.instance is not None:
            groups.setdefault(record.instance, []).append(record)
    for instance, records in groups.items():
        for number, sent in ((signal.SIGTERM, state.term_sent), (signal.SIGKILL, state.kill_sent)):
            if instance in sent or (number == signal.SIGKILL and not killing):
                continue
            for record in records:
                try:
                    if record.exited():
                        break  # independently polled exit, not a signal/cleanup receipt
                    record.signal(number)
                    sent.add(instance)
                    break
                except Exception as error:  # noqa: BLE001 - try other exact held duplicates/siblings
                    owner.custody.refuse(
                        "probe held stop refused: " + type(error).__name__ + ": " + str(error)
                    )
