"""Exact original wait bookkeeping; no identity discovery or new wait primitive."""

from __future__ import annotations

from typing import Any


class RootCustodyError(RuntimeError):
    """Uncertain ownership never authorizes a replacement waiter."""


class WaitPartition:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.records: dict[int, dict[str, Any]] = {}
        self.pins: dict[tuple[int, int], tuple[Any, int]] = {}
        self.adopted: dict[tuple[int, int, int], str] = {}

    def prepare(self, owner: Any, kind: str) -> None:
        key = id(owner)
        if key in self.records:
            raise RootCustodyError("original wait owner cannot be replaced")
        self.records[key] = {"owner": owner, "kind": kind, "process": None,
                             "state": "prepared", "status": None, "pin": None}

    def record(self, owner: Any) -> dict[str, Any]:
        record = self.records.get(id(owner))
        if record is None or record["owner"] is not owner:
            self.context.refuse("unregistered original wait owner")
            raise RootCustodyError(self.context.failure)
        return record

    def unknown(self, owner: Any, reason: str) -> None:
        record = self.records.get(id(owner))
        if record is not None and record["owner"] is owner:
            record["state"] = "unknown"
        self.context.refuse(reason)

    def publish_original(self, owner: Any, process: Any) -> None:
        record = self.record(owner)
        if record["state"] != "prepared" or owner.process is not process:
            self.unknown(owner, "original process publication is incoherent")
            raise RootCustodyError(self.context.failure)
        record["process"] = process  # no pid query/poll/callback here
        record["state"] = "ready"

    def owner_unknown(self, owners: Any) -> bool:
        return any(self.record(owner)["state"] in ("unknown", "attempting") for owner in owners)

    def unreaped_originals(self) -> set[int]:
        return {r["process"].pid for r in self.records.values()
                if r["process"] is not None and r["process"].returncode is None}

    def bind_holder(self, scope: Any, pin: Any) -> None:
        record = self.record(scope)
        if (scope.process is None or scope.holder_pin is not pin or not pin.validated or pin.closed
                or pin.instance is None or record["process"] is not scope.process
                or pin.instance[0] != scope.process.pid):
            self.unknown(scope, "original held holder association is incoherent")
            raise RootCustodyError(self.context.failure)
        self._claim_instance(scope, pin.instance, pin.descriptor)
        record["pin"] = pin
        record["instance"], record["descriptor"] = pin.instance, pin.descriptor

    def _claim_instance(self, owner: Any, instance: tuple[int, int], fd: int) -> None:
        prior = self.pins.get(instance)
        if prior is not None and (prior[0] is not owner or prior[1] != fd):
            self.context.refuse("one held instance has conflicting wait ownership")
            raise RootCustodyError(self.context.failure)
        self.pins[instance] = (owner, fd)  # retained fd metadata, never a close capability

    def job_poll(self, slot: Any) -> Any:
        record = self.record(slot)
        if record["kind"] != "job" or record["process"] is not slot.process:
            self.unknown(slot, "original job Popen owner differs")
            raise RootCustodyError(self.context.failure)
        if record["state"] == "consumed":
            return record["status"]
        if record["state"] != "ready":
            raise RootCustodyError("original job wait is unknown or already attempted")
        record["state"] = "attempting"
        try:
            status = slot.process.poll()  # SAME original Popen, not another waiter
        except BaseException:
            self.unknown(slot, "original job wait returned no proved disposition")
            raise
        record["status"] = status
        record["state"] = "ready" if status is None else "consumed"
        return status

    def holder_attempt(self, owner: Any) -> None:
        scope = owner.wait_scope
        record = self.record(scope)
        if (record["kind"] != "probe" or scope.owner is not owner
                or record["process"] is not scope.process
                or record["pin"] is not owner.holder_pin
                or scope.holder_pin is not owner.holder_pin
                or scope.root_pin is not owner.root_pin
                or not owner.holder_pin.validated or owner.holder_pin.closed
                or owner.holder_pin.instance != record["instance"]
                or owner.holder_pin.descriptor != record["descriptor"]
                or owner.holder_pin.parent != owner.root_pin.instance
                or self.context.closed or self.context.disposing
                or record["state"] != "ready"):
            self.unknown(scope, "original held holder waiter is not exclusive")
            raise RootCustodyError(self.context.failure)
        record["state"] = "attempting"  # BEFORE bound_descriptor/waitid

    def holder_result(self, owner: Any, status: int | None) -> None:
        record = self.record(owner.wait_scope)
        record["status"] = status
        record["state"] = "ready" if status is None else "consumed"

    def settled(self, owner: Any) -> None:
        self.record(owner)["state"] = "settled"

    def foreign_instances(self) -> set[tuple[int, int]]:
        result = set()
        for scope in self.context.scopes:
            pin = scope.holder_pin
            if pin is not None and pin.validated and not pin.closed and pin.instance is not None:
                result.add(pin.instance)
            owner = scope.owner
            if owner is not None:
                for pin in owner.custody.roles.values():
                    if pin.validated and not pin.closed and pin.instance is not None:
                        result.add(pin.instance)
        return result

    def foreign_unknown(self) -> bool:
        for scope in self.context.scopes:
            if not scope.spawn_attempted:
                continue
            record = self.record(scope)
            owner = scope.owner
            if record["state"] in ("unknown", "attempting") or scope.process is None:
                return True
            if scope.unadmitted_abort and owner is not None and not owner.peers:
                continue  # existing positive original pre-ACK no-descendant proof
            if (not scope.acknowledged or owner is None
                    or set(owner.custody.roles) != {"holder", "guardian", "payload"}):
                return True
        return False

    def orphan_disposition(self, tree: Any, pid: int, started: int) -> str:
        if (pid, started) in self.foreign_instances():
            return "foreign"
        if self.context.failure or self.foreign_unknown():
            self.context.refuse("foreign probe custody bars new adopted attribution")
            return "unknown"
        return "candidate"  # existing strict job proof must still establish ownership

    def note_owned_pin(self, tree: Any, pid: int, fd: int, started: int) -> None:
        if (pid, started) in self.foreign_instances():
            self.context.refuse("foreign probe instance reached a job pin owner")
            raise RootCustodyError(self.context.failure)
        self._claim_instance(tree, (pid, started), fd)
        self.adopted.setdefault((id(tree), pid, started), "ready")

    def adopted_attempt(self, tree: Any, pid: int, fd: int, started: int) -> bool:
        key = (id(tree), pid, started)
        if self.pins.get((pid, started)) != (tree, fd) or key not in self.adopted:
            self.context.refuse("adopted wait lacks its original job pin owner")
            tree.contained = False
            return False
        if self.adopted[key] != "ready":
            return False  # consumed/unknown entries never wait again
        self.adopted[key] = "attempting"
        return True

    def adopted_result(self, tree: Any, pid: int, started: int, state: str) -> None:
        self.adopted[(id(tree), pid, started)] = state
        if state == "unknown":
            tree.contained = False
            tree.reason = "original adopted wait remains unproven"
            self.context.refuse(tree.reason)


def tree_partition(tree: Any) -> WaitPartition | None:
    slot = getattr(tree, "_custody_slot", None)
    context = getattr(slot, "root_context", None)
    return context.partition if context is not None else None


def reap_owned(tree: Any, require_active: Any, exited: Any, os_module: Any) -> None:
    """Existing exact orphan loop, with state surviving its old error suppression."""
    require_active(tree)
    partition = tree_partition(tree)
    for pid, (descriptor, started) in list(tree._entries.items()):
        if pid == tree.root_pid or not exited(descriptor):
            continue
        if partition is not None and (
            pid not in tree._orphans
            or not partition.adopted_attempt(tree, pid, descriptor, started)
        ):
            continue
        try:
            result = os_module.waitid(
                os_module.P_PIDFD, descriptor, os_module.WEXITED | os_module.WNOHANG
            )
        except (OSError, AttributeError):
            if partition is not None:
                partition.adopted_result(tree, pid, started, "unknown")
            # Legacy suppression preserved, but no unknown-state retry/upgrade.
        except BaseException:
            if partition is not None:
                partition.adopted_result(tree, pid, started, "unknown")
            raise
        else:
            if partition is not None:
                partition.adopted_result(tree, pid, started,
                                         "ready" if result is None else "consumed")


def note_tree_pin(tree: Any, pid: int, fd: int, started: int) -> None:
    partition = tree_partition(tree)
    if partition is not None:
        try:
            partition.note_owned_pin(tree, pid, fd, started)
        except RootCustodyError:
            tree._custody_slot.custody_use = "blocked"
            raise


def orphan_disposition(tree: Any, pid: int, started: int) -> str:
    partition = tree_partition(tree)
    return "candidate" if partition is None else partition.orphan_disposition(tree, pid, started)


def allow_descendant_pin(tree: Any, pid: int, started: int) -> bool:
    partition = tree_partition(tree)
    if partition is None or (pid, started) not in partition.foreign_instances():
        return True  # verified own-parent proof is still required by _adopt
    tree.contained = False
    partition.context.refuse("foreign probe instance cannot become a job descendant")
    return False


def note_reparented(tree: Any, pid: int, parent: int, read_stat: Any) -> None:
    """An existing own pin can gain root wait duty without a new pin/discovery."""
    partition = tree_partition(tree)
    if partition is None or pid == tree.root_pid:
        return
    fd, started = tree._entries[pid]
    stat = read_stat(pid)
    if stat is None:
        tree._mark_unknown(pid)
        return
    if stat[0] == started and stat[1] == parent:
        partition.note_owned_pin(tree, pid, fd, started)
        tree._orphans.add(pid)  # exact already-owned instance is now root's child
