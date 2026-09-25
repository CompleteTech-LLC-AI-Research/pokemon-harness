"""The §5 capacity window and the named gap/admission validator (#84).

``docs/TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md`` §5 freezes how a *qualifying
window* is measured, and §5.1 turns it into a per-row admission decision.  §5
also states that the frozen watcher ``watch_cpu_capacity_84.py`` (pinned in §2)
updates its quiet run from quiet *threshold* samples only and applies **no
maximum-gap reset** — the ``6.0 s`` continuity rule was introduced by this
protocol after the watcher was frozen.  The unchanged watcher therefore cannot
enforce the gap rule, admission expiry, or per-row admission.

This module supplies the two things §5 and §9.1 require to close that gap:

* :class:`GapAdmissionValidator` — the **named, pinned gap/admission
  validator**: a read-only validation over a retained sample series.  It is
  pure, so a second engineer can re-run it over retained evidence without
  touching a host, and :func:`validator_identity` pins it by source and
  SHA-256 so a row record can name the exact procedure that admitted it.
* :class:`HostSample` / :func:`read_host_sample` — one read-only sample of the
  frozen watcher's own two sources (``/proc/loadavg`` and the ``some`` line of
  ``/proc/pressure/cpu``).
* :class:`AllocationRecord` — the #85 allocation held for a row.  §5 requires the
  record to state *which* allocation was held, its extent, and its **span**, so
  a record whose span is missing, unparseable, or already ended is not a held
  allocation and cannot admit a row.

It holds no policy of its own: every threshold is the frozen §5 value, stated
once as a module constant.  A row's pass or failure is never read here.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# --- §5 qualifying bounds -------------------------------------------------
PRESSURE_BOUND_PERCENT = 10.0
QUALIFYING_SECONDS = 60.0
SAMPLE_INTERVAL_SECONDS = 5.0
MAX_SAMPLE_GAP_SECONDS = 6.0
MAX_LAUNCH_DELAY_SECONDS = 6.0
OBSERVATION_BOUND_SECONDS = 1800.0

PRESSURE_PATH = Path("/proc/pressure/cpu")
LOADAVG_PATH = Path("/proc/loadavg")

# --- §5.1 states ----------------------------------------------------------
S0_NOT_ADMITTED = "S0"
S1_ADMITTED = "S1"
S2_PRESSURE_ROSE = "S2"
S3_CONTROL_LOST = "S3"
S4_SAMPLE_GAP = "S4"
S5_ADMISSION_UNAVAILABLE = "S5"
S6_FAILED_IN_WINDOW = "S6"

ADMISSION_STATES = (
    S0_NOT_ADMITTED,
    S1_ADMITTED,
    S2_PRESSURE_ROSE,
    S3_CONTROL_LOST,
    S4_SAMPLE_GAP,
    S5_ADMISSION_UNAVAILABLE,
    S6_FAILED_IN_WINDOW,
)
# §5.1: only these states yield an observation that may be read as a pass or a
# failure by §7.  S0/S3/S4/S5 are invalid controls.
CONTROLLED_STATES = (S1_ADMITTED, S2_PRESSURE_ROSE)
# §7: "an observation is a pass or a failure read in a valid controlled state
# (S1/S2/S6)".  S6 is a failure observation, so it is an observation but never a
# pass; S0/S3/S4/S5 are invalid controls that support no conclusion.
OBSERVATION_STATES = (S1_ADMITTED, S2_PRESSURE_ROSE, S6_FAILED_IN_WINDOW)
# §7 also defines a pass only for S1/S2 and a failure only for S6.
PASSING_STATES = (S1_ADMITTED, S2_PRESSURE_ROSE)
INADMISSIBLE_STATES = (S0_NOT_ADMITTED, S3_CONTROL_LOST, S4_SAMPLE_GAP, S5_ADMISSION_UNAVAILABLE)

S5_REMAINING_ROWS_REASON = "not run — blocked on the capacity prerequisite"

_ALLOCATION_KINDS = ("reservation", "affinity", "cgroup_quota")

# §5 requires the allocation's span to be recorded.  Only this one ISO-8601 UTC
# form is accepted: a naive, offset, or non-UTC instant cannot be compared with
# the run's own UTC clock without assuming an offset it never declared.
_ALLOCATION_INSTANT_FORMAT = "YYYY-MM-DDTHH:MM:SS[.ffffff]Z"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _allocation_instant(value: Any) -> datetime | None:
    """Parse one recorded span instant, or ``None`` when it is not usable.

    Deterministic and clock-free: this decides whether a *record* states a
    parseable instant, never whether that instant is still in the future.
    """

    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1])
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed


# A during-row observer must not grow the retained series without bound, and a
# degenerate interval must not spin.  The floor keeps the observer at a real
# cadence; the cap bounds memory on a long row.
MIN_SAMPLE_INTERVAL_SECONDS = 0.01
MAX_DURING_ROW_SAMPLES = 4096

# ``wait_for_window`` is bounded by the injected clock, so a clock that does not
# advance (a mis-wired harness) must not spin forever.  The iteration cap is a
# termination guard, never an admission shortcut: reaching it means the window
# did not qualify.
MAX_WINDOW_ATTEMPTS = 4096


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) == float(value)
    )


@dataclass(frozen=True)
class HostSample:
    """One read-only observation of ``avg10``/``avg60`` and one-minute load."""

    sequence: int
    monotonic_seconds: float
    avg10: float | None
    avg60: float | None
    load1: float | None
    problems: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return (
            not self.problems
            and _finite(self.avg10)
            and _finite(self.avg60)
            and _finite(self.load1)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "monotonic_seconds": self.monotonic_seconds,
            "avg10": self.avg10,
            "avg60": self.avg60,
            "load1": self.load1,
            "problems": list(self.problems),
            "usable": self.usable,
        }


def read_host_sample(
    *,
    sequence: int = 0,
    clock: Callable[[], float] = time.monotonic,
    loadavg_path: Path = LOADAVG_PATH,
    pressure_path: Path = PRESSURE_PATH,
) -> HostSample:
    """Read the frozen watcher's two sources once; an unreadable host is explicit."""

    problems: list[str] = []
    load1: float | None = None
    avg10: float | None = None
    avg60: float | None = None
    try:
        fields = loadavg_path.read_text().split()
        load1 = float(fields[0])
    except Exception as exc:  # noqa: BLE001 - unreadable host is an explicit sample
        problems.append(f"load average unavailable: {type(exc).__name__}: {exc}")
    try:
        line = next(row for row in pressure_path.read_text().splitlines() if row.startswith("some"))
        values = {
            key.strip(): float(value.rstrip("%"))
            for key, value in (token.split("=") for token in line.split()[1:])
        }
        avg10 = values.get("avg10")
        avg60 = values.get("avg60")
    except Exception as exc:  # noqa: BLE001 - unreadable host is an explicit sample
        problems.append(f"cpu pressure unavailable: {type(exc).__name__}: {exc}")
    return HostSample(
        sequence=sequence,
        monotonic_seconds=clock(),
        avg10=avg10,
        avg60=avg60,
        load1=load1,
        problems=tuple(problems),
    )


@dataclass(frozen=True)
class AllocationRecord:
    """The #85 allocation held for a row (§5, §6 item 6).

    §5 requires the record to state which allocation was held, its extent, its
    span, and **the fact that it is a reservation rather than affinity or a
    cgroup quota**.  ``kind`` is therefore constrained, and an ``affinity`` or
    ``cgroup_quota`` record never admits a row.
    """

    allocation_id: str
    kind: str
    extent: dict[str, Any]
    holder: str
    source: str
    started_utc: str
    expires_utc: str | None = None

    def problems(self) -> list[str]:
        """Everything that stops this record being a *verified, held* allocation.

        §5 requires the record to state its span as well as its extent, so an
        absent, unparseable, inverted, or already-elapsed span is a problem
        exactly like an absent extent.  The elapsed check reads this process's
        own UTC clock, so ``held`` is honest about the present instead of being
        true forever for any reservation-shaped document.
        """

        found: list[str] = []
        if self.kind not in _ALLOCATION_KINDS:
            found.append(
                f"allocation kind {self.kind!r} is not one of {', '.join(_ALLOCATION_KINDS)}"
            )
        if not self.allocation_id:
            found.append("allocation id is empty")
        if self.kind != "reservation":
            found.append(
                f"allocation kind {self.kind!r} is not a reservation, so it does not "
                "declare an operator-controlled reservation (§5)"
            )
        if not self.extent:
            found.append("allocation extent is empty")
        if not self.holder:
            found.append("allocation holder is empty")
        if not self.source:
            found.append("allocation source is empty")

        started = _allocation_instant(self.started_utc)
        if started is None:
            found.append(
                f"allocation span start {self.started_utc!r} is not a "
                f"{_ALLOCATION_INSTANT_FORMAT} instant (§5 requires the span)"
            )
        expires = _allocation_instant(self.expires_utc)
        if expires is None:
            found.append(
                f"allocation span end {self.expires_utc!r} is not a "
                f"{_ALLOCATION_INSTANT_FORMAT} instant (§5 requires the span)"
            )
        elif started is not None:
            if expires < started:
                found.append(
                    f"allocation span ends ({self.expires_utc}) before it starts "
                    f"({self.started_utc})"
                )
            elif _utc_now() >= expires.replace(tzinfo=UTC):
                found.append(
                    f"allocation span ended at {self.expires_utc}, so the reservation "
                    "is no longer held (§5 requires a verified allocation)"
                )
        return found

    @property
    def held(self) -> bool:
        return not self.problems()

    def span_expired(self) -> bool:
        """Whether the recorded span has already ended (§5).

        Distinct from :meth:`problems`, which reports *why* it is not held.
        """

        started = _allocation_instant(self.started_utc)
        expires = _allocation_instant(self.expires_utc)
        if started is None or expires is None or expires < started:
            return False
        return _utc_now() >= expires.replace(tzinfo=UTC)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allocation_id": self.allocation_id,
            "kind": self.kind,
            "extent": dict(self.extent),
            "holder": self.holder,
            "source": self.source,
            "started_utc": self.started_utc,
            "expires_utc": self.expires_utc,
            "span_expired": self.span_expired(),
            "problems": self.problems(),
            "held": self.held,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a read-only validation over one retained sample series."""

    qualifying: bool
    quiet_seconds: float
    gaps: tuple[float, ...]
    max_gap_seconds: float
    restarts: tuple[str, ...]
    final_sample: HostSample | None
    peak_avg10: float | None
    peak_avg60: float | None
    peak_load1: float | None
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "qualifying": self.qualifying,
            "quiet_seconds": self.quiet_seconds,
            "gaps": list(self.gaps),
            "max_gap_seconds": self.max_gap_seconds,
            "restarts": list(self.restarts),
            "final_sample": self.final_sample.as_dict() if self.final_sample else None,
            "peak_avg10": self.peak_avg10,
            "peak_avg60": self.peak_avg60,
            "peak_load1": self.peak_load1,
            "reasons": list(self.reasons),
        }


class GapAdmissionValidator:
    """The named, pinned gap/admission validator required by §5 and §9.1.

    The frozen watcher applies no maximum-gap reset, so it cannot establish a
    qualifying window on its own.  This validator is the declared substitute:
    it is a **read-only validation over a retained sample series**, so it can be
    re-run against retained evidence without touching a host, and it enforces
    every rule §5 states about a qualifying window.
    """

    NAME = "timed-frame-gap-admission-validator"

    def __init__(
        self,
        *,
        pressure_bound_percent: float = PRESSURE_BOUND_PERCENT,
        qualifying_seconds: float = QUALIFYING_SECONDS,
        max_gap_seconds: float = MAX_SAMPLE_GAP_SECONDS,
        allowed_cpus: int,
    ) -> None:
        self.pressure_bound_percent = float(pressure_bound_percent)
        self.qualifying_seconds = float(qualifying_seconds)
        self.max_gap_seconds = float(max_gap_seconds)
        self.allowed_cpus = int(allowed_cpus)

    def is_quiet(self, sample: HostSample) -> bool:
        """§5: ``avg10 < 10%`` **and** ``avg60 < 10%`` **and** load ``<`` allowed CPUs."""

        if not sample.usable:
            return False
        return (
            sample.avg10 < self.pressure_bound_percent
            and sample.avg60 < self.pressure_bound_percent
            and sample.load1 < float(self.allowed_cpus)
        )

    def breached(self, sample: HostSample) -> list[str]:
        """§5.1 ``S3`` breach: ``avg10 ≥ 10%`` **or** ``avg60 ≥ 10%`` **or** load ≥ CPUs."""

        breaches: list[str] = []
        if not sample.usable:
            breaches.append("sample is unusable")
            return breaches
        if sample.avg10 >= self.pressure_bound_percent:
            breaches.append(f"avg10 {sample.avg10:g}% ≥ {self.pressure_bound_percent:g}%")
        if sample.avg60 >= self.pressure_bound_percent:
            breaches.append(f"avg60 {sample.avg60:g}% ≥ {self.pressure_bound_percent:g}%")
        if sample.load1 >= float(self.allowed_cpus):
            breaches.append(f"load1 {sample.load1:g} ≥ allowed CPUs {self.allowed_cpus}")
        return breaches

    def trailing_quiet_run(
        self, samples: Sequence[HostSample]
    ) -> tuple[list[HostSample], tuple[float, ...], tuple[str, ...]]:
        """Return the trailing quiet run, its consecutive gaps, and restarts.

        A gap longer than ``max_gap_seconds`` breaks continuity: the 60 s
        interval restarts from the next sample and the gap is recorded.  A
        non-quiet sample also restarts the run.
        """

        run: list[HostSample] = []
        for sample in samples:
            if run:
                gap = sample.monotonic_seconds - run[-1].monotonic_seconds
                if gap > self.max_gap_seconds:
                    run = []
            if not self.is_quiet(sample):
                run = []
                continue
            run.append(sample)
        gaps = tuple(
            run[index].monotonic_seconds - run[index - 1].monotonic_seconds
            for index in range(1, len(run))
        )
        return run, gaps, ()

    def validate(self, samples: Sequence[HostSample]) -> ValidationResult:
        """Decide whether a retained series contains a qualifying window."""

        run, gaps, _ = self.trailing_quiet_run(samples)
        restarts: list[str] = []
        for index in range(1, len(samples)):
            previous, current = samples[index - 1], samples[index]
            gap = current.monotonic_seconds - previous.monotonic_seconds
            if gap > self.max_gap_seconds:
                restarts.append(
                    f"gap {gap:.6f}s at sample {current.sequence} exceeds "
                    f"{self.max_gap_seconds:.6f}s; continuity restarted"
                )
        quiet_seconds = 0.0
        if len(run) >= 2:
            quiet_seconds = run[-1].monotonic_seconds - run[0].monotonic_seconds
        final_sample = run[-1] if run else None
        reasons: list[str] = []
        if final_sample is None:
            reasons.append("no quiet sample in the retained series")
        elif quiet_seconds < self.qualifying_seconds:
            reasons.append(
                f"quiet run is {quiet_seconds:.6f}s, below the required "
                f"{self.qualifying_seconds:.6f}s"
            )
        if final_sample is not None and not final_sample.usable:
            reasons.append("the final quiet sample is not usable")
        return ValidationResult(
            qualifying=not reasons,
            quiet_seconds=quiet_seconds,
            gaps=gaps,
            max_gap_seconds=self.max_gap_seconds,
            restarts=tuple(restarts),
            final_sample=final_sample,
            peak_avg10=max((item.avg10 for item in run), default=None),
            peak_avg60=max((item.avg60 for item in run), default=None),
            peak_load1=max((item.load1 for item in run), default=None),
            reasons=tuple(reasons),
        )


def validator_identity(module_path: str | Path | None = None) -> dict[str, Any]:
    """Name and pin the gap/admission validator by source and SHA-256 (§5, §9.1)."""

    path = Path(module_path) if module_path is not None else Path(__file__).resolve()
    raw = path.read_bytes()
    return {
        "name": GapAdmissionValidator.NAME,
        "module": path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "procedure": (
            "read-only validation over the retained sample series: quiet bounds "
            "(avg10 < 10%, avg60 < 10%, load1 < allowed CPUs) held continuously "
            "for 60 s with every consecutive sample gap <= 6.0 s"
        ),
    }
