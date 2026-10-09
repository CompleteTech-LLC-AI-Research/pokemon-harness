"""The single published aggregate: writing it once and rendering it as text (#107.6)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def write_report(run_root: Path, report: Mapping[str, Any]) -> Path:
    """Write the single published aggregate next to the plan, never overwriting."""

    path = run_root / str(report["run_id"]) / "parallel-report.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_report_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"parallel runtime gate run {report.get('run_id')} plan {str(report.get('plan_sha256'))[:12]}",
        (
            f"commit {report.get('commit')} elapsed {report.get('elapsed_seconds')}s "
            f"mode {report.get('execution_mode')}"
        ),
    ]
    for job in report.get("jobs", []):
        lines.append(
            f"  {job.get('job_id')} ({job.get('mode')}): {job.get('state')} "
            f"exit={job.get('child_exit_status')}"
        )
        lines.extend(f"    {item}" for item in job.get("artifact_problems", [])[:3])
    lines.extend(f"problem: {item}" for item in report.get("problems", []))
    lines.extend(f"orchestration: {item}" for item in report.get("orchestration_problems", []))
    lines.extend(f"limitation: {item}" for item in report.get("limitations", []))
    if report.get("runtime_immutability"):
        lines.append(f"runtime immutability: {report['runtime_immutability']}")
    ownership = report.get("kernel_ownership")
    if isinstance(ownership, dict):
        lines.append(f"kernel ownership: {ownership.get('qualification')}")
    lines.append(f"overall: {report.get('overall')}")
    return "\n".join(lines)


__all__ = ("file_sha256", "render_report_text", "write_report")
