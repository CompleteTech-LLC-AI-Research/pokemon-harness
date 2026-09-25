"""Contracts for the §5.1 per-row capacity/admission wrapper (#84).

Asset-free: no ROM, emulator, or MCP process is started.  Every host sample is
injected, so the state table is exercised deterministically instead of at the
mercy of the host's load window.  The wrapper decides admission only; a row's
pass or failure is supplied by the caller's own result.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import timed_frame_admission as admission
from scripts import timed_frame_window as window

pytestmark = pytest.mark.unit

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


# --- the named, pinned gap/admission validator ---------------------------


def test_validator_identity_is_pinned_by_name_and_sha256():
    identity = admission.validator_identity()

    assert identity["name"] == "timed-frame-gap-admission-validator"
    assert identity["module"].endswith(".py")
    assert isinstance(identity["bytes"], int) and identity["bytes"] > 0
    assert len(identity["sha256"]) == 64
    assert set(identity["sha256"]) <= set("0123456789abcdef")
    # The digest is computed from the file's own bytes, so re-deriving it is
    # deterministic and a second engineer can re-run the check.
    assert admission.validator_identity()["sha256"] == identity["sha256"]


def test_validator_admits_only_a_continuous_sixty_second_quiet_run():
    clock = FakeClock()
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    samples = [
        admission.HostSample(index, clock.now + index * 5.0, 1.0, 2.0, 0.5) for index in range(13)
    ]

    window = validator.validate(samples)

    assert window.qualifying is True
    assert window.quiet_seconds == pytest.approx(60.0)
    assert max(window.gaps) <= admission.MAX_SAMPLE_GAP_SECONDS
    assert window.final_sample is samples[-1]
    assert window.reasons == ()


def test_validator_rejects_a_quiet_run_whose_gap_exceeds_the_permitted_maximum():
    clock = FakeClock()
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    samples = [
        admission.HostSample(index, clock.now + index * 5.0, 1.0, 2.0, 0.5) for index in range(7)
    ]
    # A 7.4 s gap breaks continuity, so the run must restart from the next
    # sample rather than counting the whole span.
    samples.append(
        admission.HostSample(len(samples), samples[-1].monotonic_seconds + 7.4, 1.0, 2.0, 0.5)
    )

    window = validator.validate(samples)

    assert window.qualifying is False
    assert window.quiet_seconds == 0.0
    assert any("exceeds" in item for item in window.restarts)
    assert any("below the required" in item for item in window.reasons)


def test_validator_treats_the_boundary_as_a_breach_and_just_under_as_quiet():
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)

    # §5: the window qualifies on strictly less than each bound.
    assert validator.is_quiet(admission.HostSample(0, 0.0, 9.999, 9.999, 3.999)) is True
    # §5.1 S3 breaches on >= each bound.
    assert validator.is_quiet(admission.HostSample(0, 0.0, 10.0, 1.0, 0.5)) is False
    assert validator.is_quiet(admission.HostSample(0, 0.0, 1.0, 10.0, 0.5)) is False
    assert validator.is_quiet(admission.HostSample(0, 0.0, 1.0, 1.0, 4.0)) is False

    assert validator.breached(admission.HostSample(0, 0.0, 10.0, 1.0, 0.5))
    assert validator.breached(admission.HostSample(0, 0.0, 1.0, 10.0, 0.5))
    assert validator.breached(admission.HostSample(0, 0.0, 1.0, 1.0, 4.0))
    assert validator.breached(admission.HostSample(0, 0.0, 9.9, 9.9, 3.9)) == []


def test_validator_restarts_the_window_when_pressure_rose_above_a_bound_then_returned():
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    samples = [admission.HostSample(index, index * 5.0, 1.0, 2.0, 0.5) for index in range(6)]
    samples.append(admission.HostSample(6, 30.0, 18.0, 12.0, 9.0))
    samples.extend(
        admission.HostSample(7 + index, 35.0 + index * 5.0, 1.0, 2.0, 0.5) for index in range(6)
    )

    window = validator.validate(samples)

    # Only the post-breach quiet run counts, and it is 25 s: below 60 s.
    assert window.qualifying is False
    assert window.quiet_seconds == pytest.approx(25.0)


def test_validator_never_admits_on_an_unusable_sample():
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    broken = [
        admission.HostSample(index, index * 5.0, None, None, None, ("unreadable host",))
        for index in range(20)
    ]

    window = validator.validate(broken)

    assert window.qualifying is False
    assert window.final_sample is None


# --- the S0-S6 state table ------------------------------------------------


def test_s1_valid_observation_with_a_during_row_rise_recorded_as_s2():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(20))
    observer = ScriptedObserver(reader)
    # A rise above the window's peak that stays strictly inside every bound.
    observer.scripted = [(6.0, 4.0, 1.5)]
    wrapper = make_wrapper(clock, reader, ["row-a"], during_row_observer=observer)

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S2_PRESSURE_ROSE
    assert attempt["dispatched"] is True
    assert attempt["observation"] is True
    assert attempt["passed"] is True and attempt["failed"] is False
    assert attempt["peak_rise_metrics"], "the rise must be named, not inferred"
    assert report["rows"][0]["state"] == admission.S2_PRESSURE_ROSE
    assert report["rows"][0]["terminal"] is True
    assert report["counts"]["observations"] == 1


def test_s1_is_recorded_when_no_during_row_sample_rises_above_the_window():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(20))
    wrapper = make_wrapper(clock, reader, ["row-a"])

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S1_ADMITTED
    assert attempt["peak_rise_metrics"] == []
    assert attempt["observation"] is True and attempt["passed"] is True


def test_s3_breach_makes_the_row_inadmissible_and_never_a_pass_or_failure():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(20))
    observer = ScriptedObserver(reader)
    observer.scripted = [(22.0, 12.0, 0.5)]
    wrapper = make_wrapper(
        clock,
        reader,
        ["row-a"],
        max_replacement_attempts=0,
        during_row_observer=observer,
    )

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S3_CONTROL_LOST
    assert attempt["observation"] is False
    assert attempt["passed"] is False and attempt["failed"] is False
    assert attempt["breaches"], "the breached bound must be recorded"
    assert report["counts"]["inadmissible"] == 1
    assert report["matrix_success"] is False
    assert report["matrix_status"] == "not successful"


def test_a_row_that_fails_inside_a_valid_window_is_s6_and_is_never_rerun():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(20))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=3)
    calls = {"n": 0}

    def dispatch(_row: str) -> bool:
        calls["n"] += 1
        return True

    report = wrapper.run(dispatch)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S6_FAILED_IN_WINDOW
    assert attempt["failed"] is True and attempt["passed"] is False
    # §5.1 rule 1: a row that failed in S1/S2 is never re-run for admission.
    assert calls["n"] == 1
    assert len(report["rows"][0]["attempts"]) == 1
    assert report["counts"]["failures_in_window"] == 1


def test_s4_gap_discards_the_observation_and_replaces_it_under_a_fresh_window():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(60))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=2)
    calls = {"n": 0}

    def dispatch(_row: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            # A 7.4 s interior gap, past the 6.0 s permitted maximum.
            reader.series.append((1.0, 2.0, 0.5))
            clock.advance(7.4)
        return False

    report = wrapper.run(dispatch)

    states = [item["state"] for item in report["rows"][0]["attempts"]]
    assert states[0] == admission.S4_SAMPLE_GAP
    # The discarded row is replaced, and the replacement is judged on its own.
    assert states[-1] == admission.S1_ADMITTED
    assert report["rows"][0]["attempts"][0]["observation"] is False
    assert report["rows"][0]["terminal"] is True
    assert report["counts"]["inadmissible"] == 0
    assert all(item["dispatched"] for item in report["rows"][0]["attempts"]), (
        "an S4 replacement must be a real dispatch, not an assumption"
    )


def test_s5_no_qualifying_window_stops_dispatch_and_leaves_remaining_rows_not_run():
    clock = FakeClock()
    # A permanently saturated host: never quiet, so no window ever qualifies.
    reader = SeriesReader(clock, [(40.0, 30.0, 50.0)] * 400)
    wrapper = make_wrapper(
        clock,
        reader,
        ["row-a", "row-b", "row-c"],
        observation_bound_seconds=30.0,
    )
    calls: list[str] = []

    report = wrapper.run(lambda row: calls.append(row) or False)

    assert calls == [], "no row may be dispatched without a qualifying window"
    assert report["rows"][0]["state"] == admission.S5_ADMISSION_UNAVAILABLE
    assert report["rows"][1]["state"] == admission.S5_ADMISSION_UNAVAILABLE
    assert report["rows"][2]["state"] == admission.S5_ADMISSION_UNAVAILABLE
    assert report["counts"]["not_run"] == 2
    assert report["matrix_success"] is False
    assert report["matrix_status"] == "not successful"
    remaining = report["rows"][1]["attempts"][-1]["reasons"]
    assert admission.S5_REMAINING_ROWS_REASON in remaining


def test_no_allocation_means_the_row_is_not_admitted_whatever_the_host_shows():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(clock, reader, ["row-a", "row-b"], allocation=None)
    calls: list[str] = []

    report = wrapper.run(lambda row: calls.append(row) or False)

    assert calls == []
    assert report["rows"][0]["state"] == admission.S5_ADMISSION_UNAVAILABLE
    assert report["allocation"] is None
    assert report["matrix_success"] is False


def test_affinity_and_quota_records_are_not_allocations():
    for kind in ("affinity", "cgroup_quota"):
        record = make_allocation(kind=kind)
        assert record.held is False
        assert any("reservation" in item for item in record.problems())


def test_allocation_loss_during_a_row_makes_that_row_s3():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(20))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)
    alive = {"held": True}

    def dispatch(_row: str) -> bool:
        alive["held"] = False
        return False

    report = wrapper.run(dispatch, allocation_held=lambda: alive["held"])

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S3_CONTROL_LOST
    assert attempt["observation"] is False
    assert any("allocation" in item for item in attempt["reasons"])


def test_admission_expiry_refuses_the_row_and_it_re_enters_s0():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(60))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=1)
    calls: list[str] = []

    original = wrapper.wait_for_window
    delays = [9.0, 0.0]

    def slow_first_admission():
        window, lapsed = original()
        # Time passes between the window's final sample and dispatch.  Nine
        # seconds is past the 6.0 s launch bound, so the admission expires.
        clock.advance(delays.pop(0))
        return window, lapsed

    wrapper.wait_for_window = slow_first_admission  # type: ignore[method-assign]
    report = wrapper.run(lambda row: calls.append(row) or False)

    first = report["rows"][0]["attempts"][0]
    assert first["state"] == admission.S0_NOT_ADMITTED
    assert first["dispatched"] is False
    assert any("expired" in item for item in first["reasons"])
    # The row re-enters S0 and is dispatched only under a fresh admission.
    assert calls == ["row-a"]


def test_admission_never_inherits_a_window_between_rows():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(200))
    wrapper = make_wrapper(clock, reader, ["row-a", "row-b", "row-c"])
    windows: list[float] = []

    original = wrapper.wait_for_window

    def recording():
        window, lapsed = original()
        windows.append(window.quiet_seconds)
        return window, lapsed

    wrapper.wait_for_window = recording  # type: ignore[method-assign]
    wrapper.run(lambda _row: False)

    # Each row waits for its own window; the third row does not reuse the
    # second row's admission.
    assert len(windows) == 3


def test_an_absent_final_sample_leaves_the_row_non_terminal():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(20))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)
    wrapper._terminal_sample = lambda: None  # type: ignore[method-assign]

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["terminal"] is False
    assert attempt["observation"] is False
    assert report["rows"][0]["terminal"] is False
    assert report["matrix_success"] is False


def test_report_records_the_validator_identity_the_allocation_and_every_sample():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(30))
    wrapper = make_wrapper(clock, reader, ["row-a"])

    report = wrapper.run(lambda _row: False)

    assert report["validator"]["name"] == "timed-frame-gap-admission-validator"
    assert report["allocation"]["kind"] == "reservation"
    assert report["allocation"]["held"] is True
    assert report["allowed_cpus"] == ALLOWED_CPUS
    assert report["qualifying_seconds"] == admission.QUALIFYING_SECONDS
    assert report["max_sample_gap_seconds"] == admission.MAX_SAMPLE_GAP_SECONDS
    # Every retained sample is in the record, so a second engineer can re-run
    # the validator over the series without touching a host.
    assert len(report["samples"]) >= 1
    assert [item["sequence"] for item in report["samples"]] == list(range(len(report["samples"])))


def test_report_is_json_serializable_and_self_checking():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(30))
    wrapper = make_wrapper(clock, reader, ["row-a", "row-b"])

    report = wrapper.run(lambda _row: False)
    text = json.dumps(report, sort_keys=True)

    assert json.loads(text)["counts"]["rows"] == 2
    assert report["matrix_success"] is True
    assert report["matrix_status"] == "passed"
    for row in report["rows"]:
        assert row["attempts"], "every row retains its attempts"


# --- §9.1's read-only validation procedure over a retained series ---------


def test_validate_retained_series_is_read_only_and_names_its_validator():
    series = [
        item.as_dict()
        for item in (admission.HostSample(index, index * 5.0, 1.0, 2.0, 0.5) for index in range(13))
    ]

    result = admission.validate_retained_series(series, allowed_cpus=ALLOWED_CPUS)

    assert result["validator"]["name"] == "timed-frame-gap-admission-validator"
    assert result["validator"]["sha256"] == admission.validator_identity()["sha256"]
    assert result["result"]["qualifying"] is True
    assert result["result"]["quiet_seconds"] == pytest.approx(60.0)
    # The input series is untouched: this is a read-only validation.
    assert len(series) == 13


def test_validate_retained_series_rejects_a_saturated_series():
    series = [
        admission.HostSample(index, index * 5.0, 30.0, 25.0, 40.0).as_dict() for index in range(50)
    ]

    result = admission.validate_retained_series(series, allowed_cpus=ALLOWED_CPUS)

    assert result["result"]["qualifying"] is False


def test_read_host_sample_reports_an_unreadable_host_explicitly(tmp_path):
    sample = admission.read_host_sample(
        loadavg_path=tmp_path / "absent-loadavg",
        pressure_path=tmp_path / "absent-pressure",
    )

    assert sample.usable is False
    assert len(sample.problems) == 2


# --- the CLI refuses to invent an admission ------------------------------


def test_cli_prints_identity_and_refuses_to_dispatch_without_a_series(capsys):
    assert admission._main(["--identity-only", "--allowed-cpus", str(ALLOWED_CPUS)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "timed-frame-gap-admission-validator"

    with pytest.raises(SystemExit):
        admission._main(["--allowed-cpus", str(ALLOWED_CPUS)])


def test_cli_validates_a_retained_series_without_running_a_row(capsys):
    series = [
        admission.HostSample(index, index * 5.0, 1.0, 2.0, 0.5).as_dict() for index in range(13)
    ]

    code = admission._main(
        [
            "--allowed-cpus",
            str(ALLOWED_CPUS),
            "--validate-series",
            json.dumps(series),
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["qualifying"] is True


# --- driving rows under the recorded allocation ---------------------------


def write_allocation(tmp_path: Path, **overrides) -> Path:
    document = make_allocation(**overrides).as_dict()
    document.pop("problems", None)
    document.pop("held", None)
    document.pop("span_expired", None)
    path = tmp_path / "allocation.json"
    path.write_text(json.dumps(document))
    return path


def passing_wrapper(clock: FakeClock, reader, rows, **overrides):
    """A wrapper whose admission always succeeds, for row-driving tests."""

    overrides.setdefault("max_replacement_attempts", 0)
    return make_wrapper(clock, reader, rows, **overrides)


def test_a_row_command_runs_unmodified_and_its_exit_status_is_the_failure_flag(tmp_path):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"])
    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["matrix_success"] is True
    assert record["rows"][0]["state"] == admission.S1_ADMITTED
    assert record["row_runs"][0]["returncode"] == 0
    assert record["row_runs"][0]["failed"] is False
    # The command is retained verbatim, so the row that ran is auditable.
    assert record["row_runs"][0]["command"].endswith("-c pass")


def test_a_failing_row_is_recorded_as_s6_and_never_converted_into_a_pass(tmp_path):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=2)
    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(row="row-a", command=f"{sys.executable} -c 'raise SystemExit(3)'")
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["row_runs"][0]["returncode"] == 3
    assert record["rows"][0]["state"] == admission.S6_FAILED_IN_WINDOW
    assert record["rows"][0]["failed"] is True
    assert record["rows"][0]["passed"] is False
    assert record["matrix_success"] is False
    # A failure inside a valid window is not retried for admission reasons.
    assert len(record["row_runs"]) == 1


def test_rows_are_not_dispatched_when_the_allocation_is_absent(tmp_path):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(clock, reader, ["row-a"], allocation=None)
    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["row_runs"] == [], "a row must never run without an allocation"
    assert record["matrix_success"] is False
    assert record["matrix_status"] == "not successful"


def test_an_affinity_record_is_not_an_allocation_and_cannot_admit_rows(tmp_path):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(clock, reader, ["row-a"], allocation=make_allocation(kind="affinity"))
    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["row_runs"] == []
    assert record["matrix_success"] is False


def test_a_lost_allocation_probe_marks_the_row_s3(tmp_path):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)
    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        # The probe fails, so the allocation is treated as lost during the row.
        allocation_probe=f"{sys.executable} -c 'raise SystemExit(1)'",
        wrapper=wrapper,
    )

    assert record["rows"][0]["state"] == admission.S3_CONTROL_LOST
    assert record["matrix_success"] is False


def test_record_is_written_where_the_caller_declares(tmp_path):
    from scripts import timed_frame_runner as runner

    destination = tmp_path / "nested" / "record.json"
    written = runner.write_record({"matrix_success": True}, destination)

    assert written == destination
    assert json.loads(destination.read_text())["matrix_success"] is True


def test_every_nine_row_orientation_is_a_separate_admission():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(400))
    rows = [f"row-{index}" for index in range(9)]
    wrapper = passing_wrapper(clock, reader, rows)
    admissions: list[float] = []
    original = wrapper.wait_for_window

    def recording():
        window, lapsed = original()
        admissions.append(window.quiet_seconds)
        return window, lapsed

    wrapper.wait_for_window = recording  # type: ignore[method-assign]
    report = wrapper.run(lambda _row: False)

    assert report["counts"]["rows"] == 9
    assert len(admissions) == 9, "each of the nine rows needs its own fresh window"
    assert report["matrix_success"] is True


def test_doc_pin_table_matches_the_sources_it_pins():
    """Every in-repo row of the §2 pin table must match the file it pins.

    The table is what a row record is checked against, so a stale byte count or
    digest makes the recorded identity unverifiable.  Rows naming files that are
    not tracked here (the external observers) are not checked: they are pinned to
    their retained copies, not to a path in this tree.
    """

    repo_root = Path(__file__).resolve().parents[1]
    document = repo_root / "docs" / "TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md"
    rows = re.findall(
        r"^\|\s*`([^`]+\.py)`\s*\|\s*(\d+)\s*\|\s*`([0-9a-f]{64})`",
        document.read_text(),
        re.MULTILINE,
    )

    pinned = {name: (int(size), digest) for name, size, digest in rows}
    assert pinned, "the §2 pin table must declare the validator and the wrapper"

    for name in (
        "scripts/timed_frame_window.py",
        "scripts/timed_frame_admission.py",
        "scripts/timed_frame_runner.py",
    ):
        assert name in pinned, f"{name} must be pinned in §2"

    for name, (size, digest) in pinned.items():
        source = repo_root / name
        if not source.exists():
            continue
        raw = source.read_bytes()
        assert size == len(raw), f"{name}: §2 says {size} bytes, source is {len(raw)}"
        assert digest == hashlib.sha256(raw).hexdigest(), f"{name}: §2 digest is stale"


def test_the_pinned_validator_identity_is_the_validator_actually_used():
    """``validator_identity()`` must describe the code that makes the decision."""

    identity = admission.validator_identity()
    source = Path(window.__file__).resolve()

    assert identity["name"] == window.GapAdmissionValidator.NAME
    assert identity["module"] == "timed_frame_window.py"
    assert identity["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert identity["bytes"] == len(source.read_bytes())


# --- §5: the allocation *span* is part of the record, not decoration -------
#
# §5 requires the rerun to record "which allocation was held, its extent, its
# **span**, and the fact that it is a reservation".  A record that states no
# parseable span, or one that has already ended, is therefore not a verified
# allocation and must not admit a row.  These rows exist because the extent,
# kind, holder and source guards can all pass while the span says the
# reservation is dead — and every other row in this module would still be green.


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("expired", {"expires_utc": "2026-09-25T06:00:00Z"}),
        ("absent", {"expires_utc": None}),
        ("empty", {"expires_utc": ""}),
        ("unparseable", {"expires_utc": "not-a-date"}),
        ("naive offset form", {"expires_utc": "2026-09-25T06:00:00+00:00"}),
        ("absent start", {"started_utc": ""}),
        ("inverted", {"expires_utc": "2020-01-01T00:00:00Z"}),
    ],
)
def test_a_span_that_does_not_state_a_live_reservation_is_not_held(label, overrides):
    record = make_allocation(**overrides)

    assert record.held is False, f"{label}: a dead or absent span is not a held allocation"
    problems = record.problems()
    assert problems, f"{label}: the reason must be recorded, not inferred"
    assert any("span" in item or "held" in item for item in problems), problems


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("expired", {"expires_utc": "2026-09-25T06:00:00Z"}),
        ("absent", {"expires_utc": None}),
        ("unparseable", {"expires_utc": "not-a-date"}),
        ("inverted", {"expires_utc": "2020-01-01T00:00:00Z"}),
    ],
)
def test_a_row_is_never_dispatched_under_a_dead_or_absent_span(tmp_path, label, overrides):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(clock, reader, ["row-a"], allocation=make_allocation(**overrides))

    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["row_runs"] == [], f"{label}: no row may run under a dead span"
    assert record["rows"][0]["state"] == admission.S5_ADMISSION_UNAVAILABLE
    assert record["matrix_success"] is False
    assert record["allocation"]["held"] is False


def test_a_live_span_is_held_and_still_admits_rows(tmp_path):
    """The positive control: the span guard must not refuse every allocation."""

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"])

    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["allocation"]["held"] is True
    assert record["allocation"]["span_expired"] is False
    assert record["rows"][0]["state"] == admission.S1_ADMITTED
    assert record["matrix_success"] is True


def test_a_span_that_ends_during_the_row_makes_it_s3(monkeypatch):
    """§5.1 "Allocation loss": the span is re-checked, not read once."""

    real_now = datetime.now(UTC)
    seen = {"now": real_now}
    monkeypatch.setattr(window, "_utc_now", lambda: seen["now"])

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(
        clock,
        reader,
        ["row-a"],
        allocation=make_allocation(expires_utc=iso(real_now + timedelta(seconds=1))),
    )
    assert wrapper.allocation.held is True

    def dispatch(_row: str) -> bool:
        # The reservation elapses while the row is running.
        seen["now"] = real_now + timedelta(hours=1)
        return False

    report = wrapper.run(dispatch, allocation_held=lambda: wrapper.allocation.held)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S3_CONTROL_LOST
    assert attempt["observation"] is False
    assert attempt["passed"] is False
    assert any("allocation" in item for item in attempt["reasons"])
    assert report["matrix_success"] is False


# --- §5.1: the guarantees the wrapper's own text claims to make -------------
#
# Each row below is a *behavioural* check of one invariant, not a structural
# one.  They exist because the six mutations in the table recorded on #243 all
# left this module fully green: every one of these invariants was previously
# asserted only by the shape of the code, so a regression in any of them was
# invisible.  Each row was verified to fail against its own mutant.


def test_a_row_is_admitted_only_from_samples_taken_after_it_became_due():
    """§5.1 rule 3: "A first row's window never admits the second."

    The wrapper must build each row's window from samples taken *after* that row
    became due.  Reusing the retained series instead would let a row qualify on
    an earlier row's window, so the recorded admission would describe a host
    state that had already been spent.
    """

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(200))
    wrapper = make_wrapper(clock, reader, ["row-a", "row-b", "row-c"])
    due_at: list[int] = []
    admitted: list[tuple[object, int]] = []
    original = wrapper.wait_for_window

    def recording():
        # The index of the first sample that belongs to this row's own window.
        due_at.append(len(wrapper.samples))
        window, lapsed = original()
        admitted.append((window, len(wrapper.samples)))
        return window, lapsed

    wrapper.wait_for_window = recording  # type: ignore[method-assign]
    wrapper.run(lambda _row: False)

    assert len(admitted) == 3
    assert due_at[0] == 0
    assert all(item > 0 for item in due_at[1:]), "each later row starts after the previous rows"

    interval = admission.SAMPLE_INTERVAL_SECONDS
    for index, (result, taken) in enumerate(admitted):
        assert result.final_sample is not None
        assert result.qualifying is True
        fresh = taken - due_at[index]
        # The window can only be as long as the samples that belong to *this*
        # row.  A row that inherited an earlier window reports a 60 s run built
        # from series it never sampled, so the reported run exceeds the span its
        # own fresh samples could possibly cover.
        span_available = (fresh - 1) * interval
        assert result.quiet_seconds <= span_available, (
            f"row {index} reports a {result.quiet_seconds:.1f}s run but only took "
            f"{fresh} sample(s) since it became due ({span_available:.1f}s available); "
            "its window was inherited rather than built from fresh samples"
        )
        assert result.final_sample.sequence >= due_at[index], (
            f"row {index} qualified on a sample taken before it became due"
        )

    # Every row genuinely re-sampled a full qualifying run of its own.
    for index, (result, taken) in enumerate(admitted):
        fresh = taken - due_at[index]
        assert fresh >= 2, f"row {index} was admitted without taking a run of its own"
        assert result.quiet_seconds >= admission.QUALIFYING_SECONDS


def test_an_observer_that_may_still_be_appending_leaves_the_row_non_terminal():
    """§5.1: an observer that cannot be shown to have stopped defines no final sample."""

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    observer = ScriptedObserver(reader)
    # The observer reports that it could not be shown to have stopped.  A series
    # that may still grow cannot be read as complete, so the row is not terminal
    # and its observation must not be read as a pass — even though every sample
    # actually retained is quiet.
    observer.problem = "during-row observer did not stop within its bound"
    wrapper = make_wrapper(
        clock, reader, ["row-a"], max_replacement_attempts=0, during_row_observer=observer
    )
    terminal_calls = {"n": 0}
    real_terminal = wrapper._terminal_sample

    def counting_terminal():
        terminal_calls["n"] += 1
        return real_terminal()

    wrapper._terminal_sample = counting_terminal  # type: ignore[method-assign]
    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["terminal_sample"] is None, (
        "no final sample may be defined while the observer may still be appending"
    )
    assert attempt["terminal"] is False
    assert attempt["observation"] is False, "a non-terminal row supports no conclusion"
    assert attempt["passed"] is False
    assert attempt["state"] == admission.S4_SAMPLE_GAP
    assert any("observer" in item for item in attempt["reasons"])
    assert report["matrix_success"] is False
    assert terminal_calls["n"] == 0, (
        "the terminal sample must not even be read when the observer is unproven"
    )


def test_an_observer_that_stops_cleanly_still_produces_a_terminal_observation():
    """The positive control for the row above: the bound is not a blanket refusal."""

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["terminal_sample"] is not None
    assert attempt["terminal"] is True
    assert attempt["state"] == admission.S1_ADMITTED
    assert attempt["observation"] is True and attempt["passed"] is True


def test_an_exhausted_admission_blocks_every_later_row_instead_of_being_retried():
    """§5.1 `S0`/`S5`: remaining rows are reported, never dispatched, after a stop."""

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(60))
    wrapper = make_wrapper(clock, reader, ["row-a", "row-b", "row-c"])
    calls: list[str] = []
    original = wrapper.wait_for_window

    def always_expired():
        window, lapsed = original()
        # Every admission expires before dispatch (past the 6.0 s launch bound).
        clock.advance(9.0)
        return window, lapsed

    wrapper.wait_for_window = always_expired  # type: ignore[method-assign]
    report = wrapper.run(lambda row: calls.append(row) or False)

    assert calls == [], "nothing may be dispatched while every admission expires"
    assert report["rows"][0]["state"] == admission.S0_NOT_ADMITTED
    for index in (1, 2):
        row = report["rows"][index]
        assert row["state"] == admission.S5_ADMISSION_UNAVAILABLE
        attempt = row["attempts"][-1]
        assert admission.S5_REMAINING_ROWS_REASON in attempt["reasons"], (
            f"row {index} must be reported as blocked, not silently attempted"
        )
        # A blocked row carries no attempt number: it was never tried.
        assert attempt["attempt"] == 0
        assert attempt["dispatched"] is False
    assert report["counts"]["not_run"] == 2
    assert report["matrix_success"] is False


def test_a_gap_of_exactly_the_permitted_maximum_stays_continuous():
    """§5: "every consecutive sample gap is **at most** 6.0 s" — equality is permitted."""

    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    at_bound = [
        admission.HostSample(index, index * admission.MAX_SAMPLE_GAP_SECONDS, 1.0, 2.0, 0.5)
        for index in range(11)
    ]

    run, gaps, _ = validator.trailing_quiet_run(at_bound)
    window = validator.validate(at_bound)

    assert len(run) == 11, "a 6.0 s gap must not restart the run"
    assert max(gaps) == admission.MAX_SAMPLE_GAP_SECONDS
    assert window.qualifying is True
    assert window.quiet_seconds == pytest.approx(60.0)

    past_bound = [
        admission.HostSample(index, index * admission.MAX_SAMPLE_GAP_SECONDS, 1.0, 2.0, 0.5)
        for index in range(11)
    ]
    past_bound.append(
        admission.HostSample(
            11,
            past_bound[-1].monotonic_seconds + admission.MAX_SAMPLE_GAP_SECONDS + 0.001,
            1.0,
            2.0,
            0.5,
        )
    )
    broken = validator.validate(past_bound)

    assert any("exceeds" in item for item in broken.restarts), (
        "a gap past the permitted maximum must still restart continuity"
    )


def test_a_during_row_gap_of_exactly_the_permitted_maximum_is_not_s4():
    """The during-row series uses the same permitted maximum, with the same equality."""

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    observer = ScriptedObserver(reader)
    # Two during-row samples, drawn at exactly one permitted gap apart.
    observer.advance_seconds = admission.MAX_SAMPLE_GAP_SECONDS
    observer.scripted = [(1.0, 2.0, 0.5), (1.0, 2.0, 0.5)]
    wrapper = make_wrapper(
        clock, reader, ["row-a"], max_replacement_attempts=0, during_row_observer=observer
    )

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S1_ADMITTED, (
        "a 6.0 s during-row gap is within the permitted maximum, not an S4 gap"
    )
    assert attempt["observation"] is True
    assert max(attempt["gaps"]) == admission.MAX_SAMPLE_GAP_SECONDS


def test_a_during_row_gap_past_the_permitted_maximum_is_s4():
    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    observer = ScriptedObserver(reader)
    observer.advance_seconds = admission.MAX_SAMPLE_GAP_SECONDS + 0.001
    observer.scripted = [(1.0, 2.0, 0.5), (1.0, 2.0, 0.5)]
    wrapper = make_wrapper(
        clock, reader, ["row-a"], max_replacement_attempts=0, during_row_observer=observer
    )

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S4_SAMPLE_GAP
    assert attempt["observation"] is False
    assert max(attempt["gaps"]) > admission.MAX_SAMPLE_GAP_SECONDS


def test_a_window_is_still_admitted_when_the_bound_is_reached_exactly():
    """§5: the 1800 s observation bound is an upper bound, not an exclusive one.

    A row whose window becomes available exactly at the bound has *not* lapsed;
    treating equality as expiry would refuse a row that the protocol still
    admits, and would report it `S5` as a capacity refusal it was not.
    """

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(
        clock,
        reader,
        ["row-a"],
        max_replacement_attempts=0,
        observation_bound_seconds=30.0,
        qualifying_seconds=30.0,
    )
    calls: list[str] = []

    report = wrapper.run(lambda row: calls.append(row) or False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S1_ADMITTED, (
        "reaching the bound exactly is not a lapse; the row must still be admitted"
    )
    assert attempt["remaining_budget_lapsed"] is False
    assert calls == ["row-a"]
    assert attempt["window"]["quiet_seconds"] == pytest.approx(30.0)


def test_a_window_that_becomes_available_past_the_bound_lapses_as_s5():
    """The positive control: past the bound really is a lapse."""

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(
        clock,
        reader,
        ["row-a"],
        max_replacement_attempts=0,
        observation_bound_seconds=30.0,
        # A 40 s window cannot be assembled before the 30 s bound is passed.
        qualifying_seconds=40.0,
    )
    calls: list[str] = []

    report = wrapper.run(lambda row: calls.append(row) or False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S5_ADMISSION_UNAVAILABLE
    assert attempt["remaining_budget_lapsed"] is True
    assert calls == []


def test_a_breach_in_the_terminal_sample_is_s3_and_not_a_valid_observation():
    """§5.1: "a discarded observation is replaced, not believed" — including the last one.

    The terminal sample is part of the controlled series.  A breach that appears
    only there means the control was lost, so the row is `inadmissible`; reading
    it as `S1` would keep a lost observation as if it were valid evidence.
    """

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)
    breach = admission.HostSample(4242, clock.now, 18.0, 12.0, 0.5)
    wrapper._terminal_sample = lambda: breach  # type: ignore[method-assign]

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S3_CONTROL_LOST
    assert attempt["observation"] is False
    assert attempt["passed"] is False and attempt["failed"] is False
    assert attempt["terminal"] is True
    assert any("4242" in item for item in attempt["breaches"]), (
        "the terminal sample's breach must be named, not assumed away"
    )
    assert report["counts"]["inadmissible"] == 1
    assert report["matrix_success"] is False


def test_a_quiet_terminal_sample_leaves_the_row_a_valid_observation():
    """The positive control for the row above: terminal sampling is still honoured."""

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    report = wrapper.run(lambda _row: False)

    attempt = report["rows"][0]["attempts"][-1]
    assert attempt["state"] == admission.S1_ADMITTED
    assert attempt["terminal"] is True and attempt["observation"] is True


# --- a row that overruns its budget is classified, not dropped --------------
#
# §5.1 leans on the failure record being preserved: a row's budget is part of
# its declared identity, and "an observed failure is never converted into a
# pass by a longer wait" (§3).  An overrun that escaped as an exception would
# produce *no run record at all*, so the row would vanish from the matrix
# instead of being reported.  These rows pin the classifying behaviour.


def test_a_row_that_overruns_its_budget_is_recorded_as_a_failure_not_an_abort(tmp_path):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a",
                command=f"{sys.executable} -c 'import time; time.sleep(30)'",
                timeout_seconds=1.0,
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    # The record exists, names the row, and reports the overrun honestly.
    assert record["row_runs"], "an overrun must still produce a run record"
    run = record["row_runs"][0]
    assert run["timed_out"] is True
    assert run["returncode"] == runner.ROW_TIMEOUT_RETURN_CODE
    assert run["failed"] is True, "an overrun is a failure, never a pass"
    assert run["command"].endswith("time.sleep(30)'")
    assert "budget" in run["stderr"]
    # It is classified in place of the row, and never re-run.
    assert len(record["row_runs"]) == 1
    assert record["rows"][0]["state"] == admission.S6_FAILED_IN_WINDOW
    assert record["rows"][0]["failed"] is True
    assert record["rows"][0]["passed"] is False
    assert record["matrix_success"] is False


def test_a_row_that_overruns_the_wrapper_budget_is_also_classified(tmp_path):
    """The wrapper-level ``--timeout-seconds`` budget behaves identically."""

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a", command=f"{sys.executable} -c 'import time; time.sleep(30)'"
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        timeout_seconds=1.0,
        wrapper=wrapper,
    )

    assert record["row_runs"][0]["timed_out"] is True
    assert record["rows"][0]["state"] == admission.S6_FAILED_IN_WINDOW
    assert record["matrix_success"] is False


def test_a_row_within_its_budget_is_not_marked_as_timed_out(tmp_path):
    """The positive control: the sentinel must not label every row an overrun."""

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        timeout_seconds=60.0,
        wrapper=wrapper,
    )

    assert record["row_runs"][0]["timed_out"] is False
    assert record["matrix_success"] is True


def test_a_command_that_exits_with_the_sentinel_code_is_not_reported_as_an_overrun(tmp_path):
    """The ``timed_out`` flag is authoritative, not the exit status."""

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a",
                command=f"{sys.executable} -c 'raise SystemExit({runner.ROW_TIMEOUT_RETURN_CODE})'",
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["row_runs"][0]["returncode"] == runner.ROW_TIMEOUT_RETURN_CODE
    assert record["row_runs"][0]["timed_out"] is False, (
        "a command that exited with the sentinel status did not overrun its budget"
    )
    assert record["row_runs"][0]["failed"] is True


# --- F4: the command line is argv, not shell -------------------------------


def test_a_row_command_is_split_into_argv_and_never_run_through_a_shell(tmp_path):
    """F4: the documented command form must match what is executed.

    Shell metacharacters are literal arguments, so a row cannot acquire pipes,
    redirection, or variable expansion that its record does not show.  The row
    below would print ``expanded`` if a shell were involved; as argv it prints
    the literal text instead.
    """

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)
    literal = "${HOME:-expanded}"
    # The program prints its first argv element, and the literal metacharacters
    # are passed as a *separate* argv element so no shell-like quoting is
    # needed.  A shell would expand this to the reader's HOME; argv must not.
    script = "import sys; print(sys.argv[1])"

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a",
                command=f'{sys.executable} -c "{script}" {literal}',
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    run = record["row_runs"][0]
    assert run["returncode"] == 0
    assert run["stdout"].strip() == literal, (
        "a shell would have expanded this argument; argv execution must not"
    )


def test_the_command_help_text_does_not_promise_a_shell(capsys):
    """F4: the documented form and the executed form must agree."""

    with pytest.raises(SystemExit):
        admission._main(["--help"])
    text = capsys.readouterr().out

    assert "argv-style" in text
    assert "<shell command>" not in text, "the help must not claim a shell"
