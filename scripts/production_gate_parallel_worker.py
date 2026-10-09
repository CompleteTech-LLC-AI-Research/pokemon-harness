"""Per-runtime worker child for ``--parallel-runtimes`` (#107.2, #107.3).

Runs the ordinary sequential gate for exactly one planned runtime inside its own
job tree and writes the terminal artifact the parent validates.  It verifies the
frozen plan digest first, never edits the plan, and never reports a PASS it did not
observe: missing or unreadable gate evidence becomes a FAIL artifact, not a guess.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    TERMINAL_ARTIFACT_NAME,
)
from scripts.production_gate_parallel_plan import PlanError, load_plan


def build_gate_command(
    *,
    python_executable: str,
    project_root: Path,
    mode: str,
    pairs: int,
    job_root: Path,
    gate_arguments: Sequence[str],
) -> list[str]:
    """Return the sequential single-runtime gate command for one job."""

    return [
        python_executable,
        str(project_root / "scripts" / "production_gate.py"),
        f"--repo-root={project_root}",
        f"--runtime-mode={mode}",
        "--format=json",
        f"--evidence-dir={job_root / 'evidence'}",
        f"--raw-output-dir={job_root / 'raw' / 'pytest'}",
        f"--matrix-workers={pairs}",
        *gate_arguments,
    ]


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_report(job_root: Path) -> tuple[dict[str, Any] | None, str]:
    path = job_root / "evidence" / "gate-report.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"gate report unreadable: {type(exc).__name__}"
    if not isinstance(body, dict):
        return None, "gate report is not an object"
    return body, ""


def build_artifact(
    *,
    plan_body: dict[str, Any],
    plan_digest: str,
    job: dict[str, Any],
    pid: int,
    started: str,
    exit_status: int,
    report: dict[str, Any] | None,
    problem: str,
) -> dict[str, Any]:
    """Assemble the terminal artifact from the observed gate report only."""

    report = report or {}
    tiers = [
        {
            "name": tier.get("name"),
            "status": tier.get("status"),
            "counts": tier.get("counts"),
            "returncodes": tier.get("returncodes"),
        }
        for tier in report.get("tiers", [])
        if isinstance(tier, dict)
    ]
    problems = [str(item) for item in report.get("gate_problems", [])]
    if problem:
        problems.append(problem)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "run_id": plan_body["run_id"],
        "plan_sha256": plan_digest,
        "job_id": job["job_id"],
        "mode": job["mode"],
        "commit": plan_body["commit"],
        "build_identity": plan_body["build_identity"],
        "input_digests": plan_body["input_digests"],
        "asset_digest": plan_body["asset_digest"],
        "pid": pid,
        "child_exit_status": exit_status,
        "started_utc": started,
        "finished_utc": _utc(),
        "fresh_run": True,
        "diagnostic_reuse": False,
        "runtime": report.get("runtime") if isinstance(report.get("runtime"), dict) else {},
        "tiers": tiers,
        "rows": [],
        "gate_problems": problems,
        "cancellation": str(report.get("cancellation") or ""),
        "overall": str(report.get("overall") or "FAIL") if not problem else "FAIL",
    }


def write_artifact(job_root: Path, artifact: dict[str, Any]) -> Path:
    target = job_root / "out" / TERMINAL_ARTIFACT_NAME
    temporary = target.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--gate-arg", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan.read_text(encoding="utf-8"))
        job = plan.job(args.job_id)
    except (OSError, PlanError) as exc:
        print(f"worker refused: {exc}", file=sys.stderr)
        return 3
    body = plan.as_dict()
    command = build_gate_command(
        python_executable=args.python_executable,
        project_root=args.project_root,
        mode=job["mode"],
        pairs=job["pairs"],
        job_root=args.job_root,
        gate_arguments=args.gate_arg,
    )
    started = _utc()
    completed = subprocess.run(
        command,
        cwd=args.project_root,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=float(job["deadline_seconds"]),
    )
    report, problem = _load_report(args.job_root)
    artifact = build_artifact(
        plan_body=body,
        plan_digest=plan.digest,
        job=job,
        pid=os.getpid(),
        started=started,
        exit_status=completed.returncode,
        report=report,
        problem=problem,
    )
    write_artifact(args.job_root, artifact)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
