"""Test-local builders shared by the split battle-item evidence modules.

``tests/_battle_item_evidence.py`` owns the production-side contract helpers.
This module owns the small snapshot/rule builders that more than one split test
module needs, so the split files never duplicate them (#157).
"""

from __future__ import annotations

from tests._battle_item_evidence import (
    HYPER_POTION_HEAL,
    POTION_HEAL,
    STATUS_ALL,
    SUPER_POTION_HEAL,
    ActiveBattleSnapshot,
    BagSnapshot,
    BagStack,
    ItemUseSnapshot,
    MedicineRule,
    PartyMonSnapshot,
)


def make_bag(order: tuple[tuple[int, int], ...]) -> BagSnapshot:
    stacks = tuple(BagStack(item_id, quantity) for item_id, quantity in order)
    return BagSnapshot(
        stacks,
        raw_count=len(stacks),
        terminator_position=len(stacks),
        valid=True,
    )


def make_mon(
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


def make_active(player_slot: int, mon: PartyMonSnapshot) -> ActiveBattleSnapshot:
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


def make_snapshot(
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


def make_potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, fixed_heal=POTION_HEAL)


def make_super_potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, fixed_heal=SUPER_POTION_HEAL)


def make_hyper_potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, fixed_heal=HYPER_POTION_HEAL)


def make_max_potion(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, heal_to_max=True)


def make_revive(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, revive=True)


def make_max_revive(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, revive=True, heal_to_max=True)


def make_full_restore(item_id: int = 1) -> MedicineRule:
    return MedicineRule(item_id=item_id, heal_to_max=True, cure_mask=STATUS_ALL)
