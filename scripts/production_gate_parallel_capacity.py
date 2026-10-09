"""One live capacity authority for every concurrent job and pair (#107.2, #107.4).

``PairBudget`` is the single integer pair budget.  ``LiveCapacity`` couples it to the
existing ``CapacitySession`` admission (quota, pressure, memory, disk, reservations)
so dispatch is governed by the same policy the sequential gate uses, never a clone.
Unknown quota, a non-``ok`` admission, an insufficient reading or a lost reservation
all fail closed and are recorded as observations.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

MAX_OBSERVATIONS = 200


class CapacityError(RuntimeError):
    """Raised when the single global pair budget cannot admit the plan (fail closed)."""


class PairBudget:
    """The one global pair authority for every job; never cloned per job."""

    def __init__(self, total_pairs: object) -> None:
        if type(total_pairs) is not int or total_pairs < 1:
            raise CapacityError("pair budget is unknown, zero or not an integer")
        self.total_pairs = total_pairs
        self._reserved: dict[str, int] = {}
        self._lock = threading.Lock()
        self.lost_reason = ""

    def reserve(self, owner: str, pairs: object) -> int:
        with self._lock:
            if self.lost_reason:
                raise CapacityError(f"capacity lost: {self.lost_reason}")
            if type(pairs) is not int or pairs < 1:
                raise CapacityError(f"{owner}: reservation must be a positive integer")
            if owner in self._reserved:
                raise CapacityError(f"{owner}: duplicate reservation")
            if sum(self._reserved.values()) + pairs > self.total_pairs:
                raise CapacityError(f"{owner}: reservation exceeds the single global budget")
            self._reserved[owner] = pairs
            return pairs

    def release(self, owner: str) -> None:
        with self._lock:
            self._reserved.pop(owner, None)

    def mark_lost(self, reason: str) -> None:
        with self._lock:
            self.lost_reason = reason

    @property
    def reserved_total(self) -> int:
        with self._lock:
            return sum(self._reserved.values())


def affinity_pairs() -> int:
    """Pairs the allowed CPU set can host (two emulators per pair); Linux affinity if present."""

    try:
        return len(os.sched_getaffinity(0)) // 2
    except AttributeError:  # pragma: no cover - non-Linux fallback
        return (os.cpu_count() or 0) // 2


def capacity_loss_probe(
    reserved_pairs: int, measure: Callable[[], object] = affinity_pairs
) -> Callable[[], object]:
    """Return a probe: ``reserved_pairs`` while the reading covers them, else the reading.

    There is no baseline floor: a reading below the reserved pairs (including a zero or
    insufficient first reading) is reported as that lower number, and an unreadable quota
    is ``None``.  The admitted ceiling stays the capacity policy.
    """

    def probe() -> object:
        try:
            current = measure()
        except (OSError, ValueError):
            return None
        if type(current) is not int:
            return None
        return min(reserved_pairs, current)

    return probe


class LiveCapacity:
    """Admission, reservation and loss monitoring for all jobs under one authority."""

    def __init__(
        self,
        budget: PairBudget,
        *,
        session: Any | None = None,
        probe: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self.session = session
        self.probe = probe
        self._clock = clock
        self._pair_ids: dict[str, list[str]] = {}
        self.observations: list[dict[str, Any]] = []

    def _note(self, event: str, **detail: Any) -> None:
        if len(self.observations) < MAX_OBSERVATIONS:
            self.observations.append({"t": round(self._clock(), 3), "event": event, **detail})

    def _reading_problem(self) -> str:
        if self.probe is None:
            return ""
        available = self.probe()
        self._note("probe", available=available, reserved=self.budget.reserved_total)
        if type(available) is not int:
            return f"capacity quota is unknown ({available!r})"
        if available < self.budget.reserved_total:
            return f"available pairs {available} below the reserved {self.budget.reserved_total}"
        return ""

    def admit(self, job_id: str, pairs: int) -> None:
        """Reserve ``pairs`` for a job before it is dispatched; raises ``CapacityError``."""

        self.budget.reserve(job_id, pairs)
        try:
            if self.session is not None:
                self._admit_session(job_id, pairs)
            problem = self._reading_problem()
            if problem:
                raise CapacityError(problem)
        except CapacityError:
            self.release(job_id)
            raise

    def _admit_session(self, job_id: str, pairs: int) -> None:
        session = self.session
        status = session.wait_until_available()
        self._note("admission", status=status, reasons=list(session.availability_reasons))
        if status != "ok":
            raise CapacityError(session.capacity_reason())
        identifiers: list[str] = []
        self._pair_ids[job_id] = identifiers
        for index in range(pairs):
            pair_id = f"{job_id}/pair-{index + 1}"
            decision = session.admission.admit(pair_id)
            identifiers.append(pair_id)
            self._note("pair", pair=pair_id, status=decision.status)
            if decision.status != "ok":
                raise CapacityError(f"{pair_id}: admission {decision.status}: {decision.reason}")

    def check(self) -> str:
        """Return an empty string while capacity holds, else the reason it was lost."""

        if self.session is not None:
            status = self.session.maybe_observe()
            if status != "ok":
                self._note("loss", status=status)
                return str(self.session.capacity_reason())
            for identifiers in self._pair_ids.values():
                for pair_id in identifiers:
                    if pair_id not in self.session.admission.active:
                        self._note("loss", lost=pair_id)
                        return f"reservation lost for {pair_id}"
        problem = self._reading_problem()
        if problem:
            self._note("loss", reason=problem)
        return problem

    def release(self, job_id: str) -> None:
        self.budget.release(job_id)
        if self.session is not None:
            for pair_id in self._pair_ids.pop(job_id, []):
                if pair_id in self.session.admission.active:
                    self.session.admission.release(pair_id)


def require_parallel_capacity(plan: Any, budget: PairBudget) -> None:
    """Refuse dispatch unless the budget admits one pair for every job of the widest phase."""

    body = plan.as_dict()
    if budget.total_pairs != body["capacity"]["total_pairs"]:
        raise CapacityError("pair budget differs from the frozen plan")
    per_phase: dict[str, int] = {}
    for job in body["jobs"]:
        per_phase[job["phase"]] = per_phase.get(job["phase"], 0) + 1
    if budget.total_pairs < max(per_phase.values()):
        raise CapacityError("parallel runtimes need one pair per job; only one heavy job admitted")
