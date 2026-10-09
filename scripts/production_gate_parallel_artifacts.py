"""Strict terminal-artifact validation and aggregation for concurrent runtimes (#107.3, #107.6).

An aggregate PASS is derived only from the observed child exit status plus a
terminal artifact that matches the frozen plan exactly.  Totals text, PASS words
and caller assertions are never trusted: every identity, ordering and count is
re-checked here, and every job outcome (including failures) is retained.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_identity import expected_identity
from scripts.production_gate_parallel_plan import ParallelPlan
from scripts.production_gate_parallel_records import row_problems
from scripts.production_gate_parallel_validation import (
    guarded,
    in_set,
    is_int,
    loads_strict,
    timestamp_problems,
)

ARTIFACT_SCHEMA_VERSION = 2
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
        "clean_head",
        "commit",
        "diagnostic_reuse",
        "finished_utc",
        "evidence",
        "fresh_run",
        "gate_problems",
        "identity_after",
        "input_digests",
        "job_id",
        "launch_token",
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


_is_int = is_int


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
        body = loads_strict(text)
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
        if not in_set(status, TIER_STATUSES):
            problems.append(f"{name}: invalid status {str(status)[:40]!r}")
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


def _evidence_problems(artifact: Mapping[str, Any]) -> list[str]:
    evidence = artifact["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "gate_report_bytes",
        "gate_report_sha256",
    }:
        return ["evidence must reference the child's complete gate report"]
    digest = evidence["gate_report_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or not _is_int(evidence["gate_report_bytes"])
    ):
        return ["evidence reference is not an authenticated gate report digest"]
    return []


def _identity_problems(
    artifact: Mapping[str, Any], body: Mapping[str, Any], mode: str
) -> list[str]:
    problems: list[str] = []
    expected = expected_identity(body, mode)
    for key in ("commit", "build_identity", "asset_digest", "input_digests"):
        if artifact[key] != expected[key]:
            problems.append(f"{key} measured by the job differs from the frozen plan")
    if artifact["clean_head"] is not True:
        problems.append("the job did not measure a clean checkout head")
    after = artifact["identity_after"]
    expected_after = {
        "commit": artifact["commit"],
        "clean_head": artifact["clean_head"],
        "build_identity": artifact["build_identity"],
        "input_digests": artifact["input_digests"],
        "asset_digest": artifact["asset_digest"],
    }
    if after != expected_after:
        problems.append("identity measured after the run differs from the one before it")
    return problems


def _validate(
    artifact: Mapping[str, Any],
    plan: ParallelPlan,
    job_id: str,
    child_exit_status: int | None,
    child_pid: int | None,
    expected_token: str | None,
    window: tuple[Any, Any] | None,
) -> list[str]:
    body = plan.as_dict()
    job = plan.job(job_id)
    extra = sorted(set(artifact) - ARTIFACT_KEYS)
    missing = sorted(ARTIFACT_KEYS - set(artifact))
    if extra or missing:
        return [f"artifact keys differ (unknown {extra}, missing {missing})"]
    problems: list[str] = []
    if artifact["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        problems.append("artifact schema version mismatch")
    if artifact["run_id"] != body["run_id"]:
        problems.append("run_id does not match the frozen plan (stale or wrong identity)")
    if artifact["plan_sha256"] != plan.digest:
        problems.append("plan_sha256 does not match the frozen plan")
    problems.extend(_identity_problems(artifact, body, job["mode"]))
    if artifact["job_id"] != job_id or artifact["mode"] != job["mode"]:
        problems.append("job or runtime identity does not match the plan")
    if expected_token is not None and artifact["launch_token"] != expected_token:
        problems.append("launch token does not bind this artifact to this execution")
    if not isinstance(artifact["launch_token"], str) or not artifact["launch_token"]:
        problems.append("launch token is missing")
    problems.extend(timestamp_problems(artifact["started_utc"], artifact["finished_utc"], window))
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
        problems.append(f"cancelled: {str(artifact['cancellation'])[:100]}")
    if artifact["overall"] != "PASS":
        problems.append(f"overall {str(artifact['overall'])[:40]!r}")
    problems.extend(_evidence_problems(artifact))
    problems.extend(guarded("tiers", _tier_problems, job, body, artifact))
    if isinstance(artifact["tiers"], list):
        problems.extend(
            guarded("rows", row_problems, artifact["rows"], job, body, artifact["tiers"])
        )
    return problems


def validate_terminal_artifact(
    artifact: object,
    plan: ParallelPlan,
    job_id: str,
    *,
    child_exit_status: int | None,
    child_pid: int | None = None,
    expected_token: str | None = None,
    window: tuple[Any, Any] | None = None,
) -> list[str]:
    """Return every reason the artifact cannot authorize this job's PASS; never raises."""

    if not isinstance(artifact, dict):
        return ["artifact is not an object"]
    return guarded(
        "artifact",
        _validate,
        artifact,
        plan,
        job_id,
        child_exit_status,
        child_pid,
        expected_token,
        window,
    )


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
        "phase": job["phase"],
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
            expected_token=outcome.get("launch_token"),
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
