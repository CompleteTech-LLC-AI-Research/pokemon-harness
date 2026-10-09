"""Production comparison of two complete, re-validated runtime bundles (#107.5).

A bundle is the aggregate report of one orchestrated run with its embedded frozen plan.
Concurrent and serial bundles must come from the same immutable plan content (only run id
and creation time may differ), both must re-validate artifact by artifact, and the elapsed
time is taken from the PARENT-observed launch/finish wall times of the jobs.  A label is
never trusted: the execution intervals themselves must be serial (ordered, non-overlapping)
or concurrent (the runtimes of a phase overlap), every smoke job must end before any
selected-tier job starts, the resource trace and capacity observations must be well formed
and equivalent, and every job must carry an affirmative lifecycle proof.  The comparison is
total over hostile JSON: anything unexpected is a refusal, never an exception.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from scripts.production_gate_parallel_artifacts import validate_terminal_artifact
from scripts.production_gate_parallel_plan import PlanError, canonical_json, load_plan
from scripts.production_gate_parallel_validation import (
    CLOCK_SKEW_SECONDS,
    is_finite_number,
    is_int,
    is_positive_number,
    parse_utc,
)

VOLATILE_PLAN_KEYS = ("created_utc", "plan_sha256", "run_id")
INTERVAL_EPSILON_SECONDS = 0.05


def equivalence_digest(plan_body: Mapping[str, Any]) -> str:
    """Digest of everything a comparison must hold equal: identities, selection, limits."""

    stable = {k: v for k, v in plan_body.items() if k not in VOLATILE_PLAN_KEYS}
    return hashlib.sha256(canonical_json(stable).encode("ascii")).hexdigest()


def _interval(job: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    first, last = parse_utc(job.get("launched_utc")), parse_utc(job.get("finished_utc"))
    if first is None or last is None or last < first:
        return None
    return first, last


def _lifecycle_problems(report: Mapping[str, Any], job_id: str) -> list[str]:
    cleanup = report.get("cleanup")
    entry = cleanup.get(job_id) if isinstance(cleanup, dict) else None
    if not isinstance(entry, dict):
        return [f"{job_id}: no lifecycle record"]
    tree, guardian = entry.get("process_tree"), entry.get("guardian")
    problems: list[str] = []
    if (
        not isinstance(tree, dict)
        or tree.get("proof") is not True
        or tree.get("contained") is not True
    ):
        problems.append(f"{job_id}: the parent's own cleanup proof is missing")
    if (
        not isinstance(guardian, dict)
        or guardian.get("phase") != "final"
        or guardian.get("proof") is not True
    ):
        problems.append(f"{job_id}: the guardian's authenticated final proof is missing")
    return problems


def _resource_problems(label: str, report: Mapping[str, Any]) -> list[str]:
    trace = report.get("resource_trace")
    if not isinstance(trace, dict):
        return [f"{label}: resource trace is missing"]
    problems: list[str] = []
    if not is_int(trace.get("affinity_cpus")) or trace["affinity_cpus"] < 1:
        problems.append(f"{label}: resource trace names no CPU affinity")
    for key in ("loadavg_start", "loadavg_end"):
        load = trace.get(key)
        if (
            not isinstance(load, list)
            or not load
            or not all(is_finite_number(v) and v >= 0 for v in load)
        ):
            problems.append(f"{label}: resource trace {key} is not measured load")
    observations = report.get("capacity_observations")
    if not isinstance(observations, list) or not all(isinstance(o, dict) for o in observations):
        problems.append(f"{label}: capacity observations are missing")
    elif any(o.get("event") == "loss" for o in observations):
        problems.append(f"{label}: capacity was lost during the run")
    return problems


def _execution_problems(label: str, report: Mapping[str, Any], plan: Any, mode: str) -> list[str]:
    jobs = report["jobs"]
    planned = plan.as_dict()["jobs"]
    intervals: dict[str, tuple[datetime, datetime]] = {}
    problems: list[str] = []
    for job in jobs:
        window = _interval(job)
        if window is None:
            problems.append(f"{label}: {job.get('job_id')} has no parent-observed interval")
        else:
            intervals[str(job.get("job_id"))] = window
    if problems:
        return problems
    phase_of = {job["job_id"]: job["phase"] for job in planned}
    smoke = [intervals[j] for j in intervals if phase_of[j] == "smoke"]
    selected = [intervals[j] for j in intervals if phase_of[j] == "selected"]
    if smoke and selected and max(end for _, end in smoke) > min(start for start, _ in selected):
        problems.append(f"{label}: a selected-tier job started before every smoke job had ended")
    ordered = sorted(intervals.values())
    if mode == "serial":
        for (_, previous_end), (start, _) in itertools.pairwise(ordered):
            if (previous_end - start).total_seconds() > INTERVAL_EPSILON_SECONDS:
                problems.append(f"{label}: serial bundle has overlapping job intervals")
                break
    else:
        for phase in {phase_of[j] for j in intervals}:
            group = [intervals[j] for j in intervals if phase_of[j] == phase]
            if len(group) > 1 and max(s for s, _ in group) >= min(e for _, e in group):
                problems.append(f"{label}: the runtimes of the {phase} phase never overlapped")
    for job in jobs:
        artifact = job.get("artifact")
        window = intervals[str(job.get("job_id"))]
        if isinstance(artifact, dict):
            first, last = (
                parse_utc(artifact.get("started_utc")),
                parse_utc(artifact.get("finished_utc")),
            )
            if first is None or last is None:
                problems.append(f"{label}: {job.get('job_id')} artifact timestamps are invalid")
            elif (window[0] - first).total_seconds() > CLOCK_SKEW_SECONDS or (
                last - window[1]
            ).total_seconds() > CLOCK_SKEW_SECONDS:
                problems.append(
                    f"{label}: {job.get('job_id')} artifact lies outside its observed interval"
                )
    for job in jobs:
        problems.extend(
            f"{label}: {item}" for item in _lifecycle_problems(report, str(job.get("job_id")))
        )
    return problems


def bundle_problems(report: object) -> tuple[Any, list[str]]:
    """Load the embedded plan and re-validate every job; returns (plan, problems)."""

    if not isinstance(report, dict):
        return None, ["bundle is not an object"]
    try:
        plan = load_plan(report.get("plan") if isinstance(report.get("plan"), str) else "")
    except PlanError as exc:
        return None, [f"embedded plan is invalid: {exc}"]
    problems: list[str] = []
    if report.get("plan_sha256") != plan.digest:
        problems.append("bundle plan_sha256 differs from its embedded plan")
    if report.get("overall") != "PASS" or report.get("orchestration_problems"):
        problems.append("bundle is not a clean PASS")
    jobs = report.get("jobs")
    planned = plan.job_ids()
    if (
        not isinstance(jobs, list)
        or not all(isinstance(j, dict) for j in jobs)
        or [j.get("job_id") for j in jobs] != planned
    ):
        return plan, [*problems, "bundle jobs do not equal the planned jobs in order"]
    for job in jobs:
        if job.get("state") != "passed":
            problems.append(f"{job.get('job_id')}: state {job.get('state')}")
            continue
        problems.extend(
            f"{job.get('job_id')}: {item}"
            for item in validate_terminal_artifact(
                job.get("artifact"),
                plan,
                str(job.get("job_id")),
                child_exit_status=job.get("child_exit_status"),
                expected_token=job.get("launch_token"),
            )
        )
    return plan, problems


def _elapsed_seconds(report: Mapping[str, Any]) -> float | None:
    windows = [_interval(job) for job in report["jobs"]]
    if not windows or any(w is None for w in windows):
        return None
    return (max(w[1] for w in windows if w) - min(w[0] for w in windows if w)).total_seconds()


def _compare(concurrent: object, serial: object) -> dict[str, Any]:
    plan_c, problems = bundle_problems(concurrent)
    plan_s, serial_problems = bundle_problems(serial)
    problems = [f"concurrent: {p}" for p in problems] + [f"serial: {p}" for p in serial_problems]
    if not isinstance(concurrent, dict) or not isinstance(serial, dict):
        return {"status": "REFUSED", "problems": problems or ["a bundle is not an object"]}
    if plan_c is not None and plan_s is not None:
        if equivalence_digest(plan_c.as_dict()) != equivalence_digest(plan_s.as_dict()):
            problems.append("plans differ in identities, selection, deadlines or capacity")
        if plan_c.run_id == plan_s.run_id:
            problems.append("both bundles are the same run")
        if not problems:
            problems.extend(_execution_problems("concurrent", concurrent, plan_c, "concurrent"))
            problems.extend(_execution_problems("serial", serial, plan_s, "serial"))
    if concurrent.get("execution_mode") != "concurrent":
        problems.append("the first bundle was not produced in concurrent mode")
    if serial.get("execution_mode") != "serial":
        problems.append("the second bundle was not produced in serial mode")
    problems.extend(_resource_problems("concurrent", concurrent))
    problems.extend(_resource_problems("serial", serial))
    traces = [b.get("resource_trace") for b in (concurrent, serial)]
    if all(isinstance(t, dict) for t in traces) and traces[0].get("affinity_cpus") != traces[1].get(
        "affinity_cpus"
    ):
        problems.append(
            "the bundles ran with different CPU affinity: conditions are not equivalent"
        )
    if problems:
        return {"status": "REFUSED", "problems": problems}
    seconds_c, seconds_s = _elapsed_seconds(concurrent), _elapsed_seconds(serial)
    if not is_positive_number(seconds_c) or not is_positive_number(seconds_s):
        return {"status": "REFUSED", "problems": ["parent-observed intervals give no elapsed time"]}
    assert seconds_c is not None and seconds_s is not None and plan_c is not None
    return {
        "status": "COMPARED",
        "concurrent_seconds": seconds_c,
        "serial_seconds": seconds_s,
        "speedup": round(seconds_s / seconds_c, 4),
        "equivalence_digest": equivalence_digest(plan_c.as_dict()),
        "resource_traces": {
            "concurrent": concurrent["resource_trace"],
            "serial": serial["resource_trace"],
        },
        "qualification": "source comparison interface only; real-ROM six-tier qualification remains separate",
    }


def compare_bundles(concurrent: object, serial: object) -> dict[str, Any]:
    """Compare a concurrent bundle with a serial one; total over hostile input."""

    try:
        return _compare(concurrent, serial)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError, OverflowError) as exc:
        return {
            "status": "REFUSED",
            "problems": [f"comparison failed closed ({type(exc).__name__})"],
        }


__all__ = ("bundle_problems", "compare_bundles", "equivalence_digest")
