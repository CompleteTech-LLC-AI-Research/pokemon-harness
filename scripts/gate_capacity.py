"""Versioned capacity policy, bounded admission, and low-overhead telemetry.

The production gate runs emulator pairs whose deadlines are sensitive to CPU
scheduling.  This module adds a declared, operator-owned capacity policy and a
deterministic slot controller that decides whether more work may be admitted.
It exists so that a host without the declared capacity is reported as
``blocked`` (or ``unsupported``) instead of being confused with a product test
failure.

Design rules:

* The policy is a versioned JSON document.  Every operating threshold is
  *declared* by the operator; nothing here derives a threshold from the
  exploratory #84 PSI/load condition.
* Host facts are collected once per observation by reusing
  :func:`scripts.qualification_runner.collect_facts`.  An unsupported or
  unavailable field is reported as ``unsupported``; it is never treated as a
  passing observation.
* System and cgroup CPU PSI ``some`` and load use measured, declared limits.
  ``full=0`` is never used as proof of available capacity.
* The admission controller has an injectable clock, never pauses, reprioritizes,
  or enlarges the deadline of an active owner, and never reinterprets a failed
  test as a pass.
* Telemetry collection failure is recorded explicitly and can never crash
  cleanup or erase an original failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import math
import sys
import threading
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


@dataclass
class AdmissionDecision:
    """One deterministic admission outcome for a queued emulator pair."""

    pair_id: str
    status: str
    reason: str
    sequence: int
    virtual_seconds: float
    owner: bool = False
    promoted: bool = False


class CapacityAdmission:
    """Deterministic, injectable-clock slot controller.

    An active owner is never paused, reprioritized, or deadline-enlarged.  A
    release promotes exactly one queued pair, preserving FIFO order.
    """

    def __init__(
        self,
        policy: CapacityPolicy,
        *,
        clock: Any | None = None,
        availability: Any | None = None,
        deadline_seconds: float | None = None,
    ) -> None:
        self.policy = policy
        self.max_concurrent_pairs = policy.max_concurrent_pairs
        self.deadline_seconds = (
            policy.admission_deadline_seconds if deadline_seconds is None else deadline_seconds
        )
        self._clock = clock or time.monotonic
        self._availability = availability
        self.active: dict[str, AdmissionDecision] = {}
        self.queued: list[str] = []
        self.waiting_since: dict[str, float] = {}
        self.decisions: list[AdmissionDecision] = []
        self._admitted: set[str] = set()
        # Expiry is a terminal outcome for a pair id: once a waiter's admission
        # deadline lapses it must never be re-admitted as a fresh row.
        self._expired: set[str] = set()
        # A pair blocked before it could be queued (capacity unavailable at
        # dispatch time) is terminal in the same way, so accounting stays
        # explicit and a retry cannot silently start it as a new row.
        self._blocked: set[str] = set()
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _availability_status(self) -> tuple[str, str]:
        if self._availability is None:
            return "ok", ""
        result = self._availability()
        if isinstance(result, tuple):
            status, reason = result
            return str(status), str(reason)
        return str(result), ""

    @property
    def admitted_count(self) -> int:
        """Distinct pairs that have ever owned a slot, counted idempotently."""

        return len(self._admitted)

    def _record(self, decision: AdmissionDecision) -> AdmissionDecision:
        if decision.status == "ok" and decision.owner:
            self._admitted.add(decision.pair_id)
        self.decisions.append(decision)
        return decision

    def _expire(self, pair_id: str, reason: str, now: float) -> AdmissionDecision:
        """Record a terminal expiry and forget the pair's waiting state."""

        self._expired.add(pair_id)
        self.waiting_since.pop(pair_id, None)
        if pair_id in self.queued:
            self.queued.remove(pair_id)
        return self._record(
            AdmissionDecision(
                pair_id,
                "expired",
                reason,
                self._next_sequence(),
                now,
                owner=False,
            )
        )

    def block(self, pair_id: str, reason: str) -> AdmissionDecision:
        """Record a terminal pre-dispatch block for a pair that never queued.

        A tier blocked before dispatch never reaches :meth:`admit`, so without
        an explicit decision its planned rows would vanish from admission and
        lifecycle accounting.  The block is terminal exactly like an expiry: a
        later ``admit`` of the same pair keeps returning ``blocked`` instead of
        granting it a fresh slot.
        """

        now = self._clock()
        self._blocked.add(pair_id)
        self.waiting_since.pop(pair_id, None)
        if pair_id in self.queued:
            self.queued.remove(pair_id)
        return self._record(
            AdmissionDecision(
                pair_id,
                "blocked",
                reason,
                self._next_sequence(),
                now,
                owner=False,
            )
        )

    def admit(self, pair_id: str) -> AdmissionDecision:
        """Return ``ok``, ``queued``, ``blocked`` or ``expired`` for ``pair_id``."""

        now = self._clock()
        if pair_id in self._expired and pair_id not in self.active:
            # A row whose admission deadline already lapsed stays terminal so a
            # dispatcher retry cannot re-admit and PASS it as a new row.
            return self._expire(
                pair_id,
                f"admission deadline {self.deadline_seconds:g}s already exceeded; "
                "expiry is terminal",
                now,
            )
        if pair_id in self._blocked and pair_id not in self.active:
            # A row blocked before dispatch stays terminal for the same reason:
            # a retry must not turn an unavailable-capacity block into a pass.
            return self.block(pair_id, "capacity was unavailable when the row was planned")
        if pair_id in self.active:
            return self._record(
                AdmissionDecision(
                    pair_id,
                    "ok",
                    "pair already owns a slot",
                    self._next_sequence(),
                    now,
                    owner=True,
                )
            )
        status, reason = self._availability_status()
        waited_since = self.waiting_since.get(pair_id)
        if waited_since is not None and now - waited_since >= self.deadline_seconds:
            return self._expire(
                pair_id,
                f"admission deadline {self.deadline_seconds:g}s exceeded "
                f"after {now - waited_since:g}s",
                now,
            )
        if status == "ok" and len(self.active) < self.max_concurrent_pairs:
            decision = AdmissionDecision(
                pair_id,
                "ok",
                f"slot {len(self.active) + 1}/{self.max_concurrent_pairs} admitted",
                self._next_sequence(),
                now,
                owner=True,
            )
            self.active[pair_id] = decision
            self.waiting_since.pop(pair_id, None)
            if pair_id in self.queued:
                self.queued.remove(pair_id)
            return self._record(decision)

        self.waiting_since.setdefault(pair_id, now)
        if pair_id not in self.queued:
            self.queued.append(pair_id)
        return self._record(
            AdmissionDecision(
                pair_id,
                "queued",
                (
                    reason or f"capacity {status}"
                    if status != "ok"
                    else f"{len(self.active)}/{self.max_concurrent_pairs} slots owned; waiting"
                ),
                self._next_sequence(),
                now,
                owner=False,
            )
        )

    def expire_waiting(self) -> list[AdmissionDecision]:
        """Mark every queued pair past the deadline as expired."""

        now = self._clock()
        expired: list[AdmissionDecision] = []
        for pair_id in list(self.queued):
            waited_since = self.waiting_since.get(pair_id, now)
            if now - waited_since < self.deadline_seconds:
                continue
            expired.append(
                self._expire(
                    pair_id,
                    f"admission deadline {self.deadline_seconds:g}s exceeded while queued",
                    now,
                )
            )
        return expired

    def state(self, pair_id: str) -> str:
        """Report a pair's current admission state without recording a decision."""

        if pair_id in self.active:
            return "active"
        if pair_id in self.queued:
            return "queued"
        if pair_id in self._expired:
            return "expired"
        if pair_id in self._blocked:
            return "blocked"
        if pair_id in self._admitted:
            return "released"
        return "unknown"

    def abandon(
        self, pair_id: str, reason: str, *, status: str = "expired"
    ) -> AdmissionDecision | None:
        """Terminalize a pair that will never run and drop its waiting state.

        A row recorded as never started (aggregate expiry, cancellation, or a
        pre-dispatch block) must not stay queued: :meth:`release` promotes the
        head of the queue, so an abandoned waiter left in ``queued`` would be
        handed a live slot once capacity recovered and starve the next real
        row.  The pair becomes terminal in the same way an expiry is, so a
        dispatcher retry cannot silently start it as a fresh row either.  A
        pair that already owns an active slot is left to :meth:`release`,
        which is the only path allowed to promote a successor.
        """

        if pair_id in self.active:
            return None
        if pair_id in self._expired or pair_id in self._blocked:
            # Already terminal: keep the single existing decision.
            return None
        now = self._clock()
        self.waiting_since.pop(pair_id, None)
        if pair_id in self.queued:
            self.queued.remove(pair_id)
        self._expired.add(pair_id)
        return self._record(
            AdmissionDecision(pair_id, status, reason, self._next_sequence(), now, owner=False)
        )

    def release(self, pair_id: str) -> AdmissionDecision | None:
        """Release a slot and promote the next queued pair, if any."""

        self.active.pop(pair_id, None)
        now = self._clock()
        while self.queued:
            status, _reason = self._availability_status()
            if status != "ok":
                self.expire_waiting()
                return None
            candidate = self.queued.pop(0)
            waited_since = self.waiting_since.pop(candidate, now)
            if now - waited_since >= self.deadline_seconds:
                self._expire(
                    candidate,
                    f"admission deadline {self.deadline_seconds:g}s exceeded while queued",
                    now,
                )
                continue
            decision = AdmissionDecision(
                candidate,
                "ok",
                "slot released; queued pair promoted",
                self._next_sequence(),
                now,
                owner=True,
                promoted=True,
            )
            self.active[candidate] = decision
            return self._record(decision)
        return None


class TelemetryRecorder:
    """Low-overhead telemetry with durable hashing and lifecycle accounting."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        policy: CapacityPolicy,
        clock: Any | None = None,
        sampler: Any | None = None,
        temp_root: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.policy = policy
        self._clock = clock or time.monotonic
        self._sampler = sampler
        self._temp_root = temp_root
        self.samples: list[CapacitySample] = []
        self.lifecycle: dict[str, int] = {state: 0 for state in LIFECYCLE_STATES}
        self.pair_states: dict[str, str] = {}
        self.collection_failures = 0
        self.capability_assumptions: list[str] = []
        self._last_sample_at: float | None = None

    def observe(self, *, sequence: int | None = None) -> CapacitySample:
        """Collect one sample; any failure becomes an explicit failed sample."""

        if sequence is None:
            sequence = len(self.samples)
        now = self._clock()
        try:
            if self._sampler is None:
                raw = qualification_runner.collect_facts(self.repo_root, self._temp_root)
            else:
                raw = self._sampler()
            if isinstance(raw, CapacitySample):
                sample = CapacitySample(
                    sequence=sequence,
                    monotonic_seconds=raw.monotonic_seconds,
                    utc_timestamp=raw.utc_timestamp,
                    status=raw.status,
                    problems=list(raw.problems),
                    facts=dict(raw.facts),
                )
            else:
                data = facts_to_dict(raw)
                problems = validate_facts(data)
                sample = CapacitySample(
                    sequence=sequence,
                    monotonic_seconds=now,
                    utc_timestamp=_utc_now(),
                    status="malformed" if problems else "ok",
                    problems=problems,
                    facts=data,
                )
        except Exception as exc:  # noqa: BLE001 - record the failure, never crash.
            sample = CapacitySample(
                sequence=sequence,
                monotonic_seconds=now,
                utc_timestamp=_utc_now(),
                status="failed",
                problems=[f"telemetry collection failed: {type(exc).__name__}: {exc}"],
                facts={},
            )
        if sample.status != "ok":
            self.collection_failures += 1
        if sample.facts and isinstance(sample.facts.get("unsupported"), list):
            for item in sample.facts["unsupported"]:
                text = str(item)
                if text not in self.capability_assumptions:
                    self.capability_assumptions.append(text)
        self.samples.append(sample)
        self._last_sample_at = sample.monotonic_seconds
        return sample

    def mark(self, pair_id: str, state: str) -> None:
        """Account a pair's lifecycle state, replacing any previous state."""

        if state not in LIFECYCLE_STATES:
            raise ValueError(f"unsupported lifecycle state: {state!r}")
        previous = self.pair_states.get(pair_id)
        if previous == state:
            return
        if previous is not None:
            self.lifecycle[previous] = max(0, self.lifecycle[previous] - 1)
        self.pair_states[pair_id] = state
        self.lifecycle[state] += 1

    def reference(self) -> dict[str, Any]:
        """Return a durable, reproducible hash of the retained samples."""

        serializable = [asdict(sample) for sample in self.samples]
        try:
            encoded = json.dumps(serializable, sort_keys=True, default=str).encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - defensive.
            encoded = repr(serializable).encode("utf-8")
        return {
            "algorithm": "sha256",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "sample_count": len(self.samples),
            "collection_failures": self.collection_failures,
            "first_monotonic_seconds": (
                self.samples[0].monotonic_seconds if self.samples else None
            ),
            "last_monotonic_seconds": (
                self.samples[-1].monotonic_seconds if self.samples else None
            ),
        }


class CapacitySession:
    """Combine policy evaluation, admission, and telemetry for one gate run."""

    def __init__(
        self,
        policy: CapacityPolicy,
        *,
        repo_root: str | Path,
        clock: Any | None = None,
        sampler: Any | None = None,
        temp_root: str | Path | None = None,
    ) -> None:
        self.policy = policy
        self.repo_root = Path(repo_root)
        self.clock = clock or time.monotonic
        self.telemetry = TelemetryRecorder(
            repo_root=self.repo_root,
            policy=policy,
            clock=self.clock,
            sampler=sampler,
            temp_root=temp_root,
        )
        self.admission = CapacityAdmission(
            policy,
            clock=self.clock,
            availability=self._availability,
        )
        self.availability_status = "unknown"
        self.availability_reasons: list[str] = []
        self.started = False
        self.interrupted = False
        self._monitor_depth = 0
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._sample_lock = threading.RLock()
        self._run_sequence = 0
        # Admission identities are allocated once per (scope, tier) generation.
        # A tier that is registered as never-dispatched and then finalized again
        # during cancellation must transition the *same* planned row instead of
        # allocating a second identity for it, otherwise one planned row appears
        # twice in the lifecycle and the row accounting silently inflates.
        self._tier_run_ids: dict[tuple[str, str], str] = {}
        self._tier_blocked_rows: dict[tuple[str, str, str], str] = {}

    def next_run_id(self, name: str, *, scope: str = "") -> str:
        """Start a new admission generation for one tier and scope."""

        self._run_sequence += 1
        run_id = f"{self._run_sequence}:{name}"
        self._tier_run_ids[(scope, name)] = run_id
        self._tier_blocked_rows = {
            key: value
            for key, value in self._tier_blocked_rows.items()
            if (key[0], key[1]) != (scope, name)
        }
        return run_id

    def current_run_id(self, name: str, *, scope: str = "") -> str:
        """Return this tier's admission generation, allocating one if needed.

        Repeated registrations for the same never-dispatched tier must reuse the
        generation that already accounted its rows.  A fresh generation is only
        created by :meth:`next_run_id`, which a real dispatch performs.
        """

        existing = self._tier_run_ids.get((scope, name))
        if existing is not None:
            return existing
        self._run_sequence += 1
        run_id = f"{self._run_sequence}:{name}"
        self._tier_run_ids[(scope, name)] = run_id
        return run_id

    def blocked_row_id(self, name: str, nodeid: str, *, scope: str = "") -> str:
        """Return the stable admission identity for one planned unstarted row."""

        key = (scope, name, nodeid)
        existing = self._tier_blocked_rows.get(key)
        if existing is not None:
            return existing
        pair_id = f"{self.current_run_id(name, scope=scope)}:{nodeid}"
        self._tier_blocked_rows[key] = pair_id
        return pair_id

    @contextlib.contextmanager
    def monitoring(self):
        """Collect outside emulator owners; bound shutdown even if sampling hangs."""

        if self._monitor_depth == 0:
            if self.started:
                self.observe()
            else:
                self.ensure_started()
            self._monitor_stop = threading.Event()
            stop = self._monitor_stop

            def collect():
                while not stop.wait(max(self.policy.observation_seconds, 0.01)):
                    # Collection does not hold the report lock. A stuck OS read
                    # cannot keep owner cleanup or report generation waiting.
                    recorder = TelemetryRecorder(
                        repo_root=self.repo_root,
                        policy=self.policy,
                        clock=self.clock,
                        sampler=self.telemetry._sampler,
                        temp_root=self.telemetry._temp_root,
                    )
                    sample = recorder.observe()
                    with self._sample_lock:
                        if stop.is_set():
                            return
                        sample.sequence = len(self.telemetry.samples)
                        self.telemetry.samples.append(sample)
                        self.telemetry._last_sample_at = sample.monotonic_seconds
                        self.telemetry.collection_failures += recorder.collection_failures
                        for assumption in recorder.capability_assumptions:
                            if assumption not in self.telemetry.capability_assumptions:
                                self.telemetry.capability_assumptions.append(assumption)
                        self._apply_sample(sample)

            self._monitor_thread = threading.Thread(
                target=collect, name="capacity-observer", daemon=True
            )
            self._monitor_thread.start()
        self._monitor_depth += 1
        try:
            yield
        finally:
            self._monitor_depth -= 1
            if self._monitor_depth == 0:
                self._monitor_stop.set()
                self._monitor_thread.join(timeout=0.25)
                if self._monitor_thread.is_alive():
                    self.telemetry.collection_failures += 1
                    self.availability_status = "unsupported"
                    self.availability_reasons = ["capacity observer did not stop within 0.25s"]

    def _availability(self) -> tuple[str, str]:
        with self._sample_lock:
            last = self.telemetry._last_sample_at
            freshness_bound = (
                max(self.policy.observation_seconds, 0.01) + self.policy.admission_deadline_seconds
            )
            if (
                self.availability_status == "ok"
                and last is not None
                and self.clock() - last >= freshness_bound
            ):
                self.availability_status = "unsupported"
                self.availability_reasons = ["capacity observation exceeded its freshness deadline"]
                self.telemetry.collection_failures += 1
                self.telemetry.samples.append(
                    CapacitySample(
                        sequence=len(self.telemetry.samples),
                        monotonic_seconds=self.clock(),
                        utc_timestamp=_utc_now(),
                        status="failed",
                        problems=list(self.availability_reasons),
                        facts={},
                    )
                )
            status = (
                self.availability_status if self.availability_status != "unknown" else "blocked"
            )
            return status, "; ".join(self.availability_reasons)

    def capacity_reason(self) -> str:
        detail = "; ".join(self.availability_reasons) or "no capacity reason recorded"
        return f"capacity {self.availability_status}: {detail}"

    def end_unstarted(self, pair_id: str, *, state: str = "not_started", reason: str = "") -> str:
        """Terminalize a row that will never dispatch and record its lifecycle.

        Every terminal unstarted row (aggregate expiry, cancellation, or a
        pre-dispatch block) must end any admission ownership or queue state,
        otherwise a later ``release`` promotes the abandoned row into a live
        slot.  Returns ``"capacity"`` when the row never ran because declared
        capacity had not admitted it (queued or expired) and ``""`` when it
        simply never dispatched, so a caller can classify a capacity-only stop
        as BLOCKED while keeping real failures as failures.
        """

        detail = reason or "row never started"
        if pair_id in self.admission.active:
            # Release the slot so a real queued successor is promoted, then
            # terminalize the row itself.  A bare release left the pair id
            # merely "released", so an explicit retry of the same id could be
            # handed ownership again as if it had never been cancelled.
            self.admission.release(pair_id)
            self.admission.abandon(pair_id, detail)
            capacity = ""
        else:
            capacity = "capacity" if self.admission.state(pair_id) in {"queued", "expired"} else ""
            self.admission.abandon(pair_id, detail)
        self.telemetry.mark(pair_id, state)
        return capacity

    def ensure_started(self) -> str:
        if not self.started:
            return self.begin()
        return self.availability_status

    def begin(self) -> str:
        """Sample once and fix the initial availability for admission."""

        self.started = True
        sample = self._bounded_observation()
        self._apply_sample(sample)
        return self.availability_status

    def _bounded_observation(self, timeout_seconds: float | None = None) -> CapacitySample:
        """Discard late collector output; no worker mutates shared session state."""

        recorder = TelemetryRecorder(
            repo_root=self.repo_root,
            policy=self.policy,
            clock=self.clock,
            sampler=self.telemetry._sampler,
            temp_root=self.telemetry._temp_root,
        )
        complete = threading.Event()
        result = []

        def collect():
            try:
                result.append(recorder.observe())
            finally:
                complete.set()

        worker = threading.Thread(target=collect, name="capacity-initial-observer", daemon=True)
        budget = (
            self.policy.admission_deadline_seconds if timeout_seconds is None else timeout_seconds
        )
        collection_deadline = time.monotonic() + budget
        worker.start()
        remaining = max(0.0, collection_deadline - time.monotonic())
        if (
            not complete.wait(min(remaining, threading.TIMEOUT_MAX))
            or time.monotonic() >= collection_deadline
            or not result
        ):
            sample = CapacitySample(
                sequence=len(self.telemetry.samples),
                monotonic_seconds=self.clock(),
                utc_timestamp=_utc_now(),
                status="failed",
                problems=["capacity collection exceeded admission deadline"],
                facts={},
            )
            self.telemetry.collection_failures += 1
        else:
            sample = result[0]
            sample.sequence = len(self.telemetry.samples)
            self.telemetry.collection_failures += recorder.collection_failures
            for assumption in recorder.capability_assumptions:
                if assumption not in self.telemetry.capability_assumptions:
                    self.telemetry.capability_assumptions.append(assumption)
        self.telemetry.samples.append(sample)
        self.telemetry._last_sample_at = sample.monotonic_seconds
        return sample

    def _apply_sample(self, sample: CapacitySample) -> None:
        if (
            sample.status == "ok"
            and self.clock() - sample.monotonic_seconds >= self.policy.admission_deadline_seconds
        ):
            sample.status = "failed"
            sample.problems.append("capacity collection exceeded admission deadline")
            self.telemetry.collection_failures += 1
        if sample.status != "ok":
            self.availability_status = "unsupported"
            self.availability_reasons = list(sample.problems) or ["telemetry sample unavailable"]
            return
        try:
            status, reasons = evaluate_capacity(self.policy, sample.facts)
        except Exception as exc:  # noqa: BLE001 - never retain healthy availability on failure.
            status = "unsupported"
            reasons = [f"capacity evaluation failed: {type(exc).__name__}: {exc}"]
            sample.status = "failed"
            sample.problems.extend(reasons)
            self.telemetry.collection_failures += 1
        self.availability_status = status
        self.availability_reasons = list(reasons)

    def observe(self, *, timeout_seconds: float | None = None) -> str:
        """Take a fresh sample so rising pressure stops new admission."""

        sample = self._bounded_observation(timeout_seconds)
        self._apply_sample(sample)
        return self.availability_status

    def maybe_observe(self, *, timeout_seconds: float | None = None) -> str:
        """Sample at most once per declared observation interval."""

        if self._monitor_depth:
            return self._availability()[0]
        interval = self.policy.observation_seconds
        last = self.telemetry._last_sample_at
        if last is None or interval <= 0 or self.clock() - last >= interval:
            return self.observe(timeout_seconds=timeout_seconds)
        return self.availability_status

    def wait_until_available(self, *, sleep: Any = time.sleep) -> str:
        """Retry unavailable observations within the declared admission budget."""

        deadline = self.clock() + self.policy.admission_deadline_seconds
        status = self.ensure_started()
        while status != "ok":
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            sleep(min(max(self.policy.observation_seconds, 0.01), remaining))
            if self.clock() >= deadline:
                break
            status = self.maybe_observe(timeout_seconds=deadline - self.clock())
        return status

    def report(self, *, failed: int = 0, outcome: str | None = None) -> dict[str, Any]:
        """Build the structured capacity section retained by the gate report."""

        self._availability()
        admitted = self.admission.admitted_count
        lifecycle = dict(self.telemetry.lifecycle)
        if outcome is None:
            outcome = overall_outcome(
                availability=(
                    self.availability_status if self.availability_status != "unknown" else "blocked"
                ),
                admitted=admitted,
                failed=failed,
                collection_failures=self.telemetry.collection_failures,
                interrupted=lifecycle["interrupted"],
                not_started=lifecycle["not_started"],
            )
        return {
            "status": outcome,
            "policy": self.policy.as_dict(),
            "pressure_metric": _PRESSURE_METRIC,
            "availability": {
                "status": self.availability_status,
                "reasons": list(self.availability_reasons),
            },
            "capability_assumptions": list(self.telemetry.capability_assumptions),
            "admission": {
                "max_concurrent_pairs": self.policy.max_concurrent_pairs,
                "admitted": admitted,
                "active": sorted(self.admission.active),
                "queued": list(self.admission.queued),
                "decisions": [asdict(decision) for decision in self.admission.decisions],
            },
            "lifecycle": lifecycle,
            "collection_failures": self.telemetry.collection_failures,
            "samples": [asdict(sample) for sample in self.telemetry.samples],
            "sample_reference": self.telemetry.reference(),
        }

    def not_applicable_report(self) -> dict[str, Any]:
        """Report an asset-free selection without claiming capacity success."""

        return not_applicable_capacity(
            self.policy,
            "asset-free selection does not dispatch emulator pairs",
        )


def overall_outcome(
    *,
    availability: str = "ok",
    admitted: int = 0,
    failed: int = 0,
    collection_failures: int = 0,
    interrupted: int = 0,
    not_started: int = 0,
) -> str:
    """Aggregate a capacity outcome; zero admitted tests can never be ``ok``."""

    if failed > 0:
        return "failed"
    if admitted <= 0:
        return "blocked"
    if availability != "ok":
        return availability if availability in CAPACITY_OUTCOMES else "blocked"
    if collection_failures > 0:
        return "unsupported"
    if interrupted > 0 or not_started > 0:
        return "blocked"
    return "ok"


def unavailable_capacity(reason: str) -> dict[str, Any]:
    """Return the explicit marker recorded when no policy is supplied."""

    return {
        "status": "unavailable",
        "capacity_policy": "unavailable",
        "availability": {"status": "unavailable", "reasons": [reason]},
        "admission": {"max_concurrent_pairs": 0, "admitted": 0, "active": [], "queued": []},
        "lifecycle": {state: 0 for state in LIFECYCLE_STATES},
        "samples": [],
        "collection_failures": 0,
    }


def not_applicable_capacity(policy: CapacityPolicy, reason: str) -> dict[str, Any]:
    """Return an explicit marker for an asset-free selection that is not gated."""

    return {
        "status": NOT_APPLICABLE,
        "capacity_policy": policy.runner_id,
        "policy": policy.as_dict(),
        "pressure_metric": _PRESSURE_METRIC,
        "availability": {"status": NOT_APPLICABLE, "reasons": [reason]},
        "capability_assumptions": [],
        "admission": {
            "max_concurrent_pairs": policy.max_concurrent_pairs,
            "admitted": 0,
            "active": [],
            "queued": [],
            "decisions": [],
        },
        "lifecycle": {state: 0 for state in LIFECYCLE_STATES},
        "collection_failures": 0,
        "samples": [],
        "sample_reference": {
            "algorithm": "sha256",
            "sha256": hashlib.sha256(b"[]").hexdigest(),
            "sample_count": 0,
            "collection_failures": 0,
            "first_monotonic_seconds": None,
            "last_monotonic_seconds": None,
        },
    }


def render_capacity_text(payload: dict[str, Any]) -> str:
    """Render the capacity section for human-readable gate reports."""

    lines = [f"capacity-policy: {payload.get('status', 'unavailable')}"]
    policy = payload.get("policy")
    if isinstance(policy, dict):
        lines.append(
            "  policy: "
            f"version={policy.get('policy_version')} "
            f"runner={policy.get('runner_id')} "
            f"effective_cpus={policy.get('effective_cpus')} "
            f"max_concurrent_pairs={policy.get('max_concurrent_pairs')}"
        )
        lines.append(
            "  thresholds: "
            f"memory_bytes_min={policy.get('memory_bytes_min')} "
            f"disk_free_bytes_min={policy.get('disk_free_bytes_min')} "
            f"observation_seconds={policy.get('observation_seconds')} "
            f"admission_deadline_seconds={policy.get('admission_deadline_seconds')}"
        )
    availability = payload.get("availability")
    if isinstance(availability, dict):
        lines.append(f"  availability: {availability.get('status', 'unknown')}")
        for reason in availability.get("reasons", []):
            lines.append(f"    reason: {reason}")
    assumptions = payload.get("capability_assumptions") or []
    if assumptions:
        lines.append("  capability-assumptions: " + ", ".join(str(item) for item in assumptions))
    admission = payload.get("admission")
    if isinstance(admission, dict):
        lines.append(
            "  admission: "
            f"max_concurrent_pairs={admission.get('max_concurrent_pairs')} "
            f"admitted={admission.get('admitted')} "
            f"active={admission.get('active')} "
            f"queued={admission.get('queued')}"
        )
    lifecycle = payload.get("lifecycle")
    if isinstance(lifecycle, dict):
        lines.append(
            "  lifecycle: "
            + " ".join(f"{state}={lifecycle.get(state, 0)}" for state in LIFECYCLE_STATES)
        )
    if payload.get("collection_failures"):
        lines.append(f"  collection-failures: {payload['collection_failures']}")
    reference = payload.get("sample_reference")
    if isinstance(reference, dict):
        lines.append(
            "  sample-reference: "
            f"sha256={reference.get('sha256')} samples={reference.get('sample_count')}"
        )
    return "\n".join(lines)


__all__ = [
    "ADMISSION_STATUSES",
    "CAPACITY_OUTCOMES",
    "LIFECYCLE_STATES",
    "NOT_APPLICABLE",
    "POLICY_SCHEMA_VERSION",
    "AdmissionDecision",
    "CapacityAdmission",
    "CapacityPolicy",
    "CapacitySample",
    "CapacitySession",
    "TelemetryRecorder",
    "capacity_policy_from_dict",
    "evaluate_capacity",
    "facts_to_dict",
    "load_capacity_policy",
    "not_applicable_capacity",
    "overall_outcome",
    "render_capacity_text",
    "sample_facts",
    "unavailable_capacity",
    "validate_facts",
    "validate_policy",
]
