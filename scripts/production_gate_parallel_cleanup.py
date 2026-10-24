"""Deadline-bounded pidfd cleanup of a job's already established owned tree."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from typing import Any


def terminate_tree(
    tree: Any,
    *,
    grace_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    deadline: float | None = None,
    orphan_rounds: int = 3,
    late_kill_number: int = signal.SIGKILL,
    retain_pins: bool = False,
) -> dict[str, Any]:
    """Use one absolute wait bound; late stages cannot buy a fresh allowance."""

    from scripts.production_gate_parallel_processes import _exited

    deadline = clock() + 60.0 if deadline is None else deadline
    tree.pin_new()
    states_before = {
        pid: ("exited" if _exited(fd) else "alive") for pid, (fd, _) in tree._entries.items()
    }
    terminated = tree._signal(tree._targets(), signal.SIGTERM) if tree.contained else []
    limit = min(deadline, clock() + grace_seconds)
    while tree._targets() and clock() < limit:
        sleeper(min(0.02, max(0.0, limit - clock())))
    killed = tree._signal(tree._targets(), signal.SIGKILL)
    limit = min(deadline, clock() + max(grace_seconds, 1.0))
    while tree._targets() and clock() < limit:
        sleeper(min(0.02, max(0.0, limit - clock())))
    for _ in range(orphan_rounds):  # orphans born while the sweep ran
        tree.pin_new()
        late = tree._targets()
        if not late:
            break
        killed += tree._signal(late, late_kill_number)
        limit = min(deadline, clock() + max(grace_seconds, 1.0))
        while tree._targets() and clock() < limit:
            sleeper(min(0.02, max(0.0, limit - clock())))
    if tree.holder_mode and tree._alive_set():  # the holder finishes its own bounded sweep first
        limit = min(deadline, clock() + 2.0 * (grace_seconds + max(grace_seconds, 1.0)) + 3.0)
        while tree._alive_set() and clock() < limit:
            sleeper(min(0.02, max(0.0, limit - clock())))
        killed += tree._signal(tree._alive_set(), late_kill_number)
        limit = min(deadline, clock() + max(grace_seconds, 1.0))
        while tree._alive_set() and clock() < limit:
            sleeper(min(0.02, max(0.0, limit - clock())))
    emptied = False
    if tree.cgroup is not None and tree.cgroup_joined:
        emptied = tree.cgroup.kill(clock=clock, sleeper=sleeper, deadline=deadline)
        if emptied:
            tree.pin_new()
    tree._reap_orphans()
    remaining = sorted(tree._alive_set())
    surviving = sorted(tree.current_ports())
    root_entry = tree._entries.get(tree.root_pid)
    root_gone = root_entry is not None and _exited(root_entry[0])
    adoption = {
        "enabled": bool(tree.adoption is not None and tree.adoption.enabled),
        "at_launch": tree.launch_adopting,
        "at_proof": bool(tree.adoption is not None and tree.adoption.verify()),
        "root_gone": root_gone,
        "orphans_pinned": tree.orphans_pinned,
        "unattributed": sorted(tree.unattributed),
        "unknown_live": sorted(tree.unknown_live),
        "cgroup": {
            "configured": tree.cgroup is not None,
            "available": bool(tree.cgroup is not None and tree.cgroup.available),
            "reason": tree.cgroup.reason if tree.cgroup is not None else "not configured",
            "joined": tree.cgroup_joined,
            "membership_evidence": getattr(tree, "cgroup_membership_evidence", None),
            "emptied": emptied,
        },
    }
    adoption["proof"] = bool(
        (emptied and not remaining)
        or (
            adoption["at_launch"]
            and adoption["at_proof"]
            and root_gone
            and not tree.unattributed
            and not remaining
        )
    )
    if tree.cgroup is not None and (emptied or not tree.cgroup_joined):
        tree.cgroup.remove()  # an empty group is removed; a populated one is left, never forced
    receipt = {
        "tracked": len(tree._entries),
        "adopted_total": tree.adopted_total,
        "states_before": states_before,
        "terminated": terminated,
        "killed": killed,
        "remaining": remaining,
        "ports_seen": sorted(tree.ports),
        "surviving_ports": surviving,
        "contained": tree.contained,
        "reason": tree.reason,
        "adoption": adoption,
        "root": {"pid": tree.root_pid, "start": tree.root_start},
        "proof": tree.contained and not remaining and not surviving,
    }
    if not retain_pins:
        tree.close()
    return receipt
