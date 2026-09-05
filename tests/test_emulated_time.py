"""ROM-free coordinator contracts; no runtime activation or hardware claims."""

import threading
import time
from dataclasses import FrozenInstanceError, replace

import pytest

from pokered_harness.link import emulated_time as module
from pokered_harness.link.emulated_time import (
    CoordinatorClosed,
    EmulatedTimeCoordinator,
    EmulatedTimeError,
)


def coordinator(**overrides):
    options = {
        "epoch": "epoch-1",
        "raw_cpu_clock": 1000,
        "rearm_budget": 32,
        "max_edge_lateness": 128,
    }
    options.update(overrides)
    c = EmulatedTimeCoordinator(**options)
    c.record_peer_progress(epoch=options["epoch"], sequence=1, committed_half_cycles=0)
    return c


def advance(c, raw, cycles, instructions=1):
    permit = c.reserve(cycles)
    assert permit is not None
    assert permit.cpu_cycles == cycles
    return c.commit(permit, raw_cpu_clock=raw + cycles, instructions=instructions)


def exhausted():
    c = coordinator()
    advance(c, 1000, 128)
    assert c.reserve(1) is None
    return c


def assert_terminal(c):
    assert c.snapshot().closed
    with pytest.raises(CoordinatorClosed):
        c.reserve(1)


def test_rearm_budget_is_required_and_initial_clock_is_only_an_anchor():
    with pytest.raises(TypeError):
        EmulatedTimeCoordinator(epoch="epoch-1", raw_cpu_clock=1000, max_edge_lateness=128)
    c = coordinator()
    s = c.snapshot()
    assert (s.local_half_cycles, s.peer_half_cycles, s.debt_half_cycles) == (0, 0, 0)
    assert not s.pending_permit and not s.closed and not s.cancelled
    permit = c.reserve(1000)
    assert (permit.cpu_cycles, permit.half_cycles) == (128, 256)
    assert c.snapshot().local_half_cycles == 0
    assert c.snapshot().pending_permit


def test_cpu_credit_requires_explicit_zero_peer_anchor():
    c = EmulatedTimeCoordinator(
        epoch="epoch-1", raw_cpu_clock=1000, rearm_budget=32, max_edge_lateness=128
    )
    assert c.reserve(1) is None
    assert c.record_peer_progress(epoch="epoch-1", sequence=1, committed_half_cycles=0)
    assert c.reserve(1) is not None


def test_nonzero_first_peer_anchor_fails_closed():
    c = EmulatedTimeCoordinator(
        epoch="epoch-1", raw_cpu_clock=1000, rearm_budget=32, max_edge_lateness=128
    )
    with pytest.raises(EmulatedTimeError):
        c.record_peer_progress(epoch="epoch-1", sequence=1, committed_half_cycles=1)
    assert_terminal(c)


def test_many_small_permits_share_one_fixed_rearm_episode_with_positive_debt():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    for index in range(32):
        before = c.snapshot()
        c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
        assert c.snapshot() == before
        s = advance(c, 1128 + index, 1)
        assert s.local_half_cycles == 256 + 2 * (index + 1)
        assert s.debt_half_cycles == 2 * (index + 1)
        assert s.remaining_episode_half_cycles == 64 - 2 * (index + 1)
        assert s.remaining_instructions == 31 - index
        assert s.local_half_cycles - s.peer_half_cycles <= 256 + 64
    assert c.reserve(1) is None
    assert c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=32)
    assert c.snapshot().debt_half_cycles == 32
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="epoch-1", sequence=3, committed_half_cycles=64)
    assert c.snapshot().debt_half_cycles == 0
    assert c.reserve(1) is None
    c.begin_episode("request-2", cycle_budget=32, instruction_cap=32)
    assert c.reserve(1) is not None


@pytest.mark.parametrize("use_cycles", [0, 1, 32])
def test_new_request_cannot_replace_active_episode_or_replenish_debt(use_cycles):
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=64)
    if use_cycles:
        advance(c, 1128, use_cycles)
    with pytest.raises(EmulatedTimeError):
        c.begin_episode("request-2", cycle_budget=32, instruction_cap=64)
    assert_terminal(c)


@pytest.mark.parametrize("changes", [{"cycle_budget": 31}, {"instruction_cap": 31}])
def test_same_request_changed_limits_fail_closed(changes):
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    options = {"cycle_budget": 32, "instruction_cap": 32}
    options.update(changes)
    with pytest.raises(EmulatedTimeError):
        c.begin_episode("request-1", **options)
    assert_terminal(c)


def test_partial_commit_charges_actual_cycles_and_instructions():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=3)
    permit = c.reserve(32)
    assert permit.instruction_cap == 3
    s = c.commit(permit, raw_cpu_clock=1130, instructions=1)
    assert (s.local_half_cycles, s.debt_half_cycles) == (260, 4)
    assert (s.remaining_episode_half_cycles, s.remaining_instructions) == (60, 2)
    advance(c, 1130, 1, instructions=2)
    assert c.snapshot().remaining_instructions == 0
    assert c.reserve(1) is None


@pytest.mark.parametrize("double_speed,factor", [(False, 2), (True, 1)])
def test_exact_normal_and_double_speed_units(double_speed, factor):
    c = coordinator(double_speed=double_speed)
    for index in range(7):
        s = advance(c, 1000 + index, 1)
        assert s.local_half_cycles == (index + 1) * factor
    permit = c.reserve(1000)
    assert permit.half_cycles == 256 - 7 * factor
    assert permit.cpu_cycles == 256 // factor - 7


def test_odd_half_cycle_carry_survives_multiple_speed_changes():
    c = coordinator(double_speed=True)
    advance(c, 1000, 1)
    c.set_speed(raw_cpu_clock=1001, double_speed=False)
    assert advance(c, 1001, 1).local_half_cycles == 3
    c.set_speed(raw_cpu_clock=1002, double_speed=True)
    assert advance(c, 1002, 1).local_half_cycles == 4
    assert advance(c, 1003, 251).local_half_cycles == 255
    c.set_speed(raw_cpu_clock=1254, double_speed=False)
    assert c.reserve(1) is None
    c.set_speed(raw_cpu_clock=1254, double_speed=True)
    assert advance(c, 1254, 1).local_half_cycles == 256
    assert c.reserve(1) is None


@pytest.mark.parametrize("pending,clock", [(False, 1001), (False, 999), (True, 1000)])
def test_speed_change_requires_committed_clock_and_no_pending_permit(pending, clock):
    c = coordinator()
    if pending:
        c.reserve(1)
    with pytest.raises(EmulatedTimeError):
        c.set_speed(raw_cpu_clock=clock, double_speed=True)
    assert_terminal(c)


@pytest.mark.parametrize("field", ["raw_cpu_clock", "rearm_budget", "max_edge_lateness"])
@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, float("inf"), float("nan"), "1"])
def test_constructor_rejects_invalid_integer_units(field, invalid):
    with pytest.raises(EmulatedTimeError):
        coordinator(**{field: invalid})


@pytest.mark.parametrize("invalid", [0, 1, "false", None])
def test_speed_requires_a_real_boolean(invalid):
    with pytest.raises(EmulatedTimeError):
        coordinator(double_speed=invalid)


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, float("inf"), float("nan"), "1"])
@pytest.mark.parametrize("operation", ["reserve", "progress", "edge", "watermark", "commit"])
def test_invalid_numeric_mutations_fail_closed(operation, invalid):
    c = coordinator()
    with pytest.raises(EmulatedTimeError):
        if operation == "reserve":
            c.reserve(invalid)
        elif operation == "progress":
            c.record_peer_progress(epoch="epoch-1", sequence=1, committed_half_cycles=invalid)
        elif operation == "edge":
            c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=invalid, payload=b"x")
        elif operation == "watermark":
            c.advance_watermark(epoch="epoch-1", sequence=0, through_half_cycle=invalid)
        else:
            permit = c.reserve(1)
            c.commit(permit, raw_cpu_clock=1001, instructions=invalid)
    assert_terminal(c)


@pytest.mark.parametrize("operation", ["progress", "edge", "watermark", "reset"])
def test_epoch_mismatch_or_load_reset_is_terminal(operation):
    c = coordinator()
    with pytest.raises(EmulatedTimeError):
        if operation == "progress":
            c.record_peer_progress(epoch="epoch-2", sequence=1, committed_half_cycles=0)
        elif operation == "edge":
            c.receive_edge(epoch="epoch-2", sequence=1, at_half_cycle=0, payload=b"x")
        elif operation == "watermark":
            c.advance_watermark(epoch="epoch-2", sequence=0, through_half_cycle=0)
        else:
            c.reset(epoch="epoch-2", raw_cpu_clock=0)
    assert_terminal(c)


@pytest.mark.parametrize(
    "raw,instructions", [(999, 1), (1000, 0), (1000, 1), (1002, 1), (1001, -1)]
)
def test_commit_rejects_clock_reset_overrun_and_invalid_instruction_count(raw, instructions):
    c = coordinator()
    permit = c.reserve(1)
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=raw, instructions=instructions)
    assert_terminal(c)


def test_permit_cannot_be_forged_or_reused():
    c = coordinator()
    permit = c.reserve(1)
    with pytest.raises(FrozenInstanceError):
        permit.cpu_cycles = 2
    with pytest.raises(EmulatedTimeError):
        c.commit(replace(permit, cpu_cycles=2), raw_cpu_clock=1001, instructions=1)
    assert_terminal(c)
    c = coordinator()
    permit = c.reserve(1)
    c.commit(permit, raw_cpu_clock=1001, instructions=1)
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=1001, instructions=1)
    assert_terminal(c)


def test_progress_identical_duplicate_does_not_repay_twice():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    advance(c, 1128, 32)
    for sequence, peer in enumerate([1, 3, 32, 64, 321], 2):
        assert c.record_peer_progress(
            epoch="epoch-1", sequence=sequence, committed_half_cycles=peer
        )
        s = c.snapshot()
        assert s.debt_half_cycles == max(0, 320 - peer - 256)
        assert not c.record_peer_progress(
            epoch="epoch-1", sequence=sequence, committed_half_cycles=peer
        )
        assert c.snapshot() == s


@pytest.mark.parametrize("sequence,peer", [(4, 10), (2, 11), (3, 9), (1, 0), (0, 10)])
def test_progress_gap_conflicting_duplicate_or_regression_fails_closed(sequence, peer):
    c = coordinator()
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=10)
    with pytest.raises(EmulatedTimeError):
        c.record_peer_progress(epoch="epoch-1", sequence=sequence, committed_half_cycles=peer)
    assert_terminal(c)


def test_edges_require_both_local_time_and_trusted_completeness_and_sort_by_time():
    c = coordinator(max_edge_lateness=0)
    assert c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=6, payload=b"late")
    assert c.receive_edge(epoch="epoch-1", sequence=2, at_half_cycle=2, payload=b"first")
    assert c.receive_edge(epoch="epoch-1", sequence=3, at_half_cycle=2, payload=b"second")
    assert not c.receive_edge(epoch="epoch-1", sequence=3, at_half_cycle=2, payload=b"second")
    advance(c, 1000, 1)
    assert c.pop_ready_edges() == ()
    assert c.reserve(1) is None
    c.advance_watermark(epoch="epoch-1", sequence=3, through_half_cycle=6)
    ready = c.pop_ready_edges()
    assert [(e.at_half_cycle, e.sequence, e.payload) for e in ready] == [
        (2, 2, b"first"),
        (2, 3, b"second"),
    ]
    assert all(e.epoch == "epoch-1" for e in ready)
    with pytest.raises(FrozenInstanceError):
        ready[0].payload = b"changed"
    assert c.pop_ready_edges() == ()
    assert c.reserve(1) is None
    c.acknowledge_delivery(ready[0].batch_token)
    advance(c, 1001, 2)
    assert [e.payload for e in c.pop_ready_edges()] == [b"late"]
    assert c.pop_ready_edges() == ()


@pytest.mark.parametrize("sequence", [0, 2])
def test_watermark_requires_exact_received_prefix(sequence):
    c = coordinator()
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=2, payload=b"x")
    with pytest.raises(EmulatedTimeError):
        c.advance_watermark(epoch="epoch-1", sequence=sequence, through_half_cycle=10)
    assert_terminal(c)


@pytest.mark.parametrize("sequence,payload", [(3, b"x"), (1, b"changed")])
def test_edge_gap_and_conflicting_duplicate_fail_closed(sequence, payload):
    c = coordinator()
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=2, payload=b"x")
    with pytest.raises(EmulatedTimeError):
        c.receive_edge(epoch="epoch-1", sequence=sequence, at_half_cycle=2, payload=payload)
    assert_terminal(c)


def test_edge_arriving_behind_attested_watermark_fails_closed():
    c = coordinator()
    c.advance_watermark(epoch="epoch-1", sequence=0, through_half_cycle=10)
    with pytest.raises(EmulatedTimeError):
        c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=9, payload=b"late")
    assert_terminal(c)


@pytest.mark.parametrize("action", ["cancel", "close", "progress"])
def test_condition_wait_is_woken_by_lifecycle_or_peer_credit(action):
    c = exhausted()
    entered = threading.Event()
    results, errors = [], []

    class ObservedCondition(threading.Condition):
        def wait(self, timeout=None):
            entered.set()  # Lock remains held until Condition.wait releases it.
            return super().wait(timeout)

    c._condition = ObservedCondition()

    def run():
        try:
            results.append(c.wait_for_permit(1, deadline=time.monotonic() + 5))
        except BaseException as error:  # noqa: BLE001 - assert worker failures in parent thread
            errors.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert entered.wait(1), "worker did not reach the condition wait"
        if action == "progress":
            c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=2)
        else:
            getattr(c, action)()
        worker.join(1)
        assert not worker.is_alive(), "notification did not release blocked waiter"
        if action == "progress":
            assert errors == [] and len(results) == 1
            assert results[0].cpu_cycles == 1
        else:
            assert results == [] and len(errors) == 1
            assert isinstance(errors[0], CoordinatorClosed)
            assert c.snapshot().cancelled if action == "cancel" else c.snapshot().closed
    finally:
        c.close()
        worker.join(1)


def test_absolute_deadline_is_not_restarted_after_spurious_wakeup(monkeypatch):
    c = exhausted()
    now, waits = [100.0], []

    class SpuriousCondition(threading.Condition):
        def wait(self, timeout=None):
            waits.append(timeout)
            now[0] += 2.0
            return True

    c._condition = SpuriousCondition()
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    assert c.wait_for_permit(1, deadline=105.0) is None
    assert waits == [5.0, 3.0, 1.0]
    assert not c.snapshot().pending_permit


@pytest.mark.parametrize("deadline", [True, float("inf"), float("-inf"), float("nan"), "5"])
def test_invalid_deadline_fails_closed(deadline):
    c = coordinator()
    with pytest.raises(EmulatedTimeError):
        c.wait_for_permit(1, deadline=deadline)
    assert_terminal(c)


def test_snapshot_is_immutable_and_detached_from_future_commits():
    c = coordinator()
    before = c.snapshot()
    with pytest.raises(FrozenInstanceError):
        before.local_half_cycles = 99
    advance(c, 1000, 1)
    assert before.local_half_cycles == 0
    assert c.snapshot().local_half_cycles == 2


def test_zero_rearm_still_allows_ordinary_quantum():
    c = coordinator(rearm_budget=0)
    advance(c, 1000, 128)
    assert c.reserve(1) is None
    with pytest.raises(EmulatedTimeError):
        c.begin_episode("request-1", cycle_budget=1, instruction_cap=1)
    assert_terminal(c)


def test_pending_permit_blocks_second_reservation_without_charging():
    c = coordinator()
    permit = c.reserve(10)
    before = c.snapshot()
    assert c.reserve(10) is None
    assert c.snapshot() == before
    c.commit(permit, raw_cpu_clock=1001, instructions=1)
    assert c.reserve(1) is not None


def test_peer_progress_cannot_move_fixed_episode_ceiling():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=8)
    advance(c, 1128, 1)
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=320)
    s = c.snapshot()
    assert s.debt_half_cycles == 0
    assert s.remaining_episode_half_cycles == 14
    permit = c.reserve(1000)
    assert (permit.cpu_cycles, permit.half_cycles) == (7, 14)


@pytest.mark.parametrize("next_id", ["request-0", "request-1"])
def test_retired_episode_cannot_be_reused(next_id):
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=1, instruction_cap=1)
    advance(c, 1128, 1)
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=2)
    c.begin_episode("request-2", cycle_budget=1, instruction_cap=1)
    advance(c, 1129, 1)
    c.record_peer_progress(epoch="epoch-1", sequence=3, committed_half_cycles=4)
    with pytest.raises(EmulatedTimeError):
        c.begin_episode(next_id, cycle_budget=1, instruction_cap=1)
    assert_terminal(c)


def test_exhausted_instruction_limit_allows_next_episode_only_after_repayment():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=1)
    advance(c, 1128, 1)
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=2)
    c.begin_episode("request-2", cycle_budget=32, instruction_cap=2)
    assert c.snapshot().remaining_episode_half_cycles == 64
    assert c.snapshot().remaining_instructions == 2


def test_new_edge_during_pending_permit_prevents_crossing_on_commit():
    c = coordinator(max_edge_lateness=0)
    permit = c.reserve(10)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=2, payload=b"edge")
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=1010, instructions=1)
    assert c.snapshot().local_half_cycles == 20
    assert_terminal(c)


@pytest.mark.parametrize("at", [9, 10])
def test_watermark_is_inclusive_for_rejecting_new_edges(at):
    c = coordinator()
    c.advance_watermark(epoch="epoch-1", sequence=0, through_half_cycle=10)
    with pytest.raises(EmulatedTimeError):
        c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=at, payload=b"edge")
    assert_terminal(c)


def test_late_edge_before_committed_time_is_terminal():
    c = coordinator(max_edge_lateness=0)
    advance(c, 1000, 2)
    with pytest.raises(EmulatedTimeError):
        c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=3, payload=b"edge")
    assert_terminal(c)


def test_edge_at_current_time_blocks_until_attested_and_drained():
    c = coordinator()
    advance(c, 1000, 2)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=4, payload=b"edge")
    assert c.reserve(1) is None
    assert c.pop_ready_edges() == ()
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=4)
    assert c.reserve(1) is None
    ready = c.pop_ready_edges()
    assert len(ready) == 1
    assert c.reserve(1) is None
    c.acknowledge_delivery(ready[0].batch_token)
    assert c.reserve(1) is not None


def test_queue_limit_is_bounded_and_latest_duplicate_uses_no_capacity():
    c = coordinator()
    for sequence in range(1, 1025):
        assert c.receive_edge(epoch="epoch-1", sequence=sequence, at_half_cycle=2, payload=b"edge")
    assert not c.receive_edge(epoch="epoch-1", sequence=1024, at_half_cycle=2, payload=b"edge")
    with pytest.raises(EmulatedTimeError):
        c.receive_edge(epoch="epoch-1", sequence=1025, at_half_cycle=2, payload=b"edge")
    assert_terminal(c)


def test_expired_deadline_never_grants_even_with_credit(monkeypatch):
    c = coordinator()
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    before = c.snapshot()
    assert c.wait_for_permit(1, deadline=100.0) is None
    assert c.snapshot() == before


@pytest.mark.parametrize("exhaust_by", ["cycles", "instructions"])
def test_exhausted_episode_cannot_fall_back_to_ordinary_credit(exhaust_by):
    c = coordinator()
    budget, cap = (1, 8) if exhaust_by == "cycles" else (8, 1)
    c.begin_episode("request-1", cycle_budget=budget, instruction_cap=cap)
    advance(c, 1000, 1)
    assert c.snapshot().debt_half_cycles == 0
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=256)
    before = c.snapshot()
    c.begin_episode("request-1", cycle_budget=budget, instruction_cap=cap)
    assert c.snapshot() == before
    assert c.reserve(1) is None
    c.begin_episode("request-2", cycle_budget=budget, instruction_cap=cap)
    assert c.reserve(1) is not None


def test_equal_permit_from_another_instance_is_rejected():
    first, second = coordinator(), coordinator()
    own, foreign = first.reserve(1), second.reserve(1)
    assert own == foreign and own is not foreign
    with pytest.raises(EmulatedTimeError):
        first.commit(foreign, raw_cpu_clock=1001, instructions=1)
    assert_terminal(first)
    assert not second.snapshot().closed


@pytest.mark.parametrize("payload", [b"x" * 4097, bytearray(b"x"), "x", None])
def test_invalid_or_oversized_edge_payload_is_terminal(payload):
    c = coordinator()
    with pytest.raises(EmulatedTimeError):
        c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=2, payload=payload)
    assert_terminal(c)


def test_payload_limit_accepts_exact_boundary():
    c = coordinator()
    payload = b"x" * 4096
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=payload)
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=0)
    assert c.pop_ready_edges()[0].payload == payload


@pytest.mark.parametrize("field", ["cycle_budget", "instruction_cap"])
@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, float("inf"), float("nan")])
def test_episode_limits_are_strict_positive_integers(field, invalid):
    c = coordinator()
    options = {"cycle_budget": 8, "instruction_cap": 8}
    options[field] = invalid
    with pytest.raises(EmulatedTimeError):
        c.begin_episode("request-1", **options)
    assert_terminal(c)


def test_episode_cannot_exceed_explicit_rearm_budget():
    c = coordinator()
    with pytest.raises(EmulatedTimeError):
        c.begin_episode("request-1", cycle_budget=33, instruction_cap=1)
    assert_terminal(c)


def test_instruction_overrun_retains_actual_local_progress():
    c = coordinator()
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=1)
    permit = c.reserve(8)
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=1008, instructions=2)
    assert c.snapshot().local_half_cycles == 16
    assert c.snapshot().raw_cpu_clock == 1008
    assert c.snapshot().terminal_reason
    assert_terminal(c)


def test_finish_preserves_debt_and_retired_identity_until_peer_credit_resumes():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    advance(c, 1128, 4)
    before = c.snapshot()
    assert before.epoch == "epoch-1"
    assert before.request_id == "request-1" and before.active_episode
    c.finish_episode("request-1")
    retired = c.snapshot()
    assert retired.local_half_cycles == before.local_half_cycles
    assert retired.peer_half_cycles == before.peer_half_cycles
    assert retired.debt_half_cycles == before.debt_half_cycles == 8
    assert retired.remaining_episode_half_cycles == retired.remaining_instructions == 0
    assert retired.request_id == "request-1" and not retired.active_episode
    c.finish_episode("request-1")
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    assert c.snapshot() == retired
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=8)
    assert c.snapshot().debt_half_cycles == 0
    assert c.reserve(1) is None  # Repayment reaches Q; further progress supplies credit.
    c.record_peer_progress(epoch="epoch-1", sequence=3, committed_half_cycles=10)
    advance(c, 1132, 1)
    assert not c.snapshot().active_episode


def test_finish_does_not_allow_new_episode_while_indebted():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    advance(c, 1128, 1)
    c.finish_episode("request-1")
    with pytest.raises(EmulatedTimeError):
        c.begin_episode("request-2", cycle_budget=32, instruction_cap=32)
    assert c.snapshot().debt_half_cycles == 2
    assert_terminal(c)


@pytest.mark.parametrize("pending,request_id", [(True, "request-1"), (False, "wrong")])
def test_finish_rejects_pending_permit_or_wrong_request(pending, request_id):
    c = coordinator()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    if pending:
        assert c.reserve(1) is not None
    with pytest.raises(EmulatedTimeError):
        c.finish_episode(request_id)
    assert_terminal(c)


def test_finished_episode_replay_cannot_reopen_or_change_limits():
    c = coordinator()
    c.begin_episode("request-1", cycle_budget=1, instruction_cap=1)
    advance(c, 1000, 1)
    assert c.snapshot().active_episode  # Exhaustion alone is not retirement.
    c.finish_episode("request-1")
    c.begin_episode("request-1", cycle_budget=1, instruction_cap=1)
    assert not c.snapshot().active_episode
    permit = c.reserve(1000)
    assert permit.cpu_cycles == 127  # Ordinary Q, not reopened one-cycle allowance.
    c.commit(permit, raw_cpu_clock=1002, instructions=1)
    with pytest.raises(EmulatedTimeError):
        c.begin_episode("request-1", cycle_budget=2, instruction_cap=1)
    assert_terminal(c)


def test_finished_episode_allows_increasing_request_after_repayment():
    c = exhausted()
    c.begin_episode("request-1", cycle_budget=32, instruction_cap=32)
    advance(c, 1128, 1)
    c.finish_episode("request-1")
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=2)
    c.begin_episode("request-2", cycle_budget=8, instruction_cap=4)
    s = c.snapshot()
    assert s.request_id == "request-2" and s.active_episode
    assert s.remaining_episode_half_cycles == 16 and s.remaining_instructions == 4


@pytest.mark.parametrize("double_speed,factor", [(False, 2), (True, 1)])
def test_zero_instruction_progress_spends_cycles_without_replenishing_episode(double_speed, factor):
    c = coordinator(double_speed=double_speed)
    raw = 1000 + 256 // factor
    advance(c, 1000, 256 // factor)
    c.begin_episode("request-1", cycle_budget=4, instruction_cap=3)
    for index in range(8 // factor):
        s = advance(c, raw + index, 1, instructions=0)
        spent = (index + 1) * factor
        assert s.local_half_cycles == 256 + spent
        assert s.debt_half_cycles == spent
        assert s.remaining_episode_half_cycles == 8 - spent
        assert s.remaining_instructions == 3
        assert not s.pending_permit
    assert c.reserve(1) is None
    c.begin_episode("request-1", cycle_budget=4, instruction_cap=3)
    assert c.reserve(1) is None


def test_ordinary_zero_instruction_progress_consumes_quantum_until_peer_advances():
    c = coordinator(rearm_budget=0)
    s = advance(c, 1000, 128, instructions=0)
    assert s.local_half_cycles == 256
    assert s.debt_half_cycles == 0
    assert s.remaining_instructions == 0
    assert not s.active_episode and not s.pending_permit
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=2)
    assert advance(c, 1128, 1, instructions=0).local_half_cycles == 258
    assert c.reserve(1) is None


def test_max_edge_lateness_requires_explicit_constructor_argument():
    with pytest.raises(TypeError):
        EmulatedTimeCoordinator(epoch="epoch-1", raw_cpu_clock=1000, rearm_budget=32)


def test_quantum_ahead_edge_preserves_scheduled_and_delivery_time_until_ack():
    c = exhausted()
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=256)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"edge")
    assert c.reserve(1) is None
    assert c.pop_ready_edges() == ()
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=0)
    ready = c.pop_ready_edges()
    assert len(ready) == 1
    delivery = ready[0]
    assert (delivery.epoch, delivery.sequence, delivery.payload) == ("epoch-1", 1, b"edge")
    assert delivery.at_half_cycle == 0
    assert delivery.delivered_half_cycle == 256
    assert delivery.lateness_half_cycles == 256
    assert c.snapshot().pending_delivery
    assert c.reserve(1) is None
    assert c.pop_ready_edges() == ()
    with pytest.raises(FrozenInstanceError):
        delivery.delivered_half_cycle = 0
    c.acknowledge_delivery(delivery.batch_token)
    assert not c.snapshot().pending_delivery
    assert c.snapshot().local_half_cycles == 256
    assert c.reserve(1) is not None


def test_lateness_limit_plus_one_half_cycle_is_terminal():
    c = coordinator(double_speed=True)
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=1)
    advance(c, 1000, 257)
    with pytest.raises(EmulatedTimeError):
        c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"edge")
    assert c.snapshot().local_half_cycles == 257
    assert c.snapshot().raw_cpu_clock == 1257
    assert c.snapshot().terminal_reason
    assert_terminal(c)


@pytest.mark.parametrize("actual_cpu,allowed", [(10, True), (11, False)])
def test_pending_arrival_uses_actual_commit_lateness_and_preserves_overrun(actual_cpu, allowed):
    c = coordinator(double_speed=True, max_edge_lateness=4)
    permit = c.reserve(20)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=2, payload=b"edge")
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=2)
    assert c.pop_ready_edges() == ()  # Pending CPU execution has no delivery timestamp yet.
    if allowed:
        c.commit(permit, raw_cpu_clock=1000 + actual_cpu, instructions=0)
        assert c.reserve(1) is None
        (delivery,) = c.pop_ready_edges()
        assert (delivery.at_half_cycle, delivery.delivered_half_cycle) == (2, 10)
        assert delivery.lateness_half_cycles == 8
        assert c.reserve(1) is None
        c.acknowledge_delivery(delivery.batch_token)
        assert c.reserve(1) is not None
    else:
        with pytest.raises(EmulatedTimeError):
            c.commit(permit, raw_cpu_clock=1000 + actual_cpu, instructions=0)
        s = c.snapshot()
        assert s.local_half_cycles == actual_cpu
        assert s.raw_cpu_clock == 1000 + actual_cpu
        assert s.terminal_reason and not s.pending_permit
        assert_terminal(c)


@pytest.mark.parametrize("violation", ["permit", "instruction_cap", "invalid_instructions"])
def test_terminal_commit_violation_keeps_actual_elapsed_diagnostics(violation):
    c = coordinator()
    permit = c.reserve(2)
    raw, instructions = {
        "permit": (1003, 1),
        "instruction_cap": (1002, 3),
        "invalid_instructions": (1002, True),
    }[violation]
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=raw, instructions=instructions)
    s = c.snapshot()
    assert s.raw_cpu_clock == raw
    assert s.local_half_cycles == 2 * (raw - 1000)
    assert s.terminal_reason and not s.pending_permit
    assert_terminal(c)


def test_clock_regression_keeps_last_committed_normalized_and_raw_time():
    c = coordinator()
    advance(c, 1000, 2)
    permit = c.reserve(2)
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=1001, instructions=0)
    s = c.snapshot()
    assert (s.local_half_cycles, s.raw_cpu_clock) == (4, 1002)
    assert s.observed_raw_cpu_clock == 1001
    assert s.terminal_reason
    assert not s.pending_permit
    assert_terminal(c)
    with pytest.raises(CoordinatorClosed):
        c.commit(permit, raw_cpu_clock=1004, instructions=0)
    assert c.snapshot() == s


@pytest.mark.parametrize("token", [True, False, -1, 1.5, "1", 0])
def test_delivery_ack_rejects_invalid_or_foreign_token(token):
    c = coordinator()
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"edge")
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=0)
    assert len(c.pop_ready_edges()) == 1
    with pytest.raises(EmulatedTimeError):
        c.acknowledge_delivery(token)
    assert_terminal(c)


def delivery_batch(c):
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"first")
    c.receive_edge(epoch="epoch-1", sequence=2, at_half_cycle=0, payload=b"second")
    c.advance_watermark(epoch="epoch-1", sequence=2, through_half_cycle=0)
    ready = c.pop_ready_edges()
    assert len(ready) == 2
    assert ready[0].batch_token == ready[1].batch_token
    return ready[0].batch_token


@pytest.mark.parametrize("kind", ["foreign", "reused", "forged"])
def test_ack_tokens_cannot_cross_instances_be_reused_or_forged(kind):
    c, other = coordinator(), coordinator()
    token, foreign = delivery_batch(c), delivery_batch(other)
    assert token != foreign
    if kind == "reused":
        c.acknowledge_delivery(token)
        invalid = token
    elif kind == "foreign":
        invalid = foreign
    else:
        invalid = max(token, foreign) + 1000000
    with pytest.raises(EmulatedTimeError):
        c.acknowledge_delivery(invalid)
    assert_terminal(c)
    assert not other.snapshot().closed


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_outstanding_execution_reports_actual_time_after_concurrent_terminal(action):
    c = coordinator()
    permit = c.reserve(10)
    getattr(c, action)()
    reason = c.snapshot().terminal_reason
    with pytest.raises(CoordinatorClosed):
        c.commit(permit, raw_cpu_clock=1003, instructions=0)
    s = c.snapshot()
    assert (s.local_half_cycles, s.raw_cpu_clock) == (6, 1003)
    assert s.terminal_reason == reason
    assert not s.pending_permit


def test_physical_overrun_beyond_lead_bound_is_retained_as_terminal_diagnostic():
    c = coordinator()
    permit = c.reserve(128)
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=1161, instructions=0)
    s = c.snapshot()
    assert s.raw_cpu_clock == 1161 and s.local_half_cycles == 322
    assert s.debt_half_cycles == 66  # Actual elapsed exceeds Q+R; it cannot be undone.
    assert s.terminal_reason
    assert_terminal(c)


def test_fractional_future_edge_allows_one_normal_cycle_with_bounded_lateness():
    c = coordinator(max_edge_lateness=1)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=1, payload=b"fraction")
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=1)
    permit = c.reserve(128)
    assert permit is not None
    assert (permit.cpu_cycles, permit.half_cycles) == (1, 2)
    c.commit(permit, raw_cpu_clock=1001, instructions=0)
    assert c.reserve(1) is None
    (delivery,) = c.pop_ready_edges()
    assert (delivery.at_half_cycle, delivery.delivered_half_cycle) == (1, 2)
    assert delivery.lateness_half_cycles == 1
    assert c.reserve(1) is None
    c.acknowledge_delivery(delivery.batch_token)
    assert c.reserve(1) is not None


def test_fractional_edge_roundup_never_exceeds_ordinary_credit():
    c = coordinator(double_speed=True, max_edge_lateness=1)
    advance(c, 1000, 255)
    c.set_speed(raw_cpu_clock=1255, double_speed=False)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=256, payload=b"fraction")
    assert c.reserve(1) is None  # Two-half CPU tick cannot fit remaining one-half Q credit.
    assert c.snapshot().local_half_cycles == 255


def test_unrepresentable_edge_with_zero_lateness_fails_explicitly():
    c = coordinator(max_edge_lateness=0)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=1, payload=b"fraction")
    with pytest.raises(EmulatedTimeError, match="boundary.*lateness"):
        c.reserve(1)
    assert c.snapshot().local_half_cycles == 0
    assert c.snapshot().raw_cpu_clock == 1000
    assert_terminal(c)


def test_future_edge_lateness_window_allows_native_sized_cpu_batch():
    c = coordinator(max_edge_lateness=32)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=8, payload=b"edge")
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=8)
    permit = c.reserve(128)
    assert permit is not None
    assert (permit.cpu_cycles, permit.half_cycles) == (36, 72)
    s = c.commit(permit, raw_cpu_clock=1020, instructions=1)
    assert s.local_half_cycles == 40
    assert c.reserve(1) is None
    (delivery,) = c.pop_ready_edges()
    assert delivery.at_half_cycle == 8
    assert delivery.delivered_half_cycle == 40
    assert delivery.lateness_half_cycles == 32
    assert c.reserve(1) is None
    c.acknowledge_delivery(delivery.batch_token)
    assert c.reserve(1) is not None


@pytest.mark.parametrize("episode", [False, True])
def test_future_edge_lateness_window_never_extends_credit_or_episode_cap(episode):
    c = coordinator(max_edge_lateness=256)
    if episode:
        c.begin_episode("request-1", cycle_budget=16, instruction_cap=8)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=8, payload=b"edge")
    permit = c.reserve(1000)
    assert permit is not None
    assert permit.cpu_cycles == (16 if episode else 128)


def test_experimental_quantum_256_accounts_206_cycle_zero_instruction_batch():
    """Abstract HDMA-sized accounting only; no vendor execution is exercised."""
    c = coordinator(quantum_cycles=256)
    before = c.snapshot()
    assert (before.local_half_cycles, before.peer_half_cycles) == (0, 0)
    permit = c.reserve(206)
    assert permit is not None
    assert (permit.cpu_cycles, permit.half_cycles) == (206, 412)
    reserved = c.snapshot()
    assert (reserved.local_half_cycles, reserved.peer_half_cycles, reserved.debt_half_cycles) == (
        0,
        0,
        0,
    )
    assert reserved.raw_cpu_clock == 1000 and reserved.pending_permit
    committed = c.commit(permit, raw_cpu_clock=1206, instructions=0)
    assert (committed.local_half_cycles, committed.peer_half_cycles) == (412, 0)
    assert committed.debt_half_cycles == 0
    assert committed.raw_cpu_clock == 1206 and not committed.pending_permit


def test_default_quantum_cannot_grant_full_206_cycle_batch():
    c = coordinator()
    permit = c.reserve(206)
    assert permit is not None
    assert (permit.cpu_cycles, permit.half_cycles) == (128, 256)
    assert permit.cpu_cycles < 206
    assert c.snapshot().local_half_cycles == c.snapshot().peer_half_cycles == 0
    # Deliberately do not execute or commit a batch larger than the permit.


@pytest.mark.parametrize("quantum", [1, 16, 256])
def test_custom_quantum_controls_ordinary_horizon_rearm_debt_and_repayment(quantum):
    c = coordinator(quantum_cycles=quantum, rearm_budget=8)
    permit = c.reserve(1000)
    assert permit.cpu_cycles == quantum
    c.commit(permit, raw_cpu_clock=1000 + quantum, instructions=0)
    assert c.snapshot().local_half_cycles == 2 * quantum
    assert c.snapshot().debt_half_cycles == 0
    assert c.reserve(1) is None
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=8)
    for index in range(8):
        s = advance(c, 1000 + quantum + index, 1, instructions=0)
        assert s.debt_half_cycles == 2 * (index + 1)
        assert s.local_half_cycles - s.peer_half_cycles <= 2 * (quantum + 8)
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=8)
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=16)
    assert c.snapshot().debt_half_cycles == 0
    c.finish_episode("request-1")
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="epoch-1", sequence=3, committed_half_cycles=18)
    assert c.reserve(1000).cpu_cycles == 1


@pytest.mark.parametrize(
    "invalid", [True, False, 0, -1, 1.5, "256", float("inf"), float("-inf"), float("nan")]
)
def test_quantum_requires_strict_positive_integer(invalid):
    with pytest.raises(EmulatedTimeError):
        coordinator(quantum_cycles=invalid)
