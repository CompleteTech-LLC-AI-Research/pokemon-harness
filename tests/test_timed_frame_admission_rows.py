"""Driving rows under a recorded allocation, and the nine-row orientation. (#84; split from tests/test_timed_frame_admission.py for #122.)

Pure relocation: every test and helper definition is byte-identical at the AST
level; only the module file changed.
"""

from __future__ import annotations

import json
import sys

from scripts import timed_frame_admission as admission
from tests._timed_frame_admission_support import (
    ALLOWED_CPUS,
    FakeClock,
    SeriesReader,
    make_allocation,
    make_wrapper,
    passing_wrapper,
    quiet_series,
    write_allocation,
)

# --- driving rows under the recorded allocation ---------------------------


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
