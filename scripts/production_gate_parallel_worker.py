"""Per-runtime worker child for ``--parallel-runtimes`` (#107.2, #107.3).

Runs the ordinary sequential gate for exactly one planned job inside its own job tree
and writes the terminal artifact the parent validates.  It verifies the frozen plan
digest first, never edits the plan, and never reports a PASS it did not observe.  The
identity it reports is MEASURED here (git head and cleanliness, build, inputs, ROM and
fixture bytes, before and after the gate), never copied from the plan; missing or
unreadable gate evidence becomes a FAIL artifact, not a guess.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    TERMINAL_ARTIFACT_NAME,
)
from scripts.production_gate_parallel_capacity import policy_file_problem
from scripts.production_gate_parallel_identity import IDENTITY_KEYS, measure_identity
from scripts.production_gate_parallel_plan import PlanError, load_plan
from scripts.production_gate_parallel_processes import JOB_TOKEN_ENV
from scripts.production_gate_parallel_records import build_rows, diagnostics_for_tier
from scripts.production_gate_parallel_runtime_inputs import runtime_pin_problem
from scripts.production_gate_parallel_validation import loads_strict, parse_utc

TIMEOUT_EXIT_STATUS = 124


def build_gate_command(
    *,
    python_executable: str,
    project_root: Path,
    mode: str,
    pairs: int,
    job_root: Path,
    gate_arguments: Sequence[str],
    tiers: Sequence[str] = (),
    rom_root: Path | None = None,
    fixture_root: Path | None = None,
    capacity_policy: Path | None = None,
) -> list[str]:
    """Return the sequential single-runtime gate command for one job (explicit tiers)."""

    command = [
        python_executable,
        str(project_root / "scripts" / "production_gate.py"),
        f"--repo-root={project_root}",
        f"--runtime-mode={mode}",
        "--format=json",
        f"--evidence-dir={job_root / 'evidence'}",
        f"--raw-output-dir={job_root / 'raw' / 'pytest'}",
        f"--matrix-workers={pairs}",
        *(f"--tier={tier}" for tier in tiers),
    ]
    if rom_root is not None:
        command.append(f"--rom-root={rom_root}")
    if fixture_root is not None:
        command.append(f"--fixture-root={fixture_root}")
    if capacity_policy is not None:
        command.append(f"--capacity-policy={capacity_policy}")
    command.extend(gate_arguments)
    return command


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_report(job_root: Path) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Return (report, problem, evidence reference) for the gate's own report file."""

    path = job_root / "evidence" / "gate-report.json"
    empty = {"gate_report_bytes": 0, "gate_report_sha256": "0" * 64}
    try:
        raw = path.read_bytes()
        body = loads_strict(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None, "gate report unreadable", empty
    evidence = {
        "gate_report_bytes": len(raw),
        "gate_report_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not isinstance(body, dict):
        return None, "gate report is not an object", evidence
    return body, "", evidence


def _fresh(job_root: Path, started: str, evidence_was_empty: bool) -> bool:
    """Fresh means: the evidence directory was empty at start and the report is newer."""

    opened = parse_utc(started)
    try:
        written = datetime.fromtimestamp(
            (job_root / "evidence" / "gate-report.json").stat().st_mtime, UTC
        )
    except OSError:
        return False
    return bool(
        evidence_was_empty and opened is not None and (written - opened).total_seconds() >= -2.0
    )


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
    token: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    """Assemble the terminal artifact from observed and MEASURED values only."""

    report = report or {}
    before, after = before or {}, after or {}
    malformed: list[str] = []
    raw = report.get("tiers", [])
    if not isinstance(raw, list):
        malformed.append("the gate report's tiers value is not a list")
        raw = []
    raw_tiers = [tier for tier in raw if isinstance(tier, dict)]
    tiers = []
    for tier in raw_tiers:
        try:
            diagnostics = diagnostics_for_tier(tier)
        except Exception as exc:  # noqa: BLE001 - malformed nested values are a terminal rejection
            diagnostics = {}
            malformed.append(
                f"{tier.get('name')}: diagnostics failed closed ({type(exc).__name__})"
            )
        tiers.append(
            {
                "name": tier.get("name"),
                "status": tier.get("status"),
                "counts": tier.get("counts"),
                "returncodes": tier.get("returncodes"),
                "diagnostics": diagnostics,
            }
        )
    try:
        rows = build_rows(job["mode"], raw_tiers, plan_body["games"], plan_body["roles"])
    except Exception as exc:  # noqa: BLE001 - keep the earlier failures, reject the malformed rows
        rows = []
        malformed.append(f"rows failed closed on malformed nested values ({type(exc).__name__})")
    gate_problems = report.get("gate_problems", [])
    problems = (
        [str(item) for item in gate_problems]
        if isinstance(gate_problems, list)
        else ["gate_problems is not a list"]
    )
    problems.extend(malformed)
    if problem:
        problems.append(problem)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "run_id": plan_body["run_id"],
        "plan_sha256": plan_digest,
        "job_id": job["job_id"],
        "mode": job["mode"],
        "commit": before.get("commit"),
        "clean_head": before.get("clean_head") is True,
        "build_identity": before.get("build_identity"),
        "input_digests": before.get("input_digests"),
        "asset_digest": before.get("asset_digest"),
        "identity_after": {key: after.get(key) for key in IDENTITY_KEYS},
        "launch_token": token,
        "evidence": evidence or {"gate_report_bytes": 0, "gate_report_sha256": "0" * 64},
        "pid": pid,
        "child_exit_status": exit_status,
        "started_utc": started,
        "finished_utc": _utc(),
        "fresh_run": fresh,
        "diagnostic_reuse": False,
        "runtime": report.get("runtime") if isinstance(report.get("runtime"), dict) else {},
        "tiers": tiers,
        "rows": rows,
        "gate_problems": problems,
        "cancellation": str(report.get("cancellation") or ""),
        "overall": str(report.get("overall") or "FAIL") if not problems else "FAIL",
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
    parser.add_argument("--git-root", type=Path, default=None)
    parser.add_argument("--rom-root", type=Path, default=None)
    parser.add_argument("--fixture-root", type=Path, default=None)
    parser.add_argument("--capacity-policy", type=Path, default=None)
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--gate-arg", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan.read_text(encoding="utf-8"))
        job = plan.job(args.job_id)
    except (OSError, PlanError) as exc:
        print(f"worker refused: {exc}", file=sys.stderr)
        return 3
    if args.rom_root is None or args.fixture_root is None:
        print("worker refused: ROM and fixture roots must be explicit", file=sys.stderr)
        return 3
    pin_problem = runtime_pin_problem(
        plan.as_dict(), job["mode"], Path(sys.prefix), args.project_root
    )
    if pin_problem:
        print(f"worker refused: {pin_problem}", file=sys.stderr)
        return 3
    pinned = job.get("policy_sha256")
    if args.capacity_policy is not None and pinned is not None:
        problem = policy_file_problem(args.capacity_policy, pinned)
        if problem:
            print(f"worker refused: {problem}", file=sys.stderr)
            return 3
    body = plan.as_dict()

    def measure() -> tuple[dict[str, Any], list[str]]:
        return measure_identity(
            mode=job["mode"],
            interpreter=args.python_executable,
            project_root=args.project_root,
            input_names=sorted(body["input_digests"]),
            rom_root=args.rom_root,
            fixture_root=args.fixture_root,
            git_root=args.git_root,
        )

    evidence_was_empty = not any((args.job_root / "evidence").iterdir())
    before, before_problems = measure()
    command = build_gate_command(
        python_executable=args.python_executable,
        project_root=args.project_root,
        mode=job["mode"],
        pairs=job["pairs"],
        job_root=args.job_root,
        gate_arguments=args.gate_arg,
        tiers=job["tiers"],
        rom_root=args.rom_root,
        fixture_root=args.fixture_root,
        capacity_policy=args.capacity_policy,
    )
    # One absolute deadline shared with the parent: measurement time is spent from the same budget.
    timeout = float(job["deadline_seconds"])
    try:
        timeout = max(
            1.0, min(timeout, float(os.environ["POKERED_PARALLEL_DEADLINE_EPOCH"]) - time.time())
        )
    except (KeyError, ValueError):
        pass
    gate_environment = {**os.environ, "POKERED_GATE_RECORD_TESTS": "1"}
    if args.git_root is not None:
        # The private snapshot is a byte copy of this checkout; the origin guard verifies that
        # equivalence itself before it accepts an installation attributed to the original.
        gate_environment["POKERED_IMPORT_ORIGIN_EQUIVALENT_ROOTS"] = str(args.git_root)
    started = _utc()
    timeout_problem = ""
    try:
        exit_status = subprocess.run(
            command,
            cwd=args.project_root,
            stdin=subprocess.DEVNULL,
            check=False,
            env=gate_environment,
            timeout=timeout,
        ).returncode
    except subprocess.TimeoutExpired:
        exit_status, timeout_problem = TIMEOUT_EXIT_STATUS, "the gate exceeded the job deadline"
    report, report_problem, evidence = _load_report(args.job_root)
    after, after_problems = measure()
    problems = [*before_problems, *after_problems, timeout_problem, report_problem]
    artifact = build_artifact(
        plan_body=body,
        plan_digest=plan.digest,
        job=job,
        pid=os.getpid(),
        started=started,
        exit_status=exit_status,
        report=report,
        problem="; ".join(item for item in problems if item),
        token=os.environ.get(JOB_TOKEN_ENV, ""),
        before=before,
        after=after,
        evidence=evidence,
        fresh=_fresh(args.job_root, started, evidence_was_empty),
    )
    write_artifact(args.job_root, artifact)
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
