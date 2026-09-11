"""ROM-free failure cases for strict, read-only battle settlement evidence."""

from __future__ import annotations

from copy import deepcopy

from tests._battle_turn_evidence import verify_battle_turns


def _mon(*, hp: int, slot: int = 0) -> dict:
    return {
        "slot": slot,
        "species": 25 if hp >= 39 else 54,
        "hp": hp,
        "max_hp": 40 if hp >= 39 else 35,
        "status": 0,
        "moves": [1, 0, 0, 0],
        "pp": [10, 0, 0, 0],
    }


def _action(before: int, after: int) -> dict:
    return {
        "executed": True,
        "done": True,
        "skip_reason": None,
        "damage_done": 1,
        "move_missed": 0,
        "damage_samples": [
            {"before_hp": before, "after_hp": after, "damage": before - after, "move_missed": 0}
        ],
    }


def _row(*, reverse: bool = False) -> dict:
    # On either peer, local action damages enemy and enemy action damages local.
    local_before, enemy_before = (_mon(hp=35), _mon(hp=40)) if reverse else (_mon(hp=40), _mon(hp=35))
    local_after, enemy_after = (_mon(hp=34), _mon(hp=39)) if reverse else (_mon(hp=39), _mon(hp=34))
    local_action = _action(enemy_before["hp"], enemy_after["hp"])
    enemy_action = _action(local_before["hp"], local_after["hp"])
    local_after["pp"][0] = 9
    return {
        "schema_version": 1,
        "settled": True,
        "exchange_seq": 2,
        "settled_seq": 4,
        "baseline": {"local": local_before, "enemy": enemy_before},
        "turn": {
            "exchange_seq": 2,
            "settled_seq": 4,
            "send": 0,
            "receive": 0,
            "local_move_id": 1,
            "enemy_move_id": 1,
            "local_move_effect": 0,
            "enemy_move_effect": 0,
            "move_data": {"local": [1, 0, 40, 100, 35, 0], "enemy": [1, 0, 40, 100, 35, 0]},
            "local": local_after,
            "enemy": enemy_after,
            "actions": {"local": local_action, "enemy": enemy_action},
            "pp": {"before": [10, 0, 0, 0], "after": [9, 0, 0, 0], "decrement_entries": 1},
        },
        "terminal": None,
        "cleanup": None,
    }


def test_matching_applied_turns_pass() -> None:
    assert verify_battle_turns([_row(), _row(reverse=True)]) == []


def test_hook_only_or_missing_settlement_fails_closed() -> None:
    row = _row()
    row["settled"] = False
    assert verify_battle_turns([row, _row(reverse=True)])


def test_divergent_peer_hp_fails() -> None:
    left, right = _row(), _row(reverse=True)
    right["turn"]["enemy"]["hp"] = 38
    assert verify_battle_turns([left, right])


def test_damage_without_matching_application_fails() -> None:
    left, right = _row(), _row(reverse=True)
    left["turn"]["actions"]["local"]["damage_samples"][0]["after_hp"] = 33
    assert verify_battle_turns([left, right])


def test_cleanup_requires_return_to_cable_club_state() -> None:
    left, right = _row(), _row(reverse=True)
    for row in (left, right):
        row["cleanup"] = {"is_in_battle": 0, "map": 0xF0, "link_state": 1}
    assert verify_battle_turns([left, right], require_cleanup=True) == []
    bad = deepcopy(left)
    bad["cleanup"]["is_in_battle"] = 2
    assert verify_battle_turns([bad, right], require_cleanup=True)
