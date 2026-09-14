"""ROM-free unit tests for the shared battle-item contract (#93/#94).

Every expected HP/status/inventory value below is written literally so a wrong
formula, amount, status mask, ordering, or continuation rule fails the test
rather than being derived from the helper under test.
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
    HYPER_POTION_HEAL,
    INVALID_TARGET_REASONS,
    NO_EFFECT_CONTINUATION,
    POTION_HEAL,
    REASON_APPLIED,
    REASON_FAINTED_NON_REVIVE,
    REASON_FULL_HP,
    REASON_FULL_HP_NO_STATUS,
    REASON_REVIVE_ON_LIVING,
    REASON_WRONG_STATUS,
    STATUS_ALL,
    STATUS_BURN,
    STATUS_FREEZE,
    STATUS_PARALYSIS,
    STATUS_POISON,
    STATUS_SLEEP_MASK,
    SUCCESSFUL_ITEM_CONTINUATION,
    SUPER_POTION_HEAL,
    TURN_CONSUMING_FAILURE_CONTINUATION,
    UNAVAILABLE_ITEM_CONTINUATION,
    VALID_NO_EFFECT_REASONS,
    ActionTimeline,
    ActiveBattleSnapshot,
    BagSnapshot,
    BagStack,
    ItemUseSnapshot,
    MedicineRule,
    PartyMonSnapshot,
    account_timeline,
    assert_active_battle_refresh,
    assert_continuation_idempotent,
    assert_last_unit_compaction,
    assert_medicine_application,
    assert_no_duplicate_consumption,
    assert_selected_target_changes_only,
    assert_single_consumption,
    assert_successful_item_turn,
    capped_heal,
    cure_status,
    item_effect_hp_delta,
    later_hp_delta,
    medicine_outcome,
    revive_hp,
    status_matches_mask,
    validate_snapshot,
)


def _bag(order: tuple[tuple[int, int], ...]) -> BagSnapshot:
    return BagSnapshot(tuple(BagStack(item_id, quantity) for item_id, quantity in order))


def _mon(
    slot: int,
    *,
    species: int = 25,
    level: int = 10,
    hp: int,
    max_hp: int,
    status: int = 0,
    moves: tuple[int, int, int, int] = (1, 2, 3, 4),
    pp: tuple[int, int, int, int] = (10, 10, 10, 10),
) -> PartyMonSnapshot:
    return PartyMonSnapshot(
        slot=slot,
        species=species,
        level=level,
        hp=hp,
        max_hp=max_hp,
        status=status,
        moves=moves,
        pp=pp,
    )


def _active(player_slot: int, mon: PartyMonSnapshot) -> ActiveBattleSnapshot:
    return ActiveBattleSnapshot(
        player_slot=player_slot,
        species=mon.species,
        level=mon.level,
        hp=mon.hp,
        max_hp=mon.max_hp,
        status=mon.status,
        moves=mon.moves,
        pp=mon.pp,
    )


def _snapshot(
    item_id: int,
    target_slot: int,
    bag: BagSnapshot,
    party: list[PartyMonSnapshot],
    active: ActiveBattleSnapshot,
) -> ItemUseSnapshot:
    return ItemUseSnapshot(
        item_id=item_id,
        target_slot=target_slot,
        bag=bag,
        party=tuple(party),
        active=active,
    )


def _potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, fixed_heal=POTION_HEAL)


def _super_potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, fixed_heal=SUPER_POTION_HEAL)


def _hyper_potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, fixed_heal=HYPER_POTION_HEAL)


def _max_potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, heal_to_max=True)


def _revive(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, revive=True)


def _max_revive(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, revive=True, heal_to_max=True)


def _full_restore(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, heal_to_max=True, cure_mask=STATUS_ALL)


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def test_bag_snapshot_records_ordered_stacks_and_position_lookup() -> None:
    bag = _bag(((2, 1), (1, 3), (4, 9)))
    assert bag.quantity_of(1) == 3
    assert bag.quantity_of(4) == 9
    assert bag.quantity_of(3) == 0
    assert bag.has(2) is True
    assert bag.has(3) is False
    assert bag.stacks[1] == BagStack(1, 3)


def test_snapshot_validation_rejects_duplicate_or_malformed_records() -> None:
    active = _active(0, _mon(0, hp=10, max_hp=10))
    with pytest.raises(ValueError, match="duplicate bag item stack"):
        validate_snapshot(
            _snapshot(1, 0, _bag(((1, 1), (1, 2))), [_mon(0, hp=10, max_hp=10)], active)
        )
    with pytest.raises(ValueError, match="invalid bag terminator"):
        validate_snapshot(
            _snapshot(
                1,
                0,
                BagSnapshot((BagStack(1, 1),), terminator=0x00),
                [_mon(0, hp=10, max_hp=10)],
                active,
            )
        )
    with pytest.raises(ValueError, match="party HP exceeds max HP"):
        validate_snapshot(_snapshot(1, 0, _bag(((1, 1),)), [_mon(0, hp=11, max_hp=10)], active))
    with pytest.raises(ValueError, match="party slots must be contiguous"):
        validate_snapshot(
            _snapshot(
                1,
                1,
                _bag(((1, 1),)),
                [_mon(0, hp=10, max_hp=10), _mon(2, hp=10, max_hp=10)],
                active,
            )
        )


# ---------------------------------------------------------------------------
# Inventory consumption (#93)
# ---------------------------------------------------------------------------


def test_successful_consumption_decrements_one_from_a_larger_stack() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((4, 2), (1, 3), (5, 1))), party, _active(0, party[0]))
    after_party = [_mon(0, hp=50, max_hp=100)]
    after = _snapshot(1, 0, _bag(((4, 2), (1, 2), (5, 1))), after_party, _active(0, after_party[0]))

    assert_single_consumption(before, after, 1, expected_consumed=1)
    assert before.bag.quantity_of(1) - after.bag.quantity_of(1) == 1


def test_last_unit_is_removed_compacted_and_terminator_preserved() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((4, 2), (1, 1), (5, 1))), party, _active(0, party[0]))
    after_party = [_mon(0, hp=50, max_hp=100)]
    after = _snapshot(1, 0, _bag(((4, 2), (5, 1))), after_party, _active(0, after_party[0]))

    assert_last_unit_compaction(before, after, 1)
    assert after.bag.stacks == (BagStack(4, 2), BagStack(5, 1))
    assert after.bag.terminator == 0xFF


def test_stale_bag_count_after_removal_is_rejected() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((4, 2), (1, 1), (5, 1))), party, _active(0, party[0]))
    # The last unit was removed and the stack list compacted to two entries,
    # but the ROM count byte was not updated and still reads three.
    stale = _snapshot(
        1,
        0,
        BagSnapshot((BagStack(4, 2), BagStack(5, 1)), raw_count=3, terminator_position=3),
        [_mon(0, hp=50, max_hp=100)],
        _active(0, party[0]),
    )

    with pytest.raises(ValueError, match="raw count does not match"):
        assert_last_unit_compaction(before, stale, 1)


def test_terminator_not_after_last_stack_is_rejected() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((4, 2), (1, 1), (5, 1))), party, _active(0, party[0]))
    stale = _snapshot(
        1,
        0,
        BagSnapshot((BagStack(4, 2), BagStack(5, 1)), raw_count=2, terminator_position=3),
        [_mon(0, hp=50, max_hp=100)],
        _active(0, party[0]),
    )

    with pytest.raises(ValueError, match="terminator does not immediately follow"):
        assert_last_unit_compaction(before, stale, 1)


def test_no_effect_and_cancelled_branches_consume_zero_and_do_not_reorder() -> None:
    party = [_mon(0, hp=100, max_hp=100)]
    before = _snapshot(1, 0, _bag(((4, 2), (1, 3))), party, _active(0, party[0]))
    after = _snapshot(1, 0, _bag(((4, 2), (1, 3))), party, _active(0, party[0]))

    assert_single_consumption(before, after, 1, expected_consumed=0)
    assert_single_consumption(before, after, 1, expected_consumed=0)


def test_double_consumption_across_a_continuation_is_rejected() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((1, 3),)), party, _active(0, party[0]))
    # A buggy driver advances text twice and decrements twice.
    after = _snapshot(1, 0, _bag(((1, 1),)), party, _active(0, party[0]))

    with pytest.raises(AssertionError, match="observed 2"):
        assert_single_consumption(before, after, 1, expected_consumed=1)


def test_settled_snapshot_does_not_reconsume_on_text_advance() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((1, 2),)), party, _active(0, party[0]))
    after_party = [_mon(0, hp=50, max_hp=100)]
    after = _snapshot(1, 0, _bag(((1, 1),)), after_party, _active(0, after_party[0]))
    continued = _snapshot(1, 0, _bag(((1, 1),)), after_party, _active(0, after_party[0]))

    assert_continuation_idempotent(before, after, continued, 1)
    assert continued.bag.quantity_of(1) == 1


def test_continuation_detects_a_second_decrement_after_text_advance() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((1, 3),)), party, _active(0, party[0]))
    after_party = [_mon(0, hp=50, max_hp=100)]
    after = _snapshot(1, 0, _bag(((1, 2),)), after_party, _active(0, after_party[0]))
    # The bug: advancing the continuation text decrements the settled stack again.
    continued = _snapshot(1, 0, _bag(((1, 1),)), after_party, _active(0, after_party[0]))

    with pytest.raises(AssertionError, match="observed 1"):
        assert_continuation_idempotent(before, after, continued, 1)


def test_inventory_underflow_or_unexpected_stack_change_is_rejected() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((1, 1),)), party, _active(0, party[0]))
    grew = _snapshot(1, 0, _bag(((1, 2),)), party, _active(0, party[0]))
    with pytest.raises(AssertionError, match="observed -1"):
        assert_single_consumption(before, grew, 1, expected_consumed=1)

    duplicated = _snapshot(1, 0, _bag(((1, 1), (9, 1))), party, _active(0, party[0]))
    after = _snapshot(1, 0, _bag(((9, 2),)), party, _active(0, party[0]))
    with pytest.raises(AssertionError, match="beyond the single consumption"):
        assert_single_consumption(duplicated, after, 1, expected_consumed=1)


def test_consuming_an_absent_or_zero_quantity_stack_is_rejected() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    before = _snapshot(1, 0, _bag(((4, 2),)), party, _active(0, party[0]))
    after = _snapshot(1, 0, _bag(((4, 2),)), party, _active(0, party[0]))
    with pytest.raises(AssertionError, match="inventory underflow"):
        assert_single_consumption(before, after, 1, expected_consumed=1)
    with pytest.raises(ValueError, match="invalid bag quantity"):
        validate_snapshot(
            _snapshot(1, 0, BagSnapshot((BagStack(1, 0),)), party, _active(0, party[0]))
        )


def test_expected_consumption_must_be_declared_and_bounded() -> None:
    party = [_mon(0, hp=30, max_hp=100)]
    snap = _snapshot(1, 0, _bag(((1, 1),)), party, _active(0, party[0]))
    with pytest.raises(ValueError, match="exactly 0 or 1"):
        assert_single_consumption(snap, snap, 1, expected_consumed=2)
    with pytest.raises(AssertionError, match="different item"):
        assert_single_consumption(snap, snap, 2, expected_consumed=0)


# ---------------------------------------------------------------------------
# Target identity and active/benched isolation
# ---------------------------------------------------------------------------


def test_two_same_species_members_remain_distinguishable_by_slot() -> None:
    bag_before = _bag(((1, 2),))
    bag_after = _bag(((1, 1),))
    active_mon = _mon(0, species=54, hp=40, max_hp=40)
    benched_before = _mon(1, species=54, hp=5, max_hp=40)
    benched_after = _mon(1, species=54, hp=25, max_hp=40)
    before = _snapshot(1, 1, bag_before, [active_mon, benched_before], _active(0, active_mon))
    after = _snapshot(1, 1, bag_after, [active_mon, benched_after], _active(0, active_mon))

    outcome = assert_medicine_application(before, after, _potion(1), expected_consumed=1)
    assert outcome.hp == 25
    assert after.mon(0) == active_mon
    assert after.mon(1).hp == 25


def test_benched_target_never_overwrites_active_identity_or_hp() -> None:
    active_mon = _mon(0, species=25, level=20, hp=80, max_hp=80)
    target_before = _mon(1, species=4, level=5, hp=10, max_hp=20)
    target_after = _mon(1, species=4, level=5, hp=20, max_hp=20)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, target_before], _active(0, active_mon))
    after = _snapshot(1, 1, _bag(()), [active_mon, target_after], _active(0, active_mon))

    assert_medicine_application(before, after, _super_potion(1), expected_consumed=1)


def test_active_target_refresh_mirrors_hp_and_status_only() -> None:
    target_before = _mon(0, species=25, hp=30, max_hp=100)
    target_after = _mon(0, species=25, hp=50, max_hp=100)
    before = _snapshot(1, 0, _bag(((1, 1),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(()), [target_after], _active(0, target_after))

    assert_active_battle_refresh(before, after)


def test_active_refresh_rejects_benched_target_leaking_into_active_data() -> None:
    active_mon = _mon(0, species=25, hp=80, max_hp=80)
    target_before = _mon(1, species=4, hp=10, max_hp=20)
    target_after = _mon(1, species=4, hp=20, max_hp=20)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, target_before], _active(0, active_mon))
    # The bug: the benched target's HP/identity is copied onto the active record.
    leaked_active = _active(0, target_after)
    after = _snapshot(1, 1, _bag(()), [active_mon, target_after], leaked_active)

    with pytest.raises(AssertionError, match="benched item target refreshed"):
        assert_active_battle_refresh(before, after)


def test_active_refresh_rejects_swapped_active_identity() -> None:
    target_before = _mon(0, species=25, hp=30, max_hp=100)
    target_after = _mon(0, species=25, hp=50, max_hp=100)
    before = _snapshot(1, 0, _bag(((1, 1),)), [target_before], _active(0, target_before))
    swapped = _active(0, target_after)
    swapped = ActiveBattleSnapshot(
        player_slot=swapped.player_slot,
        species=4,
        level=swapped.level,
        hp=swapped.hp,
        max_hp=swapped.max_hp,
        status=swapped.status,
        moves=swapped.moves,
        pp=swapped.pp,
    )
    after = _snapshot(1, 0, _bag(()), [target_after], swapped)

    with pytest.raises(AssertionError, match="active battle species changed"):
        assert_active_battle_refresh(before, after)


def test_active_refresh_rejects_active_max_hp_corruption() -> None:
    target_before = _mon(0, species=25, hp=30, max_hp=100)
    target_after = _mon(0, species=25, hp=50, max_hp=100)
    before = _snapshot(1, 0, _bag(((1, 1),)), [target_before], _active(0, target_before))
    # The bug: the item doubles the active battle max HP while party max HP stays 100.
    corrupted = ActiveBattleSnapshot(
        player_slot=0,
        species=target_after.species,
        level=target_after.level,
        hp=target_after.hp,
        max_hp=200,
        status=target_after.status,
        moves=target_after.moves,
        pp=target_after.pp,
    )
    after = _snapshot(1, 0, _bag(()), [target_after], corrupted)

    with pytest.raises(AssertionError, match="active battle max HP"):
        assert_active_battle_refresh(before, after)


def test_selected_target_changes_only_rejects_other_slot_mutation() -> None:
    target = _mon(1, hp=10, max_hp=20)
    other_before = _mon(0, hp=40, max_hp=40)
    other_after = _mon(0, hp=39, max_hp=40)
    before = _snapshot(1, 1, _bag(((1, 1),)), [other_before, target], _active(0, other_before))
    after = _snapshot(1, 1, _bag(()), [other_after, target], _active(0, other_before))

    with pytest.raises(AssertionError, match="unselected party slot 0 changed"):
        assert_selected_target_changes_only(before, after)


# ---------------------------------------------------------------------------
# Medicine math and branches
# ---------------------------------------------------------------------------


def test_fixed_heal_formula_below_and_at_cap() -> None:
    assert capped_heal(30, 100, POTION_HEAL) == 50
    assert capped_heal(80, 100, SUPER_POTION_HEAL) == 100
    assert capped_heal(1, 100, HYPER_POTION_HEAL) == 100


def test_fixed_heal_multi_byte_max_hp_without_byte_order_error() -> None:
    assert capped_heal(90, 300, HYPER_POTION_HEAL) == 290
    assert capped_heal(150, 300, HYPER_POTION_HEAL) == 300
    assert capped_heal(120, 300, 50) == 170


def test_revive_and_max_revive_amounts() -> None:
    assert revive_hp(301, to_max=False) == 150
    assert revive_hp(300, to_max=False) == 150
    assert revive_hp(300, to_max=True) == 300
    assert revive_hp(1, to_max=False) == 0


def test_pinned_amounts_and_status_bytes_are_literal() -> None:
    assert POTION_HEAL == 20
    assert SUPER_POTION_HEAL == 50
    assert HYPER_POTION_HEAL == 200
    assert STATUS_SLEEP_MASK == 0x07
    assert STATUS_POISON == 0x08
    assert STATUS_BURN == 0x10
    assert STATUS_FREEZE == 0x20
    assert STATUS_PARALYSIS == 0x40
    assert STATUS_ALL == 0xFF


def test_cure_status_zeroes_the_entire_byte_when_eligible() -> None:
    poisoned_burned = STATUS_POISON | STATUS_BURN
    assert cure_status(poisoned_burned, STATUS_POISON) == 0
    assert cure_status(poisoned_burned, STATUS_ALL) == 0
    assert cure_status(STATUS_PARALYSIS, STATUS_BURN) == STATUS_PARALYSIS
    assert cure_status(0x4F, 0x01) == 0
    assert status_matches_mask(poisoned_burned, STATUS_POISON) is True
    assert status_matches_mask(poisoned_burned, STATUS_PARALYSIS) is False


def test_potion_heals_below_cap_and_is_applied() -> None:
    target_before = _mon(0, hp=30, max_hp=100)
    target_after = _mon(0, hp=50, max_hp=100)
    before = _snapshot(1, 0, _bag(((1, 2),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(((1, 1),)), [target_after], _active(0, target_after))

    outcome = assert_medicine_application(before, after, _potion(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp, outcome.reason) == (True, 50, REASON_APPLIED)


def test_potion_clamps_at_cap_and_does_not_charge_extra() -> None:
    target_before = _mon(0, hp=90, max_hp=100)
    target_after = _mon(0, hp=100, max_hp=100)
    before = _snapshot(1, 0, _bag(((1, 3),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(((1, 2),)), [target_after], _active(0, target_after))

    outcome = assert_medicine_application(before, after, _potion(1), expected_consumed=1)
    assert outcome.hp == 100


def test_super_potion_heals_its_full_fifty_below_the_cap() -> None:
    target_before = _mon(0, hp=30, max_hp=200)
    target_after = _mon(0, hp=80, max_hp=200)
    before = _snapshot(1, 0, _bag(((1, 2),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(((1, 1),)), [target_after], _active(0, target_after))

    outcome = assert_medicine_application(before, after, _super_potion(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp) == (True, 80)


def test_max_potion_restores_to_max() -> None:
    target_before = _mon(0, hp=10, max_hp=300)
    target_after = _mon(0, hp=300, max_hp=300)
    before = _snapshot(1, 0, _bag(((1, 1),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(()), [target_after], _active(0, target_after))

    outcome = assert_medicine_application(before, after, _max_potion(1), expected_consumed=1)
    assert outcome.hp == 300


def test_revive_restores_floor_half_max_hp_on_a_fainted_member() -> None:
    active_mon = _mon(0, species=25, hp=80, max_hp=80)
    fainted = _mon(1, species=4, hp=0, max_hp=301)
    revived = _mon(1, species=4, hp=150, max_hp=301)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, fainted], _active(0, active_mon))
    after = _snapshot(1, 1, _bag(()), [active_mon, revived], _active(0, active_mon))

    outcome = assert_medicine_application(before, after, _revive(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp) == (True, 150)


def test_max_revive_restores_full_max_hp() -> None:
    active_mon = _mon(0, species=25, hp=80, max_hp=80)
    fainted = _mon(1, species=4, hp=0, max_hp=300)
    revived = _mon(1, species=4, hp=300, max_hp=300)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, fainted], _active(0, active_mon))
    after = _snapshot(1, 1, _bag(()), [active_mon, revived], _active(0, active_mon))

    outcome = assert_medicine_application(before, after, _max_revive(1), expected_consumed=1)
    assert outcome.hp == 300


def test_full_restore_heals_and_cures_status() -> None:
    target_before = _mon(0, hp=50, max_hp=200, status=STATUS_BURN | STATUS_PARALYSIS)
    target_after = _mon(0, hp=200, max_hp=200, status=0)
    before = _snapshot(1, 0, _bag(((1, 1),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(()), [target_after], _active(0, target_after))

    outcome = assert_medicine_application(before, after, _full_restore(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp, outcome.status) == (True, 200, 0)


def test_full_restore_at_full_hp_with_status_only_cures() -> None:
    target_before = _mon(0, hp=200, max_hp=200, status=STATUS_BURN)
    target_after = _mon(0, hp=200, max_hp=200, status=0)
    before = _snapshot(1, 0, _bag(((1, 1),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(()), [target_after], _active(0, target_after))

    outcome = assert_medicine_application(before, after, _full_restore(1), expected_consumed=1)
    assert (outcome.applied, outcome.status, outcome.reason) == (True, 0, REASON_APPLIED)


def test_full_restore_at_full_hp_without_status_is_a_no_effect() -> None:
    mon = _mon(0, hp=200, max_hp=200, status=0)
    before = _snapshot(1, 0, _bag(((1, 1),)), [mon], _active(0, mon))
    after = _snapshot(1, 0, _bag(((1, 1),)), [mon], _active(0, mon))

    outcome = assert_medicine_application(before, after, _full_restore(1), expected_consumed=0)
    assert outcome.applied is False
    assert outcome.reason == REASON_FULL_HP_NO_STATUS


def test_wrong_status_medicine_has_no_effect() -> None:
    burned = _mon(0, hp=20, max_hp=40, status=STATUS_BURN)
    before = _snapshot(1, 0, _bag(((1, 1),)), [burned], _active(0, burned))
    after = _snapshot(1, 0, _bag(((1, 1),)), [burned], _active(0, burned))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=STATUS_POISON), expected_consumed=0
    )
    assert (outcome.applied, outcome.reason) == (False, REASON_WRONG_STATUS)


def test_matching_status_medicine_cures_and_consumes_one() -> None:
    poisoned = _mon(0, hp=20, max_hp=40, status=STATUS_POISON)
    cured = _mon(0, hp=20, max_hp=40, status=0)
    before = _snapshot(1, 0, _bag(((1, 1),)), [poisoned], _active(0, poisoned))
    after = _snapshot(1, 0, _bag(()), [cured], _active(0, cured))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=STATUS_POISON), expected_consumed=1
    )
    assert outcome.applied is True


def test_status_only_medicine_cures_a_fainted_target_and_zeroes_the_byte() -> None:
    active_mon = _mon(0, species=25, hp=80, max_hp=80)
    fainted = _mon(1, species=4, hp=0, max_hp=60, status=0x18)
    cured = _mon(1, species=4, hp=0, max_hp=60, status=0x00)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, fainted], _active(0, active_mon))
    after = _snapshot(1, 1, _bag(()), [active_mon, cured], _active(0, active_mon))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=STATUS_POISON), expected_consumed=1
    )
    assert (outcome.applied, outcome.hp, outcome.status, outcome.reason) == (
        True,
        0,
        0x00,
        REASON_APPLIED,
    )


def test_full_heal_clears_every_status_bit_at_once() -> None:
    active_mon = _mon(0, species=25, hp=80, max_hp=80)
    afflicted = _mon(1, species=4, hp=10, max_hp=60, status=0x4F)
    cured = _mon(1, species=4, hp=10, max_hp=60, status=0x00)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, afflicted], _active(0, active_mon))
    after = _snapshot(1, 1, _bag(()), [active_mon, cured], _active(0, active_mon))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=0xFF), expected_consumed=1
    )
    assert outcome.applied is True
    assert outcome.status == 0x00


def test_status_only_medicine_without_a_matching_bit_is_wrong_status() -> None:
    active_mon = _mon(0, species=25, hp=80, max_hp=80)
    burned = _mon(1, species=4, hp=10, max_hp=60, status=0x10)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, burned], _active(0, active_mon))
    after = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, burned], _active(0, active_mon))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=0x08), expected_consumed=0
    )
    assert (outcome.applied, outcome.status, outcome.reason) == (False, 0x10, REASON_WRONG_STATUS)


def test_potion_on_fainted_target_is_an_invalid_target_not_a_no_effect() -> None:
    fainted = _mon(0, hp=0, max_hp=40)
    before = _snapshot(1, 0, _bag(((1, 1),)), [fainted], _active(0, fainted))
    after = _snapshot(1, 0, _bag(((1, 1),)), [fainted], _active(0, fainted))

    outcome = assert_medicine_application(before, after, _potion(1), expected_consumed=0)
    assert outcome.applied is False
    assert outcome.reason == REASON_FAINTED_NON_REVIVE
    assert outcome.reason in INVALID_TARGET_REASONS
    assert outcome.reason not in VALID_NO_EFFECT_REASONS


def test_revive_on_living_target_is_an_invalid_target() -> None:
    living = _mon(1, hp=20, max_hp=40)
    active_mon = _mon(0, hp=40, max_hp=40)
    before = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, living], _active(0, active_mon))
    after = _snapshot(1, 1, _bag(((1, 1),)), [active_mon, living], _active(0, active_mon))

    outcome = assert_medicine_application(before, after, _revive(1), expected_consumed=0)
    assert outcome.applied is False
    assert outcome.reason == REASON_REVIVE_ON_LIVING
    assert outcome.reason in INVALID_TARGET_REASONS


def test_valid_no_effect_is_distinct_from_an_invalid_target() -> None:
    full = _mon(0, hp=40, max_hp=40)
    before = _snapshot(1, 0, _bag(((1, 1),)), [full], _active(0, full))
    after = _snapshot(1, 0, _bag(((1, 1),)), [full], _active(0, full))

    outcome = assert_medicine_application(before, after, _potion(1), expected_consumed=0)
    assert outcome.reason == REASON_FULL_HP
    assert outcome.reason in VALID_NO_EFFECT_REASONS
    assert outcome.reason not in INVALID_TARGET_REASONS


def test_wrong_declared_amount_is_caught_by_the_snapshot_assertion() -> None:
    target_before = _mon(0, hp=30, max_hp=100)
    # Literal 50 is the correct Potion result; a 21-HP rule must fail.
    target_after = _mon(0, hp=50, max_hp=100)
    before = _snapshot(1, 0, _bag(((1, 1),)), [target_before], _active(0, target_before))
    after = _snapshot(1, 0, _bag(()), [target_after], _active(0, target_after))

    with pytest.raises(AssertionError, match="declared medicine outcome"):
        assert_medicine_application(
            before, after, MedicineRule(item_id=1, fixed_heal=21), expected_consumed=1
        )


def test_wrong_cure_mask_is_caught_by_the_snapshot_assertion() -> None:
    burned = _mon(0, hp=20, max_hp=40, status=STATUS_BURN)
    still_burned = _mon(0, hp=20, max_hp=40, status=STATUS_BURN)
    before = _snapshot(1, 0, _bag(((1, 1),)), [burned], _active(0, burned))
    after = _snapshot(1, 0, _bag(()), [still_burned], _active(0, still_burned))

    with pytest.raises(AssertionError, match="declared medicine outcome"):
        assert_medicine_application(
            before, after, MedicineRule(item_id=1, cure_mask=STATUS_ALL), expected_consumed=1
        )


def test_medicine_outcome_does_not_mutate_its_input() -> None:
    mon = _mon(0, hp=30, max_hp=100)
    outcome = medicine_outcome(_potion(1), mon)
    assert outcome.hp == 50
    assert mon.hp == 30


# ---------------------------------------------------------------------------
# Ordered action timeline and turn accounting (#94)
# ---------------------------------------------------------------------------


def _successful_timeline() -> ActionTimeline:
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
        _successful_timeline(), item_id=1, target_slot=0, expected_hp_delta=POTION_HEAL
    )
    assert account.consumes_player_action is True
    assert account.item_consumed == 1
    assert account.opponent_actions == 1
    assert account.player_moves == 0
    assert account.pp_decrements == 0
    assert account.applied is True


def test_effects_are_at_the_application_boundary_and_opponent_damage_separate() -> None:
    timeline = _successful_timeline()
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
    timeline = _successful_timeline()
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
            _successful_timeline(), item_id=2, target_slot=0, expected_hp_delta=POTION_HEAL
        )
    with pytest.raises(AssertionError, match="selected item/target"):
        assert_successful_item_turn(
            _successful_timeline(), item_id=1, target_slot=3, expected_hp_delta=POTION_HEAL
        )
    with pytest.raises(AssertionError, match="application HP delta"):
        assert_successful_item_turn(
            _successful_timeline(), item_id=1, target_slot=0, expected_hp_delta=21
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
