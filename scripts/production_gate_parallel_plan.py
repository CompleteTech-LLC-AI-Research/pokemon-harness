"""Immutable shared plan for opt-in concurrent runtime gates (#107.1).

The plan is frozen as canonical JSON *before* any job is dispatched.  Children
and the aggregator never trust a mutable caller claim: they re-validate the
serialized text, recompute its digest, and compare identities against it.
Nothing here runs a gate, starts a process, or touches the network.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scripts.production_gate_model import DEFAULT_TIERS, RUNTIME_MODES, TIER_EXPRESSIONS
from scripts.production_gate_parallel_validation import (
    guarded,
    is_positive_number,
    loads_strict,
)

PLAN_SCHEMA_VERSION = 1
MIN_TIMING_REPETITIONS = 5
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

PLAN_KEYS = frozenset(
    {
        "asset_digest",
        "build_identity",
        "capacity",
        "commit",
        "created_utc",
        "deadlines",
        "early_smoke",
        "fail_fast",
        "games",
        "input_digests",
        "jobs",
        "plan_sha256",
        "repetitions",
        "require_row_ids",
        "required_nodeids",
        "roles",
        "run_id",
        "schema_version",
        "tiers",
    }
)
JOB_KEYS = frozenset(
    {"deadline_seconds", "job_id", "mode", "pairs", "phase", "steps", "tiers", "timing_rep_ids"}
)
DEADLINE_KEYS = frozenset(
    {
        "cleanup_seconds",
        "measurement_seconds",
        "preflight_seconds",
        "repetition_seconds",
        "row_seconds",
        "tier_seconds",
    }
)
PHASES = ("smoke", "selected")
STEP_KEYS = frozenset({"mode", "role", "tier"})
CAPACITY_KEYS = frozenset({"policy_digest", "total_pairs"})


class PlanError(ValueError):
    """Raised when a serialized plan is not a valid, unmodified frozen plan."""


def canonical_json(value: Any) -> str:
    """Return the one canonical text form used for hashing and transport."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_of(body: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in body.items() if key != "plan_sha256"}
    return hashlib.sha256(canonical_json(unsigned).encode("ascii")).hexdigest()


def job_id_for(mode: str, phase: str = "selected") -> str:
    return f"{mode}-job" if phase == "selected" else f"{mode}-{phase}-job"


def derive_job_deadline(
    deadlines: Mapping[str, float], steps: Sequence[Mapping[str, Any]], repetitions: int
) -> float:
    """Outer job bound: preflight + every step (timing = repetitions) + cleanup; inner kept."""

    # The worker measures identity before and after its gate run: two measurement terms.
    total = (
        float(deadlines["preflight_seconds"])
        + float(deadlines["cleanup_seconds"])
        + 2 * float(deadlines["measurement_seconds"])
    )
    for step in steps:
        if step["tier"] == "timing":
            total += repetitions * float(deadlines["repetition_seconds"])
        else:
            total += float(deadlines["tier_seconds"])
    return total


def timing_rep_ids(repetitions: int) -> list[str]:
    return [f"timing-rep-{index}" for index in range(1, repetitions + 1)]


@dataclass(frozen=True)
class ParallelPlan:
    """A validated plan.  ``text`` is the frozen canonical form; ``as_dict`` copies it."""

    text: str
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.text)

    @property
    def run_id(self) -> str:
        return str(self.as_dict()["run_id"])

    def job(self, job_id: str) -> dict[str, Any]:
        for job in self.as_dict()["jobs"]:
            if job["job_id"] == job_id:
                return job
        raise PlanError(f"unknown job {job_id!r}")

    def job_ids(self) -> list[str]:
        return [job["job_id"] for job in self.as_dict()["jobs"]]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unknown(label: str, body: Mapping[str, Any], allowed: frozenset[str]) -> list[str]:
    extra = sorted(set(body) - allowed)
    missing = sorted(allowed - set(body))
    problems = [f"{label}: unknown keys {extra}"] if extra else []
    if missing:
        problems.append(f"{label}: missing keys {missing}")
    return problems


def _validate_digests(label: str, value: object) -> list[str]:
    if not isinstance(value, dict) or not value:
        return [f"{label} must be a non-empty object"]
    return [
        f"{label}[{name!r}] must be a lowercase sha256"
        for name, digest in sorted(value.items())
        if not isinstance(name, str) or not name or not SHA256_RE.match(str(digest))
    ]


def _validate_job(index: int, job: object, plan: Mapping[str, Any]) -> list[str]:
    label = f"jobs[{index}]"
    if not isinstance(job, dict):
        return [f"{label} must be an object"]
    problems = _unknown(label, job, JOB_KEYS)
    if problems:
        return problems
    mode, phase = job["mode"], job["phase"]
    if mode not in RUNTIME_MODES:
        problems.append(f"{label}: unknown runtime mode {mode!r}")
    if phase not in PHASES or (phase == "smoke" and not plan["early_smoke"]):
        problems.append(f"{label}: phase {phase!r} is not declared by the plan")
        return problems
    if job["job_id"] != job_id_for(str(mode), phase):
        problems.append(f"{label}: job_id must be {job_id_for(str(mode), phase)!r}")
    if phase == "smoke":
        expected = [{"mode": mode, "tier": "smoke", "role": "additional-probe"}]
        expected_tiers = ["smoke"]
        expected_reps: list[str] = []
    else:
        expected = [{"mode": mode, "tier": tier, "role": "selected-tier"} for tier in plan["tiers"]]
        expected_tiers = list(plan["tiers"])
        expected_reps = timing_rep_ids(plan["repetitions"]) if "timing" in plan["tiers"] else []
    if list(job["tiers"]) != expected_tiers:
        problems.append(f"{label}: tiers must equal the phase tiers in order")
    pairs = job["pairs"]
    if not _is_int(pairs) or pairs < 1:
        problems.append(f"{label}: pairs must be a positive integer")
    if job["timing_rep_ids"] != expected_reps:
        problems.append(f"{label}: timing_rep_ids must match the declared repetitions")
    if not isinstance(job["steps"], list) or job["steps"] != expected:
        problems.append(f"{label}: steps do not match the declared tiers and smoke role")
    deadlines = plan["deadlines"]
    if not is_positive_number(job["deadline_seconds"]):
        problems.append(f"{label}: deadline_seconds must be a finite positive number")
    elif (
        isinstance(deadlines, dict)
        and not _unknown("deadlines", deadlines, DEADLINE_KEYS)
        and all(is_positive_number(v) for v in deadlines.values())
        and job["deadline_seconds"] != derive_job_deadline(deadlines, expected, plan["repetitions"])
    ):
        problems.append(f"{label}: deadline_seconds is not derived from the plan deadlines")
    return problems


def _validate_plan_body(body: object) -> list[str]:
    """Return every problem with a decoded plan; an empty list means valid."""

    if not isinstance(body, dict):
        return ["plan must be a JSON object"]
    problems = _unknown("plan", body, PLAN_KEYS)
    if problems:
        return problems
    if body["schema_version"] != PLAN_SCHEMA_VERSION or not _is_int(body["schema_version"]):
        problems.append(f"schema_version must be {PLAN_SCHEMA_VERSION}")
    if not RUN_ID_RE.match(str(body["run_id"])):
        problems.append("run_id must be a 32 character lowercase hex UUID")
    if not COMMIT_RE.match(str(body["commit"])):
        problems.append("commit must be an exact 40 character lowercase sha")
    builds = body["build_identity"]
    if (
        not isinstance(builds, dict)
        or not builds
        or not all(isinstance(k, str) and SHA256_RE.match(str(v)) for k, v in builds.items())
    ):
        problems.append("build_identity must map each runtime to a lowercase sha256")
    elif isinstance(body["jobs"], list):
        used = {job.get("mode") for job in body["jobs"] if isinstance(job, dict)}
        if used - set(builds):
            problems.append("build_identity lacks a runtime used by the jobs")
    if not SHA256_RE.match(str(body["asset_digest"])):
        problems.append("asset_digest must be a lowercase sha256")
    problems.extend(_validate_digests("input_digests", body["input_digests"]))
    for name in ("games", "roles"):
        value = body[name]
        if (
            not isinstance(value, list)
            or not value
            or len(set(map(str, value))) != len(value)
            or not all(isinstance(item, str) and IDENTIFIER_RE.match(item) for item in value)
        ):
            problems.append(f"{name} must be a non-empty list of distinct identifiers")
    tiers = body["tiers"]
    if (
        not isinstance(tiers, list)
        or not tiers
        or len(set(map(str, tiers))) != len(tiers)
        or any(tier not in TIER_EXPRESSIONS for tier in tiers)
    ):
        problems.append("tiers must be a non-empty list of distinct known tiers")
        return problems
    if not _is_int(body["repetitions"]) or body["repetitions"] < MIN_TIMING_REPETITIONS:
        problems.append(f"repetitions must be an integer >= {MIN_TIMING_REPETITIONS}")
        return problems
    for name in ("early_smoke", "fail_fast", "require_row_ids"):
        if type(body[name]) is not bool:
            problems.append(f"{name} must be a boolean")
    if body["require_row_ids"] is not True:
        problems.append("require_row_ids must be true: row identity is mandatory")
    deadlines = body["deadlines"]
    if not isinstance(deadlines, dict):
        problems.append("deadlines must be an object")
    else:
        problems.extend(_unknown("deadlines", deadlines, DEADLINE_KEYS))
        for name, value in sorted(deadlines.items()):
            if not is_positive_number(value):
                problems.append(f"deadlines[{name!r}] must be a finite positive number")
    if body["early_smoke"] and "smoke" in tiers:
        problems.append("an additional smoke cannot also be a selected tier")
    required = body["required_nodeids"]
    if not isinstance(required, dict) or set(required) - set(tiers):
        problems.append("required_nodeids must map only selected tiers to lists")
    else:
        for tier, ids in required.items():
            if (
                not isinstance(ids, list)
                or ids != sorted(set(map(str, ids)))
                or not all(isinstance(item, str) and item for item in ids)
            ):
                problems.append(f"required_nodeids[{tier!r}] must be a sorted list of unique ids")
    capacity = body["capacity"]
    if not isinstance(capacity, dict):
        problems.append("capacity must be an object")
    else:
        problems.extend(_unknown("capacity", capacity, CAPACITY_KEYS))
        if not _is_int(capacity.get("total_pairs")) or capacity.get("total_pairs", 0) < 1:
            problems.append("capacity.total_pairs must be a positive integer")
        if not SHA256_RE.match(str(capacity.get("policy_digest"))):
            problems.append("capacity.policy_digest must be a lowercase sha256")
    jobs = body["jobs"]
    if not isinstance(jobs, list) or not jobs:
        problems.append("jobs must be a non-empty list")
        return problems
    keys = [(job.get("mode"), job.get("phase")) if isinstance(job, dict) else None for job in jobs]
    if len(set(map(str, keys))) != len(keys):
        problems.append("jobs must not repeat a runtime mode within a phase")
    for index, job in enumerate(jobs):
        problems.extend(_validate_job(index, job, body))
    if isinstance(capacity, dict) and _is_int(capacity.get("total_pairs")):
        for phase in PHASES:
            shares = [
                j.get("pairs") for j in jobs if isinstance(j, dict) and j.get("phase") == phase
            ]
            if all(_is_int(share) for share in shares) and sum(shares) > capacity["total_pairs"]:
                problems.append("job pair shares exceed the single global pair budget")
    if body["plan_sha256"] != _digest_of(body):
        problems.append("plan_sha256 does not match the plan contents")
    return problems


def validate_plan_body(body: object) -> list[str]:
    """Total over decoded JSON: any unexpected shape becomes a problem, never an exception."""

    return guarded("plan", _validate_plan_body, body)


def load_plan(text: str) -> ParallelPlan:
    """Decode and strictly validate serialized plan text."""

    try:
        body = loads_strict(text)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"plan is not valid JSON: {type(exc).__name__}") from exc
    problems = validate_plan_body(body)
    if problems:
        raise PlanError("; ".join(problems))
    try:
        canonical = canonical_json(body)
    except (TypeError, ValueError, RecursionError) as exc:
        raise PlanError(f"plan cannot be canonicalised: {type(exc).__name__}") from exc
    if canonical != text:
        raise PlanError("plan text is not in canonical form")
    return ParallelPlan(text=text, digest=str(body["plan_sha256"]))


def build_parallel_plan(
    *,
    commit: str,
    build_identity: Mapping[str, str],
    input_digests: Mapping[str, str],
    asset_digest: str,
    games: Sequence[str],
    roles: Sequence[str],
    modes: Sequence[str],
    tiers: Sequence[str] = DEFAULT_TIERS,
    required_nodeids_by_tier: Mapping[str, Sequence[str]] | None = None,
    repetitions: int = MIN_TIMING_REPETITIONS,
    deadline_seconds: float | None = None,
    deadlines: Mapping[str, float] | None = None,
    total_pairs: int,
    pairs_per_job: Mapping[str, int] | None = None,
    policy_digest: str,
    early_smoke: bool,
    fail_fast: bool,
    require_row_ids: bool = True,
    matrix_workers: int | None = None,
    run_id: str | None = None,
    created_utc: str | None = None,
) -> ParallelPlan:
    """Freeze a validated plan; raises :class:`PlanError` instead of guessing."""

    modes = tuple(modes)
    if not modes or len(set(modes)) != len(modes):
        raise PlanError("plan requires distinct explicit runtime modes")
    tiers = tuple(tiers)
    ceiling = total_pairs // len(modes)
    if matrix_workers is not None:
        ceiling = min(ceiling, matrix_workers)
    shares = {mode: (pairs_per_job or {}).get(mode, ceiling) for mode in modes}
    required_source = required_nodeids_by_tier or {}
    required = {tier: sorted(set(map(str, required_source.get(tier, ())))) for tier in tiers}
    if deadlines is None:
        if not is_positive_number(deadline_seconds):
            raise PlanError("deadline_seconds must be a finite positive number")
        parts = len(tiers) + 4
        unit = float(deadline_seconds) / parts
        deadlines = {
            "preflight_seconds": unit,
            "row_seconds": unit,
            "tier_seconds": unit,
            "repetition_seconds": unit / max(int(repetitions), 1),
            "cleanup_seconds": unit,
            "measurement_seconds": unit,
        }
    deadlines = {key: deadlines[key] for key in sorted(deadlines)}
    phases = (["smoke"] if early_smoke else []) + ["selected"]
    jobs = []
    for phase in phases:
        for mode in modes:
            if phase == "smoke":
                steps = [{"mode": mode, "tier": "smoke", "role": "additional-probe"}]
                job_tiers, reps = ["smoke"], []
            else:
                steps = [{"mode": mode, "tier": tier, "role": "selected-tier"} for tier in tiers]
                job_tiers = list(tiers)
                reps = timing_rep_ids(repetitions) if "timing" in tiers else []
            jobs.append(
                {
                    "job_id": job_id_for(mode, phase),
                    "mode": mode,
                    "phase": phase,
                    "tiers": job_tiers,
                    "steps": steps,
                    "deadline_seconds": derive_job_deadline(deadlines, steps, repetitions),
                    "pairs": shares[mode],
                    "timing_rep_ids": reps,
                }
            )
    body: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "run_id": run_id or uuid.uuid4().hex,
        "commit": commit,
        "build_identity": dict(sorted(build_identity.items())),
        "input_digests": dict(sorted(input_digests.items())),
        "asset_digest": asset_digest,
        "games": list(games),
        "roles": list(roles),
        "tiers": list(tiers),
        "required_nodeids": required,
        "repetitions": repetitions,
        "early_smoke": early_smoke,
        "fail_fast": fail_fast,
        "require_row_ids": require_row_ids,
        "capacity": {"total_pairs": total_pairs, "policy_digest": policy_digest},
        "deadlines": deadlines,
        "jobs": jobs,
        "created_utc": created_utc or datetime.now(UTC).isoformat(timespec="seconds"),
    }
    body["plan_sha256"] = _digest_of(body)
    problems = validate_plan_body(body)
    if problems:
        raise PlanError("; ".join(problems))
    return ParallelPlan(text=canonical_json(body), digest=body["plan_sha256"])


MEASUREMENT_SECONDS = 300.0


def cli_deadlines(
    args: Any,
    required: Mapping[str, Sequence[str]],
    pairs: int,
    cleanup_seconds: float,
    tiers: Sequence[str] = (),
) -> dict[str, float]:
    """Finite phase/row/tier/repetition/measurement/cleanup bounds from the work actually selected.

    ``pairs`` is the EFFECTIVE per-job worker count (after any explicit ``--matrix-workers``
    ceiling); ``--matrix-timeout-seconds`` is the gate's aggregate override, not a row budget;
    preflight covers the runtime probe plus one bounded collection per selected tier.
    """

    from scripts.production_gate_model import (
        COLLECTION_TIMEOUT_SECONDS,
        DEFAULT_TIMEOUT_SECONDS,
        MATRIX_AGGREGATE_GRACE_SECONDS,
        MATRIX_CASE_TIMEOUT_SECONDS,
    )

    tier = (
        float(args.timeout_seconds)
        if args.timeout_seconds
        else max(DEFAULT_TIMEOUT_SECONDS.values())
    )
    row = max(MATRIX_CASE_TIMEOUT_SECONDS.values())
    largest = max((len(items) for items in required.values()), default=0)
    aggregate = (
        float(args.matrix_timeout_seconds)
        if args.matrix_timeout_seconds
        else row * math.ceil(max(largest, 1) / max(pairs, 1))
    )
    matrix = aggregate + MATRIX_AGGREGATE_GRACE_SECONDS
    return {
        "preflight_seconds": float(COLLECTION_TIMEOUT_SECONDS) * (1 + max(len(tiers), 1)),
        "measurement_seconds": MEASUREMENT_SECONDS,
        "row_seconds": row,
        "tier_seconds": max(tier, matrix),
        "repetition_seconds": tier,
        "cleanup_seconds": cleanup_seconds,
    }
