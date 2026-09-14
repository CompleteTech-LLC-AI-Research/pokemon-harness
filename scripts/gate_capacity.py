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
* System CPU PSI ``some`` is the only pressure signal used.  ``full=0`` is
  never used as proof of available capacity.
* The admission controller has an injectable clock, never pauses, reprioritizes,
  or enlarges the deadline of an active owner, and never reinterprets a failed
  test as a pass.
* Telemetry collection failure is recorded explicitly and can never crash
  cleanup or erase an original failure.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POLICY_SCHEMA_VERSION = 1
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
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    quota = data["cpu_quota_cores"]
    if quota is not None and not _is_number(quota):
        problems.append("cpu_quota_cores must be null or a number")
    weight = data["cpu_weight"]
    if weight is not None and not _is_non_negative_int(weight):
        problems.append("cpu_weight must be null or a non-negative integer")
    psi = data["psi_cpu_some_avg300"]
    if psi is not None and not _is_number(psi):
        problems.append("psi_cpu_some_avg300 must be null or a number")
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
        observed_cpus = data["affinity_count"] or data["logical_cpus"]
    else:
        observed_cpus = None
    if observed_cpus is None or observed_cpus <= 0:
        unsupported.append("effective CPU allocation is unavailable")
    elif observed_cpus < policy.effective_cpus:
        blocked.append(
            f"effective CPUs {observed_cpus} below declared minimum {policy.effective_cpus}"
        )

    # A cgroup quota, when one is in effect, is an upper bound on effective
    # CPUs.  ``None`` means no quota is configured, which is not a blocker.
    quota = data["cpu_quota_cores"]
    if quota is not None and quota < policy.effective_cpus:
        blocked.append(
            f"cgroup CPU quota {quota} cores below declared minimum {policy.effective_cpus}"
        )

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

    def admit(self, pair_id: str) -> AdmissionDecision:
        """Return ``ok``, ``queued``, ``blocked`` or ``expired`` for ``pair_id``."""

        now = self._clock()
        status, reason = self._availability_status()
        if status != "ok":
            return self._record(
                AdmissionDecision(
                    pair_id,
                    "blocked",
                    reason or f"capacity {status}",
                    self._next_sequence(),
                    now,
                    owner=False,
                )
            )
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
        if len(self.active) < self.max_concurrent_pairs:
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
            return self._record(decision)

        waited_since = self.waiting_since.setdefault(pair_id, now)
        waited = now - waited_since
        if waited >= self.deadline_seconds:
            self.waiting_since.pop(pair_id, None)
            if pair_id in self.queued:
                self.queued.remove(pair_id)
            return self._record(
                AdmissionDecision(
                    pair_id,
                    "expired",
                    f"admission deadline {self.deadline_seconds:g}s exceeded after {waited:g}s",
                    self._next_sequence(),
                    now,
                    owner=False,
                )
            )
        if pair_id not in self.queued:
            self.queued.append(pair_id)
        return self._record(
            AdmissionDecision(
                pair_id,
                "queued",
                f"{len(self.active)}/{self.max_concurrent_pairs} slots owned; waiting",
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
            self.queued.remove(pair_id)
            self.waiting_since.pop(pair_id, None)
            expired.append(
                self._record(
                    AdmissionDecision(
                        pair_id,
                        "expired",
                        f"admission deadline {self.deadline_seconds:g}s exceeded while queued",
                        self._next_sequence(),
                        now,
                        owner=False,
                    )
                )
            )
        return expired

    def release(self, pair_id: str) -> AdmissionDecision | None:
        """Release a slot and promote the next queued pair, if any."""

        self.active.pop(pair_id, None)
        now = self._clock()
        while self.queued:
            candidate = self.queued.pop(0)
            status, reason = self._availability_status()
            if status != "ok":
                self.waiting_since.pop(candidate, None)
                self._record(
                    AdmissionDecision(
                        candidate,
                        "blocked",
                        reason or f"capacity {status}",
                        self._next_sequence(),
                        now,
                        owner=False,
                    )
                )
                continue
            waited_since = self.waiting_since.pop(candidate, now)
            if now - waited_since >= self.deadline_seconds:
                self._record(
                    AdmissionDecision(
                        candidate,
                        "expired",
                        f"admission deadline {self.deadline_seconds:g}s exceeded while queued",
                        self._next_sequence(),
                        now,
                        owner=False,
                    )
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

    def _availability(self) -> tuple[str, str]:
        status = self.availability_status if self.availability_status != "unknown" else "blocked"
        return status, "; ".join(self.availability_reasons)

    def capacity_reason(self) -> str:
        detail = "; ".join(self.availability_reasons) or "no capacity reason recorded"
        return f"capacity {self.availability_status}: {detail}"

    def ensure_started(self) -> str:
        if not self.started:
            return self.begin()
        return self.availability_status

    def begin(self) -> str:
        """Sample once and fix the initial availability for admission."""

        self.started = True
        sample = self.telemetry.observe()
        self._apply_sample(sample)
        return self.availability_status

    def _apply_sample(self, sample: CapacitySample) -> None:
        if sample.status != "ok":
            self.availability_status = "unsupported"
            self.availability_reasons = list(sample.problems) or ["telemetry sample unavailable"]
            return
        status, reasons = evaluate_capacity(self.policy, sample.facts)
        self.availability_status = status
        self.availability_reasons = list(reasons)

    def observe(self) -> str:
        """Take a fresh sample so rising pressure stops new admission."""

        sample = self.telemetry.observe()
        self._apply_sample(sample)
        return self.availability_status

    def maybe_observe(self) -> str:
        """Sample at most once per declared observation interval."""

        if self._availability()[0] != "ok":
            return self.availability_status
        interval = self.policy.observation_seconds
        last = self.telemetry._last_sample_at
        if last is None or interval <= 0 or self.clock() - last >= interval:
            return self.observe()
        return self.availability_status

    def report(self, *, failed: int = 0, outcome: str | None = None) -> dict[str, Any]:
        """Build the structured capacity section retained by the gate report."""

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
