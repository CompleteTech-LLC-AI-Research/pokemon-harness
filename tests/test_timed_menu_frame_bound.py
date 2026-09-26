"""The owner's frame bound must not be a wall-clock throughput assertion (#252).

``scripts/probe_timed_rom_pair.py::_run_owner`` stops on whichever comes first:
the ``--frame-limit`` frame bound or the supplied monotonic ``deadline``. The
fake session used by the milestone tests performs a fixed amount of real Python
work per call, so against the real clock the deadline becomes a statement about
how fast the host is rather than about how many calls the frame bound
authorizes. That turned ``assert len(record["calls"]) == 300`` -- a retention
contract -- into a CPU measurement that passes or fails with machine load.

These tests drive the owner's clock binding directly, so the frame bound is
exercised deterministically on any host. The deadline branch is asserted
separately, with a clock slow enough to reach it, so the seam cannot be used to
quietly delete that contract.
"""

import inspect

import pytest

from scripts import probe_timed_rom_pair as probe
from tests._probe_timed_rom_pair_support import _run_menu_owner
from tests.test_timed_menu_milestones import FRAME_BOUND, run_owner

INLINE_STEPS = [0.0, 0.001, 0.002, 0.005]
STREAM_STEPS = [0.0, 0.001, 0.005]
SLOW_STEP = 1.0


@pytest.mark.parametrize("clock_step", INLINE_STEPS)
def test_frame_bound_evidence_is_independent_of_wall_clock_speed(monkeypatch, tmp_path, clock_step):
    """A full-speed host must retire the whole inline frame bound.

    ``clock_step=0.0`` models a host that consumes no wall time per call. The
    owner's termination is decided by ``args.frame_limit``, so the 300-call
    contract asserted by the retention test must hold as a property of the
    frame bound and not of machine load.
    """
    record, _path, _, _, clock = run_owner(
        monkeypatch, tmp_path, clock_step=clock_step, stream=False, milestones=False
    )
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    # Inline retention keeps every call, so the retained list is exact.
    assert len(record["calls"]) == FRAME_BOUND
    assert [call["call_index"] for call in record["calls"]] == list(range(FRAME_BOUND))
    assert [call["requested_frames"] for call in record["calls"]] == [1] * FRAME_BOUND
    assert clock.reads > FRAME_BOUND


@pytest.mark.parametrize("clock_step", STREAM_STEPS)
def test_streamed_frame_bound_totals_are_independent_of_wall_clock_speed(
    monkeypatch, tmp_path, clock_step
):
    """The same frame-bound contract, measured where stream retention records it.

    Stream mode keeps only a bounded window in ``record["calls"]``, so the
    authoritative totals are ``call_counts`` and the on-disk artifact.
    """
    record, path, _, _, _clock = run_owner(monkeypatch, tmp_path, clock_step=clock_step)
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    assert record["call_counts"]["total"] == FRAME_BOUND
    assert record["call_counts"]["completed"] == FRAME_BOUND
    assert record["call_counts"]["actual_completed_frames"] == FRAME_BOUND
    assert record["call_log"]["record_count"] == FRAME_BOUND
    assert len(path.read_bytes().splitlines()) == FRAME_BOUND


def test_deadline_still_terminates_a_slow_host_before_the_frame_bound(monkeypatch, tmp_path):
    """The deadline branch stays a real contract under a slow clock.

    A step of ``1.0`` against the 20-unit deadline cannot retire 300 frames, so
    the owner must stop on the deadline. This is the coupling #252 exposed; it
    is asserted deliberately so the clock seam cannot silently disable it.
    """
    record, _, _, _, _clock = run_owner(
        monkeypatch, tmp_path, clock_step=SLOW_STEP, stream=False, milestones=False
    )
    assert record["errors"] == []
    assert record["termination"] == "cancelled_or_deadline"
    assert 0 < len(record["calls"]) < FRAME_BOUND


def test_injected_clock_is_optional_and_defaults_to_real_monotonic():
    """Omitting ``clock`` keeps the production monotonic source unchanged."""
    signature = inspect.signature(probe._run_owner)
    assert signature.parameters["clock"].default is None
    # The seam is last in the signature, so no existing caller's meaning shifts.
    assert list(signature.parameters)[-1] == "clock"
    assert probe.time.monotonic is not None


def test_real_clock_still_guards_the_owner_loop(monkeypatch, tmp_path):
    """With no clock injected the real monotonic clock is the only deadline guard.

    This does not assert a call count -- that would reinstate the host-speed
    coupling -- but it does assert that the loop still terminates and never
    reports a call count beyond the frame bound.
    """
    record, _, _, _, _clock = run_owner(
        monkeypatch, tmp_path, clock_step=None, stream=False, milestones=False
    )
    assert record["termination"] in {"frame_bound", "cancelled_or_deadline"}
    assert len(record["calls"]) <= FRAME_BOUND


MENU_FRAME_BOUND = 77


@pytest.mark.parametrize("clock_step", INLINE_STEPS)
def test_menu_profile_schedule_is_independent_of_wall_clock_speed(clock_step):
    """The menu profile's 77-input schedule is a frame bound, not a CPU claim (#260).

    ``_run_menu_owner`` used to supply a hardcoded ``time.monotonic() + 3``, so
    asserting ``termination == "frame_bound"`` asserted "77 menu inputs within
    three seconds of wall clock" and failed on a loaded host with
    ``cancelled_or_deadline``. The step may not change the schedule.
    """
    _session, record = _run_menu_owner(clock_step=clock_step)
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    assert [call["frame_offset"] for call in record["calls"]] == list(range(MENU_FRAME_BOUND))
    assert all(
        call["actual_completed_frames"] == call["requested_frames"] == 1 for call in record["calls"]
    )


def test_menu_profile_deadline_still_terminates_a_slow_host():
    """A clock slow enough to exhaust the budget still stops on the deadline.

    Same reasoning as the milestone deadline test: the seam must not be usable to
    delete the real deadline contract.
    """
    # A step of 0.1 against the 3-unit budget retires a few frames before the
    # deadline, so the run is genuinely mid-schedule rather than empty.
    _session, record = _run_menu_owner(clock_step=0.1)
    assert record["errors"] == []
    assert record["termination"] == "cancelled_or_deadline"
    assert 0 < len(record["calls"]) < MENU_FRAME_BOUND


def test_menu_profile_real_clock_path_still_terminates():
    """``clock_step=None`` keeps the no-injection path reachable.

    This asserts termination only, never a count, so the real-clock path cannot
    reintroduce the host-speed coupling the seam removes.
    """
    _session, record = _run_menu_owner(clock_step=None)
    assert record["termination"] in {"frame_bound", "cancelled_or_deadline"}
    assert len(record["calls"]) <= MENU_FRAME_BOUND
