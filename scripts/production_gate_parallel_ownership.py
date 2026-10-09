"""Integration of the separate holder and guardian identities into job ownership."""

import signal
from collections.abc import Mapping
from typing import Any


def signal_guardians(running: list[Any], problems: list[str]) -> None:
    """A failed sibling notification cannot undo a latched containment loss."""

    for item in tuple(running):
        try:
            view = item.process.guardian_status
            view.drain()
            if view.ready:
                item.owned.signal_root(signal.SIGTERM)
        except Exception as exc:  # noqa: BLE001 - retain failure and continue other owned jobs
            problems.append(f"{item.job_id}: stop notification failed: {type(exc).__name__}")


def latch_holder_loss(budget: Any, hub: Any, job_id: str, reason: str) -> None:
    """Commit loss before cleanup or receipt reading can raise; admission cannot reopen."""

    detail = f"{job_id}: {reason}"
    budget.mark_lost(detail)
    hub.lose(detail)


def observe_cgroup_membership(tree: Any) -> None:
    """Record membership only for a pinned, still-live guardian instance.

    H stays outside the job cgroup so it can sweep after cgroup.kill. G joins before
    launching W. The status pipe names G, but does not itself pin it: the lineage walk
    must first have adopted that exact instance. Membership is checked between two
    checks of the held pidfd/start pair to reject a recycled PID or unreadable process.
    """

    if tree.cgroup is None or tree.cgroup_joined:
        return
    target = (tree.root_pid, tree.root_start)
    if tree.holder_mode:
        tree.status.drain()
        target = tree.status.guardian
        if target is None:
            return
    pid, started = target
    entry = tree._entries.get(pid)
    if entry is None or entry[1] != started or not tree._parent_instance_valid(pid):
        return
    joined = tree.cgroup.contains(pid)
    if tree._parent_instance_valid(pid):
        tree.cgroup_joined = joined


def receipt_loss(
    status: Any, owned: Mapping[str, Any], guardian: Mapping[str, Any] | None
) -> str | None:
    """Containment loss closes sibling admission even when fail_fast is disabled."""

    loss = holder_loss(status, owned.get("root") or {})
    if loss:
        return loss
    if not (guardian and guardian.get("phase") == "final"):
        return "the job's guardian was lost"
    return None


def record_cgroup_receipt(tree: Any, guardian: Any, problems: list[str]) -> bool:
    """Keep authenticated G join evidence after a fast exit, before kernel cleanup.

    The caller has authenticated the final receipt against G's recorded instance.
    G's existing producer attempts and reads back the exact allocated cgroup before
    launching W. An empty error on that final receipt records that completed step;
    this does not assert that the cgroup was emptied or that H supplied G's identity.
    """

    if problems or not isinstance(guardian, Mapping) or tree.cgroup is None:
        return False
    expected = tree.status.guardian if tree.holder_mode else (tree.root_pid, tree.root_start)
    if (
        expected is None
        or expected != (guardian.get("guardian_pid"), guardian.get("guardian_start"))
        or tree.cgroup.path is None
        or guardian.get("phase") != "final"
        or guardian.get("contained") is not True
        or guardian.get("cgroup") != str(tree.cgroup.path)
        or guardian.get("cgroup_error") != ""
    ):
        return False
    tree.cgroup_joined = True
    tree.cgroup_membership_evidence = {
        "source": "authenticated-guardian-final",
        "guardian_pid": expected[0],
        "guardian_start": expected[1],
        "path": str(tree.cgroup.path),
    }
    return True


def holder_loss(status: Any, root: Mapping[str, Any]) -> str | None:
    """Read terminal holder loss before waiting on a finished root's cleanup."""

    holder = getattr(status, "holder", None)
    if (
        holder is not None
        and holder.verdict(root.get("pid"), root.get("start"))["proof"] is not True
    ):
        return "the job's holder was lost or its terminal containment proof is unproven"
    return None
