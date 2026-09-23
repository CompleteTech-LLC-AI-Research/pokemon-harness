"""ROM-free medicine math and branch tests for the item contract.

Split from ``tests/test_battle_item_evidence.py`` for #157 with no behavior
change: every assertion and test ID below is preserved verbatim, and the
expectation values stay literal so a wrong formula, amount, status mask,
ordering, or continuation rule fails the test rather than being derived from
the helper under test.
"""

from __future__ import annotations

import pytest

from tests._battle_item_evidence import (
    HYPER_POTION_HEAL,
    INVALID_TARGET_REASONS,
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
    SUPER_POTION_HEAL,
    VALID_NO_EFFECT_REASONS,
    MedicineRule,
    assert_medicine_application,
    capped_heal,
    cure_status,
    medicine_outcome,
    revive_hp,
    status_matches_mask,
)
from tests._battle_item_evidence_factories import (
    make_active,
    make_bag,
    make_full_restore,
    make_max_potion,
    make_max_revive,
    make_mon,
    make_potion,
    make_revive,
    make_snapshot,
    make_super_potion,
)

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
    target_before = make_mon(0, hp=30, max_hp=100)
    target_after = make_mon(0, hp=50, max_hp=100)
    before = make_snapshot(
        1, 0, make_bag(((1, 2),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(((1, 1),)), [target_after], make_active(0, target_after))

    outcome = assert_medicine_application(before, after, make_potion(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp, outcome.reason) == (True, 50, REASON_APPLIED)


def test_potion_clamps_at_cap_and_does_not_charge_extra() -> None:
    target_before = make_mon(0, hp=90, max_hp=100)
    target_after = make_mon(0, hp=100, max_hp=100)
    before = make_snapshot(
        1, 0, make_bag(((1, 3),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(((1, 2),)), [target_after], make_active(0, target_after))

    outcome = assert_medicine_application(before, after, make_potion(1), expected_consumed=1)
    assert outcome.hp == 100


def test_super_potion_heals_its_full_fifty_below_the_cap() -> None:
    target_before = make_mon(0, hp=30, max_hp=200)
    target_after = make_mon(0, hp=80, max_hp=200)
    before = make_snapshot(
        1, 0, make_bag(((1, 2),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(((1, 1),)), [target_after], make_active(0, target_after))

    outcome = assert_medicine_application(before, after, make_super_potion(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp) == (True, 80)


def test_max_potion_restores_to_max() -> None:
    target_before = make_mon(0, hp=10, max_hp=300)
    target_after = make_mon(0, hp=300, max_hp=300)
    before = make_snapshot(
        1, 0, make_bag(((1, 1),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(()), [target_after], make_active(0, target_after))

    outcome = assert_medicine_application(before, after, make_max_potion(1), expected_consumed=1)
    assert outcome.hp == 300


def test_revive_restores_floor_half_max_hp_on_a_fainted_member() -> None:
    active_mon = make_mon(0, species=25, hp=80, max_hp=80)
    fainted = make_mon(1, species=4, hp=0, max_hp=301)
    revived = make_mon(1, species=4, hp=150, max_hp=301)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, fainted], make_active(0, active_mon)
    )
    after = make_snapshot(1, 1, make_bag(()), [active_mon, revived], make_active(0, active_mon))

    outcome = assert_medicine_application(before, after, make_revive(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp) == (True, 150)


def test_max_revive_restores_full_max_hp() -> None:
    active_mon = make_mon(0, species=25, hp=80, max_hp=80)
    fainted = make_mon(1, species=4, hp=0, max_hp=300)
    revived = make_mon(1, species=4, hp=300, max_hp=300)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, fainted], make_active(0, active_mon)
    )
    after = make_snapshot(1, 1, make_bag(()), [active_mon, revived], make_active(0, active_mon))

    outcome = assert_medicine_application(before, after, make_max_revive(1), expected_consumed=1)
    assert outcome.hp == 300


def test_full_restore_heals_and_cures_status() -> None:
    target_before = make_mon(0, hp=50, max_hp=200, status=STATUS_BURN | STATUS_PARALYSIS)
    target_after = make_mon(0, hp=200, max_hp=200, status=0)
    before = make_snapshot(
        1, 0, make_bag(((1, 1),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(()), [target_after], make_active(0, target_after))

    outcome = assert_medicine_application(before, after, make_full_restore(1), expected_consumed=1)
    assert (outcome.applied, outcome.hp, outcome.status) == (True, 200, 0)


def test_full_restore_at_full_hp_with_status_only_cures() -> None:
    target_before = make_mon(0, hp=200, max_hp=200, status=STATUS_BURN)
    target_after = make_mon(0, hp=200, max_hp=200, status=0)
    before = make_snapshot(
        1, 0, make_bag(((1, 1),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(()), [target_after], make_active(0, target_after))

    outcome = assert_medicine_application(before, after, make_full_restore(1), expected_consumed=1)
    assert (outcome.applied, outcome.status, outcome.reason) == (True, 0, REASON_APPLIED)


def test_full_restore_at_full_hp_without_status_is_a_no_effect() -> None:
    mon = make_mon(0, hp=200, max_hp=200, status=0)
    before = make_snapshot(1, 0, make_bag(((1, 1),)), [mon], make_active(0, mon))
    after = make_snapshot(1, 0, make_bag(((1, 1),)), [mon], make_active(0, mon))

    outcome = assert_medicine_application(before, after, make_full_restore(1), expected_consumed=0)
    assert outcome.applied is False
    assert outcome.reason == REASON_FULL_HP_NO_STATUS


def test_wrong_status_medicine_has_no_effect() -> None:
    burned = make_mon(0, hp=20, max_hp=40, status=STATUS_BURN)
    before = make_snapshot(1, 0, make_bag(((1, 1),)), [burned], make_active(0, burned))
    after = make_snapshot(1, 0, make_bag(((1, 1),)), [burned], make_active(0, burned))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=STATUS_POISON), expected_consumed=0
    )
    assert (outcome.applied, outcome.reason) == (False, REASON_WRONG_STATUS)


def test_matching_status_medicine_cures_and_consumes_one() -> None:
    poisoned = make_mon(0, hp=20, max_hp=40, status=STATUS_POISON)
    cured = make_mon(0, hp=20, max_hp=40, status=0)
    before = make_snapshot(1, 0, make_bag(((1, 1),)), [poisoned], make_active(0, poisoned))
    after = make_snapshot(1, 0, make_bag(()), [cured], make_active(0, cured))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=STATUS_POISON), expected_consumed=1
    )
    assert outcome.applied is True


def test_status_only_medicine_cures_a_fainted_target_and_zeroes_the_byte() -> None:
    active_mon = make_mon(0, species=25, hp=80, max_hp=80)
    fainted = make_mon(1, species=4, hp=0, max_hp=60, status=0x18)
    cured = make_mon(1, species=4, hp=0, max_hp=60, status=0x00)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, fainted], make_active(0, active_mon)
    )
    after = make_snapshot(1, 1, make_bag(()), [active_mon, cured], make_active(0, active_mon))

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
    active_mon = make_mon(0, species=25, hp=80, max_hp=80)
    afflicted = make_mon(1, species=4, hp=10, max_hp=60, status=0x4F)
    cured = make_mon(1, species=4, hp=10, max_hp=60, status=0x00)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, afflicted], make_active(0, active_mon)
    )
    after = make_snapshot(1, 1, make_bag(()), [active_mon, cured], make_active(0, active_mon))

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=0xFF), expected_consumed=1
    )
    assert outcome.applied is True
    assert outcome.status == 0x00


def test_status_only_medicine_without_a_matching_bit_is_wrong_status() -> None:
    active_mon = make_mon(0, species=25, hp=80, max_hp=80)
    burned = make_mon(1, species=4, hp=10, max_hp=60, status=0x10)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, burned], make_active(0, active_mon)
    )
    after = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, burned], make_active(0, active_mon)
    )

    outcome = assert_medicine_application(
        before, after, MedicineRule(item_id=1, cure_mask=0x08), expected_consumed=0
    )
    assert (outcome.applied, outcome.status, outcome.reason) == (False, 0x10, REASON_WRONG_STATUS)


def test_potion_on_fainted_target_is_an_invalid_target_not_a_no_effect() -> None:
    fainted = make_mon(0, hp=0, max_hp=40)
    before = make_snapshot(1, 0, make_bag(((1, 1),)), [fainted], make_active(0, fainted))
    after = make_snapshot(1, 0, make_bag(((1, 1),)), [fainted], make_active(0, fainted))

    outcome = assert_medicine_application(before, after, make_potion(1), expected_consumed=0)
    assert outcome.applied is False
    assert outcome.reason == REASON_FAINTED_NON_REVIVE
    assert outcome.reason in INVALID_TARGET_REASONS
    assert outcome.reason not in VALID_NO_EFFECT_REASONS


def test_revive_on_living_target_is_an_invalid_target() -> None:
    living = make_mon(1, hp=20, max_hp=40)
    active_mon = make_mon(0, hp=40, max_hp=40)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, living], make_active(0, active_mon)
    )
    after = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, living], make_active(0, active_mon)
    )

    outcome = assert_medicine_application(before, after, make_revive(1), expected_consumed=0)
    assert outcome.applied is False
    assert outcome.reason == REASON_REVIVE_ON_LIVING
    assert outcome.reason in INVALID_TARGET_REASONS


def test_valid_no_effect_is_distinct_from_an_invalid_target() -> None:
    full = make_mon(0, hp=40, max_hp=40)
    before = make_snapshot(1, 0, make_bag(((1, 1),)), [full], make_active(0, full))
    after = make_snapshot(1, 0, make_bag(((1, 1),)), [full], make_active(0, full))

    outcome = assert_medicine_application(before, after, make_potion(1), expected_consumed=0)
    assert outcome.reason == REASON_FULL_HP
    assert outcome.reason in VALID_NO_EFFECT_REASONS
    assert outcome.reason not in INVALID_TARGET_REASONS


def test_wrong_declared_amount_is_caught_by_the_snapshot_assertion() -> None:
    target_before = make_mon(0, hp=30, max_hp=100)
    # Literal 50 is the correct Potion result; a 21-HP rule must fail.
    target_after = make_mon(0, hp=50, max_hp=100)
    before = make_snapshot(
        1, 0, make_bag(((1, 1),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(()), [target_after], make_active(0, target_after))

    with pytest.raises(AssertionError, match="declared medicine outcome"):
        assert_medicine_application(
            before, after, MedicineRule(item_id=1, fixed_heal=21), expected_consumed=1
        )


def test_wrong_cure_mask_is_caught_by_the_snapshot_assertion() -> None:
    burned = make_mon(0, hp=20, max_hp=40, status=STATUS_BURN)
    still_burned = make_mon(0, hp=20, max_hp=40, status=STATUS_BURN)
    before = make_snapshot(1, 0, make_bag(((1, 1),)), [burned], make_active(0, burned))
    after = make_snapshot(1, 0, make_bag(()), [still_burned], make_active(0, still_burned))

    with pytest.raises(AssertionError, match="declared medicine outcome"):
        assert_medicine_application(
            before, after, MedicineRule(item_id=1, cure_mask=STATUS_ALL), expected_consumed=1
        )


def test_medicine_outcome_does_not_mutate_its_input() -> None:
    mon = make_mon(0, hp=30, max_hp=100)
    outcome = medicine_outcome(make_potion(1), mon)
    assert outcome.hp == 50
    assert mon.hp == 30
