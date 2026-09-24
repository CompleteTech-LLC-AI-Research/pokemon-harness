"""Bounded capacity admission, slot control, and telemetry recording.

Split from ``scripts/gate_capacity.py`` for issue #122 with no behavior
change: every class below is copied verbatim from that module and is
re-exported by ``scripts.gate_capacity``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.gate_capacity_policy import (
    _PRESSURE_METRIC,
    LIFECYCLE_STATES,
    CapacityPolicy,
    CapacitySample,
    _utc_now,
    evaluate_capacity,
    facts_to_dict,
    qualification_runner,
    validate_facts,
)
from scripts.gate_capacity_report import (
    not_applicable_capacity,
    overall_outcome,
)


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
