"""Immutable shared plan for opt-in concurrent runtime gates (#107.1).

The plan is frozen as canonical JSON *before* any job is dispatched.  Children
and the aggregator never trust a mutable caller claim: they re-validate the
serialized text, recompute its digest, and compare identities against it.
Nothing here runs a gate, starts a process, or touches the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scripts.production_gate_model import DEFAULT_TIERS, RUNTIME_MODES, TIER_EXPRESSIONS

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
    {"deadline_seconds", "job_id", "mode", "pairs", "steps", "tiers", "timing_rep_ids"}
)
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


def job_id_for(mode: str) -> str:
    return f"{mode}-job"


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
    mode = job["mode"]
    if mode not in RUNTIME_MODES:
        problems.append(f"{label}: unknown runtime mode {mode!r}")
    if job["job_id"] != job_id_for(str(mode)):
        problems.append(f"{label}: job_id must be {job_id_for(str(mode))!r}")
    if list(job["tiers"]) != list(plan["tiers"]):
        problems.append(f"{label}: tiers must equal the plan tiers in order")
    deadline = job["deadline_seconds"]
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline <= 0:
        problems.append(f"{label}: deadline_seconds must be a positive number")
    pairs = job["pairs"]
    if not _is_int(pairs) or pairs < 1:
        problems.append(f"{label}: pairs must be a positive integer")
    if job["timing_rep_ids"] != timing_rep_ids(plan["repetitions"]):
        problems.append(f"{label}: timing_rep_ids must match the declared repetitions")
    steps = job["steps"]
    expected = [
        {"mode": mode, "tier": tier, "role": role}
        for role in ("selected-tier",)
        for tier in plan["tiers"]
    ]
    if plan["early_smoke"]:
        expected = [{"mode": mode, "tier": "smoke", "role": "additional-probe"}, *expected]
    if not isinstance(steps, list) or steps != expected:
        problems.append(f"{label}: steps do not match the declared tiers and smoke role")
    return problems


def validate_plan_body(body: object) -> list[str]:
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
    if not isinstance(body["build_identity"], str) or not body["build_identity"]:
        problems.append("build_identity must be a non-empty string")
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
    modes = [job.get("mode") if isinstance(job, dict) else None for job in jobs]
    if len(set(map(str, modes))) != len(modes):
        problems.append("jobs must not repeat a runtime mode")
    for index, job in enumerate(jobs):
        problems.extend(_validate_job(index, job, body))
    if isinstance(capacity, dict) and _is_int(capacity.get("total_pairs")):
        shares = [job.get("pairs") for job in jobs if isinstance(job, dict)]
        if all(_is_int(share) for share in shares) and sum(shares) > capacity["total_pairs"]:
            problems.append("job pair shares exceed the single global pair budget")
    if body["plan_sha256"] != _digest_of(body):
        problems.append("plan_sha256 does not match the plan contents")
    return problems


def load_plan(text: str) -> ParallelPlan:
    """Decode and strictly validate serialized plan text."""

    try:
        body = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"plan is not valid JSON: {type(exc).__name__}") from exc
    problems = validate_plan_body(body)
    if problems:
        raise PlanError("; ".join(problems))
    if canonical_json(body) != text:
        raise PlanError("plan text is not in canonical form")
    return ParallelPlan(text=text, digest=str(body["plan_sha256"]))


def build_parallel_plan(
    *,
    commit: str,
    build_identity: str,
    input_digests: Mapping[str, str],
    asset_digest: str,
    games: Sequence[str],
    roles: Sequence[str],
    modes: Sequence[str],
    tiers: Sequence[str] = DEFAULT_TIERS,
    required_nodeids_by_tier: Mapping[str, Sequence[str]] | None = None,
    repetitions: int = MIN_TIMING_REPETITIONS,
    deadline_seconds: float,
    total_pairs: int,
    pairs_per_job: Mapping[str, int] | None = None,
    policy_digest: str,
    early_smoke: bool,
    fail_fast: bool,
    require_row_ids: bool = True,
    run_id: str | None = None,
    created_utc: str | None = None,
) -> ParallelPlan:
    """Freeze a validated plan; raises :class:`PlanError` instead of guessing."""

    modes = tuple(modes)
    if not modes or len(set(modes)) != len(modes):
        raise PlanError("plan requires distinct explicit runtime modes")
    tiers = tuple(tiers)
    shares = {mode: (pairs_per_job or {}).get(mode, total_pairs // len(modes)) for mode in modes}
    required_source = required_nodeids_by_tier or {}
    required = {tier: sorted(set(map(str, required_source.get(tier, ())))) for tier in tiers}
    jobs = []
    for mode in modes:
        steps = [{"mode": mode, "tier": tier, "role": "selected-tier"} for tier in tiers]
        if early_smoke:
            steps = [{"mode": mode, "tier": "smoke", "role": "additional-probe"}, *steps]
        jobs.append(
            {
                "job_id": job_id_for(mode),
                "mode": mode,
                "tiers": list(tiers),
                "steps": steps,
                "deadline_seconds": deadline_seconds,
                "pairs": shares[mode],
                "timing_rep_ids": timing_rep_ids(repetitions),
            }
        )
    body: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "run_id": run_id or uuid.uuid4().hex,
        "commit": commit,
        "build_identity": build_identity,
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
        "jobs": jobs,
        "created_utc": created_utc or datetime.now(UTC).isoformat(timespec="seconds"),
    }
    body["plan_sha256"] = _digest_of(body)
    problems = validate_plan_body(body)
    if problems:
        raise PlanError("; ".join(problems))
    return ParallelPlan(text=canonical_json(body), digest=body["plan_sha256"])
