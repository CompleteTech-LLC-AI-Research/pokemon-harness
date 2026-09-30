"""Versioned capacity policy, fact validation, and admission evaluation.

Split from ``scripts/gate_capacity.py`` for issue #122 with no behavior
change: every function and class below is copied verbatim from that module.
The public surface stays importable from ``scripts.gate_capacity``, which
re-exports each name so existing callers and monkeypatch targets are
unaffected.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POLICY_SCHEMA_VERSION = 2
ADMISSION_STATUSES = ("ok", "queued", "blocked", "expired")
CAPACITY_OUTCOMES = ("ok", "blocked", "failed", "unsupported")
NOT_APPLICABLE = "not_applicable"
LIFECYCLE_STATES = ("running", "completed", "interrupted", "not_started")
_REQUIRED_FACT_KEYS = (
    "platform",
    "logical_cpus",
    "affinity_cpus",
    "affinity_count",
    "affinity_supported",
    "cgroup_version",
    "cpu_quota_cores",
    "cpu_quota_status",
    "cgroup_cpu_some_avg300",
    "cpu_throttled",
    "load_average",
    "cpu_weight",
    "memory_total_bytes",
    "memory_available_bytes",
    "repo_disk_free_bytes",
    "psi_cpu_some_avg300",
    "unsupported",
)
_PRESSURE_METRIC = "some"


def _load_qualification_runner() -> Any:
    """Resolve the sibling fact collector for both script and package import."""

    try:  # Imported as ``scripts.gate_capacity`` (tests and the package).
        from scripts import qualification_runner  # type: ignore[import-not-found]

        return qualification_runner
    except ImportError:  # pragma: no cover - executed as a top-level script.
        path = Path(__file__).resolve().parent / "qualification_runner.py"
        spec = importlib.util.spec_from_file_location("gate_capacity_qualification_runner", path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive.
            raise
        module = importlib.util.module_from_spec(spec)
        # Register the module before execution: its ``from __future__ import
        # annotations`` string annotations are resolved by ``@dataclass`` via
        # ``sys.modules[cls.__module__]``.  Leaving it unregistered raises
        # ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        return module


qualification_runner = _load_qualification_runner()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
    )


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class CapacityPolicy:
    """Operator-declared capacity thresholds for one qualification runner."""

    policy_version: int
    runner_id: str
    effective_cpus: int
    max_concurrent_pairs: int
    memory_bytes_min: int
    disk_free_bytes_min: int
    observation_seconds: float
    admission_deadline_seconds: float
    max_system_some_avg300: float
    max_cgroup_some_avg300: float
    max_load_per_cpu: float
    measurement_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_policy(policy: CapacityPolicy) -> list[str]:
    """Return explicit problems for a declared policy; never guess a threshold."""

    problems: list[str] = []
    if policy.policy_version != POLICY_SCHEMA_VERSION:
        problems.append(
            f"unsupported policy_version {policy.policy_version!r}; "
            f"expected {POLICY_SCHEMA_VERSION}"
        )
    if not isinstance(policy.runner_id, str) or not policy.runner_id.strip():
        problems.append("runner_id must be a non-empty string")
    if not _is_non_negative_int(policy.effective_cpus) or policy.effective_cpus <= 0:
        problems.append("effective_cpus must be a positive integer")
    if not _is_non_negative_int(policy.max_concurrent_pairs) or policy.max_concurrent_pairs <= 0:
        problems.append("max_concurrent_pairs must be a positive integer")
    if not _is_non_negative_int(policy.memory_bytes_min):
        problems.append("memory_bytes_min must be a non-negative integer")
    if not _is_non_negative_int(policy.disk_free_bytes_min):
        problems.append("disk_free_bytes_min must be a non-negative integer")
    if not _is_number(policy.observation_seconds) or policy.observation_seconds < 0:
        problems.append("observation_seconds must be a non-negative number")
    if not _is_number(policy.admission_deadline_seconds) or policy.admission_deadline_seconds <= 0:
        problems.append("admission_deadline_seconds must be a positive number")
    for name in ("max_system_some_avg300", "max_cgroup_some_avg300"):
        value = getattr(policy, name)
        if not _is_number(value) or not 0 <= value <= 100:
            problems.append(f"{name} must be a finite percentage in [0, 100]")
    if not _is_number(policy.max_load_per_cpu) or policy.max_load_per_cpu <= 0:
        problems.append("max_load_per_cpu must be finite and positive")
    if (
        not isinstance(policy.measurement_sha256, str)
        or len(policy.measurement_sha256) != 64
        or any(char not in "0123456789abcdef" for char in policy.measurement_sha256)
    ):
        problems.append(
            "measurement_sha256 must identify the operator's measured capacity evidence"
        )
    return problems


def capacity_policy_from_dict(document: dict[str, Any]) -> CapacityPolicy:
    """Build a policy from a JSON object, raising on any structural error."""

    if not isinstance(document, dict):
        raise TypeError("capacity policy must be a JSON object")
    fields = (
        "policy_version",
        "runner_id",
        "effective_cpus",
        "max_concurrent_pairs",
        "memory_bytes_min",
        "disk_free_bytes_min",
        "observation_seconds",
        "admission_deadline_seconds",
        "max_system_some_avg300",
        "max_cgroup_some_avg300",
        "max_load_per_cpu",
        "measurement_sha256",
    )
    missing = [name for name in fields if name not in document]
    if missing:
        raise ValueError(f"capacity policy is missing required fields: {', '.join(missing)}")
    unknown = sorted(set(document) - set(fields))
    if unknown:
        raise ValueError(f"capacity policy has unknown fields: {', '.join(unknown)}")
    try:
        return CapacityPolicy(
            policy_version=document["policy_version"],
            runner_id=document["runner_id"],
            effective_cpus=document["effective_cpus"],
            max_concurrent_pairs=document["max_concurrent_pairs"],
            memory_bytes_min=document["memory_bytes_min"],
            disk_free_bytes_min=document["disk_free_bytes_min"],
            observation_seconds=document["observation_seconds"],
            admission_deadline_seconds=document["admission_deadline_seconds"],
            max_system_some_avg300=document["max_system_some_avg300"],
            max_cgroup_some_avg300=document["max_cgroup_some_avg300"],
            max_load_per_cpu=document["max_load_per_cpu"],
            measurement_sha256=document["measurement_sha256"],
        )
    except (KeyError, TypeError) as exc:  # pragma: no cover - defensive.
        raise ValueError(f"capacity policy could not be constructed: {exc}") from exc


def load_capacity_policy(path: str | Path) -> tuple[CapacityPolicy | None, str | None]:
    """Load and validate a policy; return ``(policy, error)`` without raising."""

    policy_path = Path(path)
    try:
        document = json.loads(policy_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"capacity policy not found: {policy_path}"
    except OSError as exc:
        return None, f"cannot read capacity policy: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"cannot decode capacity policy: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"capacity policy is not valid JSON: {exc}"
    try:
        policy = capacity_policy_from_dict(document)
    except (TypeError, ValueError) as exc:
        return None, str(exc)
    problems = validate_policy(policy)
    if problems:
        return None, "capacity policy is invalid: " + "; ".join(problems)
    return policy, None


def facts_to_dict(facts: Any) -> dict[str, Any]:
    """Normalize a fact object (dataclass or mapping) to a plain dictionary."""

    if isinstance(facts, dict):
        return dict(facts)
    if is_dataclass(facts) and not isinstance(facts, type):
        return asdict(facts)
    raise TypeError(f"unsupported fact object: {type(facts).__name__}")


def validate_facts(data: dict[str, Any]) -> list[str]:
    """Return explicit problems for a malformed fact sample."""

    if not isinstance(data, dict):
        return ["facts must be a mapping"]
    problems = [f"facts missing key: {key}" for key in _REQUIRED_FACT_KEYS if key not in data]
    if problems:
        return problems
    if not isinstance(data["platform"], str) or not data["platform"]:
        problems.append("platform must be a non-empty string")
    if not _is_non_negative_int(data["logical_cpus"]):
        problems.append("logical_cpus must be a non-negative integer")
    if not isinstance(data["affinity_cpus"], list):
        problems.append("affinity_cpus must be a list")
    if not _is_non_negative_int(data["affinity_count"]):
        problems.append("affinity_count must be a non-negative integer")
    if not isinstance(data["affinity_supported"], bool):
        problems.append("affinity_supported must be a boolean")
    if not isinstance(data["unsupported"], list):
        problems.append("unsupported must be a list")
    if not isinstance(data["cgroup_version"], str):
        problems.append("cgroup_version must be a string")
    for key in ("memory_total_bytes", "memory_available_bytes", "repo_disk_free_bytes"):
        value = data[key]
        if value is not None and not _is_non_negative_int(value):
            problems.append(f"{key} must be null or a non-negative integer")
    affinity = data["affinity_cpus"]
    if isinstance(affinity, list):
        if any(not _is_non_negative_int(cpu) for cpu in affinity):
            problems.append("affinity_cpus must contain non-negative integer CPU IDs")
        elif len(set(affinity)) != len(affinity) or len(affinity) != data["affinity_count"]:
            problems.append("affinity_count must match distinct affinity_cpus")
    quota_status = data["cpu_quota_status"]
    if not isinstance(quota_status, str) or quota_status not in {"limited", "unlimited", "unknown"}:
        problems.append("cpu_quota_status must be limited, unlimited, or unknown")
    quota = data["cpu_quota_cores"]
    if quota is not None and (not _is_number(quota) or quota <= 0):
        problems.append("cpu_quota_cores must be null or a finite positive number")
    weight = data["cpu_weight"]
    if weight is not None and not _is_non_negative_int(weight):
        problems.append("cpu_weight must be null or a non-negative integer")
    for key in ("psi_cpu_some_avg300", "cgroup_cpu_some_avg300"):
        psi = data[key]
        if psi is not None and (not _is_number(psi) or not 0 <= psi <= 100):
            problems.append(f"{key} must be null or a finite percentage")
    stat = data["cpu_throttled"]
    if stat is not None and (
        not isinstance(stat, dict)
        or not stat
        or any(
            not isinstance(key, str) or not _is_non_negative_int(value)
            for key, value in stat.items()
        )
    ):
        problems.append("cpu_throttled must be null or nonempty integer CPU statistics")
    load = data["load_average"]
    if load is not None and (
        not isinstance(load, (list, tuple))
        or len(load) != 3
        or any(not _is_number(value) or value < 0 for value in load)
    ):
        problems.append("load_average must be null or three finite non-negative numbers")
    return problems


@dataclass
class CapacitySample:
    """One timestamped observation of host/cgroup capacity."""

    sequence: int
    monotonic_seconds: float
    utc_timestamp: str
    status: str
    problems: list[str]
    facts: dict[str, Any]


def sample_facts(
    repo_root: str | Path,
    *,
    temp_root: str | Path | None = None,
    sequence: int = 0,
    clock: Any | None = None,
) -> CapacitySample:
    """Collect one read-only fact sample, reusing the qualification runner."""

    monotonic = clock or time.monotonic
    now = monotonic()
    try:
        raw = qualification_runner.collect_facts(Path(repo_root), temp_root)
        data = facts_to_dict(raw)
    except Exception as exc:  # noqa: BLE001 - an unreadable host is an explicit sample.
        return CapacitySample(
            sequence=sequence,
            monotonic_seconds=now,
            utc_timestamp=_utc_now(),
            status="failed",
            problems=[f"fact collection failed: {type(exc).__name__}: {exc}"],
            facts={},
        )
    problems = validate_facts(data)
    return CapacitySample(
        sequence=sequence,
        monotonic_seconds=now,
        utc_timestamp=_utc_now(),
        status="malformed" if problems else "ok",
        problems=problems,
        facts=data,
    )


def evaluate_capacity(policy: CapacityPolicy, data: dict[str, Any]) -> tuple[str, list[str]]:
    """Compare declared thresholds against observed facts.

    Returns an explicit outcome in ``("ok", "blocked", "unsupported")``.  An
    unsupported or missing observation is never silently reported as ``ok``.
    """

    problems = validate_facts(data)
    if problems:
        return "unsupported", ["malformed capacity sample: " + "; ".join(problems)]

    blocked: list[str] = []
    unsupported: list[str] = []

    if data["affinity_supported"]:
        observed_cpus = data["affinity_count"]
    else:
        observed_cpus = None
    if observed_cpus is None or observed_cpus <= 0:
        unsupported.append("effective CPU allocation is unavailable")
    elif observed_cpus < policy.effective_cpus:
        blocked.append(
            f"effective CPUs {observed_cpus} below declared minimum {policy.effective_cpus}"
        )

    # Unknown quota is never evidence of unlimited capacity.
    quota_status = data.get("cpu_quota_status", "unknown")
    if quota_status not in {"limited", "unlimited"}:
        unsupported.append("effective cgroup CPU quota is unknown")
    quota = data["cpu_quota_cores"]
    if quota_status == "limited" and (quota is None or quota <= 0):
        unsupported.append("limited quota lacks a positive observed bound")
    if quota_status == "unlimited" and quota is not None:
        unsupported.append("unlimited quota contradicts its observed bound")
    if quota is not None and quota < policy.effective_cpus:
        blocked.append(
            f"cgroup CPU quota {quota} cores below declared minimum {policy.effective_cpus}"
        )

    for key, maximum in (
        ("psi_cpu_some_avg300", policy.max_system_some_avg300),
        ("cgroup_cpu_some_avg300", policy.max_cgroup_some_avg300),
    ):
        pressure = data.get(key)
        if not _is_number(pressure) or not 0 <= pressure <= 100:
            unsupported.append(f"{key} is unavailable or malformed")
        elif pressure > maximum:
            blocked.append(f"{key} {pressure:g}% exceeds declared maximum {maximum:g}%")
    load = data.get("load_average")
    if (
        not isinstance(load, (list, tuple))
        or len(load) != 3
        or any(not _is_number(value) or value < 0 for value in load)
    ):
        unsupported.append("load average is unavailable or malformed")
    elif max(load) > policy.max_load_per_cpu * policy.effective_cpus:
        blocked.append("load per allocated CPU exceeds declared maximum")
    if data.get("cpu_throttled") is None:
        unsupported.append("cgroup CPU stat is unavailable")

    if policy.memory_bytes_min > 0:
        available_memory = data["memory_available_bytes"]
        if available_memory is None:
            unsupported.append("available memory is unavailable")
        elif available_memory < policy.memory_bytes_min:
            blocked.append(
                f"available memory {available_memory} below declared minimum "
                f"{policy.memory_bytes_min}"
            )

    if policy.disk_free_bytes_min > 0:
        free_disk = data["repo_disk_free_bytes"]
        if free_disk is None:
            unsupported.append("repo disk free space is unavailable")
        elif free_disk < policy.disk_free_bytes_min:
            blocked.append(
                f"repo disk free {free_disk} below declared minimum {policy.disk_free_bytes_min}"
            )

    if blocked:
        return "blocked", blocked
    if unsupported:
        return "unsupported", unsupported
    return "ok", []
