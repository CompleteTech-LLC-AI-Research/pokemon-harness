"""Production comparison of two complete, re-validated runtime bundles (#107.5).

A bundle is the aggregate report of one orchestrated run with its embedded frozen
plan.  Concurrent and serial bundles must come from the same immutable plan content
(only run id and creation time may differ), both must re-validate artifact by
artifact, and elapsed time is taken from the validated artifacts' own timestamps, not
from a caller-supplied number.  Nothing here runs a gate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from scripts.production_gate_parallel_artifacts import validate_terminal_artifact
from scripts.production_gate_parallel_plan import PlanError, canonical_json, load_plan
from scripts.production_gate_parallel_validation import is_positive_number, parse_utc

VOLATILE_PLAN_KEYS = ("created_utc", "plan_sha256", "run_id")


def equivalence_digest(plan_body: Mapping[str, Any]) -> str:
    """Digest of everything a comparison must hold equal: identities, selection, limits."""

    stable = {k: v for k, v in plan_body.items() if k not in VOLATILE_PLAN_KEYS}
    return hashlib.sha256(canonical_json(stable).encode("ascii")).hexdigest()


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
        or [j.get("job_id") for j in jobs if isinstance(j, dict)] != planned
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


def _span_seconds(report: Mapping[str, Any]) -> float | None:
    starts, ends = [], []
    for job in report["jobs"]:
        artifact = job.get("artifact") or {}
        first, last = (
            parse_utc(artifact.get("started_utc")),
            parse_utc(artifact.get("finished_utc")),
        )
        if first is None or last is None:
            return None
        starts.append(first)
        ends.append(last)
    return (max(ends) - min(starts)).total_seconds() if starts else None


def compare_bundles(concurrent: object, serial: object) -> dict[str, Any]:
    """Compare a concurrent bundle with a serial one produced under equivalent conditions."""

    plan_c, problems = bundle_problems(concurrent)
    plan_s, serial_problems = bundle_problems(serial)
    problems = [f"concurrent: {p}" for p in problems] + [f"serial: {p}" for p in serial_problems]
    if plan_c is not None and plan_s is not None:
        if equivalence_digest(plan_c.as_dict()) != equivalence_digest(plan_s.as_dict()):
            problems.append("plans differ in identities, selection, deadlines or capacity")
        if plan_c.run_id == plan_s.run_id:
            problems.append("both bundles are the same run")
    if isinstance(concurrent, dict) and concurrent.get("execution_mode") != "concurrent":
        problems.append("the first bundle was not produced in concurrent mode")
    if isinstance(serial, dict) and serial.get("execution_mode") != "serial":
        problems.append("the second bundle was not produced in serial mode")
    for label, bundle in (("concurrent", concurrent), ("serial", serial)):
        if isinstance(bundle, dict) and not isinstance(bundle.get("resource_trace"), dict):
            problems.append(f"{label}: resource trace is missing")
    if problems:
        return {"status": "REFUSED", "problems": problems}
    seconds_c, seconds_s = _span_seconds(concurrent), _span_seconds(serial)  # type: ignore[arg-type]
    if not is_positive_number(seconds_c) or not is_positive_number(seconds_s):
        return {
            "status": "REFUSED",
            "problems": ["validated artifact timestamps give no elapsed time"],
        }
    return {
        "status": "COMPARED",
        "concurrent_seconds": seconds_c,
        "serial_seconds": seconds_s,
        "speedup": round(seconds_s / seconds_c, 4),  # type: ignore[operator]
        "equivalence_digest": equivalence_digest(plan_c.as_dict()),  # type: ignore[union-attr]
        "resource_traces": {
            "concurrent": concurrent["resource_trace"],  # type: ignore[index]
            "serial": serial["resource_trace"],  # type: ignore[index]
        },
        "qualification": "authored/source comparison only; real-ROM six-tier qualification remains separate",
    }
