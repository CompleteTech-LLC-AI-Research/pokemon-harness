"""Managed job custody: no recovery authority, renewed deadline or extra wait owner."""

from __future__ import annotations

import copy
import os
import threading
from typing import Any


class CustodyError(RuntimeError):
    """An original capability cannot safely be used or disposed."""


class HubLossNotice:
    """Background callback capability is only a permanent notification bit."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def notify(self, _reason: str) -> None:
        self.event.set()


def hub_reason(hub: Any) -> str:
    # No resource operation or callback is permitted under the Hub lock.
    with hub._lock:
        return hub.closed_reason


def seal_report(report: dict[str, Any], hub: Any) -> None:
    reason = hub_reason(hub)
    if reason:
        report["overall"] = "FAIL"
        report["cancellation"] = (
            report.get("cancellation") or f"live arbiter closed admission: {reason}"
        )
        note = f"live arbiter closed admission: {reason}"
        if not any(reason in problem for problem in report["orchestration_problems"]):
            report["orchestration_problems"].append(note)
    # A NEW loss committed after this snapshot remains the B2 settlement gap.


def associated(slot: Any) -> bool:
    item, process, tree, status = slot.item, slot.process, slot.owned, slot.status
    return bool(
        item is not None
        and process is not None
        and tree is not None
        and status is not None
        and getattr(item, "launch", None) is slot
        and item.process is process
        and item.owned is tree
        and tree.root_pid == process.pid
        and getattr(process, "guardian_status", None) is status
        and tree.status is status
        and getattr(tree, "_custody_slot", None) is slot
        and getattr(status, "_custody_slot", None) is slot
    )


def usable(slot: Any) -> bool:
    return bool(
        associated(slot)
        and slot.initialized
        and slot.setup_complete
        and slot.custody_use == "active"
        and slot.custody_state == "holding"
        and (not slot.final_wait_attempted or slot.final_wait_complete)
    )


def active_item(item: Any) -> bool:
    slot = getattr(item, "launch", None)
    return slot is None or (slot.item is item and usable(slot))


def require_active(resource: Any) -> None:
    slot = getattr(resource, "_custody_slot", None)
    if slot is not None and (
        resource is not slot.owned and resource is not slot.status or not usable(slot)
    ):
        raise CustodyError("managed resource use is barred or its association is incoherent")


def require_legacy_close(tree: Any) -> None:
    if getattr(tree, "_custody_slot", None) is not None:
        raise CustodyError("managed pins require accepted checked disposition")


def poll_item(item: Any) -> Any:
    """Never repeat or upgrade the moved final observation, even inside a fallback."""
    slot = getattr(item, "launch", None)
    if slot is not None and not associated(slot):
        raise CustodyError("original observation association is incoherent")
    if slot is not None and slot.final_wait_attempted:
        return slot.final_wait_status if slot.final_wait_complete else None
    if not active_item(item):
        raise CustodyError("inactive original cannot be polled")
    if slot is not None and slot.root_context is not None:
        try:
            return slot.root_context.partition.job_poll(slot)
        except BaseException:
            slot.custody_use = "blocked"  # before cancellation/DeadlineWatch can reenter
            raise
    return item.process.poll()


def final_poll(item: Any) -> Any:
    slot = getattr(item, "launch", None)
    if slot is None:
        return item.process.poll()
    if not associated(slot):
        raise CustodyError("final observation association is incoherent")
    if slot.final_wait_attempted:
        return slot.final_wait_status if slot.final_wait_complete else None
    if not active_item(item):
        raise CustodyError("final observation requires the original active association")
    slot.final_wait_attempted = True  # before Popen can mutate/reap and then escape
    status = (
        slot.root_context.partition.job_poll(slot) if slot.root_context is not None
        else item.process.poll()
    )  # SAME original Popen, no wait/discovery substitute
    slot.final_wait_status = status
    slot.final_wait_complete = status is not None
    return status


def remove_active(item: Any, running: list[Any]) -> None:
    for index, other in enumerate(running):
        if other is item:
            del running[index]
            return


def retire_inactive(item: Any, running: list[Any], outcomes: dict[str, Any]) -> None:
    slot = getattr(item, "launch", None)
    if slot is not None and slot.custody_use == "active":
        slot.custody_use = "blocked"
    outcome = outcomes.get(item.job_id)
    if not isinstance(outcome, dict):
        outcome = getattr(slot, "pending_outcome", None) or {
            "job_id": item.job_id,
            "artifact_problems": [],
            "child_exit_status": getattr(slot, "final_wait_status", None),
        }
        outcomes[item.job_id] = outcome
    outcome["state"] = "failed"
    note = "managed job custody or original final observation remains unproven"
    if note not in outcome.setdefault("artifact_problems", []):
        outcome["artifact_problems"].append(note)
    outcome["reason"] = outcome.get("reason") or note
    remove_active(item, running)


def retained_outcome(item: Any) -> Any:
    slot = getattr(item, "launch", None)
    return slot.pending_outcome if slot is not None and slot.item is item else None


def restore_pending_outcomes(ledger: Any, outcomes: dict[str, Any]) -> None:
    """An escape between disposition and publication cannot erase prior job evidence."""
    for job_id, slot in ledger.slots.items():
        if job_id in outcomes or not isinstance(slot.pending_outcome, dict):
            continue
        outcome = slot.pending_outcome
        if outcome["state"] == "passed":
            outcome["state"] = "failed"
        note = "job disposition or publication was interrupted"
        outcome["reason"] = outcome.get("reason") or note
        outcome.setdefault("artifact_problems", []).append(note)
        outcomes[job_id] = outcome


def active_snapshot(running: list[Any], outcomes: dict[str, Any]) -> list[Any]:
    for item in tuple(running):
        if not active_item(item):
            retire_inactive(item, running, outcomes)
    return list(running)


def enter_finish(item: Any, status: int, running: list[Any], outcomes: dict[str, Any]) -> bool:
    if not active_item(item):
        retire_inactive(item, running, outcomes)
        return False
    slot = getattr(item, "launch", None)
    if slot is not None:
        slot.final_wait_attempted = True
        slot.final_wait_status = status  # already harvested by SAME original Popen
        slot.final_wait_complete = status is not None
    return True


def sweep_owned(item: Any, **bounds: Any) -> dict[str, Any]:
    if not active_item(item):
        raise CustodyError("inactive or uncertain original cannot start another sweep")
    if getattr(item, "launch", None) is not None:
        return item.owned.terminate(retain_pins=True, **bounds)
    return item.owned.terminate(**bounds)


def close_legacy_status(item: Any) -> None:
    if getattr(item, "launch", None) is None:
        item.process.guardian_status.close()


def fallback_record(item: Any, record: dict[str, Any]) -> bool:
    """True means reporting ONLY; no fallback use may follow a barrier/uncertain wait."""
    slot = getattr(item, "launch", None)
    if slot is None:
        return False
    if not active_item(item):
        if slot.custody_state != "disposed":
            tree = record.setdefault("process_tree", {})
            tree["proof"] = False
            tree["reason"] = "managed custody disposition or original wait is unproven"
        if slot.custody_state == "disposed" and slot.custody_eligibility is not None:
            record.setdefault("process_tree", copy.deepcopy(slot.custody_eligibility["receipt"]))
        record["custody_state"] = slot.custody_state
        return True
    return False


def retain_failed_status(item: Any, record: dict[str, Any]) -> bool:
    if getattr(item, "launch", None) is None:
        return False
    fallback_record(item, record)
    slot = item.launch
    if slot.custody_state != "disposed":
        record.setdefault("process_tree", {})["proof"] = False
    return True


def begin_status_close(view: Any, custody: Any) -> None:
    slot = getattr(view, "_custody_slot", None)
    if slot is None:
        if custody is not None:
            raise CustodyError("foreign status close permit")
        return
    transaction = slot.custody_dispositions.get("status", {})
    if not (
        custody is slot
        and associated(slot)
        and slot.status is view
        and slot.custody_use == "quiescent"
        and slot.custody_state == "disposing"
        and transaction.get("state") == "held"
        and transaction.get("fd") == view.fd
        and view.close_state == "open"
    ):
        raise CustodyError("managed status close permit absent or already consumed")
    transaction["state"] = "closing"  # consumed before actual GuardianStatus.close effect


def _close_pins(slot: Any) -> None:
    for pid, (descriptor, _started) in slot.custody_entries.items():
        transaction = slot.custody_dispositions[pid]
        if transaction["state"] != "held":
            raise CustodyError("pin disposition cannot resume")
        transaction["state"] = "closing"
        os.close(descriptor)
        transaction["state"] = "closed"


def dispose(item: Any, running: list[Any], receipt: Any, proven: bool) -> None:
    slot = getattr(item, "launch", None)
    if slot is None:
        return
    if not proven or not slot.final_wait_complete:
        receipt["proof"] = False
        return
    if not (
        active_item(item)
        and slot.initialized
        and slot.setup_complete
        and slot.pin_state == "tree"
        and slot.raw_pidfd is None
        and slot.status.fd >= 0
        and slot.status.close_state == "open"
    ):
        raise CustodyError("local disposition eligibility is not established")
    tree, view = slot.owned, slot.status
    root_entry = tree._entries.get(tree.root_pid)
    if root_entry is None or root_entry[1] != tree.root_start or root_entry[0] < 0:
        raise CustodyError("original held root pin is not available for disposition")
    # Retain the whole original map and status identity BEFORE flag/removal/detachment.
    slot.custody_entries = tree._entries
    slot.custody_dispositions = {
        pid: {"pid": pid, "start": start, "fd": fd, "state": "held"}
        for pid, (fd, start) in tree._entries.items()
    }
    slot.custody_dispositions["status"] = {"fd": view.fd, "state": "held"}
    slot.custody_eligibility = {
        "slot": slot,
        "process": slot.process,
        "tree": tree,
        "status": view,
        "root": (tree.root_pid, tree.root_start),
        "receipt": copy.deepcopy(receipt),
        "cleanup_cap": slot.current_cleanup_cap,
        "accepted": True,
    }
    slot.custody_use = "quiescing"  # FIRST irreversible active-use barrier
    try:
        remove_active(item, running)
        tree._entries = {}  # new live map; retained original map is metadata only
        if any(other is item for other in running) or not associated(slot):
            raise CustodyError("active-use barrier did not complete")
        slot.custody_use = "quiescent"
        slot.custody_state = "disposing"
        _close_pins(slot)
        view.close(custody=slot)  # ACTUAL GuardianStatus method consumes exact permit
        status = slot.custody_dispositions["status"]
        if view.close_state != "closed" or view.fd >= 0:
            raise CustodyError("status close did not commit known success")
        status["state"] = "closed"
        slot.custody_state = "disposed"
    except BaseException as error:
        # An interrupted syscall or success-before-commit stays unresolved forever.
        for transaction in slot.custody_dispositions.values():
            if transaction["state"] == "closing":
                transaction["state"] = "unknown"
        slot.errors.append(f"custody disposition unproven: {type(error).__name__}")
        if slot.pending_outcome is not None and slot.pending_outcome["state"] == "passed":
            slot.pending_outcome["state"] = "failed"
        raise


def finish_custody(
    item: Any, receipt: Any, proven: bool, outcome: Any, running: list[Any], service: Any
) -> None:
    service.consume_hub_loss()
    if service.cancel_reason and outcome["state"] == "passed":
        outcome["state"] = "failed"
        outcome["artifact_problems"].append(service.cancel_reason)
    slot = getattr(item, "launch", None)
    if slot is not None:
        slot.pending_outcome = outcome
    dispose(item, running, receipt, proven)
    service.consume_hub_loss()
    if service.cancel_reason and outcome["state"] == "passed":
        outcome["state"] = "failed"
        outcome["artifact_problems"].append(service.cancel_reason)


def stop_job(
    item: Any,
    *,
    running: Any,
    hub: Any,
    cleanup: Any,
    ports_seen: Any,
    close_handoff: Any,
    cleanup_proven: Any,
    stop_process: Any,
    reader: Any,
    record_cgroup_receipt: Any,
    clock: Any,
    sleeper: Any,
    grace_seconds: float,
    cleanup_seconds: float,
    owner_service: Any,
) -> None:
    if not active_item(item):
        fallback_record(item, cleanup.setdefault(item.job_id, {}))
        return
    view = item.process.guardian_status
    hub.expect_drop(item.job_id)
    deadline = min(
        item.cleanup_deadline if item.cleanup_deadline is not None else float("inf"),
        item.deadline + cleanup_seconds,
        clock() + cleanup_seconds,
    )
    item.cleanup_deadline = deadline
    stop_process(item, view, deadline, grace_seconds=grace_seconds, clock=clock, sleeper=sleeper)
    guardian, problems = view.read_receipts(
        item.receipt, item.secret, item.process.pid, item.owned.root_start, reader=reader
    )
    record_cgroup_receipt(item.owned, guardian, problems)
    # Caller supplies the original authority callback; its implementation is unchanged.
    receipt = sweep_owned(
        item, grace_seconds=grace_seconds, clock=clock, sleeper=sleeper, deadline=deadline
    )
    proven = cleanup_proven(receipt, guardian, view)
    if not proven:
        receipt["proof"] = False
    ports_seen[item.job_id] = set(item.owned.ports)
    record = cleanup.setdefault(item.job_id, {})
    record.update(process_tree=receipt, guardian=guardian or {})
    if getattr(item, "launch", None) is not None:
        final_poll(item)  # moved original outer observation, including failed proof
        owner_service.consume_hub_loss()
        dispose(item, running, receipt, proven)
        owner_service.consume_hub_loss()
    else:
        close_legacy_status(item)
    record.update(
        listener_handoff=close_handoff(item.job_id),
        arbiter=hub.unregister(item.job_id, cleanup_proven=receipt["proof"] is True),
    )
