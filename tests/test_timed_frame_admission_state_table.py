"""The S0-S6 state table, admission loss, expiry, and report shape. (#84; split from tests/test_timed_frame_admission.py for #122.)

Pure relocation: every test and helper definition is byte-identical at the AST
level; only the module file changed.
"""

from __future__ import annotations

import json

from scripts import timed_frame_admission as admission
from tests._timed_frame_admission_support import (
    ALLOWED_CPUS,
    FakeClock,
    ScriptedObserver,
    SeriesReader,
    make_allocation,
    make_wrapper,
    quiet_series,
)

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
