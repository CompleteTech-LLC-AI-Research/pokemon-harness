"""Behavioural §5.1 invariants measured by the #243 mutation table. (#84; split from tests/test_timed_frame_admission.py for #122.)

Pure relocation: every test and helper definition is byte-identical at the AST
level; only the module file changed.
"""

from __future__ import annotations

import pytest

from scripts import timed_frame_admission as admission
from tests._timed_frame_admission_support import (
    ALLOWED_CPUS,
    FakeClock,
    ScriptedObserver,
    SeriesReader,
    make_wrapper,
    quiet_series,
)

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
    # The diagnostic restart loop is a *second* comparison site, and it must
    # read the same equality: a series whose every gap is exactly the permitted
    # maximum reports no restart at all.  Asserting only the run length above
    # leaves this site unpinned, so flipping it alone would fill a retained
    # record with false "continuity restarted" lines while staying green here.
    assert window.restarts == (), (
        "a gap of exactly the permitted maximum must not be reported as a restart"
    )

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
