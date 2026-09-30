"""Episode identity, edge admission, and finish/debt accounting (#141).

Split from ``tests/test_emulated_time.py`` for #141 with no behavior change:
every assertion and test ID below is preserved verbatim from the original
module. ROM-free coordinator contracts only; no runtime activation or hardware
claims.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pokered_harness.link import emulated_time as module
from pokered_harness.link.emulated_time import (
    EmulatedTimeCoordinator,
    EmulatedTimeError,
)
from tests._emulated_time_helpers import (
    advance,
    assert_terminal,
    coordinator,
    exhausted,
)


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
