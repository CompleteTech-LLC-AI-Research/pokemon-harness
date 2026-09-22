"""Terminal-failure diagnostics and the completeness option (#141).

Split from ``tests/test_emulated_time.py`` for #141 with no behavior change:
every assertion and test ID below is preserved verbatim from the original
module. ROM-free coordinator contracts only; no runtime activation or hardware
claims.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields

import pytest

from pokered_harness.link import emulated_time as module
from pokered_harness.link.emulated_time import (
    CoordinatorClosed,
    EmulatedTimeCoordinator,
    EmulatedTimeError,
)
from tests._emulated_time_helpers import (
    advance,
    coordinator,
    delivery_batch,
)


def assert_lateness_failure(c, reason, phase, *, sequence, at, measured, allowed):
    snapshot = c.snapshot()
    failure = snapshot.failure
    assert type(failure) is module.FailureSnapshot
    assert asdict(failure) == {
        "reason": reason,
        "terminal_reason": reason,
        "phase": phase,
        "epoch": "epoch-1",
        "local_half_cycles": snapshot.local_half_cycles,
        "peer_half_cycles": snapshot.peer_half_cycles,
        "raw_cpu_clock": snapshot.raw_cpu_clock,
        "observed_raw_cpu_clock": snapshot.observed_raw_cpu_clock,
        "edge_sequence": sequence,
        "edge_at_half_cycle": at,
        "measured_lateness_half_cycles": measured,
        "allowed_lateness_half_cycles": allowed,
        "excess_half_cycles": max(0, measured - allowed),
        "emission_complete_half_cycle": c._watermark,
    }
    assert snapshot.closed and snapshot.terminal_reason == reason
    assert all(type(value) in (str, int, type(None)) for value in asdict(failure).values())
    with pytest.raises(FrozenInstanceError):
        failure.excess_half_cycles = 0
    return failure


@pytest.mark.parametrize("phase", ["receive", "commit"])
@pytest.mark.parametrize("lateness", [64, 65])
def test_failure_exact_64_half_cycle_bound_and_one_half_excess(phase, lateness):
    c = coordinator(double_speed=True, max_edge_lateness=32)
    edge = {"epoch": "epoch-1", "sequence": 1, "at_half_cycle": 3, "payload": b"edge"}
    if phase == "commit":
        permit = c.reserve(100)
        assert c.receive_edge(**edge)
        operation = lambda: c.commit(permit, raw_cpu_clock=1003 + lateness, instructions=0)
        reason = "edge lateness exceeds bound; actual elapsed recorded"
    else:
        advance(c, 1000, 3 + lateness)
        operation = lambda: c.receive_edge(**edge)
        reason = "edge lateness exceeds bound"
    if lateness == 64:
        operation()
        assert c.snapshot().failure is None
        c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=3)
        (delivery,) = c.pop_ready_edges()
        assert (delivery.at_half_cycle, delivery.delivered_half_cycle) == (3, 67)
        assert delivery.lateness_half_cycles == 64
        c.acknowledge_delivery(delivery.batch_token)
        assert c.snapshot().failure is None
    else:
        with pytest.raises(EmulatedTimeError) as error:
            operation()
        assert type(error.value) is EmulatedTimeError
        assert str(error.value) == reason
        failure_phase = "commit.edge_lateness" if phase == "commit" else "receive_edge.lateness"
        assert_lateness_failure(
            c,
            reason,
            failure_phase,
            sequence=1,
            at=3,
            measured=65,
            allowed=64,
        )
        assert (c.snapshot().local_half_cycles, c.snapshot().raw_cpu_clock) == (68, 1068)
        if phase == "receive":
            assert c._edge_sequence == 0 and c._last_edge is None and c._edges == []


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"epoch": "other"}, "epoch mismatch; construct a new coordinator"),
        ({"sequence": True}, "sequence must be an integer >= 1"),
        ({"at_half_cycle": -1}, "at_half_cycle must be an integer >= 0"),
        ({"payload": "edge"}, "payload must be bytes within MAX_EDGE_PAYLOAD_BYTES"),
        ({"sequence": 3}, "noncontiguous edge receipt sequence"),
        ({"sequence": 1}, "conflicting edge replay"),
        ({}, "edge contradicts inclusive completeness watermark"),
    ],
)
def test_failure_receive_validation_precedes_lateness(changes, reason):
    c = coordinator(double_speed=True, max_edge_lateness=32)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"original")
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=0)
    (delivery,) = c.pop_ready_edges()
    c.acknowledge_delivery(delivery.batch_token)
    advance(c, 1000, 65)
    edge = {"epoch": "epoch-1", "sequence": 2, "at_half_cycle": 0, "payload": b"edge"}
    edge.update(changes)
    with pytest.raises(EmulatedTimeError) as error:
        c.receive_edge(**edge)
    assert type(error.value) is EmulatedTimeError and str(error.value) == reason
    failure = c.snapshot().failure
    assert failure.reason == failure.terminal_reason == reason
    assert failure.phase == "validation"
    assert all(
        getattr(failure, name) is None
        for name in (
            "edge_sequence",
            "edge_at_half_cycle",
            "measured_lateness_half_cycles",
            "allowed_lateness_half_cycles",
            "excess_half_cycles",
        )
    )


def test_failure_identical_replay_precedes_watermark_and_lateness():
    c = coordinator(double_speed=True, max_edge_lateness=32)
    edge = {"epoch": "epoch-1", "sequence": 1, "at_half_cycle": 0, "payload": b"edge"}
    assert c.receive_edge(**edge)
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=0)
    (delivery,) = c.pop_ready_edges()
    c.acknowledge_delivery(delivery.batch_token)
    advance(c, 1000, 65)
    before = c.snapshot()
    assert c.receive_edge(**edge) is False
    assert c.snapshot() == before and before.failure is None


@pytest.mark.parametrize("action", ["cancel", "close"])
@pytest.mark.parametrize("settlement", ["commit", "discard", "invalid_clock"])
def test_failure_first_record_survives_lifecycle_and_outstanding_settlement(action, settlement):
    c = coordinator(double_speed=True, max_edge_lateness=32)
    advance(c, 1000, 65)
    permit = c.reserve(10)
    reason = "edge lateness exceeds bound"
    with pytest.raises(EmulatedTimeError, match=f"^{reason}$"):
        c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"rejected")
    first = c.snapshot()
    failure = first.failure
    assert failure is not None
    getattr(c, action)()
    if settlement == "invalid_clock":
        with pytest.raises(EmulatedTimeError) as error:
            c.commit(permit, raw_cpu_clock=-1, instructions=0)
        assert type(error.value) is EmulatedTimeError
        assert str(error.value) == "raw_cpu_clock must be an integer >= 0"
    else:
        with pytest.raises(CoordinatorClosed) as error:
            if settlement == "commit":
                c.commit(permit, raw_cpu_clock=1068, instructions=0)
            else:
                c.discard_unconsumed_permit(permit, raw_cpu_clock=1065)
        assert str(error.value) == reason
        assert not c.snapshot().pending_permit
    assert c.snapshot().failure is failure
    assert c.snapshot().terminal_reason == reason
    assert (failure.local_half_cycles, failure.raw_cpu_clock) == (65, 1065)
    if settlement == "commit":
        assert (c.snapshot().local_half_cycles, c.snapshot().raw_cpu_clock) == (68, 1068)
    assert first.local_half_cycles == 65 and first.pending_permit
    c.cancel()
    c.close()
    with pytest.raises(CoordinatorClosed, match=f"^{reason}$"):
        c.reserve(1)
    assert c.snapshot().failure is failure


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_failure_validation_after_lifecycle_does_not_manufacture_failure(action):
    c = coordinator()
    permit = c.reserve(1)
    getattr(c, action)()
    terminal = "coordinator cancelled" if action == "cancel" else "coordinator closed"
    assert c.snapshot().failure is None
    with pytest.raises(EmulatedTimeError) as error:
        c.commit(permit, raw_cpu_clock=-1, instructions=0)
    assert type(error.value) is EmulatedTimeError
    assert str(error.value) == "raw_cpu_clock must be an integer >= 0"
    assert c.snapshot().failure is None
    assert c.snapshot().terminal_reason == terminal
    with pytest.raises(CoordinatorClosed, match=f"^{terminal}$"):
        c.commit(permit, raw_cpu_clock=1001, instructions=0)
    assert c.snapshot().failure is None


@pytest.mark.parametrize(
    "phase,reason",
    [
        ("delivery.lateness", "delivery lateness exceeds bound"),
        ("reserve.held_lateness", "held delivery lateness exceeds bound"),
        ("commit.held_lateness", "held delivery lateness exceeds bound; actual elapsed recorded"),
    ],
)
def test_failure_defensive_lateness_guards_capture_earliest_edge(phase, reason):
    c = coordinator(double_speed=True, max_edge_lateness=32)
    advance(c, 1000, 2)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=2, payload=b"later")
    c.receive_edge(epoch="epoch-1", sequence=2, at_half_cycle=0, payload=b"earliest")
    c.advance_watermark(epoch="epoch-1", sequence=2, through_half_cycle=2)
    if phase != "delivery.lateness":
        ready = c.pop_ready_edges()
        assert [edge.sequence for edge in ready] == [2, 1]
        c.begin_delivery_rearm(
            ready[0].batch_token, "request-1", cycle_budget=32, instruction_cap=2
        )
        if phase == "commit.held_lateness":
            permit = c.reserve(1)
    # Earlier public guards normally prevent these defensive states. Inject only
    # normalized time to isolate the guard; this is not emulator/runtime evidence.
    c._local = 64 if phase == "commit.held_lateness" else 65
    with pytest.raises(EmulatedTimeError) as error:
        if phase == "delivery.lateness":
            c.pop_ready_edges()
        elif phase == "reserve.held_lateness":
            c.reserve(1)
        else:
            c.commit(permit, raw_cpu_clock=1003, instructions=0)
    assert type(error.value) is EmulatedTimeError and str(error.value) == reason
    assert_lateness_failure(c, reason, phase, sequence=2, at=0, measured=65, allowed=64)


def test_failure_boundary_lateness_reports_prospective_half_cycle_without_execution():
    c = coordinator(max_edge_lateness=0)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=1, payload=b"odd")
    reason = "future edge boundary cannot fit speed and lateness bound"
    with pytest.raises(EmulatedTimeError) as error:
        c.reserve(1)
    assert type(error.value) is EmulatedTimeError and str(error.value) == reason
    assert_lateness_failure(
        c,
        reason,
        "reserve.boundary_lateness",
        sequence=1,
        at=1,
        measured=1,
        allowed=0,
    )
    assert (c.snapshot().local_half_cycles, c.snapshot().raw_cpu_clock) == (0, 1000)
    assert not c.snapshot().pending_permit


def test_failure_optional_snapshot_field_preserves_legacy_constructor_and_normal_schema():
    c = coordinator()
    snapshot = c.snapshot()
    legacy_fields = (
        "local_half_cycles",
        "peer_half_cycles",
        "debt_half_cycles",
        "remaining_episode_half_cycles",
        "remaining_instructions",
        "pending_permit",
        "closed",
        "cancelled",
        "epoch",
        "request_id",
        "active_episode",
        "raw_cpu_clock",
        "terminal_reason",
        "pending_delivery",
        "observed_raw_cpu_clock",
        "double_speed",
    )
    assert tuple(field.name for field in fields(snapshot)) == (*legacy_fields, "failure")
    assert module.TimeSnapshot(*(getattr(snapshot, name) for name in legacy_fields)) == snapshot
    assert (
        module.TimeSnapshot(**{name: getattr(snapshot, name) for name in legacy_fields}) == snapshot
    )
    assert asdict(snapshot)["failure"] is None
    assert advance(c, 1000, 1).failure is None
    token = delivery_batch(c)
    c.begin_delivery_rearm(token, "request-1", cycle_budget=1, instruction_cap=1)
    advance(c, 1001, 1)
    c.acknowledge_delivery(token)
    assert c.snapshot().failure is None
    c.close()
    assert c.snapshot().failure is None


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_failure_invalid_discard_after_lifecycle_preserves_none_and_immediate_error(action):
    c = coordinator()
    permit = c.reserve(1)
    getattr(c, action)()
    terminal = c.snapshot().terminal_reason
    with pytest.raises(EmulatedTimeError) as error:
        c.discard_unconsumed_permit(permit, raw_cpu_clock=1001)
    assert type(error.value) is EmulatedTimeError
    assert str(error.value) == "invalid unconsumed permit report; unknown normalized interval"
    snapshot = c.snapshot()
    assert snapshot.failure is None and snapshot.terminal_reason == terminal
    assert not snapshot.pending_permit
    assert (snapshot.local_half_cycles, snapshot.raw_cpu_clock) == (0, 1000)
    assert snapshot.observed_raw_cpu_clock == 1001
    with pytest.raises(CoordinatorClosed) as error:
        c.reserve(1)
    assert str(error.value) == terminal
    assert c.snapshot() == snapshot


@pytest.mark.parametrize(
    "cycles,instructions,reason",
    [
        (66, True, "instructions must be an integer >= 0"),
        (65, 67, "physical permit overrun; actual elapsed recorded"),
        (66, 67, "instruction cap exceeded; actual elapsed recorded"),
    ],
)
def test_failure_commit_validation_precedes_edge_lateness(cycles, instructions, reason):
    c = coordinator(double_speed=True, max_edge_lateness=32)
    permit = c.reserve(cycles)
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"edge")
    with pytest.raises(EmulatedTimeError) as error:
        c.commit(permit, raw_cpu_clock=1066, instructions=instructions)
    assert type(error.value) is EmulatedTimeError and str(error.value) == reason
    snapshot = c.snapshot()
    assert (snapshot.local_half_cycles, snapshot.raw_cpu_clock) == (66, 1066)
    assert not snapshot.pending_permit
    failure = snapshot.failure
    assert failure.phase == "validation" and failure.reason == reason
    assert failure.terminal_reason == reason
    assert (failure.local_half_cycles, failure.raw_cpu_clock) == (66, 1066)
    assert failure.edge_sequence is failure.edge_at_half_cycle is None
    assert failure.measured_lateness_half_cycles is None
    assert failure.allowed_lateness_half_cycles is failure.excess_half_cycles is None


@pytest.mark.parametrize("invalid", [None, 0, 1, -1, 0.0, 1.0, "false", [], {}])
def test_completeness_option_requires_real_boolean(invalid):
    with pytest.raises(EmulatedTimeError) as error:
        coordinator(enforce_completeness=invalid)
    assert type(error.value) is EmulatedTimeError
    assert str(error.value) == "enforce_completeness must be bool"


def test_completeness_disabled_preserves_default_standalone_permits():
    default = coordinator(max_edge_lateness=32, quantum_cycles=256)
    disabled = coordinator(max_edge_lateness=32, quantum_cycles=256, enforce_completeness=False)
    assert default.snapshot() == disabled.snapshot()
    for c in (default, disabled):
        permit = c.reserve(206)
        assert (permit.cpu_cycles, permit.half_cycles) == (206, 412)
        assert c.snapshot().local_half_cycles == 0
        c.commit(permit, raw_cpu_clock=1206, instructions=0)
    assert default.snapshot() == disabled.snapshot()


@pytest.mark.parametrize("double_speed,cycles,halves", [(False, 31, 62), (True, 63, 63)])
def test_completeness_bootstrap_minus_one_bounds_whole_permit_and_accepts_edge_zero(
    double_speed, cycles, halves
):
    c = coordinator(enforce_completeness=True, max_edge_lateness=32, double_speed=double_speed)
    permit = c.reserve(1000)
    assert (permit.cpu_cycles, permit.half_cycles) == (cycles, halves)
    assert permit.half_cycles <= -1 + 64
    c.commit(permit, raw_cpu_clock=1000 + cycles, instructions=0)
    assert c.reserve(1) is None
    assert c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"bootstrap")
    assert c.pop_ready_edges() == ()
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=0)
    (edge,) = c.pop_ready_edges()
    assert edge.at_half_cycle == 0 and edge.lateness_half_cycles == halves <= 64
    c.acknowledge_delivery(edge.batch_token)
    assert c.snapshot().failure is None


@pytest.mark.parametrize("mode", ["ordinary", "episode", "known_edge", "held_rearm"])
@pytest.mark.parametrize("double_speed", [False, True])
def test_completeness_caps_whole_permit_after_other_allowances(mode, double_speed):
    c = coordinator(enforce_completeness=True, max_edge_lateness=32, double_speed=double_speed)
    if mode == "held_rearm":
        token = delivery_batch(c)
        c.begin_delivery_rearm(token, "request-1", cycle_budget=32, instruction_cap=100)
    else:
        c.advance_watermark(epoch="epoch-1", sequence=0, through_half_cycle=0)
        if mode == "episode":
            c.begin_episode("request-1", cycle_budget=32, instruction_cap=100)
        elif mode == "known_edge":
            c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=8, payload=b"known")
    before = c.snapshot()
    permit = c.reserve(1000)
    assert permit.half_cycles == 64
    assert permit.cpu_cycles == (64 if double_speed else 32)
    assert before.local_half_cycles + permit.half_cycles <= 0 + 64
    c.commit(permit, raw_cpu_clock=1000 + permit.cpu_cycles, instructions=0)
    assert c.reserve(1) is None
    assert c.snapshot().local_half_cycles == 64 and c.snapshot().failure is None


def test_completeness_delayed_edge_512_cannot_be_crossed_past_540_before_receipt():
    c = coordinator(enforce_completeness=True, max_edge_lateness=32)
    c.record_peer_progress(epoch="epoch-1", sequence=2, committed_half_cycles=476)
    c.advance_watermark(epoch="epoch-1", sequence=0, through_half_cycle=476)
    advance(c, 1000, 238)
    assert c.snapshot().local_half_cycles == c.snapshot().peer_half_cycles == 476
    permit = c.reserve(48)
    assert (permit.cpu_cycles, permit.half_cycles) == (32, 64)
    assert 476 + permit.half_cycles == 540
    # Commit only the granted budget; the delayed receipt is still absent.
    c.commit(permit, raw_cpu_clock=1270, instructions=0)
    before = c.snapshot()
    assert c.reserve(48) is None and c.snapshot() == before
    assert c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=512, payload=b"delayed")
    assert c.pop_ready_edges() == ()
    c.advance_watermark(epoch="epoch-1", sequence=1, through_half_cycle=512)
    (edge,) = c.pop_ready_edges()
    assert (edge.at_half_cycle, edge.delivered_half_cycle, edge.lateness_half_cycles) == (
        512,
        540,
        28,
    )
    assert edge.delivered_half_cycle <= 512 + 64
    c.acknowledge_delivery(edge.batch_token)
    permit = c.reserve(48)
    assert permit.half_cycles == 36 and 540 + permit.half_cycles == 576
    c.commit(permit, raw_cpu_clock=1288, instructions=0)
    assert c.reserve(1) is None and c.snapshot().failure is None


@pytest.mark.parametrize("double_speed", [False, True])
def test_completeness_hdma_412_half_cycle_batch_cannot_fit_without_execution(double_speed):
    c = coordinator(
        enforce_completeness=True,
        max_edge_lateness=32,
        quantum_cycles=256,
        double_speed=double_speed,
    )
    c.advance_watermark(epoch="epoch-1", sequence=0, through_half_cycle=0)
    before = c.snapshot()
    atomic_cycles = 412 if double_speed else 206
    for _ in range(2):
        permit = c.reserve(atomic_cycles)
        assert permit.half_cycles == 64
        assert permit.cpu_cycles < atomic_cycles
        # An indivisible batch cannot use this partial budget. Release it with
        # zero execution; never commit part of HDMA or retry with an uncapped path.
        assert c.discard_unconsumed_permit(permit, raw_cpu_clock=1000) == before
    assert c.snapshot().local_half_cycles == 0 and c.snapshot().failure is None


def test_completeness_first_failure_watermark_is_additive_scalar_and_frozen():
    c = coordinator(enforce_completeness=True, max_edge_lateness=32)
    c.advance_watermark(epoch="epoch-1", sequence=0, through_half_cycle=476)
    permit = c.reserve(1)
    with pytest.raises(EmulatedTimeError) as error:
        c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=476, payload=b"private payload")
    assert str(error.value) == "edge contradicts inclusive completeness watermark"
    first = c.snapshot().failure
    assert first.emission_complete_half_cycle == 476
    values = asdict(first)
    assert all(type(value) in (str, int, type(None)) for value in values.values())
    assert "private payload" not in repr(values) and "payload" not in values
    assert fields(first)[-1].name == "emission_complete_half_cycle"
    legacy = {
        name: value for name, value in values.items() if name != "emission_complete_half_cycle"
    }
    assert module.FailureSnapshot(**legacy).emission_complete_half_cycle is None
    assert module.FailureSnapshot(*legacy.values()).emission_complete_half_cycle is None
    with pytest.raises(FrozenInstanceError):
        first.emission_complete_half_cycle = 999
    c.cancel()
    c.close()
    with pytest.raises(CoordinatorClosed):
        c.commit(permit, raw_cpu_clock=1001, instructions=0)
    assert c.snapshot().failure is first and first.emission_complete_half_cycle == 476


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"raw_cpu_clock": -1}, "raw_cpu_clock must be an integer >= 0"),
        ({"double_speed": 1}, "double_speed must be bool"),
    ],
)
def test_completeness_option_preserves_prior_constructor_validation_order(options, reason):
    with pytest.raises(EmulatedTimeError) as error:
        coordinator(enforce_completeness=1, **options)
    assert type(error.value) is EmulatedTimeError and str(error.value) == reason


def test_completeness_constructor_failure_watermark_is_unavailable():
    c = EmulatedTimeCoordinator.__new__(EmulatedTimeCoordinator)
    with pytest.raises(EmulatedTimeError, match="^enforce_completeness must be bool$"):
        c.__init__(
            epoch="epoch-1",
            raw_cpu_clock=1000,
            rearm_budget=32,
            max_edge_lateness=32,
            enforce_completeness=1,
        )
    # Construction failed before snapshot accounting exists; inspect its retained
    # failure directly to distinguish unavailable completeness from bootstrap -1.
    assert c._failure.emission_complete_half_cycle is None
