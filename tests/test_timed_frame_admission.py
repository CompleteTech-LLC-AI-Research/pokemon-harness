"""Contracts for the §5.1 per-row capacity/admission wrapper (#84).

Asset-free: no ROM, emulator, or MCP process is started.  Every host sample is
injected, so the state table is exercised deterministically instead of at the
mercy of the host's load window.  The wrapper decides admission only; a row's
pass or failure is supplied by the caller's own result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import timed_frame_admission as admission

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


def make_allocation(**overrides) -> admission.AllocationRecord:
    values = {
        "allocation_id": "resv-84-1",
        "kind": "reservation",
        "extent": {"cpus": 4, "cpuset": "0-3", "memory_bytes": 8 * 1024**3},
        "holder": "operator-runner-85",
        "source": "qualification-runner://reservation/84",
        "started_utc": "2026-09-25T00:00:00Z",
        "expires_utc": "2026-09-25T06:00:00Z",
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
