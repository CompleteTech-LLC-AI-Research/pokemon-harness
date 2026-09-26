"""The owner's frame bound must not be a wall-clock throughput assertion (#252).

``scripts/probe_timed_rom_pair.py::_run_owner`` stops on whichever comes first:
the ``--frame-limit`` frame bound or the supplied monotonic ``deadline``. The
fake session used by the milestone tests does a fixed amount of real Python work
per call, so on a real clock the deadline is a statement about how fast the host
is rather than about how many calls the frame bound authorizes. That turned
``assert len(record["calls"]) == 300`` -- a retention contract -- into a CPU
measurement that passes or fails with machine load.

These tests drive the owner's clock binding directly, so the frame bound is
exercised deterministically on any host, and the deadline branch is asserted
separately with a clock slow enough to reach it.
"""

from types import SimpleNamespace

import pytest

from tests.test_timed_menu_milestones import run_owner

FRAME_BOUND = 300


class FakeClock:
    """Monotonic stand-in that advances a fixed amount per read.

    ``step`` is the wall time the owner "consumes" per observation: ``0``
    models an arbitrarily fast host, a large step an arbitrarily slow one.
    Neither may change how many calls the frame bound authorizes.
    """

    def __init__(self, start=1000.0, step=0.0):
        self.value = start
        self.step = step
        self.reads = 0

    def __call__(self):
        self.reads += 1
        current = self.value
        self.value += self.step
        return current


@pytest.mark.parametrize("step", [0.0, 0.001, 0.002, 0.005])
def test_frame_bound_retires_every_call_at_any_host_speed(monkeypatch, tmp_path, step):
    """A full frame bound is reached regardless of how fast the host is."""
    clock = FakeClock(step=step)
    record, _, _, _ = run_owner(monkeypatch, tmp_path, stream=False, milestones=False, clock=clock)
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    assert len(record["calls"]) == FRAME_BOUND
    assert [call["call_index"] for call in record["calls"]] == list(range(FRAME_BOUND))
    assert [call["requested_frames"] for call in record["calls"]] == [1] * FRAME_BOUND
    assert all(call["status"] == "completed" for call in record["calls"])
    assert "in_flight" not in record


@pytest.mark.parametrize("step", [0.0, 0.001, 0.005])
def test_streamed_counters_for_a_full_frame_bound_are_clock_independent(
    monkeypatch, tmp_path, step
):
    """Stream retention keeps a bounded window, so assert where the total is kept."""
    clock = FakeClock(step=step)
    record, path, _, _ = run_owner(monkeypatch, tmp_path, clock=clock)
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    assert record["call_counts"]["total"] == FRAME_BOUND
    assert record["call_counts"]["completed"] == FRAME_BOUND
    assert record["call_counts"]["actual_completed_frames"] == FRAME_BOUND
    assert record["call_counts"]["noncompleted"] == 0
    assert record["call_log"]["record_count"] == FRAME_BOUND
    assert len(path.read_bytes().splitlines()) == FRAME_BOUND


def test_deadline_still_terminates_a_slow_host_before_the_frame_bound(monkeypatch, tmp_path):
    """The deadline remains a real contract, asserted deliberately.

    One wall-time unit per read against a 20-unit deadline cannot retire 300
    frames, so the owner must stop on the deadline. This keeps the deadline
    branch covered instead of being erased along with the load coupling.
    """
    clock = FakeClock(step=1.0)
    record, _, _, _ = run_owner(monkeypatch, tmp_path, stream=False, milestones=False, clock=clock)
    assert record["errors"] == []
    assert record["termination"] == "cancelled_or_deadline"
    assert 0 < len(record["calls"]) < FRAME_BOUND


def test_omitting_the_clock_leaves_the_production_binding_in_place(monkeypatch, tmp_path):
    """No clock means the real monotonic source is used; the seam is inert."""
    from scripts import probe_timed_rom_pair as probe

    real = probe.time.monotonic
    run_owner(monkeypatch, tmp_path, stream=False, milestones=False)
    assert probe.time.monotonic is real


def test_the_frame_bound_is_not_reachable_by_a_clock_that_never_advances(monkeypatch, tmp_path):
    """A frozen clock must not manufacture calls the frame bound forbids.

    With a clock that always returns the same instant the owner is still bounded
    by ``--frame-limit``, so the count is exact rather than "at least".
    """
    record, _, _, _ = run_owner(
        monkeypatch,
        tmp_path,
        stream=False,
        milestones=False,
        clock=lambda: 1000.0,
    )
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    assert len(record["calls"]) == FRAME_BOUND


def test_owner_module_keeps_its_real_monotonic_source():
    """The repair is test-side only: production takes no clock parameter."""
    import inspect

    from scripts import probe_timed_rom_pair as probe

    assert "clock" not in inspect.signature(probe._run_owner).parameters
    assert probe.time.monotonic is not None
    assert isinstance(probe.time, SimpleNamespace) is False
