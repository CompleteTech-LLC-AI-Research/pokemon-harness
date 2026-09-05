"""ROM-free STOP segment and unconsumed execution-permit contracts."""

from dataclasses import FrozenInstanceError, replace

import pytest

from pokered_harness.link.emulated_time import (
    CoordinatorClosed,
    EmulatedTimeCoordinator,
    EmulatedTimeError,
    SpeedTransition,
)


def coordinator(double_speed=False):
    c = EmulatedTimeCoordinator(
        epoch="segments",
        raw_cpu_clock=1000,
        rearm_budget=32,
        max_edge_lateness=128,
        double_speed=double_speed,
    )
    c.record_peer_progress(epoch="segments", sequence=1, committed_half_cycles=0)
    return c


def assert_clock(c, raw, half, speed):
    s = c.snapshot()
    assert (s.raw_cpu_clock, s.observed_raw_cpu_clock, s.local_half_cycles) == (
        raw,
        raw,
        half,
    )
    assert s.double_speed is speed
    assert not s.pending_permit
    return s


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("offset", [0, 3, 8])
def test_stop_segments_include_start_and_end_boundaries(initial, offset):
    c = coordinator(initial)
    permit = c.reserve(16 if initial else 8)
    s = c.commit(
        permit,
        raw_cpu_clock=1008,
        instructions=1,
        speed_transition=SpeedTransition(1000 + offset, not initial),
    )
    expected = offset * (1 if initial else 2) + (8 - offset) * (2 if initial else 1)
    assert s == assert_clock(c, 1008, expected, not initial)
    next_permit = c.reserve(1)
    c.commit(next_permit, raw_cpu_clock=1009, instructions=0)
    assert_clock(c, 1009, expected + (2 if initial else 1), not initial)


def test_transition_is_frozen():
    transition = SpeedTransition(1001, True)
    with pytest.raises(FrozenInstanceError):
        transition.raw_cpu_clock = 1002
    with pytest.raises(FrozenInstanceError):
        transition.double_speed = False


def test_odd_half_carry_survives_stop_in_both_directions():
    c = coordinator(True)
    c.commit(
        c.reserve(4),
        raw_cpu_clock=1002,
        instructions=1,
        speed_transition=SpeedTransition(1001, False),
    )
    assert_clock(c, 1002, 3, False)
    c.commit(
        c.reserve(2),
        raw_cpu_clock=1004,
        instructions=1,
        speed_transition=SpeedTransition(1003, True),
    )
    assert_clock(c, 1004, 6, True)
    c.commit(c.reserve(249), raw_cpu_clock=1253, instructions=0)
    assert_clock(c, 1253, 255, True)
    c.commit(
        c.reserve(1),
        raw_cpu_clock=1254,
        instructions=1,
        speed_transition=SpeedTransition(1254, False),
    )
    assert_clock(c, 1254, 256, False)
    assert c.reserve(1) is None


def test_caller_reserves_double_cpu_allowance_for_normal_speed_worst_case():
    c = coordinator(True)
    permit = c.reserve(16)
    assert permit.cpu_cycles == 16
    assert permit.half_cycles == 16
    c.commit(
        permit, raw_cpu_clock=1008, instructions=1, speed_transition=SpeedTransition(1000, False)
    )
    assert_clock(c, 1008, 16, False)


def test_conservative_reservation_still_allows_last_odd_half_cycle():
    c = coordinator(True)
    c.commit(c.reserve(255), raw_cpu_clock=1255, instructions=0)
    permit = c.reserve(8)
    assert (permit.cpu_cycles, permit.half_cycles) == (1, 1)
    c.commit(permit, raw_cpu_clock=1256, instructions=0)
    assert_clock(c, 1256, 256, True)


@pytest.mark.parametrize("overrun", ["cpu", "half"])
def test_each_overrun_records_actual_segments_and_final_speed(overrun):
    c = coordinator(overrun == "half")
    if overrun == "half":
        c.commit(c.reserve(255), raw_cpu_clock=1255, instructions=0)
        permit, raw, transition, expected = c.reserve(1), 1256, SpeedTransition(1255, False), 257
    else:
        permit, raw, transition, expected = c.reserve(2), 1003, SpeedTransition(1000, True), 3
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=raw, instructions=1, speed_transition=transition)
    s = assert_clock(c, raw, expected, overrun == "cpu")
    assert s.closed


@pytest.mark.parametrize(
    "transition",
    [
        SpeedTransition(True, True),
        SpeedTransition(1001.0, True),
        SpeedTransition("1001", True),
        SpeedTransition(None, True),
        SpeedTransition(-1, True),
        SpeedTransition(999, True),
        SpeedTransition(1005, True),
        SpeedTransition(1001, 1),
        SpeedTransition(1001, 0),
        SpeedTransition(1001, None),
        SpeedTransition(1001, "true"),
        (1001, True),
        {"raw_cpu_clock": 1001, "double_speed": True},
        True,
    ],
)
def test_malformed_segment_keeps_observation_without_committing(transition):
    c = coordinator()
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=4)
    before = c.snapshot()
    permit = c.reserve(4)
    with pytest.raises(EmulatedTimeError):
        c.commit(permit, raw_cpu_clock=1004, instructions=1, speed_transition=transition)
    s = c.snapshot()
    assert s.observed_raw_cpu_clock == 1004
    assert (s.raw_cpu_clock, s.local_half_cycles, s.double_speed) == (1000, 0, False)
    assert (s.remaining_episode_half_cycles, s.remaining_instructions) == (
        before.remaining_episode_half_cycles,
        before.remaining_instructions,
    )
    assert s.closed and not s.pending_permit
    assert "unknown normalized interval" in s.terminal_reason


@pytest.mark.parametrize("action", ["cancel", "close"])
@pytest.mark.parametrize("initial", [False, True])
def test_lifecycle_during_execution_still_commits_observed_segments(action, initial):
    c = coordinator(initial)
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=4)
    permit = c.reserve(8 if initial else 4)
    getattr(c, action)()
    reason = c.snapshot().terminal_reason
    with pytest.raises(CoordinatorClosed):
        c.commit(
            permit,
            raw_cpu_clock=1004,
            instructions=1,
            speed_transition=SpeedTransition(1001, not initial),
        )
    s = assert_clock(c, 1004, 7 if initial else 5, not initial)
    assert s.closed and s.cancelled is (action == "cancel")
    assert s.terminal_reason == reason
    assert s.remaining_instructions == 3


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("explicit_speed", [False, True])
def test_zero_breakpoint_discard_preserves_debt_and_episode(initial, explicit_speed):
    c = coordinator(initial)
    raw = 1256 if initial else 1128
    c.commit(c.reserve(raw - 1000), raw_cpu_clock=raw, instructions=0)
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=4)
    c.commit(c.reserve(1), raw_cpu_clock=raw + 1, instructions=1)
    before = c.snapshot()
    assert before.debt_half_cycles > 0
    permit = c.reserve(4)
    kwargs = {"double_speed": initial} if explicit_speed else {}
    s = c.discard_unconsumed_permit(permit, raw_cpu_clock=raw + 1, **kwargs)
    assert s == before
    replacement = c.reserve(4)
    assert replacement is not permit
    assert replacement.instruction_cap == 3
    c.commit(replacement, raw_cpu_clock=raw + 2, instructions=1)
    assert c.snapshot().remaining_instructions == 2


@pytest.mark.parametrize("operation", ["commit", "discard_unconsumed_permit"])
@pytest.mark.parametrize("kind", ["cross-instance", "forged", "replayed", "committed"])
def test_permit_identity_for_segments_and_discard(operation, kind):
    c = coordinator()
    permit = c.reserve(4)
    if kind == "cross-instance":
        other = coordinator()
        supplied = other.reserve(4)
        other_before = other.snapshot()
    elif kind == "forged":
        supplied = replace(permit)
    else:
        if kind == "committed":
            c.commit(permit, raw_cpu_clock=1001, instructions=1)
        else:
            c.discard_unconsumed_permit(permit, raw_cpu_clock=1000)
        supplied = permit
        assert c.reserve(4) is not permit
    before = c.snapshot()
    kwargs = {"raw_cpu_clock": 1000}
    if operation == "commit":
        kwargs = {
            "raw_cpu_clock": 1004,
            "instructions": 1,
            "speed_transition": SpeedTransition(1002, True),
        }
    with pytest.raises(EmulatedTimeError):
        getattr(c, operation)(supplied, **kwargs)
    s = c.snapshot()
    assert s.closed
    assert (s.raw_cpu_clock, s.observed_raw_cpu_clock, s.local_half_cycles) == (
        before.raw_cpu_clock,
        before.observed_raw_cpu_clock,
        before.local_half_cycles,
    )
    if kind == "cross-instance":
        assert other.snapshot() == other_before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"raw_cpu_clock": 999},
        {"raw_cpu_clock": 1001},
        {"raw_cpu_clock": True},
        {"raw_cpu_clock": 1000.0},
        {"raw_cpu_clock": "1000"},
        {"raw_cpu_clock": -1},
        {"instructions": 1},
        {"instructions": -1},
        {"instructions": True},
        {"instructions": False},
        {"instructions": 0.0},
        {"instructions": None},
        {"double_speed": True},
        {"double_speed": 0},
        {"double_speed": 1},
        {"double_speed": "false"},
    ],
)
def test_invalid_discard_fails_without_fabricating_elapsed(kwargs):
    c = coordinator()
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=4)
    before = c.snapshot()
    permit = c.reserve(4)
    arguments = {"raw_cpu_clock": 1000, **kwargs}
    with pytest.raises(EmulatedTimeError):
        c.discard_unconsumed_permit(permit, **arguments)
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert (s.raw_cpu_clock, s.local_half_cycles, s.double_speed) == (1000, 0, False)
    assert s.remaining_instructions == before.remaining_instructions
    assert s.remaining_episode_half_cycles == before.remaining_episode_half_cycles
    raw = arguments["raw_cpu_clock"]
    assert s.observed_raw_cpu_clock == (raw if type(raw) is int and raw >= 0 else 1000)
    assert "unknown normalized interval" in s.terminal_reason


@pytest.mark.parametrize("initial", [False, True])
def test_same_rate_transition_is_valid_noop_metadata(initial):
    c = coordinator(initial)
    c.commit(
        c.reserve(4),
        raw_cpu_clock=1004,
        instructions=1,
        speed_transition=SpeedTransition(1001, initial),
    )
    assert_clock(c, 1004, 4 if initial else 8, initial)


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_zero_execution_discard_after_shutdown_releases_pending_only(action):
    c = coordinator(True)
    permit = c.reserve(4)
    getattr(c, action)()
    before = c.snapshot()
    with pytest.raises(CoordinatorClosed):
        c.discard_unconsumed_permit(permit, raw_cpu_clock=1000, double_speed=True)
    assert c.snapshot() == replace(before, pending_permit=False)


def test_snapshot_speed_is_immutable_and_detached_from_transition():
    c = coordinator()
    before = c.snapshot()
    with pytest.raises(FrozenInstanceError):
        before.double_speed = True
    c.commit(
        c.reserve(2),
        raw_cpu_clock=1002,
        instructions=1,
        speed_transition=SpeedTransition(1001, True),
    )
    assert before.double_speed is False
    assert c.snapshot().double_speed is True


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_malformed_transition_after_shutdown_preserves_original_reason(action):
    c = coordinator()
    permit = c.reserve(4)
    getattr(c, action)()
    before = c.snapshot()
    with pytest.raises(EmulatedTimeError):
        c.commit(
            permit, raw_cpu_clock=1004, instructions=1, speed_transition=SpeedTransition(1005, True)
        )
    assert c.snapshot() == replace(
        before,
        pending_permit=False,
        observed_raw_cpu_clock=1004,
    )


def test_known_half_overrun_charges_episode_and_instructions():
    c = coordinator(True)
    c.commit(c.reserve(256), raw_cpu_clock=1256, instructions=0)
    c.begin_episode("request-1", cycle_budget=8, instruction_cap=4)
    permit = c.reserve(4)
    assert (permit.cpu_cycles, permit.half_cycles) == (4, 4)
    with pytest.raises(EmulatedTimeError, match="half-cycle permit overrun"):
        c.commit(
            permit,
            raw_cpu_clock=1260,
            instructions=2,
            speed_transition=SpeedTransition(1256, False),
        )
    s = assert_clock(c, 1260, 264, False)
    assert s.closed
    assert (s.debt_half_cycles, s.remaining_episode_half_cycles, s.remaining_instructions) == (
        8,
        8,
        2,
    )
