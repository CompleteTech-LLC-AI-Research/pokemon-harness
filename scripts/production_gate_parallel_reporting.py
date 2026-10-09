"""Structured supplemental facts of the concurrent runtime gate."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_artifacts import aggregate_outcomes
from scripts.production_gate_parallel_cgroup import qualification_after
from scripts.production_gate_parallel_declaration import COVERAGE_DECLARATION, LIMITATIONS
from scripts.production_gate_parallel_plan import ParallelPlan


def _report_extras(
    plan: ParallelPlan,
    started: float,
    clock: Callable[[], float],
    cancel_reason: str,
    cleanup: Mapping[str, Any],
    problems: Sequence[str],
    *,
    observations: Sequence[Any] = (),
    serial: bool = False,
    load: tuple[Any, Any] = ((), ()),
) -> dict[str, Any]:
    body = plan.as_dict()
    return {
        "fail_fast": body["fail_fast"],
        "elapsed_seconds": round(clock() - started, 3),
        "cancellation": cancel_reason,
        "cleanup": dict(cleanup),
        "orchestration_problems": list(problems),
        "capacity": body["capacity"],
        "capacity_observations": list(observations),
        "execution_mode": "serial" if serial else "concurrent",
        "plan": plan.text,
        "resource_trace": {
            "affinity_cpus": len(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else 0,
            "loadavg_start": list(load[0]),
            "loadavg_end": list(load[1]),
        },
        "row_identity_enforced": True,
        "sequential_comparison": {"status": "NOT_RUN"},
        "limitations": list(LIMITATIONS),
        "coverage_declaration": COVERAGE_DECLARATION,
        **({"overall": "FAIL"} if problems else {}),
    }


def _write_progress(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def write_running_progress(
    path: Path,
    phase_of: Mapping[str, Any],
    outcomes: Mapping[str, Any],
    running: Sequence[Any],
    cancel_reason: str,
    wall_clock: Callable[[], Any],
) -> None:
    """Publish the same job states for normal work and bounded cancellation."""

    states = {
        job_id: outcomes[job_id]["state"]
        if job_id in outcomes
        else ("running" if any(item.job_id == job_id for item in running) else "pending")
        for job_id in phase_of
    }
    _write_progress(
        path,
        {
            "updated_utc": wall_clock().isoformat(),
            "cancellation": cancel_reason,
            "jobs": states,
        },
    )


def completed_report(
    plan: ParallelPlan,
    ordered: Sequence[Any],
    started: float,
    clock: Callable[[], float],
    cancel_reason: str,
    cleanup: Mapping[str, Any],
    problems: Sequence[str],
    ownership: Mapping[str, Any],
    launched_jobs: Sequence[str] | set[str],
    complete: bool,
    **extras: Any,
) -> dict[str, Any]:
    """Required final ownership is an acceptance condition of the published result."""

    report = aggregate_outcomes(plan, ordered)
    report.update(_report_extras(plan, started, clock, cancel_reason, cleanup, problems, **extras))
    final = qualification_after(ownership, cleanup, started_jobs=launched_jobs)
    report["kernel_ownership"] = final
    report["cleanup_complete"] = complete
    if final["required"] and final["qualified_final"] is not True:
        report["overall"] = "FAIL"
        report["orchestration_problems"].append("required final kernel ownership is unproven")
    return report
