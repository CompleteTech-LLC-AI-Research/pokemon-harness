"""Authored owner-driver lifecycle, fault, and spawn contracts (#160).

Split from ``tests/test_probe_timed_trade_pair.py`` for #160 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Authored/fake trade-entry checks only; never commercial-ROM
qualification.
"""

import json

import pytest

from scripts import probe_timed_rom_pair as ordinary
from scripts import probe_timed_trade_pair as trade
from tests._probe_timed_trade_pair_drivers import (
    authored_spawn_owner,
    run_authored_goal_owner,
)
from tests._timed_menu_frame_bound_support import assert_not_deadline_truncated


def test_trade_pair_deadline_is_read_through_the_injected_clock():
    """The trade-pair helper must reach its frame bound, not a wall clock (#274).

    The rows above all run at `--frame-limit=4`, where the owner retires in
    about a millisecond. A 3 s wall-clock deadline cannot fire in that time, so
    on their own they pass whether or not the helper reads the injected clock --
    which is how the #268 seam could be reverted with every row still green.

    These two rows close that gap from both sides, and the pair is what pins
    the seam rather than either row alone:

    * A fast clock must reach the frame bound and terminate as ``frame_bound``.
      The owner has spent none of its 3 s budget, so the deadline branch is not
      what stopped it.
    * A slow clock must instead be cut off by the deadline. Only a clock whose
      reads the helper controls can consume 3 s of budget across four calls, so
      this row can only truncate if the seam is wired through.

    Reverting the helper to ``time.monotonic()`` makes the second row retire at
    the frame bound like the first, and it fails.
    """
    _session, fast_record, _goal, _done_at = run_authored_goal_owner(clock_step=0.0)
    assert fast_record["errors"] == []
    assert_not_deadline_truncated(fast_record)
    assert fast_record["termination"] == "frame_bound"
    assert len(fast_record["calls"]) == 4

    _session, slow_record, _goal, _done_at = run_authored_goal_owner(clock_step=0.5)
    assert slow_record["errors"] == []
    # 0.5 s per read exhausts the 3 s deadline before four calls complete, so
    # the deadline genuinely wins and the run is truncated. That is the deadline
    # contract, not a retention result, which is why it is asserted separately.
    assert slow_record["termination"] == "cancelled_or_deadline"
    assert len(slow_record["calls"]) < 4


def test_first_local_goal_keeps_whole_cpu_steps_until_bound_without_early_done():
    session, record, goal, done_at = run_authored_goal_owner()
    assert record["errors"] == []
    assert session.steps == [1, 1, 1, 1]
    assert session.authored_driver.offsets == [0, 1, 2, 3]
    assert session.authored_driver.observations == [(1, 1)] * 4
    assert len(session.inputs) == 1
    assert goal.is_set()
    assert done_at and all(count == 4 for count in done_at)
    assert record["termination"] == "frame_bound"
    assert record["driver_snapshot"]["objective_complete"] is True


@pytest.mark.parametrize("method", ["after_step", "snapshot", "objective_complete"])
def test_driver_poststep_failure_preserves_executed_call_artifact(tmp_path, method):
    session, record, _, _ = run_authored_goal_owner(
        driver_fault=method,
        evidence_path=tmp_path / "executed.jsonl",
    )
    assert session.steps == [1]
    assert any(f"authored {method} failure" in error for error in record["errors"])
    assert record["termination"] == "owner_failure"
    assert record["call_counts"]["total"] == 1
    assert record["call_counts"]["actual_completed_frames"] == 1
    calls = [json.loads(line) for line in (tmp_path / "executed.jsonl").read_text().splitlines()]
    assert len(calls) == 1
    assert calls[0]["requested_frames"] == calls[0]["actual_completed_frames"] == 1
    assert record["final"]["frame_count"] == 11
    assert "session_closed_without_save" in record["cleanup"]
    ordinary.validate_call_artifact(record)


def test_driver_failure_and_call_finalization_failure_both_remain_visible(monkeypatch, tmp_path):
    def fail_append(self, call):
        raise OSError("authored finalization failure")

    monkeypatch.setattr(ordinary.CallEvidenceLog, "append", fail_append)
    session, record, _, _ = run_authored_goal_owner(
        driver_fault="after_step",
        evidence_path=tmp_path / "executed.jsonl",
    )
    assert session.steps == [1]
    errors = " ".join(record["errors"])
    assert "authored after_step failure" in errors
    assert "authored finalization failure" in errors
    assert record["call_counts"]["total"] == 1
    assert record["call_counts"]["actual_completed_frames"] == 1
    assert record["unspooled_call"]["actual_completed_frames"] == 1
    assert record["call_log"]["complete"] is False


@pytest.mark.parametrize("close_failure", [False, True])
def test_driver_hooks_close_after_endpoint_detach_before_session_close(close_failure):
    session, record, _, _ = run_authored_goal_owner(
        close_driver=True,
        close_failure=close_failure,
        milestones=True,
    )
    assert (
        session.calls.index("unbind")
        < session.calls.index("driver_close")
        < session.calls.index("close")
    )
    assert session.calls.count("driver_close") == 1
    assert record["milestone_observer"] == "owner_driver"
    assert "milestone_hooks_removed" not in record["cleanup"]
    assert "session_closed_without_save" in record["cleanup"]
    if close_failure:
        assert any("authored driver close failure" in error for error in record["errors"])
        assert "owner_driver_closed" not in record["cleanup"]
    else:
        assert record["errors"] == []
        assert (
            record["cleanup"].index("endpoint_detached")
            < record["cleanup"].index("owner_driver_closed")
            < record["cleanup"].index("session_closed_without_save")
        )


@pytest.mark.parametrize("exception_name", ["Cancelled", "DeadlineExceeded", "ChannelClosed"])
@pytest.mark.parametrize("goal_stop_requested", [False, True])
def test_owner_tolerates_only_exact_cancelled_after_joint_goal(
    tmp_path,
    exception_name,
    goal_stop_requested,
):
    from pokered_harness.link import timed_wire

    session, record, goal, done_at = run_authored_goal_owner(
        exception=getattr(timed_wire, exception_name),
        goal_stop_requested=goal_stop_requested,
        evidence_path=tmp_path / "owner-calls.jsonl",
    )
    expected = exception_name == "Cancelled" and goal_stop_requested
    assert session.steps == [1, 1]
    assert goal.is_set()
    assert record["call_counts"]["noncompleted"] == 1
    assert record["call_counts"]["actual_completed_frames"] == 1
    terminal = [
        json.loads(line) for line in (tmp_path / "owner-calls.jsonl").read_text().splitlines()
    ][-1]
    assert terminal["status"] == "interrupted"
    assert terminal["actual_completed_frames"] == 0
    assert terminal["error"].startswith(exception_name + ":")
    assert bool(terminal.get("expected_goal_cancellation")) is expected
    assert bool(record.get("expected_goal_cancellation")) is expected
    if expected:
        assert record["errors"] == []
        assert record["termination"] == "goal_cancelled"
        assert done_at == []
        trade._validate_calls(record)
    else:
        assert record["errors"]
        assert record["termination"] == "owner_failure"
        assert done_at == [2]


@pytest.mark.parametrize("with_driver", [False, True])
def test_spawn_supervisor_joint_goal_and_baseline_first_done_contract(tmp_path, with_driver):
    from tests._probe_timed_rom_pair_support import _NoSharedEventContext, arguments

    args = arguments(
        "--owner-mode=process",
        "--listener-chunk=1",
        "--connector-chunk=1",
        "--overall-timeout=12",
        "--pair-timeout=8",
        "--cleanup-timeout=3",
    )
    args.owner_driver_options = [
        {"version": "blue_color", "outgoing_slot": 0, "checkpoint": "select-mon"},
        {"version": "yellow", "outgoing_slot": 5, "checkpoint": "select-mon"},
    ]
    context = _NoSharedEventContext()
    try:
        result = ordinary.run_process_pair(
            args,
            context=context,
            child_target=authored_spawn_owner,
            owner_driver="tests._probe_timed_trade_pair_drivers:create_authored_goal_driver"
            if with_driver
            else None,
        )
    finally:
        context.close()
    assert result["supervisor_cancel_errors"] == []
    assert result["processes_alive"] == result["report_readers_alive"] == []
    assert all(owner["errors"] == [] for owner in result["owners"])
    assert all(
        owner["exitcode"] == 0 and not owner["forced_termination"] for owner in result["owners"]
    )
    if with_driver:
        assert result["stop_reason"] == "both_owner_goals"
        assert result["owner_goals"] == [True, True]
        assert result["goal_stop"] is True
        assert result["owners"][0]["continuations"] > 0
    else:
        assert result["stop_reason"] == "owner_completion_or_failure"
        assert "owner_driver" not in result
