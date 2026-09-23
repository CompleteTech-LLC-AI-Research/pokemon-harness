"""ROM-free ordered action-timeline and turn-accounting tests (#94).

Split from ``tests/test_battle_item_evidence.py`` for #157 with no behavior
change: every assertion and test ID below is preserved verbatim, and the
expectation values stay literal so a wrong formula, amount, status mask,
ordering, or continuation rule fails the test rather than being derived from
the helper under test.
"""

from __future__ import annotations

import pytest

from tests._battle_item_evidence import (
    CANCELLATION_CONTINUATION,
    EVENT_APPLICATION,
    EVENT_COMMAND_SELECTION,
    EVENT_ITEM_SELECTION,
    EVENT_NEXT_COMMAND,
    EVENT_OPPONENT_ACTION,
    EVENT_PLAYER_MOVE,
    EVENT_REJECTION,
    EVENT_REPLACEMENT,
    EVENT_TERMINAL,
    NO_EFFECT_CONTINUATION,
    POTION_HEAL,
    REASON_FULL_HP,
    REASON_WRONG_STATUS,
    SUCCESSFUL_ITEM_CONTINUATION,
    TURN_CONSUMING_FAILURE_CONTINUATION,
    UNAVAILABLE_ITEM_CONTINUATION,
    ActionTimeline,
    account_timeline,
    assert_no_duplicate_consumption,
    assert_successful_item_turn,
    item_effect_hp_delta,
    later_hp_delta,
)

# ---------------------------------------------------------------------------
# Ordered action timeline and turn accounting (#94)
# ---------------------------------------------------------------------------


def make_successful_timeline() -> ActionTimeline:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(
        EVENT_APPLICATION,
        item_id=1,
        target_slot=0,
        consumed=1,
        consumes_action=True,
        hp_delta=POTION_HEAL,
    )
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-5)
    timeline.record(EVENT_NEXT_COMMAND)
    return timeline


def test_successful_item_use_spends_the_turn_and_allows_one_opponent_action() -> None:
    account = assert_successful_item_turn(
        make_successful_timeline(), item_id=1, target_slot=0, expected_hp_delta=POTION_HEAL
    )
    assert account.consumes_player_action is True
    assert account.item_consumed == 1
    assert account.opponent_actions == 1
    assert account.player_moves == 0
    assert account.pp_decrements == 0
    assert account.applied is True


def test_effects_are_at_the_application_boundary_and_opponent_damage_separate() -> None:
    timeline = make_successful_timeline()
    assert item_effect_hp_delta(timeline) == POTION_HEAL
    assert later_hp_delta(timeline) == -5


def test_hidden_player_move_is_rejected() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_APPLICATION, item_id=1, target_slot=0, consumed=1, consumes_action=True)
    timeline.record(EVENT_PLAYER_MOVE)
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-5)
    timeline.record(EVENT_NEXT_COMMAND)
    with pytest.raises(AssertionError, match="hidden player move"):
        account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)


def test_pp_decrement_on_an_item_turn_is_rejected() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(
        EVENT_APPLICATION,
        item_id=1,
        target_slot=0,
        consumed=1,
        consumes_action=True,
        pp_decrements=1,
    )
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-5)
    timeline.record(EVENT_NEXT_COMMAND)
    with pytest.raises(AssertionError, match="must not decrement move PP"):
        account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)


def test_extra_or_early_opponent_action_is_rejected() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_APPLICATION, item_id=1, target_slot=0, consumed=1, consumes_action=True)
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-5)
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-1)
    timeline.record(EVENT_NEXT_COMMAND)
    with pytest.raises(AssertionError, match="expects 1 opponent action"):
        account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)

    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-3)
    timeline.record(EVENT_APPLICATION, item_id=1, target_slot=0, consumed=1, consumes_action=True)
    timeline.record(EVENT_NEXT_COMMAND)
    with pytest.raises(AssertionError, match="precedes the item application boundary"):
        account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)


def test_no_effect_continuation_is_a_declared_input_not_a_universal() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_REJECTION, item_id=1, target_slot=0, reason=REASON_FULL_HP)
    timeline.record(EVENT_NEXT_COMMAND)

    account = account_timeline(timeline, continuation=NO_EFFECT_CONTINUATION)
    assert account.consumes_player_action is False
    assert account.item_consumed == 0
    assert account.opponent_actions == 0
    assert account.applied is False
    assert account.reason == REASON_FULL_HP

    with pytest.raises(AssertionError, match="expects application"):
        account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)


def test_turn_consuming_failure_is_expressible_as_a_separate_rule() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(
        EVENT_REJECTION, item_id=1, target_slot=0, consumes_action=True, reason=REASON_WRONG_STATUS
    )
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-3)
    timeline.record(EVENT_NEXT_COMMAND)

    account = account_timeline(timeline, continuation=TURN_CONSUMING_FAILURE_CONTINUATION)
    assert account.consumes_player_action is True
    assert account.item_consumed == 0
    assert account.opponent_actions == 1

    with pytest.raises(AssertionError, match="expects 0 opponent action"):
        account_timeline(timeline, continuation=NO_EFFECT_CONTINUATION)


def test_cancellation_and_unavailable_item_continuations() -> None:
    for continuation, reason in (
        (CANCELLATION_CONTINUATION, "cancelled"),
        (UNAVAILABLE_ITEM_CONTINUATION, "unavailable"),
    ):
        timeline = ActionTimeline()
        timeline.record(EVENT_COMMAND_SELECTION)
        timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
        timeline.record(EVENT_REJECTION, item_id=1, target_slot=0, reason=reason)
        timeline.record(EVENT_NEXT_COMMAND)
        account = account_timeline(timeline, continuation=continuation)
        assert account.item_consumed == 0
        assert account.opponent_actions == 0
        assert account.applied is False


def test_replacement_and_terminal_boundaries_are_accepted() -> None:
    terminal = ActionTimeline()
    terminal.record(EVENT_COMMAND_SELECTION)
    terminal.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=1)
    terminal.record(EVENT_REJECTION, item_id=1, target_slot=1, reason="cancelled")
    terminal.record(EVENT_TERMINAL)
    assert account_timeline(terminal, continuation=CANCELLATION_CONTINUATION).item_consumed == 0

    replacement = ActionTimeline()
    replacement.record(EVENT_COMMAND_SELECTION)
    replacement.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=1)
    replacement.record(EVENT_REJECTION, item_id=1, target_slot=1, reason="cancelled")
    replacement.record(EVENT_REPLACEMENT)
    account = account_timeline(replacement, continuation=CANCELLATION_CONTINUATION)
    assert account.item_consumed == 0
    assert account.opponent_actions == 0


def test_repeated_or_delayed_input_cannot_consume_the_same_unit_twice() -> None:
    timeline = make_successful_timeline()
    timeline.record(EVENT_APPLICATION, item_id=1, target_slot=0, consumed=1, consumes_action=True)
    with pytest.raises(AssertionError):
        assert_no_duplicate_consumption(timeline)
    # The declared-rule account also fails because there are two outcomes.
    with pytest.raises(AssertionError, match="exactly one application or rejection"):
        account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)


def test_non_outcome_event_cannot_account_for_consumption() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION, consumed=1)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_REJECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_NEXT_COMMAND)
    with pytest.raises(AssertionError, match="only the item outcome"):
        account_timeline(timeline, continuation=CANCELLATION_CONTINUATION)


def test_timeline_requires_exactly_one_selection_and_one_boundary() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_REJECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_NEXT_COMMAND)
    with pytest.raises(AssertionError, match="exactly one item/target selection"):
        account_timeline(timeline, continuation=CANCELLATION_CONTINUATION)

    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    timeline.record(EVENT_ITEM_SELECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_REJECTION, item_id=1, target_slot=0)
    timeline.record(EVENT_NEXT_COMMAND)
    timeline.record(EVENT_TERMINAL)
    with pytest.raises(AssertionError, match="exactly one boundary"):
        account_timeline(timeline, continuation=CANCELLATION_CONTINUATION)


def test_event_consumption_and_kind_are_validated() -> None:
    with pytest.raises(ValueError, match="unknown timeline event kind"):
        ActionTimeline().record("not_a_kind")
    with pytest.raises(ValueError, match="event consumed count"):
        ActionTimeline().record(EVENT_APPLICATION, consumed=2)
    with pytest.raises(ValueError, match="event PP decrements"):
        ActionTimeline().record(EVENT_APPLICATION, pp_decrements=-1)


def test_successful_item_turn_rejects_wrong_item_or_target_identity() -> None:
    with pytest.raises(AssertionError, match="selected item/target"):
        assert_successful_item_turn(
            make_successful_timeline(), item_id=2, target_slot=0, expected_hp_delta=POTION_HEAL
        )
    with pytest.raises(AssertionError, match="selected item/target"):
        assert_successful_item_turn(
            make_successful_timeline(), item_id=1, target_slot=3, expected_hp_delta=POTION_HEAL
        )
    with pytest.raises(AssertionError, match="application HP delta"):
        assert_successful_item_turn(
            make_successful_timeline(), item_id=1, target_slot=0, expected_hp_delta=21
        )


def test_timeline_selection_identity_must_match_the_application_outcome() -> None:
    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION)
    # The selection names item 2 / slot 1 but the applied outcome is item 1 / slot 0.
    timeline.record(EVENT_ITEM_SELECTION, item_id=2, target_slot=1)
    timeline.record(EVENT_APPLICATION, item_id=1, target_slot=0, consumed=1, consumes_action=True)
    timeline.record(EVENT_OPPONENT_ACTION, hp_delta=-5)
    timeline.record(EVENT_NEXT_COMMAND)

    with pytest.raises(AssertionError, match="identity does not match"):
        account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)
