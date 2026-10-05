from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _read_head(project_root: Path) -> str:
    """Return the tested commit identity, or ``unavailable``."""

    try:
        completed = subprocess.run(
            ("git", "-C", str(project_root), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return completed.stdout.strip() or "unavailable"


def render_text(report: dict[str, Any]) -> str:
    """Render the benchmark report for a terminal transcript."""

    identity = report.get("identity", {})
    lines = [
        "matrix-concurrency benchmark",
        f"  head: {identity.get('head')}",
        f"  report-sha256: {identity.get('sha256')}",
    ]
    for arm in report.get("arms", []):
        rows = arm.get("rows", {})
        lines.append(
            f"  [{arm.get('arm')}] effective_workers={arm.get('effective_workers')} "
            f"passing={rows.get('completed_passing')} failed={rows.get('failed')} "
            f"not_started={rows.get('not_started')} wall={arm.get('wall_seconds')}s "
            f"required-rows/hour={arm.get('required_rows_per_hour')}"
        )
    selection = report.get("selection", {})
    lines.append(f"  selection: {selection.get('outcome')}")
    for reason in selection.get("reasons", []):
        lines.append(f"    reason: {reason}")
    policy = selection.get("policy")
    if isinstance(policy, dict):
        lines.append(f"    matrix_workers: {policy.get('matrix_workers')}")
    return "\n".join(lines)
