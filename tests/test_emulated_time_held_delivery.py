"""Held-delivery pure coordinator accounting contracts; no native execution."""

import time

import pytest

from pokered_harness.link.emulated_time import (
    CoordinatorClosed,
    EmulatedTimeCoordinator,
    EmulatedTimeError,
    SpeedTransition,
)


def coordinator(**overrides):
    options = {
        "epoch": "held",
        "raw_cpu_clock": 1000,
        "rearm_budget": 8,
        "max_edge_lateness": 32,
        "quantum_cycles": 4,
    }
    options.update(overrides)
    c = EmulatedTimeCoordinator(**options)
    c.record_peer_progress(epoch="held", sequence=1, committed_half_cycles=0)
    return c


def commit(c, cycles, instructions=0):
    permit = c.reserve(cycles)
    assert permit is not None
    assert permit.cpu_cycles >= cycles
    return c.commit(
        permit, raw_cpu_clock=c.snapshot().raw_cpu_clock + cycles, instructions=instructions
    )


def deliver(c, times=(0,)):
    for sequence, timestamp in enumerate(times, 1):
        c.receive_edge(
            epoch="held", sequence=sequence, at_half_cycle=timestamp, payload=bytes([sequence])
        )
    c.advance_watermark(epoch="held", sequence=len(times), through_half_cycle=max(times))
    batch = c.pop_ready_edges()
    assert len(batch) == len(times)
    assert len({edge.batch_token for edge in batch}) == 1
    return batch


def begin(c, token, request="0001", cycles=8, instructions=8):
    return c.begin_delivery_rearm(token, request, cycle_budget=cycles, instruction_cap=instructions)


def test_actual_partial_grants_discard_retries_and_debt():
    c = coordinator()
    commit(c, 4)
    token = deliver(c, (8,))[0].batch_token
    assert begin(c, token, instructions=3) is None
    before = c.snapshot()
    permit = c.reserve(100)
    assert (permit.cpu_cycles, permit.instruction_cap) == (8, 3)
    assert c.discard_unconsumed_permit(permit, raw_cpu_clock=1004) == before
    permit = c.reserve(100)
    s = c.commit(permit, raw_cpu_clock=1006, instructions=1)
    assert (s.local_half_cycles, s.debt_half_cycles) == (12, 4)
    assert (s.remaining_episode_half_cycles, s.remaining_instructions) == (12, 2)
    assert s.pending_delivery and c.pop_ready_edges() == ()
    begin(c, token, instructions=3)
    assert c.snapshot() == s
    c.record_peer_progress(epoch="held", sequence=2, committed_half_cycles=100)
    permit = c.reserve(100)
    assert (permit.cpu_cycles, permit.instruction_cap) == (6, 2)
    c.commit(permit, raw_cpu_clock=1012, instructions=0)
    assert c.reserve(1) is None
    before = c.snapshot()
    begin(c, token, instructions=3)
    assert c.snapshot() == before
    assert c.reserve(1) is None


def test_instruction_counter_exhaustion_blocks_even_with_cycle_credit():
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token, instructions=1)
    s = commit(c, 1, instructions=1)
    assert s.remaining_episode_half_cycles == 14
    assert s.remaining_instructions == 0
    begin(c, token, instructions=1)
    assert c.snapshot() == s
    assert c.reserve(1) is None


@pytest.mark.parametrize("times", [(6, 2), (2, 6)])
def test_multi_edge_deadline_uses_earliest_original_timestamp(times):
    c = coordinator(max_edge_lateness=4)
    commit(c, 4)
    batch = deliver(c, times)
    assert [edge.at_half_cycle for edge in batch] == [2, 6]
    assert [edge.delivered_half_cycle for edge in batch] == [8, 8]
    token = batch[0].batch_token
    begin(c, token)
    permit = c.reserve(100)
    assert (permit.cpu_cycles, permit.half_cycles) == (1, 2)
    c.commit(permit, raw_cpu_clock=1005, instructions=0)
    assert c.reserve(1) is None
    before = c.snapshot()
    begin(c, token)
    assert c.snapshot() == before
    assert c.reserve(1) is None


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("offset", [0, 1, 2])
def test_both_stop_segments_consume_actual_held_allowance(initial, offset):
    c = coordinator(double_speed=initial)
    token = deliver(c)[0].batch_token
    begin(c, token)
    permit = c.reserve(8)
    s = c.commit(
        permit,
        raw_cpu_clock=1002,
        instructions=1,
        speed_transition=SpeedTransition(1000 + offset, not initial),
    )
    elapsed = offset * (1 if initial else 2) + (2 - offset) * (2 if initial else 1)
    assert (s.local_half_cycles, s.remaining_episode_half_cycles) == (elapsed, 16 - elapsed)
    assert s.remaining_instructions == 7 and s.pending_delivery
    assert s.double_speed is not initial
    s = commit(c, 1)
    assert s.local_half_cycles == elapsed + (2 if initial else 1)


@pytest.mark.parametrize("initial", [False, True])
def test_lateness_overrun_keeps_both_actual_stop_segments(initial):
    c = coordinator(double_speed=initial, max_edge_lateness=2)
    token = deliver(c)[0].batch_token
    begin(c, token)
    permit = c.reserve(100)
    assert permit.half_cycles == 4
    with pytest.raises(EmulatedTimeError):
        c.commit(
            permit,
            raw_cpu_clock=1004,
            instructions=1,
            speed_transition=SpeedTransition(1002, not initial),
        )
    s = c.snapshot()
    assert (s.raw_cpu_clock, s.observed_raw_cpu_clock, s.local_half_cycles) == (1004, 1004, 6)
    assert s.closed and s.pending_delivery and not s.pending_permit
    assert s.remaining_instructions == 7


@pytest.mark.parametrize("instructions", [2, True, -1])
def test_invalid_actual_instruction_report_retains_elapsed(instructions):
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token, instructions=1)
    permit = c.reserve(2)
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=1002, instructions=instructions)
    s = c.snapshot()
    assert (s.raw_cpu_clock, s.local_half_cycles) == (1002, 4)
    assert s.closed and s.pending_delivery and not s.pending_permit


def test_finish_restores_block_identical_retry_never_reopens_ack_clears():
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token)
    commit(c, 1)
    c.finish_episode("0001")
    retired = c.snapshot()
    assert retired.pending_delivery and not retired.active_episode
    assert c.reserve(1) is None
    begin(c, token)
    assert c.snapshot() == retired
    assert c.reserve(1) is None
    c.acknowledge_delivery(token)
    assert not c.snapshot().pending_delivery
    assert c.reserve(100).cpu_cycles == 3


@pytest.mark.parametrize("replacement", ["held", "ordinary"])
def test_same_batch_cannot_gain_new_allowance_or_bypass_via_ordinary_episode(replacement):
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token)
    c.finish_episode("0001")
    if replacement == "held":
        with pytest.raises(EmulatedTimeError):
            begin(c, token, request="0002")
        assert c.snapshot().closed
    else:
        with pytest.raises(EmulatedTimeError):
            c.begin_episode("0002", cycle_budget=8, instruction_cap=8)
        assert c.snapshot().closed
    assert c.snapshot().pending_delivery


@pytest.mark.parametrize("operation", ["ack", "finish", "begin"])
def test_outstanding_permit_rejects_lifecycle_changes_and_retains_actual(operation):
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token)
    permit = c.reserve(2)
    with pytest.raises(EmulatedTimeError):
        if operation == "ack":
            c.acknowledge_delivery(token)
        elif operation == "finish":
            c.finish_episode("0001")
        else:
            begin(c, token)
    with pytest.raises(CoordinatorClosed):
        c.commit(permit, raw_cpu_clock=1001, instructions=1)
    s = c.snapshot()
    assert s.pending_delivery and not s.pending_permit
    assert (s.local_half_cycles, s.raw_cpu_clock) == (2, 1001)


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_terminal_during_held_execution_retains_actual_and_original_reason(action):
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token)
    permit = c.reserve(8)
    getattr(c, action)()
    reason = c.snapshot().terminal_reason
    with pytest.raises(CoordinatorClosed):
        c.commit(permit, raw_cpu_clock=1003, instructions=1)
    s = c.snapshot()
    assert (s.local_half_cycles, s.raw_cpu_clock) == (6, 1003)
    assert s.terminal_reason == reason and s.pending_delivery and not s.pending_permit
    with pytest.raises(CoordinatorClosed):
        c.reserve(1)


@pytest.mark.parametrize("field", ["cycle_budget", "instruction_cap"])
@pytest.mark.parametrize("invalid", [0, -1, True, False, 1.5, "8", None])
def test_rearm_requires_strict_positive_budgets(field, invalid):
    c = coordinator()
    token = deliver(c)[0].batch_token
    options = {"cycle_budget": 8, "instruction_cap": 8}
    options[field] = invalid
    with pytest.raises(EmulatedTimeError):
        c.begin_delivery_rearm(token, "0001", **options)
    assert c.snapshot().closed and c.snapshot().pending_delivery


@pytest.mark.parametrize("request_id", ["", "x" * 257, None, True, 1])
def test_rearm_validates_request_identity(request_id):
    c = coordinator()
    token = deliver(c)[0].batch_token
    with pytest.raises(EmulatedTimeError):
        begin(c, token, request=request_id)
    assert c.snapshot().closed


@pytest.mark.parametrize("token", [0, -1, True, False, 1.5, "1", None])
def test_rearm_validates_token_type(token):
    c = coordinator()
    deliver(c)
    with pytest.raises(EmulatedTimeError):
        begin(c, token)
    assert c.snapshot().closed


@pytest.mark.parametrize("kind", ["foreign", "wrong", "acknowledged", "absent"])
def test_rearm_requires_exact_current_delivered_batch(kind):
    c, other = coordinator(), coordinator()
    foreign = deliver(other)[0].batch_token
    token = deliver(c)[0].batch_token if kind != "absent" else foreign
    if kind == "foreign":
        token = foreign
    elif kind == "wrong":
        token += 1000000
    elif kind == "acknowledged":
        c.acknowledge_delivery(token)
    with pytest.raises(EmulatedTimeError):
        begin(c, token)
    assert c.snapshot().closed and not other.snapshot().closed


@pytest.mark.parametrize("rearm", [0, 7])
def test_explicit_rearm_cap_is_not_extended(rearm):
    c = coordinator(rearm_budget=rearm)
    token = deliver(c)[0].batch_token
    with pytest.raises(EmulatedTimeError):
        begin(c, token, cycles=8)
    assert c.snapshot().closed


def test_expired_owner_timeout_does_not_mutate_or_consume_available_allowance():
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token)
    before = c.snapshot()
    assert c.wait_for_permit(1, deadline=time.monotonic()) is None
    assert c.snapshot() == before
    assert c.reserve(1) is not None


@pytest.mark.parametrize("complete", [False, True])
def test_due_queued_edge_is_never_bypassed_by_held_episode(complete):
    c = coordinator()
    commit(c, 4)
    token = deliver(c)[0].batch_token
    c.receive_edge(epoch="held", sequence=2, at_half_cycle=2, payload=b"queued")
    if complete:
        c.advance_watermark(epoch="held", sequence=2, through_half_cycle=2)
    begin(c, token)
    before = c.snapshot()
    assert c.reserve(1) is None and c.pop_ready_edges() == ()
    assert c.wait_for_permit(1, deadline=time.monotonic() + 0.005) is None
    assert c.snapshot() == before


def test_ordinary_quantum_and_pending_delivery_behavior_remain_unchanged():
    c = coordinator()
    assert commit(c, 4).local_half_cycles == 8
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="held", sequence=2, committed_half_cycles=8)
    token = deliver(c, (8,))[0].batch_token
    c.begin_episode("0001", cycle_budget=8, instruction_cap=8)
    assert c.reserve(1) is None
    c.finish_episode("0001")
    c.acknowledge_delivery(token)
    assert c.reserve(100).cpu_cycles == 4


def test_ack_retires_active_allowance_keeps_debt_and_replay_protection():
    c = coordinator()
    commit(c, 4)
    token = deliver(c, (8,))[0].batch_token
    begin(c, token)
    commit(c, 2, instructions=1)
    c.acknowledge_delivery(token)
    s = c.snapshot()
    assert not s.pending_delivery and not s.active_episode
    assert s.debt_half_cycles == 4
    assert s.remaining_episode_half_cycles == s.remaining_instructions == 0
    c.begin_episode("0001", cycle_budget=8, instruction_cap=8)
    assert c.snapshot() == s
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="held", sequence=2, committed_half_cycles=6)
    assert c.reserve(100).cpu_cycles == 1


@pytest.mark.parametrize("change", ["cycles", "instructions", "request"])
def test_conflicting_held_retry_never_refills(change):
    c = coordinator()
    token = deliver(c)[0].batch_token
    begin(c, token)
    commit(c, 1, instructions=1)
    before = c.snapshot()
    with pytest.raises(EmulatedTimeError):
        begin(
            c,
            token,
            cycles=7 if change == "cycles" else 8,
            instructions=7 if change == "instructions" else 8,
            request="0002" if change == "request" else "0001",
        )
    s = c.snapshot()
    assert s.remaining_episode_half_cycles == before.remaining_episode_half_cycles
    assert s.remaining_instructions == before.remaining_instructions
    assert s.closed and s.pending_delivery


def test_prior_ordinary_request_cannot_be_associated_by_replaying_it():
    c = coordinator()
    c.begin_episode("0001", cycle_budget=8, instruction_cap=8)
    c.finish_episode("0001")
    token = deliver(c)[0].batch_token
    with pytest.raises(EmulatedTimeError):
        begin(c, token)
    assert c.snapshot().closed and c.snapshot().pending_delivery


@pytest.mark.parametrize("state", ["active", "debt"])
def test_held_start_inherits_ordinary_active_and_debt_guards(state):
    c = coordinator()
    c.begin_episode("0001", cycle_budget=8, instruction_cap=8)
    if state == "debt":
        commit(c, 5)
        c.finish_episode("0001")
    token = deliver(c)[0].batch_token
    with pytest.raises(EmulatedTimeError):
        begin(c, token, request="0002")
    assert c.snapshot().closed and c.snapshot().pending_delivery


def test_ack_allows_new_batch_with_new_deadline_and_request():
    c = coordinator(max_edge_lateness=2)
    token = deliver(c)[0].batch_token
    begin(c, token)
    commit(c, 2)
    assert c.reserve(1) is None
    c.acknowledge_delivery(token)
    c.receive_edge(epoch="held", sequence=2, at_half_cycle=4, payload=b"next")
    c.advance_watermark(epoch="held", sequence=2, through_half_cycle=4)
    new_token = c.pop_ready_edges()[0].batch_token
    assert new_token != token
    begin(c, new_token, request="0002")
    permit = c.reserve(100)
    assert permit.half_cycles == 4
    c.commit(permit, raw_cpu_clock=1004, instructions=0)
    assert c.reserve(1) is None


def test_unanchored_peer_cannot_be_bypassed_by_held_episode():
    c = EmulatedTimeCoordinator(
        epoch="held", raw_cpu_clock=1000, rearm_budget=8, max_edge_lateness=32
    )
    token = deliver(c)[0].batch_token
    begin(c, token)
    assert c.reserve(1) is None
    c.record_peer_progress(epoch="held", sequence=1, committed_half_cycles=0)
    assert c.reserve(1) is not None


def test_zero_lateness_held_batch_has_no_execution_credit():
    c = coordinator(max_edge_lateness=0)
    token = deliver(c)[0].batch_token
    begin(c, token)
    before = c.snapshot()
    assert c.reserve(1) is None
    assert c.snapshot() == before
