"""Validate completed jobs against measured identity and their original frozen deadline."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from scripts.production_gate_parallel_artifacts import (
    outcome_record,
    read_terminal_artifact,
    validate_terminal_artifact,
    verify_gate_report,
)
from scripts.production_gate_parallel_identity import (
    IdentityDeadline,
    Measurer,
    expected_identity,
    identity_differences,
    noop_checkpoint,
)
from scripts.production_gate_parallel_plan import ParallelPlan
from scripts.production_gate_parallel_validation import parse_utc

if TYPE_CHECKING:
    from scripts.production_gate_parallel import _Running

_WALL_NOW = partial(datetime.now, UTC)


def finish_job(
    plan: ParallelPlan,
    running: _Running,
    exit_status: int,
    clock: Callable[[], float],
    wall_clock: Callable[[], datetime] = _WALL_NOW,
    measure: Measurer | None = None,
    worker_pid: int | None = None,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
) -> dict[str, Any]:
    artifact, child_capacity = None, None
    problems: list[str] = []

    def measurement_checkpoint() -> None:
        # Broadcast sibling expiry before refusing this job's expired finalization.
        checkpoint()
        if clock() >= running.deadline:
            raise IdentityDeadline("completion exceeded the frozen deadline")

    try:
        measurement_checkpoint()
        artifact, read_problems = read_terminal_artifact(
            running.tree["root"], checkpoint=measurement_checkpoint
        )
        problems.extend(read_problems)
        if artifact is not None:
            report_problems, gate_report = verify_gate_report(
                running.tree["root"],
                artifact,
                plan.as_dict(),
                plan.job(running.job_id),
                checkpoint=measurement_checkpoint,
            )
            problems.extend(report_problems)
            if gate_report is not None and isinstance(gate_report.get("capacity"), dict):
                child_capacity = gate_report["capacity"]
            measurement_checkpoint()
            problems.extend(
                validate_terminal_artifact(
                    artifact,
                    plan,
                    running.job_id,
                    child_exit_status=exit_status,
                    child_pid=worker_pid if worker_pid is not None else running.process.pid,
                    expected_token=running.token,
                    window=(running.launched_wall, wall_clock()),
                )
            )
            measurement_checkpoint()
        if measure is not None:
            measurement_checkpoint()
            measured, measure_problems = measure(running.job_id, checkpoint=measurement_checkpoint)
            measurement_checkpoint()
            mode = plan.job(running.job_id)["mode"]
            problems.extend(measure_problems)
            problems.extend(identity_differences(measured, expected_identity(plan.as_dict(), mode)))
    except IdentityDeadline as exc:
        problems.append(str(exc))
    finalized = clock()
    exceeded = finalized >= running.deadline
    if artifact is not None:
        finished = parse_utc(artifact.get("finished_utc"))
        wall_deadline = running.launched_wall + timedelta(
            seconds=float(plan.job(running.job_id)["deadline_seconds"])
        )
        exceeded = exceeded or (finished is not None and finished >= wall_deadline)
    if exceeded:
        problems.append(
            "job exceeded its frozen deadline during completion or identity verification"
        )
    elapsed = finalized - running.started
    record = outcome_record(
        plan,
        running.job_id,
        "timeout" if exceeded else "failed" if problems else "passed",
        child_exit_status=exit_status,
        artifact=artifact,
        artifact_problems=problems,
        reason="; ".join(problems[:1]),
        elapsed_seconds=round(elapsed, 3),
        launched_utc=running.launched_wall.isoformat(),
        finished_utc=wall_clock().isoformat(),
    )
    record["launch_token"] = running.token
    record["capacity"] = child_capacity
    return record
