"""Terminal-context contracts for the timed battle probe (#147).

Split from ``tests/test_timed_battle_probe.py`` for #147 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Direction/A attempt quotas and serial-role admission only;
never gameplay or commercial-ROM acceptance.
"""

import pytest

from tests._timed_battle_probe_support import (
    PHASES,
    terminal_driver,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("version", ["red_color", "blue_color", "yellow"])
@pytest.mark.parametrize("context", [None, "warp"])
@pytest.mark.parametrize("role,direction,facing", [(2, "right", 0x0C), (1, "left", 0x08)])
def test_terminal_faces_then_uses_a_until_actual_context_advance(
    probe, version, context, role, direction, facing
):
    session, _, driver = terminal_driver(probe, role=role, context=context, version=version)
    try:
        before = dict(session.memory.values)
        assert driver.before_step(frame_offset=0) == (direction, 8)
        snapshot = driver.snapshot()
        assert snapshot["interaction_observation"] == {
            "frame_offset": 0,
            "map": 0xF0,
            "link_state": 1,
            "serial_role": role,
            "x": 3 if role == 2 else 6,
            "y": 4,
            "facing": 0,
            "joy_ignore": 0,
            "walk_counter": 0,
            "status_flags5": 0,
            "admission_reason": "orient",
        }
        assert snapshot["last_action"] == {
            "frame_offset": 0,
            "button": direction,
            "duration": 8,
            "cadence": 30,
            "phase": context,
        }
        assert snapshot["hidden_event_quota_used"] == 1
        assert driver.before_step(frame_offset=29) is None
        assert session.memory.values == before
        # Only the test harness changes this synthetic observation; no CPU runs.
        session.set_bytes("wSpritePlayerStateData1FacingDirection", [facing])
        before = dict(session.memory.values)
        assert driver.before_step(frame_offset=30) == ("a", 4)
        assert driver.before_step(frame_offset=49) is None
        assert driver.before_step(frame_offset=50) == ("a", 4)
        assert session.memory.values == before
        snapshot = driver.snapshot()
        assert snapshot["interaction_observation"]["facing"] == facing
        assert snapshot["interaction_observation"]["admission_reason"] == "interact"
        assert snapshot["last_action"] == {
            "frame_offset": 50,
            "button": "a",
            "duration": 4,
            "cadence": 20,
            "phase": context,
        }
        assert (
            snapshot["hidden_event_quota_used"] == snapshot["input_attempts"]["hidden_event"] == 3
        )
        session.fire("CableClubLeftGameboy" if role == 2 else "CableClubRightGameboy")
        assert driver.snapshot()["phase"] == "dialogue"
        driver.before_step(frame_offset=70)
        assert driver.snapshot()["hidden_event_quota_used"] == 3
        session.fire("CableClub_DoBattleOrTrade")
        assert driver.before_step(frame_offset=100) is None
        assert driver.objective_complete() is False  # Hook is not gameplay proof.
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role,facing", [(2, 0x0C), (1, 0x08)])
@pytest.mark.parametrize(
    "blocked",
    [
        "map",
        "link",
        "event_tile",
        "y",
        "joy",
        "walk",
        "status04",
        "status80",
        "status84",
        "peer_room",
        "peer_party",
    ],
)
def test_terminal_invalid_admission_waits_without_spending_attempts(probe, role, facing, blocked):
    session, readiness, driver = terminal_driver(probe, role=role, context="warp", facing=facing)
    faults = {
        "map": ("wCurMap", 0xEF),
        "link": ("wLinkState", 0),
        "event_tile": ("wXCoord", 4 if role == 2 else 5),
        "y": ("wYCoord", 3),
        "joy": ("wJoyIgnore", 1),
        "walk": ("wWalkCounter", 1),
        "status04": ("wStatusFlags5", 0x04),
        "status80": ("wStatusFlags5", 0x80),
        "status84": ("wStatusFlags5", 0x84),
    }
    try:
        # Establish observed room readiness without permitting an input first.
        readiness.peer.remove("colosseum_reached")
        assert driver.before_step(frame_offset=0) is None
        readiness.peer.add("colosseum_reached")
        if blocked.startswith("peer_"):
            readiness.peer.remove(
                "colosseum_reached" if blocked == "peer_room" else "party_qualified"
            )
        else:
            name, value = faults[blocked]
            original = session.memory.values[session.symbols.addr_of(name)]
            session.set_bytes(name, [value])
        before = dict(session.memory.values)
        for frame in range(20, 420, 20):
            assert driver.before_step(frame_offset=frame) is None
        assert session.memory.values == before
        snapshot = driver.snapshot()
        assert snapshot["hidden_event_quota_used"] == 0
        assert snapshot["input_attempts"].get("hidden_event", 0) == 0
        assert snapshot["last_action"] is None
        reasons = {
            "map": "room_not_ready",
            "link": "room_not_ready",
            "event_tile": "unexpected_adjacent_coordinates",
            "y": "unexpected_adjacent_coordinates",
            "joy": "input_masked_or_walking",
            "walk": "input_masked_or_walking",
            "status04": "a_blocked_or_scripted_movement",
            "status80": "a_blocked_or_scripted_movement",
            "status84": "a_blocked_or_scripted_movement",
            "peer_room": "peer_room_not_ready",
        }
        if blocked in reasons:
            assert snapshot["interaction_observation"]["admission_reason"] == reasons[blocked]
        if blocked.startswith("peer_"):
            readiness.peer.update(PHASES)
        else:
            session.set_bytes(name, [original])
        # All six attempts must still be available after arbitrarily many waits.
        for frame in range(500, 620, 20):
            assert driver.before_step(frame_offset=frame) == ("a", 4)
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role,direction,facing", [(2, "right", 0x0C), (1, "left", 0x08)])
@pytest.mark.parametrize("context", [None, "warp"])
def test_terminal_direction_and_a_share_exactly_six_attempts(
    probe, role, direction, facing, context
):
    session, _, driver = terminal_driver(probe, role=role, context=context)
    try:
        for frame in (0, 30, 60):
            assert driver.before_step(frame_offset=frame) == (direction, 8)
        if context is None:
            session.fire(
                "PrepareForSpecialWarp"
            )  # Context change must not reset the shared budget.
        session.set_bytes("wSpritePlayerStateData1FacingDirection", [facing])
        for frame in (90, 110, 130):
            assert driver.before_step(frame_offset=frame) == ("a", 4)
        try:
            action = driver.before_step(frame_offset=150)
        except RuntimeError as exc:
            assert "quota" in str(exc).lower() or "budget" in str(exc).lower()
        else:
            assert action is None, "facing and A must not receive separate six-attempt budgets"
        assert driver.snapshot()["hidden_event_quota_used"] == 6
        assert driver.snapshot()["input_attempts"]["hidden_event"] == 6
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role,facing", [(2, 0x0C), (1, 0x08)])
@pytest.mark.parametrize("status", [0x01, 0x02, 0x08, 0x40, 0x7B])
def test_terminal_status_mask_does_not_block_unrelated_bits(probe, role, facing, status):
    session, _, driver = terminal_driver(probe, role=role, facing=facing)
    try:
        session.set_bytes("wStatusFlags5", [status])
        assert driver.before_step(frame_offset=0) == ("a", 4)
        observed = driver.snapshot()["interaction_observation"]
        assert observed["status_flags5"] == status
        assert observed["admission_reason"] == "interact"
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role", [0, 3])
def test_terminal_invalid_serial_role_fails_before_input_or_quota(probe, role):
    session, _, driver = terminal_driver(probe, role=role)
    try:
        with pytest.raises(ValueError, match="serial role"):
            driver.before_step(frame_offset=0)
        snapshot = driver.snapshot()
        assert snapshot["hidden_event_quota_used"] == 0
        assert snapshot["last_action"] is None
        assert snapshot["unsupported_reason"]
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()
