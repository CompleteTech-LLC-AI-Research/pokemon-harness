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
from tests._timed_menu_frame_bound_support import (
    FRAME_BOUND,
    INLINE_STEPS,
    SLOW_STEP,
    STREAM_STEPS,
    FakeClock,
    assert_not_deadline_truncated,
)
from tests.test_timed_menu_milestones import run_owner


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
    assert_not_deadline_truncated(record)
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
        monkeypatch,
        tmp_path,
        clock_step=SLOW_STEP,
        stream=False,
        milestones=False,
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


def test_the_owner_loop_actually_reads_the_injected_clock(monkeypatch, tmp_path):
    """The seam is read, not merely accepted.

    A signature test cannot tell an honoured ``clock`` parameter from an
    ignored one, so this asserts the observable consequence: when a clock is
    injected, the owner loop reads *it* rather than the real monotonic source.
    ``FakeClock.reads`` counts every call, so this fails if the loop reverts to
    ``time.monotonic`` while still accepting the parameter.
    """
    _record, _path, _lifecycle, _checkpoints, clock = run_owner(
        monkeypatch,
        tmp_path,
        stream=False,
        milestones=False,
    )
    assert isinstance(clock, FakeClock)
    # The loop reads the clock many times per retired call, so a bound far
    # above one read per call is stable; a loop that ignored the seam would
    # leave this at zero.
    assert clock.reads > FRAME_BOUND


def test_real_clock_run_terminates_and_never_exceeds_the_frame_bound(monkeypatch, tmp_path):
    """With no clock injected the run terminates and respects the frame bound.

    The name deliberately does not claim the real clock *guards* the loop. With
    a real clock the outcome is host-speed dependent -- either bound may win --
    so asserting which one stopped the loop would reinstate exactly the
    wall-clock coupling #252 removed. What is asserted here is only what holds
    on every host: the run terminates, it never reports more calls than the
    frame bound authorises, and it never errors.

    The real-clock guard itself is pinned by
    ``test_deadline_still_terminates_a_slow_host_before_the_frame_bound``, which
    reaches the deadline branch deterministically via ``SLOW_STEP``.
    """
    record, _, _, _, _clock = run_owner(
        monkeypatch,
        tmp_path,
        clock_step=None,
        stream=False,
        milestones=False,
    )
    assert record["errors"] == []
    assert record["termination"] in {"frame_bound", "cancelled_or_deadline"}
    assert len(record["calls"]) <= FRAME_BOUND
