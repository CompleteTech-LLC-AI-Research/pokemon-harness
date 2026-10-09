"""Containment helpers of the concurrent orchestrator (#107 post-Astra repairs).

Cleanup completeness is explicit evidence, never a diagnostic string, and nested evidence that cannot
be interpreted is a failed job with a diagnostic, never an exception that aborts the aggregation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from typing import Any


def cleanup_proven_for(launched: Collection[str], cleanup: Mapping[str, Any], job_id: str) -> bool:
    """A job that never launched owes no cleanup; a launched one needs an explicit proved record."""

    if job_id not in launched:
        return True
    record = cleanup.get(job_id)
    tree = record.get("process_tree") if isinstance(record, Mapping) else None
    return isinstance(tree, Mapping) and tree.get("proof") is True


def cleanup_completeness(
    launched: Collection[str], cleanup: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """(complete, unproven job ids): complete only when EVERY launched job has a proved record."""

    unproven = sorted(j for j in launched if not cleanup_proven_for(launched, cleanup, j))
    return not unproven, unproven


def note_unproven_cleanup(
    launched: Collection[str], cleanup: Mapping[str, Any], problems: list[str]
) -> bool:
    """One diagnostic per launched job without proved cleanup; True only when every job is proved."""

    complete, unproven = cleanup_completeness(launched, cleanup)
    for job_id in unproven:
        note = f"{job_id}: cleanup unproven; reservation retained"
        if note not in problems:
            problems.append(note)
    return complete


def fail_finish(
    outcome_record: Callable[..., dict[str, Any]],
    plan: Any,
    item: Any,
    status: int,
    exc: BaseException,
    outcomes: dict[str, dict[str, Any]],
    cleanup: dict[str, dict[str, Any]],
    running: list[Any],
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """An error inside finish fails that job closed: its cleanup stays unproven, siblings go on."""

    if item in running:
        running.remove(item)
    job_id = item.job_id
    reason = f"finish failed closed: {type(exc).__name__}: {str(exc)[:160]}"
    published = outcomes.get(job_id)
    try:
        if isinstance(published, dict):
            # Whatever was already published (even a pass), an error in finish makes it a failure;
            # earlier failures, partial evidence and artifacts stay in the row.
            published["state"] = "failed"
            earlier = published.get("artifact_problems")
            published["artifact_problems"] = [
                *(earlier if isinstance(earlier, list) else []),
                reason,
            ]
            published["reason"] = (
                f"{published['reason']}; {reason}" if published.get("reason") else reason
            )
        else:
            outcomes[job_id] = outcome_record(
                plan,
                job_id,
                "failed",
                child_exit_status=status,
                artifact=None,
                artifact_problems=[reason],
                reason=reason,
                launched_utc=None,
                finished_utc=None,
                partial_evidence=None,
            )
    except Exception:  # noqa: BLE001 - the fallback must neither raise nor drop the diagnostic
        outcomes[job_id] = {
            "job_id": job_id,
            "state": "failed",
            "child_exit_status": status,
            "artifact_problems": [reason],
            "reason": reason,
        }
    record = cleanup.get(job_id)
    if isinstance(record, dict):
        record["finish_error"] = reason  # the cleanup facts already recorded are kept as they are
        return
    try:  # one more bounded, pidfd-only attempt; the guardian proof was never evaluated, so never proved
        receipt = dict(
            item.owned.terminate(grace_seconds=1.0, deadline=deadline, clock=clock, sleeper=sleeper)
        )
        receipt.update(proof=False, reason="the guardian proof was not evaluated: " + reason)
    except Exception as again:  # noqa: BLE001
        receipt = {"proof": False, "reason": f"{reason}; the retry failed: {type(again).__name__}"}
    cleanup[job_id] = {"process_tree": receipt, "finish_error": reason}


def should_retain_runtime(report: Mapping[str, Any]) -> bool:
    """Runtime files are removed only on explicit, complete cleanup proof; anything else retains."""

    return report.get("cleanup_complete") is not True


def capacity_evidence_problem(capacity: Any) -> str:
    """Empty when every admission decision is an object with a string status, else why not."""

    admission = capacity.get("admission") if isinstance(capacity, Mapping) else None
    decisions = admission.get("decisions") if isinstance(admission, Mapping) else None
    if decisions is None:
        return ""
    if not isinstance(decisions, list):
        return "the admission decisions are not a list"
    for index, decision in enumerate(decisions):
        if not isinstance(decision, Mapping):
            return f"admission decision {index} is not an object"
        if not isinstance(decision.get("status"), str):
            return f"admission decision {index} has a non-string status"
    return ""


def child_capacity_loss_total(loss: Callable[[Any], str], capacity: Any) -> tuple[str, str]:
    """(loss reason, malformed note): evidence that cannot be read fails the job, not the run."""

    problem = capacity_evidence_problem(capacity)
    if problem:
        return "", f"the child's capacity evidence is malformed ({problem})"
    try:
        return loss(capacity), ""
    except Exception as exc:  # noqa: BLE001 - containment boundary for untrusted nested values
        return "", f"the child's capacity evidence is malformed ({type(exc).__name__})"


def recover_evidence(
    read_terminal: Callable[[Any], tuple[Any, Any]],
    collect_partial: Callable[[Any], Mapping[str, Any]],
    tree: Mapping[str, Any],
) -> tuple[Any, Mapping[str, Any]]:
    """What a cancelled job already produced; a recovery error is kept as a diagnostic."""

    try:
        artifact, _problems = read_terminal(tree["root"])
        return artifact, collect_partial(tree)
    except Exception as exc:  # noqa: BLE001 - containment boundary for untrusted nested values
        return None, {"recovery_error": f"{type(exc).__name__}: {str(exc)[:160]}"}


__all__ = (
    "capacity_evidence_problem",
    "child_capacity_loss_total",
    "cleanup_completeness",
    "cleanup_proven_for",
    "fail_finish",
    "note_unproven_cleanup",
    "recover_evidence",
    "should_retain_runtime",
)
