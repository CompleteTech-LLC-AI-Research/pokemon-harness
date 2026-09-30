"""Commit-lateness, delivery acknowledgement, and lateness-window contracts (#141).

Split from ``tests/test_emulated_time.py`` for #141 with no behavior change:
every assertion and test ID below is preserved verbatim from the original
module. ROM-free coordinator contracts only; no runtime activation or hardware
claims.
"""

from __future__ import annotations

import pytest

from pokered_harness.link.emulated_time import (
    CoordinatorClosed,
    EmulatedTimeError,
)
from tests._emulated_time_helpers import (
    advance,
    assert_terminal,
    coordinator,
    delivery_batch,
)


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
