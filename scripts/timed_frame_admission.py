"""The §5.1 per-row capacity admission wrapper (#84).

``docs/TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md`` §5.1 states that the frozen
matrix runner ``run_timed_mcp_84_matrix.py`` launches **one** pytest selection
containing all nine orientations and then samples the host globally while it
runs.  It has no inter-row admission check and no admission pause, so a
successful first row can be followed automatically by an inadmissible second
row.  The protocol therefore requires a **declared, hashed admission wrapper**
that:

* waits for a fresh qualifying window (its own, never an inherited one) before
  each of the nine rows;
* samples the host during each row, plus one final sample at or after the row
  terminates;
* classifies each row into exactly one ``S0``-``S6`` state from that row's own
  pre-admission window, its during-row samples, and its allocation record;
* stops dispatching when admission is unavailable (``S5``).

It also requires the **named, pinned gap/admission validator** described in
:mod:`scripts.timed_frame_window`; the wrapper records that validator's identity
with every run so the admission decision is reproducible from the record alone.

Design rules:

* Nothing here lowers a deadline or reads a product outcome.  A row's pass or
  failure is reported by the caller; this module only decides whether that
  result is *admissible* as an observation.
* ``S3``/``S4`` discard the observation, never the failure record: an
  inadmissible row counts as neither a pass nor a failure.
* An inadmissible row is replaced only under a fresh window and only within a
  **declared** attempt cap.  The cap bounds termination; it is never a filter
  over outcomes, and a row that failed in ``S1``/``S2`` is never re-run.
* No allocation held means the row is not admitted, whatever ``/proc`` shows.
  Affinity and a cgroup quota are not allocations (§5).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The §5 window/validator component is re-exported verbatim so the module
# surface (and every ``timed_frame_admission.<name>`` reference in a run record,
# test, or caller) is unchanged by the split.
from scripts.timed_frame_window import (  # noqa: F401
    _ALLOCATION_KINDS,
    ADMISSION_STATES,
    CONTROLLED_STATES,
    INADMISSIBLE_STATES,
    LOADAVG_PATH,
    MAX_DURING_ROW_SAMPLES,
    MAX_LAUNCH_DELAY_SECONDS,
    MAX_SAMPLE_GAP_SECONDS,
    MAX_WINDOW_ATTEMPTS,
    MIN_SAMPLE_INTERVAL_SECONDS,
    OBSERVATION_BOUND_SECONDS,
    OBSERVATION_STATES,
    PASSING_STATES,
    PRESSURE_BOUND_PERCENT,
    PRESSURE_PATH,
    QUALIFYING_SECONDS,
    S0_NOT_ADMITTED,
    S1_ADMITTED,
    S2_PRESSURE_ROSE,
    S3_CONTROL_LOST,
    S4_SAMPLE_GAP,
    S5_ADMISSION_UNAVAILABLE,
    S5_REMAINING_ROWS_REASON,
    S6_FAILED_IN_WINDOW,
    SAMPLE_INTERVAL_SECONDS,
    SCHEMA_VERSION,
    AllocationRecord,
    GapAdmissionValidator,
    HostSample,
    ValidationResult,
    _finite,
    read_host_sample,
    validator_identity,
)


@dataclass
class AdmissionAttempt:
    """One attempt to dispatch one row."""

    row: str
    attempt: int
    state: str
    dispatched: bool
    launch_delay_seconds: float | None = None
    window: ValidationResult | None = None
    during_samples: list[HostSample] = field(default_factory=list)
    terminal_sample: HostSample | None = None
    peak_rise_metrics: list[str] = field(default_factory=list)
    breaches: list[str] = field(default_factory=list)
    gaps: list[float] = field(default_factory=list)
    remaining_budget_lapsed: bool = False
    reasons: list[str] = field(default_factory=list)
    allocation: AllocationRecord | None = None

    @property
    def terminal(self) -> bool:
        """§5.1: an absent final sample leaves the row non-terminal."""

        return (not self.dispatched) or self.terminal_sample is not None

    @property
    def observation(self) -> bool:
        return self.state in OBSERVATION_STATES and self.terminal

    @property
    def passed(self) -> bool:
        return self.state in PASSING_STATES and self.terminal

    @property
    def failed(self) -> bool:
        return self.state == S6_FAILED_IN_WINDOW and self.terminal

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "attempt": self.attempt,
            "state": self.state,
            "dispatched": self.dispatched,
            "terminal": self.terminal,
            "observation": self.observation,
            "passed": self.passed,
            "failed": self.failed,
            "launch_delay_seconds": self.launch_delay_seconds,
            "window": self.window.as_dict() if self.window else None,
            "during_samples": [sample.as_dict() for sample in self.during_samples],
            "terminal_sample": self.terminal_sample.as_dict() if self.terminal_sample else None,
            "peak_rise_metrics": list(self.peak_rise_metrics),
            "breaches": list(self.breaches),
            "gaps": list(self.gaps),
            "remaining_budget_lapsed": self.remaining_budget_lapsed,
            "reasons": list(self.reasons),
            "allocation": self.allocation.as_dict() if self.allocation else None,
        }


@dataclass(frozen=True)
class Classification:
    """The state decided for one dispatched row, with the evidence for it."""

    state: str
    reasons: list[str]
    breaches: list[str]
    gaps: list[float]
    rise_metrics: list[str]


def classify_row(
    *,
    window: ValidationResult,
    launch_delay_seconds: float,
    during_samples: Sequence[HostSample],
    terminal_sample: HostSample | None,
    allocated: bool,
    failed: bool,
    validator: GapAdmissionValidator,
    max_launch_delay_seconds: float = MAX_LAUNCH_DELAY_SECONDS,
) -> Classification:
    """Classify one dispatched row into exactly one ``S1``/``S2``/``S3``/``S4``/``S6``.

    ``S0``/``S5`` are decided before dispatch and are not produced here.  The
    order of the checks follows §5.1: a lost allocation or a breached bound is
    ``S3``; an absent final sample leaves the row non-terminal; a gap longer
    than the permitted maximum is ``S4``; a pressure rise that stayed inside
    every bound is ``S2``; otherwise ``S1`` (or ``S6`` when the row failed
    inside the valid window).
    """

    reasons: list[str] = []
    if not allocated:
        return Classification(
            state=S3_CONTROL_LOST,
            reasons=["recorded allocation was lost during the row (§5.1 S3)"],
            breaches=[],
            gaps=[],
            rise_metrics=[],
        )
    if launch_delay_seconds > max_launch_delay_seconds:
        return Classification(
            state=S0_NOT_ADMITTED,
            reasons=[
                (
                    f"launch delay {launch_delay_seconds:.6f}s exceeds "
                    f"{max_launch_delay_seconds:.6f}s; admission expired (§5.1)"
                )
            ],
            breaches=[],
            gaps=[],
            rise_metrics=[],
        )

    breaches: list[str] = []
    for sample in during_samples:
        for breach in validator.breached(sample):
            breaches.append(f"sample {sample.sequence}: {breach}")
    if terminal_sample is not None:
        for breach in validator.breached(terminal_sample):
            breaches.append(f"sample {terminal_sample.sequence}: {breach}")
    if breaches:
        return Classification(
            state=S3_CONTROL_LOST, reasons=reasons, breaches=breaches, gaps=[], rise_metrics=[]
        )

    if terminal_sample is None:
        return Classification(
            state=S4_SAMPLE_GAP,
            reasons=["no final sample at or after row termination; row is non-terminal (§5.1)"],
            breaches=[],
            gaps=[],
            rise_metrics=[],
        )

    # The continuous series from the admitted window's final sample through the
    # terminal sample.  A gap anywhere in it is an S4 gap, including the lead-in
    # from the window itself.
    series = [item for item in ([window.final_sample] if window.final_sample else [])]
    series.extend(during_samples)
    series.append(terminal_sample)
    gaps: list[float] = []
    for index in range(1, len(series)):
        gap = series[index].monotonic_seconds - series[index - 1].monotonic_seconds
        gaps.append(gap)
        if gap > validator.max_gap_seconds:
            reasons.append(
                f"during-row gap {gap:.6f}s before sample {series[index].sequence} exceeds "
                f"{validator.max_gap_seconds:.6f}s (§5.1 S4)"
            )
    if reasons:
        return Classification(
            state=S4_SAMPLE_GAP, reasons=reasons, breaches=[], gaps=gaps, rise_metrics=[]
        )

    rise: list[str] = []
    for name, peak in (
        ("avg10", window.peak_avg10),
        ("avg60", window.peak_avg60),
        ("load1", window.peak_load1),
    ):
        values = [
            value
            for value in (getattr(sample, name) for sample in [*during_samples, terminal_sample])
            if _finite(value)
        ]
        if peak is not None and values and max(values) > peak:
            rise.append(name)
    state = S2_PRESSURE_ROSE if rise else S1_ADMITTED
    if failed:
        state = S6_FAILED_IN_WINDOW
        reasons.append("row failed inside a valid window (§5.1 S6)")
    return Classification(state=state, reasons=reasons, breaches=[], gaps=gaps, rise_metrics=rise)


@dataclass
class RowResult:
    """The retained record for one of the nine rows."""

    row: str
    attempts: list[AdmissionAttempt] = field(default_factory=list)
    outcome: str = "not run"

    @property
    def final_attempt(self) -> AdmissionAttempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def state(self) -> str:
        attempt = self.final_attempt
        return attempt.state if attempt else S5_ADMISSION_UNAVAILABLE

    @property
    def terminal(self) -> bool:
        final = self.final_attempt
        if final is None or not final.terminal:
            return False
        return final.state not in INADMISSIBLE_STATES

    @property
    def observation(self) -> bool:
        final = self.final_attempt
        return final is not None and final.observation

    @property
    def passed(self) -> bool:
        final = self.final_attempt
        return final is not None and final.passed

    @property
    def failed(self) -> bool:
        final = self.final_attempt
        return final is not None and final.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "state": self.state,
            "outcome": self.outcome,
            "terminal": self.terminal,
            "observation": self.observation,
            "passed": self.passed,
            "failed": self.failed,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


class AdmissionWrapper:
    """The declared, hashed per-row admission wrapper required by §5.1.

    ``max_replacement_attempts`` is **declared**, not inferred: it bounds a
    replacement of an ``S3``/``S4`` row (whose observation was discarded
    because the control was invalid) and never re-runs a row that failed in
    ``S1``/``S2``.  It exists so the run terminates; it is not an outcome
    filter, and every attempt is retained.
    """

    def __init__(
        self,
        *,
        rows: Sequence[str],
        allowed_cpus: int,
        allocation: AllocationRecord | None = None,
        clock: Callable[[], float] = time.monotonic,
        reader: Callable[[], HostSample] = read_host_sample,
        sleep: Callable[[float], None] = time.sleep,
        sample_interval_seconds: float = SAMPLE_INTERVAL_SECONDS,
        max_sample_gap_seconds: float = MAX_SAMPLE_GAP_SECONDS,
        max_launch_delay_seconds: float = MAX_LAUNCH_DELAY_SECONDS,
        observation_bound_seconds: float = OBSERVATION_BOUND_SECONDS,
        pressure_bound_percent: float = PRESSURE_BOUND_PERCENT,
        qualifying_seconds: float = QUALIFYING_SECONDS,
        max_replacement_attempts: int = 1,
        during_row_observer: Callable[
            [Callable[[], bool], list[HostSample]], tuple[bool, None | str]
        ]
        | None = None,
    ) -> None:
        self.rows = list(rows)
        self.allowed_cpus = int(allowed_cpus)
        self.allocation = allocation
        self.clock = clock
        self.reader = reader
        self.sleep = sleep
        self.sample_interval_seconds = float(sample_interval_seconds)
        self.max_launch_delay_seconds = float(max_launch_delay_seconds)
        self.observation_bound_seconds = float(observation_bound_seconds)
        self.max_replacement_attempts = int(max_replacement_attempts)
        self._during_row_observer = during_row_observer or self._observe_during_row
        self.validator = GapAdmissionValidator(
            pressure_bound_percent=pressure_bound_percent,
            qualifying_seconds=qualifying_seconds,
            max_gap_seconds=max_sample_gap_seconds,
            allowed_cpus=self.allowed_cpus,
        )
        self.samples: list[HostSample] = []
        self._sequence = 0
        self._sample_lock = threading.Lock()

    # -- sampling ---------------------------------------------------------
    def _next(self) -> HostSample:
        """Take one sample and append it to the retained series.

        The reader may return a reusable object, so an immutable copy is stored
        with the sequence assigned here.  Callers that read concurrently with
        the during-row observer hold ``_sample_lock``.
        """

        sample = self.reader()
        sample = HostSample(
            sequence=self._sequence,
            monotonic_seconds=sample.monotonic_seconds,
            avg10=sample.avg10,
            avg60=sample.avg60,
            load1=sample.load1,
            problems=sample.problems,
        )
        self._sequence += 1
        self.samples.append(sample)
        return sample

    def _sample_until(self, stop: threading.Event, sink: list[HostSample]) -> None:
        """Sample at the frozen cadence into ``sink`` until ``stop`` is set.

        Sampling is bounded in both rate and count: a degenerate interval is
        floored to a real cadence and the retained series is capped, so a long
        or badly-configured row cannot spin or exhaust memory.
        """

        interval = max(self.sample_interval_seconds, MIN_SAMPLE_INTERVAL_SECONDS)
        while len(sink) < MAX_DURING_ROW_SAMPLES and not stop.wait(interval):
            with self._sample_lock:
                sample = self._next()
            sink.append(sample)
        if len(sink) >= MAX_DURING_ROW_SAMPLES:
            sink.append(
                HostSample(
                    sequence=-1,
                    monotonic_seconds=self.clock(),
                    avg10=None,
                    avg60=None,
                    load1=None,
                    problems=(f"during-row sampling stopped at {MAX_DURING_ROW_SAMPLES} samples",),
                )
            )

    def _observe_during_row(
        self, dispatch: Callable[[], bool], sink: list[HostSample]
    ) -> tuple[bool, None | str]:
        """Run ``dispatch`` beside a during-row observer.

        Returns the row's ``failed`` flag and a problem string when the observer
        could not be shown to have stopped.  The observer is a daemon thread, so
        a pathological dispatch cannot keep the interpreter alive after the run.
        """

        stop = threading.Event()
        observer = threading.Thread(
            target=self._sample_until,
            args=(stop, sink),
            name="during-row-observer",
            daemon=True,
        )
        observer.start()
        try:
            failed = bool(dispatch())
        finally:
            stop.set()
            observer.join(timeout=max(self.sample_interval_seconds * 2, 1.0))
        if observer.is_alive():
            return failed, (
                "during-row observer did not stop within its bound; the during-row "
                "series cannot be shown to be continuous (§5.1 S4)"
            )
        return failed, None

    def wait_for_window(self) -> tuple[ValidationResult, bool]:
        """Wait for a **fresh** qualifying window; return it and whether waiting lapsed.

        §5.1 is explicit that "admission is never inherited" and that "a first
        row's window never admits the second".  The validator therefore sees
        only the samples taken **after this row became due**: a run that
        qualified for an earlier row cannot be handed to this one, and each row
        must build its own continuous 60 s run from new samples.
        """

        start_index = len(self.samples)
        started = self.clock()
        for _attempt in range(MAX_WINDOW_ATTEMPTS):
            if self.clock() - started > self.observation_bound_seconds:
                break
            with self._sample_lock:
                self._next()
                fresh = list(self.samples[start_index:])
            window = self.validator.validate(fresh)
            if window.qualifying:
                return window, False
            self.sleep(self.sample_interval_seconds)
        with self._sample_lock:
            self._next()
            fresh = list(self.samples[start_index:])
        return self.validator.validate(fresh), True

    def _terminal_sample(self) -> HostSample:
        """Take the one final sample required at or after row termination."""

        with self._sample_lock:
            return self._next()

    # -- driving ----------------------------------------------------------
    def run(
        self,
        dispatch: Callable[[str], bool],
        *,
        outcome: Callable[[str], str] | None = None,
        allocation_held: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Drive the rows; ``dispatch`` returns whether the row failed.

        ``dispatch`` must run the row's unchanged command and return ``True``
        when it failed.  This module never alters the row, its bounds, or its
        assertion.  ``allocation_held`` is re-checked after each row; losing the
        recorded allocation during the row makes the row ``S3`` (§5.1).
        """

        identity = validator_identity()
        results: list[RowResult] = []
        blocked_from: int | None = None
        held = allocation_held or (lambda: True)
        for index, row in enumerate(self.rows):
            if blocked_from is not None:
                results.append(self._blocked(row))
                continue
            record = RowResult(row=row)
            results.append(record)
            for attempt in range(1, self.max_replacement_attempts + 2):
                if self.allocation is None or not self.allocation.held:
                    attempt_record = AdmissionAttempt(
                        row=row,
                        attempt=attempt,
                        state=S5_ADMISSION_UNAVAILABLE,
                        dispatched=False,
                        reasons=["no verified allocation is held (§5, §5.1 S0/S5)"],
                        allocation=self.allocation,
                    )
                    record.attempts.append(attempt_record)
                    blocked_from = index
                    break
                window, lapsed = self.wait_for_window()
                if lapsed or not window.qualifying:
                    attempt_record = AdmissionAttempt(
                        row=row,
                        attempt=attempt,
                        state=S5_ADMISSION_UNAVAILABLE,
                        dispatched=False,
                        window=window,
                        remaining_budget_lapsed=lapsed,
                        reasons=[
                            "no fresh qualifying window within the observation bound (§5.1 S5)"
                        ]
                        + list(window.reasons),
                        allocation=self.allocation,
                    )
                    record.attempts.append(attempt_record)
                    blocked_from = index
                    break
                assert window.final_sample is not None
                launch_delay = self.clock() - window.final_sample.monotonic_seconds
                attempt_record = AdmissionAttempt(
                    row=row,
                    attempt=attempt,
                    state=S0_NOT_ADMITTED,
                    dispatched=False,
                    launch_delay_seconds=launch_delay,
                    window=window,
                    allocation=self.allocation,
                )
                if launch_delay > self.max_launch_delay_seconds:
                    attempt_record.reasons.append(
                        f"launch delay {launch_delay:.6f}s exceeds "
                        f"{self.max_launch_delay_seconds:.6f}s; admission expired, row re-enters S0"
                    )
                    record.attempts.append(attempt_record)
                    continue

                # Dispatch with a during-row observer running beside it.
                attempt_record.dispatched = True
                during: list[HostSample] = []
                failed, observer_problem = self._during_row_observer(
                    lambda row=row: dispatch(row), during
                )
                lost = (self.allocation is None) or (not held())
                if observer_problem is None:
                    terminal = self._terminal_sample()
                else:
                    # An observer that may still be appending cannot define a
                    # terminal sample either: the series is incomplete.
                    terminal = None
                attempt_record.during_samples = during
                attempt_record.terminal_sample = terminal
                classification = classify_row(
                    window=window,
                    launch_delay_seconds=launch_delay,
                    during_samples=during,
                    terminal_sample=terminal,
                    allocated=not lost,
                    failed=failed,
                    validator=self.validator,
                    max_launch_delay_seconds=self.max_launch_delay_seconds,
                )
                attempt_record.state = classification.state
                attempt_record.reasons.extend(classification.reasons)
                if observer_problem is not None:
                    attempt_record.reasons.append(observer_problem)
                attempt_record.breaches.extend(classification.breaches)
                attempt_record.gaps.extend(classification.gaps)
                attempt_record.peak_rise_metrics.extend(classification.rise_metrics)
                if outcome is not None:
                    record.outcome = outcome(row)
                record.attempts.append(attempt_record)
                if classification.state in CONTROLLED_STATES or (
                    classification.state == S6_FAILED_IN_WINDOW
                ):
                    # S1/S2/S6 are terminal: a row that failed in a valid window
                    # is never re-run for admission reasons (§5.1 rule 1).
                    break
                if attempt <= self.max_replacement_attempts:
                    continue
                break
            final = record.final_attempt
            if final is not None and final.state in (S5_ADMISSION_UNAVAILABLE, S0_NOT_ADMITTED):
                blocked_from = index
        return self.report(results, identity)

    def _blocked(self, row: str) -> RowResult:
        record = RowResult(row=row)
        record.attempts.append(
            AdmissionAttempt(
                row=row,
                attempt=0,
                state=S5_ADMISSION_UNAVAILABLE,
                dispatched=False,
                reasons=[S5_REMAINING_ROWS_REASON],
                allocation=self.allocation,
            )
        )
        return record

    def report(self, results: Sequence[RowResult], identity: dict[str, Any]) -> dict[str, Any]:
        """Assemble the run record; success requires every row to be an observation."""

        completed = sum(1 for item in results if item.terminal)
        observations = sum(1 for item in results if item.observation)
        passes = sum(1 for item in results if item.passed)
        inadmissible = sum(
            1
            for item in results
            if (item.final_attempt is not None) and item.final_attempt.state in INADMISSIBLE_STATES
        )
        failures = sum(1 for item in results if item.failed)
        not_run = sum(
            1
            for item in results
            if item.final_attempt is not None
            and S5_REMAINING_ROWS_REASON in item.final_attempt.reasons
        )
        success = (
            len(results) == len(self.rows) and observations == len(self.rows) and failures == 0
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "validator": identity,
            "allocation": self.allocation.as_dict() if self.allocation else None,
            "allowed_cpus": self.allowed_cpus,
            "qualifying_seconds": self.validator.qualifying_seconds,
            "pressure_bound_percent": self.validator.pressure_bound_percent,
            "max_sample_gap_seconds": self.validator.max_gap_seconds,
            "max_launch_delay_seconds": self.max_launch_delay_seconds,
            "sample_interval_seconds": self.sample_interval_seconds,
            "max_replacement_attempts": self.max_replacement_attempts,
            "rows": [item.as_dict() for item in results],
            "counts": {
                "rows": len(results),
                "terminal": completed,
                "observations": observations,
                "passes": passes,
                "inadmissible": inadmissible,
                "failures_in_window": failures,
                "not_run": not_run,
            },
            "matrix_success": success,
            "matrix_status": "passed" if success else "not successful",
            "samples": [sample.as_dict() for sample in self.samples],
        }


def load_allocation(document: dict[str, Any] | str | Path) -> AllocationRecord:
    """Load an allocation record from a mapping, a JSON file, or a JSON string."""

    if isinstance(document, (str, Path)):
        text = Path(document).read_text() if Path(document).exists() else str(document)
        document = json.loads(text)
    return AllocationRecord(
        allocation_id=str(document.get("allocation_id", "")),
        kind=str(document.get("kind", "")),
        extent=dict(document.get("extent") or {}),
        holder=str(document.get("holder", "")),
        source=str(document.get("source", "")),
        started_utc=str(document.get("started_utc", "")),
        expires_utc=document.get("expires_utc"),
    )


def validate_retained_series(
    samples: Iterable[Any], *, allowed_cpus: int, **kwargs: Any
) -> dict[str, Any]:
    """§9.1's read-only validation procedure over a retained sample series."""

    series: list[HostSample] = []
    for index, item in enumerate(samples):
        if isinstance(item, HostSample):
            series.append(item)
            continue
        facts = item.get("facts") if isinstance(item, dict) else None
        avg10 = item.get("avg10") if isinstance(item, dict) else None
        avg60 = item.get("avg60") if isinstance(item, dict) else None
        load1 = item.get("load1") if isinstance(item, dict) else None
        if isinstance(facts, dict) and avg10 is None and load1 is None:
            load = facts.get("load_average")
            load1 = load[0] if isinstance(load, (list, tuple)) and load else None
            avg10 = None
        series.append(
            HostSample(
                sequence=int(item.get("sequence", index)) if isinstance(item, dict) else index,
                monotonic_seconds=float(
                    item.get("monotonic_seconds", index) if isinstance(item, dict) else index
                ),
                avg10=None if avg10 is None else float(avg10),
                avg60=None if avg60 is None else float(avg60),
                load1=None if load1 is None else float(load1),
                problems=()
                if all(_finite(value) for value in (avg10, avg60, load1))
                else ("series entry is unusable",),
            )
        )
    validator = GapAdmissionValidator(allowed_cpus=allowed_cpus, **kwargs)
    result = validator.validate(series)
    return {"validator": validator_identity(), "result": result.as_dict()}


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from scripts.timed_frame_runner import RowCommand, run_wrapper, write_record

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--validate-series",
        help="read-only validation over a retained sample series (JSON list); "
        "prints the window decision and exits",
    )
    parser.add_argument("--allowed-cpus", type=int, required=True)
    parser.add_argument("--allocation", help="JSON allocation record (§5)")
    parser.add_argument("--rows", help="JSON list of row names, in order")
    parser.add_argument("--output", help="where to write the run record")
    parser.add_argument("--identity-only", action="store_true")
    parser.add_argument(
        "--command",
        action="append",
        default=[],
        metavar="ROW=CMD",
        help=(
            "the unchanged per-row command, as ROW=<shell command>; repeat once "
            "per row. The row is dispatched only after it is admitted"
        ),
    )
    parser.add_argument(
        "--allocation-probe",
        help="a shell command whose zero exit status proves the recorded "
        "allocation is still held; re-checked after each row",
    )
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument(
        "--observation-bound-seconds",
        type=float,
        default=None,
        help=(
            "how long a row may wait for a fresh qualifying window before it is "
            "reported S5; defaults to the protocol's 1800 s bound"
        ),
    )
    args = parser.parse_args(argv)

    if args.identity_only:
        print(json.dumps(validator_identity(), indent=2, sort_keys=True))
        return 0
    if args.validate_series:
        document = json.loads(args.validate_series)
        print(
            json.dumps(
                validate_retained_series(document, allowed_cpus=args.allowed_cpus),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command:
        if not args.allocation:
            parser.error(
                "a row is dispatched only under a recorded allocation; supply "
                "--allocation from #85 rather than relying on host load"
            )
        rows = json.loads(args.rows) if args.rows else []
        commands = []
        for item in args.command:
            name, _, command = item.partition("=")
            if not command:
                parser.error(f"--command needs ROW=CMD, got {item!r}")
            commands.append(RowCommand(row=name, command=command))
        if rows and [item.row for item in commands] != rows:
            parser.error("--rows and --command must name the same rows, in the same order")
        record = run_wrapper(
            commands=commands,
            allocation_path=args.allocation,
            allowed_cpus=args.allowed_cpus,
            allocation_probe=args.allocation_probe,
            timeout_seconds=args.timeout_seconds,
            observation_bound_seconds=args.observation_bound_seconds,
        )
        if args.output:
            write_record(record, args.output)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0 if record["matrix_success"] else 1
    parser.error(
        "this wrapper only admits rows under a verified allocation and a fresh "
        "per-row window; supply --command (to drive rows) or --validate-series "
        "for a read-only check"
    )
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
