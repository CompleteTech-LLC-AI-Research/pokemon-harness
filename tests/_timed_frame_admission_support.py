"""Shared test doubles for the §5.1 per-row capacity/admission wrapper (#84).

Extracted verbatim from ``tests/test_timed_frame_admission.py`` (#122).  These
doubles inject every host sample, so the state table is exercised
deterministically instead of at the mercy of the host's load window.  The
wrapper decides admission only; a row's pass or failure is supplied by the
caller's own result (#84).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import timed_frame_admission as admission

ALLOWED_CPUS = 4


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class SeriesReader:
    """A reader that yields scripted samples and then repeats the last one."""

    def __init__(self, clock: FakeClock, series: list[tuple[float, float, float]]) -> None:
        self.clock = clock
        self.series = list(series)
        self.index = 0

    def __call__(self) -> admission.HostSample:
        if self.index < len(self.series):
            avg10, avg60, load1 = self.series[self.index]
            self.index += 1
        else:
            avg10, avg60, load1 = self.series[-1]
        self.clock.advance(5.0)
        return admission.HostSample(
            sequence=self.index,
            monotonic_seconds=self.clock.now,
            avg10=avg10,
            avg60=avg60,
            load1=load1,
        )


def quiet_series(count: int = 20) -> list[tuple[float, float, float]]:
    return [(1.0, 2.0, 0.5)] * count


def iso(instant: datetime) -> str:
    """Render one instant in the only form §5's record accepts."""

    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_allocation(**overrides) -> admission.AllocationRecord:
    """A reservation that is *live* unless the row overrides its span.

    The span is derived from the wall clock rather than hard-coded: a fixed
    ``expires_utc`` becomes a dead reservation the moment it passes, and a dead
    span is not a held allocation (§5).  Rows that want a dead or malformed span
    override ``started_utc``/``expires_utc`` explicitly.
    """

    now = datetime.now(UTC)
    values = {
        "allocation_id": "resv-84-1",
        "kind": "reservation",
        "extent": {"cpus": 4, "cpuset": "0-3", "memory_bytes": 8 * 1024**3},
        "holder": "operator-runner-85",
        "source": "qualification-runner://reservation/84",
        "started_utc": iso(now - timedelta(hours=1)),
        "expires_utc": iso(now + timedelta(hours=6)),
    }
    values.update(overrides)
    return admission.AllocationRecord(**values)


def make_wrapper(clock: FakeClock, reader, rows, **overrides):
    values = {
        "rows": rows,
        "allowed_cpus": ALLOWED_CPUS,
        "allocation": make_allocation(),
        "clock": clock,
        "reader": reader,
        "sleep": lambda _seconds: None,
        "max_replacement_attempts": 1,
        "during_row_observer": ScriptedObserver(reader),
    }
    values.update(overrides)
    return admission.AdmissionWrapper(**values)


class ScriptedObserver:
    """Deterministic during-row observer: run the row, then draw N samples.

    The state table is about *what the samples say*, not about thread timing, so
    the tests drive the series directly.  ``ScriptedObserver`` still performs a
    real dispatch call, so a row's own result is genuinely collected.
    """

    def __init__(self, reader: SeriesReader) -> None:
        self.reader = reader
        self.samples_per_row = 1
        self.problem: str | None = None
        # When set, these replace the reader for the during-row series.  The
        # clock still advances at the frozen cadence between them.
        self.scripted: list[tuple[float, float, float]] = []
        self.advance_seconds = 5.0

    def __call__(self, dispatch, sink):
        failed = bool(dispatch())
        if self.scripted:
            for avg10, avg60, load1 in self.scripted:
                self.reader.clock.advance(self.advance_seconds)
                sink.append(
                    admission.HostSample(
                        sequence=self.reader.index,
                        monotonic_seconds=self.reader.clock.now,
                        avg10=avg10,
                        avg60=avg60,
                        load1=load1,
                    )
                )
        else:
            for _ in range(self.samples_per_row):
                sink.append(self.reader())
        return failed, self.problem


def write_allocation(tmp_path: Path, **overrides) -> Path:
    document = make_allocation(**overrides).as_dict()
    document.pop("problems", None)
    document.pop("held", None)
    document.pop("span_expired", None)
    document.pop("span_not_started", None)
    path = tmp_path / "allocation.json"
    path.write_text(json.dumps(document))
    return path


def passing_wrapper(clock: FakeClock, reader, rows, **overrides):
    """A wrapper whose admission always succeeds, for row-driving tests."""

    overrides.setdefault("max_replacement_attempts", 0)
    return make_wrapper(clock, reader, rows, **overrides)
