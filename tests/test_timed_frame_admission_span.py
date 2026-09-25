"""The allocation span is part of the record: held, future, and expiring spans. (#84; split from tests/test_timed_frame_admission.py for #122.)

Pure relocation: every test and helper definition is byte-identical at the AST
level; only the module file changed.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

from scripts import timed_frame_admission as admission
from scripts import timed_frame_window as window
from tests._timed_frame_admission_support import (
    ALLOWED_CPUS,
    FakeClock,
    SeriesReader,
    iso,
    make_allocation,
    make_wrapper,
    passing_wrapper,
    quiet_series,
    write_allocation,
)

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
        (
            "not yet started",
            {"started_utc": "2999-01-01T00:00:00Z", "expires_utc": "2999-12-31T00:00:00Z"},
        ),
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
        (
            "not yet started",
            {"started_utc": "2999-01-01T00:00:00Z", "expires_utc": "2999-12-31T00:00:00Z"},
        ),
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
    assert record["allocation"]["span_not_started"] is False
    assert record["rows"][0]["state"] == admission.S1_ADMITTED
    assert record["matrix_success"] is True


def test_a_span_that_has_not_started_yet_is_not_held_and_blocks_every_row(tmp_path):
    """The mirror of the expired span: the present can lie *before* the span.

    A reservation whose span begins in the future is not an allocation held at
    read time, so a row dispatched under it would be reported as a valid
    controlled observation while no capacity was actually reserved for it.
    """

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = make_wrapper(
        clock,
        reader,
        ["row-a", "row-b"],
        allocation=make_allocation(
            started_utc="2999-01-01T00:00:00Z", expires_utc="2999-12-31T00:00:00Z"
        ),
    )

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass"),
            runner.RowCommand(row="row-b", command=f"{sys.executable} -c pass"),
        ],
        allocation_path=write_allocation(
            tmp_path, started_utc="2999-01-01T00:00:00Z", expires_utc="2999-12-31T00:00:00Z"
        ),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["row_runs"] == [], "no row may run before the span has started"
    assert record["rows"][0]["state"] == admission.S5_ADMISSION_UNAVAILABLE
    assert record["allocation"]["held"] is False
    assert record["allocation"]["span_expired"] is False
    assert record["allocation"]["span_not_started"] is True
    assert any("future" in item for item in record["allocation"]["problems"]), (
        "the reason must name the lower end, not merely report 'not held'"
    )
    assert record["matrix_success"] is False


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
