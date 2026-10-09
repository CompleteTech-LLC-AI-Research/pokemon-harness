"""Containment helpers of the concurrent orchestrator (#107 post-Astra repairs).

Cleanup completeness is explicit evidence, never a diagnostic string, and nested evidence that cannot
be interpreted is a failed job with a diagnostic, never an exception that aborts the aggregation.
"""

from __future__ import annotations

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
) -> None:
    """An error inside finish fails that job closed: its cleanup stays unproven, siblings go on."""

    if item in running:
        running.remove(item)
    reason = f"finish failed closed: {type(exc).__name__}: {str(exc)[:160]}"
    outcomes.setdefault(
        item.job_id,
        outcome_record(
            plan,
            item.job_id,
            "failed",
            child_exit_status=status,
            artifact=None,
            artifact_problems=[reason],
            reason=reason,
            launched_utc=None,
            finished_utc=None,
            partial_evidence=None,
        ),
    )
    cleanup.setdefault(item.job_id, {"process_tree": {"proof": False, "reason": reason}})


def should_retain_runtime(report: Mapping[str, Any]) -> bool:
    """Runtime files are removed only on explicit, complete cleanup proof; anything else retains."""

    return report.get("cleanup_complete") is not True


def child_capacity_loss_total(loss: Callable[[Any], str], capacity: Any) -> tuple[str, str]:
    """(loss reason, malformed note): evidence that cannot be read fails the job, not the run."""

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
    "child_capacity_loss_total",
    "cleanup_completeness",
    "cleanup_proven_for",
    "fail_finish",
    "note_unproven_cleanup",
    "recover_evidence",
    "should_retain_runtime",
)
