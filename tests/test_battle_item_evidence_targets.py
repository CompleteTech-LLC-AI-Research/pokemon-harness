"""ROM-free item-target identity and active/benched isolation tests.

Split from ``tests/test_battle_item_evidence.py`` for #157 with no behavior
change: every assertion and test ID below is preserved verbatim, and the
expectation values stay literal so a wrong formula, amount, status mask,
ordering, or continuation rule fails the test rather than being derived from
the helper under test.
"""

from __future__ import annotations

import pytest

from tests._battle_item_evidence import (
    ActiveBattleSnapshot,
    assert_active_battle_refresh,
    assert_medicine_application,
    assert_selected_target_changes_only,
)
from tests._battle_item_evidence_factories import (
    make_active,
    make_bag,
    make_mon,
    make_potion,
    make_snapshot,
    make_super_potion,
)

# ---------------------------------------------------------------------------
# Target identity and active/benched isolation
# ---------------------------------------------------------------------------


def test_two_same_species_members_remain_distinguishable_by_slot() -> None:
    bag_before = make_bag(((1, 2),))
    bag_after = make_bag(((1, 1),))
    active_mon = make_mon(0, species=54, hp=40, max_hp=40)
    benched_before = make_mon(1, species=54, hp=5, max_hp=40)
    benched_after = make_mon(1, species=54, hp=25, max_hp=40)
    before = make_snapshot(
        1, 1, bag_before, [active_mon, benched_before], make_active(0, active_mon)
    )
    after = make_snapshot(1, 1, bag_after, [active_mon, benched_after], make_active(0, active_mon))

    outcome = assert_medicine_application(before, after, make_potion(1), expected_consumed=1)
    assert outcome.hp == 25
    assert after.mon(0) == active_mon
    assert after.mon(1).hp == 25


def test_benched_target_never_overwrites_active_identity_or_hp() -> None:
    active_mon = make_mon(0, species=25, level=20, hp=80, max_hp=80)
    target_before = make_mon(1, species=4, level=5, hp=10, max_hp=20)
    target_after = make_mon(1, species=4, level=5, hp=20, max_hp=20)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, target_before], make_active(0, active_mon)
    )
    after = make_snapshot(
        1, 1, make_bag(()), [active_mon, target_after], make_active(0, active_mon)
    )

    assert_medicine_application(before, after, make_super_potion(1), expected_consumed=1)


def test_active_target_refresh_mirrors_hp_and_status_only() -> None:
    target_before = make_mon(0, species=25, hp=30, max_hp=100)
    target_after = make_mon(0, species=25, hp=50, max_hp=100)
    before = make_snapshot(
        1, 0, make_bag(((1, 1),)), [target_before], make_active(0, target_before)
    )
    after = make_snapshot(1, 0, make_bag(()), [target_after], make_active(0, target_after))

    assert_active_battle_refresh(before, after)


def test_active_refresh_rejects_benched_target_leaking_into_active_data() -> None:
    active_mon = make_mon(0, species=25, hp=80, max_hp=80)
    target_before = make_mon(1, species=4, hp=10, max_hp=20)
    target_after = make_mon(1, species=4, hp=20, max_hp=20)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [active_mon, target_before], make_active(0, active_mon)
    )
    # The bug: the benched target's HP/identity is copied onto the active record.
    leaked_active = make_active(0, target_after)
    after = make_snapshot(1, 1, make_bag(()), [active_mon, target_after], leaked_active)

    with pytest.raises(AssertionError, match="benched item target refreshed"):
        assert_active_battle_refresh(before, after)


def test_active_refresh_rejects_swapped_active_identity() -> None:
    target_before = make_mon(0, species=25, hp=30, max_hp=100)
    target_after = make_mon(0, species=25, hp=50, max_hp=100)
    before = make_snapshot(
        1, 0, make_bag(((1, 1),)), [target_before], make_active(0, target_before)
    )
    swapped = make_active(0, target_after)
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
    after = make_snapshot(1, 0, make_bag(()), [target_after], swapped)

    with pytest.raises(AssertionError, match="active battle species changed"):
        assert_active_battle_refresh(before, after)


def test_active_refresh_rejects_active_max_hp_corruption() -> None:
    target_before = make_mon(0, species=25, hp=30, max_hp=100)
    target_after = make_mon(0, species=25, hp=50, max_hp=100)
    before = make_snapshot(
        1, 0, make_bag(((1, 1),)), [target_before], make_active(0, target_before)
    )
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
    after = make_snapshot(1, 0, make_bag(()), [target_after], corrupted)

    with pytest.raises(AssertionError, match="active battle max HP"):
        assert_active_battle_refresh(before, after)


def test_selected_target_changes_only_rejects_other_slot_mutation() -> None:
    target = make_mon(1, hp=10, max_hp=20)
    other_before = make_mon(0, hp=40, max_hp=40)
    other_after = make_mon(0, hp=39, max_hp=40)
    before = make_snapshot(
        1, 1, make_bag(((1, 1),)), [other_before, target], make_active(0, other_before)
    )
    after = make_snapshot(1, 1, make_bag(()), [other_after, target], make_active(0, other_before))

    with pytest.raises(AssertionError, match="unselected party slot 0 changed"):
        assert_selected_target_changes_only(before, after)
