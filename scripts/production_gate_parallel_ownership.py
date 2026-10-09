"""Integration of the separate holder and guardian identities into job ownership."""

from collections.abc import Mapping
from typing import Any


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


def holder_loss(status: Any, root: Mapping[str, Any]) -> str | None:
    """Read terminal holder loss before waiting on a finished root's cleanup."""

    holder = getattr(status, "holder", None)
    if (
        holder is not None
        and holder.verdict(root.get("pid"), root.get("start"))["proof"] is not True
    ):
        return "the job's holder was lost or its terminal containment proof is unproven"
    return None
