"""Strict terminal-artifact validation and aggregation for concurrent runtimes (#107.3, #107.6).

An aggregate PASS is derived only from the observed child exit status plus a
terminal artifact that matches the frozen plan exactly.  Totals text, PASS words
and caller assertions are never trusted: every identity, ordering and count is
re-checked here, and every job outcome (including failures) is retained.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_plan import ParallelPlan

ARTIFACT_SCHEMA_VERSION = 1
TERMINAL_ARTIFACT_NAME = "terminal.json"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
TIER_STATUSES = frozenset({"PASS", "FAIL", "INTERRUPTED", "BLOCKED", "UNRUN", "TIMEOUT"})
JOB_STATES = frozenset(
    {"passed", "failed", "invalid_artifact", "timeout", "interrupted", "not_started"}
)
ARTIFACT_KEYS = frozenset(
    {
        "asset_digest",
        "build_identity",
        "cancellation",
        "child_exit_status",
        "commit",
        "diagnostic_reuse",
        "finished_utc",
        "fresh_run",
        "gate_problems",
        "input_digests",
        "job_id",
        "mode",
        "overall",
        "pid",
        "plan_sha256",
        "rows",
        "run_id",
        "runtime",
        "schema_version",
        "started_utc",
        "tiers",
    }
)
COUNT_KEYS = ("passed", "failed", "skipped", "xfailed", "xpassed", "errors", "total")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_terminal_artifact(
    job_root: Path, relative: str = f"out/{TERMINAL_ARTIFACT_NAME}"
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read the planned artifact path, refusing symlinks, escapes and oversize files."""

    root = job_root.resolve()
    path = job_root / relative
    problems: list[str] = []
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, ["terminal artifact is missing"]
    except OSError as exc:
        return None, [f"terminal artifact is unreadable: {type(exc).__name__}"]
    if stat.S_ISLNK(info.st_mode):
        return None, ["terminal artifact is a symlink"]
    if not stat.S_ISREG(info.st_mode):
        return None, ["terminal artifact is not a regular file"]
    if info.st_size > MAX_ARTIFACT_BYTES:
        return None, ["terminal artifact is oversize"]
    if not path.resolve().is_relative_to(root):
        return None, ["terminal artifact escapes the owned job root"]
    try:
        text = path.read_text(encoding="utf-8")
        body = json.loads(text)
    except (OSError, UnicodeDecodeError, ValueError):
        return None, ["terminal artifact is malformed or truncated"]
    if not isinstance(body, dict):
        return None, ["terminal artifact is not a JSON object"]
    return body, problems


def _tier_problems(
    job: Mapping[str, Any], plan: Mapping[str, Any], artifact: Mapping[str, Any]
) -> list[str]:
    problems: list[str] = []
    tiers = artifact["tiers"]
    if not isinstance(tiers, list) or not all(isinstance(item, dict) for item in tiers):
        return ["tiers must be a list of objects"]
    expected = [step["tier"] for step in job["steps"]]
    names = [item.get("name") for item in tiers]
    if len(set(map(str, names))) != len(names):
        problems.append(
            f"duplicate tier records: {sorted({n for n in names if names.count(n) > 1})}"
        )
    if names != expected:
        problems.append(f"tier records {names} do not equal the planned order {expected}")
    for tier in tiers:
        name = tier.get("name")
        status = tier.get("status")
        if status not in TIER_STATUSES:
            problems.append(f"{name}: invalid status {status!r}")
            continue
        if status != "PASS":
            problems.append(f"{name}: status {status}")
            continue
        counts = tier.get("counts")
        if not isinstance(counts, dict) or not all(_is_int(counts.get(k)) for k in COUNT_KEYS):
            problems.append(f"{name}: PASS without complete integer counts")
            continue
        bad = {k: counts[k] for k in ("failed", "skipped", "xfailed", "xpassed", "errors")}
        if any(bad.values()) or counts["total"] <= 0 or counts["passed"] != counts["total"]:
            problems.append(f"{name}: PASS with failed/skipped/xfailed/xpassed/errors {bad}")
        returncodes = tier.get("returncodes")
        if not isinstance(returncodes, list) or not all(_is_int(code) for code in returncodes):
            problems.append(f"{name}: returncodes missing")
        elif any(returncodes):
            problems.append(f"{name}: PASS with non-zero return codes {returncodes}")
        elif name == "timing" and len(returncodes) != plan["repetitions"]:
            problems.append(
                f"{name}: {len(returncodes)} timing repetitions, plan requires {plan['repetitions']}"
            )
    return problems


def _row_problems(
    job: Mapping[str, Any], plan: Mapping[str, Any], artifact: Mapping[str, Any]
) -> list[str]:
    """Check unique ordered runtime-tier-case-rep IDs against the frozen requirements."""

    rows = artifact["rows"]
    if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
        return ["rows must be a list of strings"]
    problems: list[str] = []
    if len(set(rows)) != len(rows):
        problems.append(f"duplicate row ids {sorted({r for r in rows if rows.count(r) > 1})[:5]}")
    if not plan["require_row_ids"]:
        return problems
    mode = job["mode"]
    prefix_ok = [row for row in rows if row.startswith(f"{mode}/")]
    if len(prefix_ok) != len(rows):
        problems.append("a row id names a different runtime")
    expected = [
        f"{mode}/{tier}/{case}"
        for tier in job["tiers"]
        for case in plan["required_nodeids"].get(tier, [])
    ]
    for rep in job["timing_rep_ids"]:
        expected.append(f"{mode}/timing/{rep}")
    missing = sorted(set(expected) - set(rows))
    extra = sorted(set(rows) - set(expected))
    if missing:
        problems.append(f"missing required rows ({len(missing)}): {missing[:3]}")
    if extra:
        problems.append(f"unplanned rows ({len(extra)}): {extra[:3]}")
    return problems


def validate_terminal_artifact(
    artifact: object,
    plan: ParallelPlan,
    job_id: str,
    *,
    child_exit_status: int | None,
    child_pid: int | None = None,
) -> list[str]:
    """Return every reason the artifact cannot authorize this job's PASS."""

    if not isinstance(artifact, dict):
        return ["artifact is not an object"]
    body = plan.as_dict()
    job = plan.job(job_id)
    problems: list[str] = []
    extra = sorted(set(artifact) - ARTIFACT_KEYS)
    missing = sorted(ARTIFACT_KEYS - set(artifact))
    if extra or missing:
        return [f"artifact keys differ (unknown {extra}, missing {missing})"]
    if artifact["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        problems.append("artifact schema version mismatch")
    for key in ("run_id", "commit", "build_identity", "asset_digest", "input_digests"):
        if artifact[key] != body[key]:
            problems.append(f"{key} does not match the frozen plan (stale or wrong identity)")
    if artifact["plan_sha256"] != plan.digest:
        problems.append("plan_sha256 does not match the frozen plan")
    if artifact["job_id"] != job_id or artifact["mode"] != job["mode"]:
        problems.append("job or runtime identity does not match the plan")
    if artifact["fresh_run"] is not True or artifact["diagnostic_reuse"] is not False:
        problems.append("artifact is not a fresh run (diagnostic reuse)")
    runtime = artifact["runtime"]
    if not isinstance(runtime, dict) or runtime.get("pyboy_mode") != job["mode"]:
        problems.append(
            "runtime identity differs from the planned mode (source fallback in native)"
        )
    if child_exit_status is None or not _is_int(artifact["child_exit_status"]):
        problems.append("child exit status is not an integer")
    elif artifact["child_exit_status"] != child_exit_status:
        problems.append("artifact exit status disagrees with the observed child exit status")
    elif child_exit_status != 0:
        problems.append(f"child exit status {child_exit_status}")
    if child_pid is not None and artifact["pid"] != child_pid:
        problems.append("artifact pid is not the owned child pid")
    if not isinstance(artifact["gate_problems"], list) or artifact["gate_problems"]:
        problems.append(f"gate problems present: {str(artifact['gate_problems'])[:200]}")
    if artifact["cancellation"]:
        problems.append(f"cancelled: {artifact['cancellation']}")
    if artifact["overall"] != "PASS":
        problems.append(f"overall {artifact['overall']!r}")
    problems.extend(_tier_problems(job, body, artifact))
    problems.extend(_row_problems(job, body, artifact))
    return problems


def outcome_record(
    plan: ParallelPlan,
    job_id: str,
    state: str,
    *,
    child_exit_status: int | None,
    artifact: object,
    artifact_problems: Sequence[str],
    reason: str = "",
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """Build one retained per-job outcome; failures and diagnostics are never dropped."""

    if state not in JOB_STATES:
        raise ValueError(f"unknown job state {state!r}")
    job = plan.job(job_id)
    return {
        "job_id": job_id,
        "mode": job["mode"],
        "state": state,
        "reason": reason,
        "child_exit_status": child_exit_status,
        "elapsed_seconds": elapsed_seconds,
        "artifact_problems": list(artifact_problems),
        "artifact": artifact if isinstance(artifact, dict) else None,
    }


def aggregate_outcomes(plan: ParallelPlan, outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate retained outcomes; PASS requires every planned job to be a valid pass."""

    body = plan.as_dict()
    planned = [job["job_id"] for job in body["jobs"]]
    seen = [outcome.get("job_id") for outcome in outcomes]
    problems: list[str] = []
    for job_id in planned:
        if seen.count(job_id) == 0:
            problems.append(f"{job_id}: missing outcome")
        elif seen.count(job_id) > 1:
            problems.append(f"{job_id}: duplicate outcome")
    for job_id in seen:
        if job_id not in planned:
            problems.append(f"{job_id}: not in the plan")
    for outcome in outcomes:
        state = outcome.get("state")
        if outcome.get("job_id") not in planned:
            continue
        if state != "passed":
            detail = list(outcome.get("artifact_problems") or []) or [str(outcome.get("reason"))]
            problems.append(f"{outcome.get('job_id')}: {state}: {'; '.join(detail)[:300]}")
            continue
        recheck = validate_terminal_artifact(
            outcome.get("artifact"),
            plan,
            str(outcome.get("job_id")),
            child_exit_status=outcome.get("child_exit_status"),
        )
        if recheck:
            problems.append(
                f"{outcome.get('job_id')}: aggregate re-validation failed: {recheck[0]}"
            )
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "run_id": body["run_id"],
        "plan_sha256": plan.digest,
        "commit": body["commit"],
        "overall": "FAIL" if problems else "PASS",
        "problems": problems,
        "jobs": [dict(outcome) for outcome in outcomes],
    }


def compare_with_sequential(
    parallel: Mapping[str, Any], sequential: Mapping[str, Any], plan: ParallelPlan
) -> dict[str, Any]:
    """Compare elapsed time only when both runs share the frozen identities and conditions.

    ``sequential`` must be a summary captured for the same commit, build, inputs, assets,
    tiers, repetitions and capacity; anything else is refused instead of compared.
    """

    body = plan.as_dict()
    required = {
        "commit": body["commit"],
        "build_identity": body["build_identity"],
        "asset_digest": body["asset_digest"],
        "input_digests": body["input_digests"],
        "tiers": body["tiers"],
        "repetitions": body["repetitions"],
        "total_pairs": body["capacity"]["total_pairs"],
        "policy_digest": body["capacity"]["policy_digest"],
    }
    problems = [
        f"sequential {key} differs from the frozen plan"
        for key, value in required.items()
        if sequential.get(key) != value
    ]
    if parallel.get("overall") != "PASS" or sequential.get("overall") != "PASS":
        problems.append("both runs must be PASS to compare elapsed time")
    p_elapsed, s_elapsed = parallel.get("elapsed_seconds"), sequential.get("elapsed_seconds")
    for label, value in (("parallel", p_elapsed), ("sequential", s_elapsed)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            problems.append(f"{label} elapsed_seconds missing or not positive")
    if problems:
        return {"status": "REFUSED", "problems": problems}
    return {
        "status": "COMPARED",
        "parallel_seconds": p_elapsed,
        "sequential_seconds": s_elapsed,
        "speedup": round(s_elapsed / p_elapsed, 4),
    }
