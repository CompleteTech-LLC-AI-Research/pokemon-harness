"""ROM-free failure cases for strict, read-only battle settlement evidence."""

from __future__ import annotations

from copy import deepcopy

import pytest

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
    local_before, enemy_before = (
        (_mon(hp=35), _mon(hp=40)) if reverse else (_mon(hp=40), _mon(hp=35))
    )
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


def _paralysis_rows(*, attacker: str, status_on_attacker: bool = False) -> list[dict]:
    rows = [_row(), _row(reverse=True)]
    opposite = "enemy" if attacker == "local" else "local"
    for row, side in zip(rows, (attacker, opposite), strict=True):
        target = "enemy" if side == "local" else "local"
        for phase in ("baseline", "turn"):
            row[phase][side]["moves"][0] = 85
        row["turn"][f"{side}_move_id"] = 85
        row["turn"][f"{side}_move_effect"] = 6
        row["turn"]["move_data"][side][:2] = [85, 6]
        row["turn"][side if status_on_attacker else target]["status"] = 64
    return rows


@pytest.mark.parametrize("attacker", ("local", "enemy"))
def test_paralysis_is_supported_by_the_attack_on_the_affected_combatant(attacker) -> None:
    rows = _paralysis_rows(attacker=attacker)
    before = deepcopy(rows)
    assert verify_battle_turns(rows) == []
    assert rows == before


@pytest.mark.parametrize("attacker", ("local", "enemy"))
def test_using_a_paralysis_move_does_not_support_status_on_the_attacker(attacker) -> None:
    rows = _paralysis_rows(attacker=attacker, status_on_attacker=True)
    assert verify_battle_turns(rows)


@pytest.mark.parametrize("attacker", ("local", "enemy"))
@pytest.mark.parametrize(
    "failure", ("missed_attack", "unfinished_attack", "unsupported_status", "peer_disagreement")
)
def test_paralysis_requires_completed_supported_and_agreed_application(attacker, failure) -> None:
    rows = _paralysis_rows(attacker=attacker)
    opposite = "enemy" if attacker == "local" else "local"
    for index, (row, side) in enumerate(zip(rows, (attacker, opposite), strict=True)):
        target = "enemy" if side == "local" else "local"
        action = row["turn"]["actions"][side]
        if failure == "missed_attack":
            action.update(damage_done=0, damage_samples=[], move_missed=1)
            row["turn"][target]["hp"] = row["baseline"][target]["hp"]
        elif failure == "unfinished_attack":
            action["done"] = False
        elif failure == "unsupported_status":
            row["turn"][target]["status"] = 8
        elif index == 1:
            row["turn"][target]["status"] = 0
    assert verify_battle_turns(rows)


def test_matching_applied_turns_pass() -> None:
    assert verify_battle_turns([_row(), _row(reverse=True)]) == []


def test_tcp_diagnostic_wrapper_keeps_complete_settlement_envelope() -> None:
    """The TCP peer nests a complete observer snapshot beside its counters."""
    assert verify_battle_turns([{"battle_turn": _row()}, {"battle_turn": _row(reverse=True)}]) == []


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


def _observer(monkeypatch):
    from types import SimpleNamespace

    from tests import _battle_turn_evidence as evidence

    memory = {
        "wSerialExchangeNybbleSendData": 4,
        "wSerialExchangeNybbleReceiveData": 4,
        "wPlayerSelectedMove": 1,
        # Stale enemy selection must not override the newly received slot.
        "wEnemySelectedMove": 45,
    }
    monkeypatch.setattr(evidence, "_read", lambda session, name: memory[name])
    monkeypatch.setattr(evidence, "_validate_party", lambda party: 0)
    monkeypatch.setattr(evidence, "_combatants", lambda session: deepcopy(_row()["baseline"]))
    monkeypatch.setattr(
        evidence, "_move_data", lambda session, move: bytes([move, 0, 40, 100, 35, 0])
    )
    session = SimpleNamespace(symbols=SimpleNamespace(addr_of=lambda name: 0))
    observer = evidence.BattleTurnObserver(session, role="test", version="blue", before_party={})
    observer.baseline = deepcopy(_row()["baseline"])
    return observer, memory


def test_exchange_entry_does_not_read_stale_wire_slots(monkeypatch) -> None:
    observer, memory = _observer(monkeypatch)
    observer.observe("LinkBattleExchangeData")
    assert observer.error is None
    assert observer.exchange is None
    assert observer.counts["LinkBattleExchangeData"] == 1

    memory["wSerialExchangeNybbleSendData"] = 0
    memory["wSerialExchangeNybbleReceiveData"] = 0
    observer.observe("post_exchange")
    assert observer.error is None
    assert observer.exchange["exchange_seq"] == 2
    assert observer.exchange["enemy_move_id"] == 1


def test_invalid_wire_slot_after_exchange_still_fails_closed(monkeypatch) -> None:
    observer, memory = _observer(monkeypatch)
    memory["wSerialExchangeNybbleSendData"] = 0
    observer.observe("post_exchange")
    assert "invalid receive slot: 4" in observer.error
    assert observer.exchange is None


def test_missing_selected_local_move_after_exchange_fails_closed(monkeypatch) -> None:
    observer, memory = _observer(monkeypatch)
    memory.update(
        wSerialExchangeNybbleSendData=0, wSerialExchangeNybbleReceiveData=0, wPlayerSelectedMove=0
    )
    observer.observe("post_exchange")
    assert "invalid local move ID: 0" in observer.error
    assert observer.exchange is None


def test_move_choice_uses_existing_later_supported_slot(monkeypatch) -> None:
    from tests import _battle_turn_evidence as evidence

    rows = {
        76: [76, 39, 120, 0, 0, 0],
        45: [45, 18, 0, 0, 0, 0],
        73: [73, 84, 0, 0, 0, 0],
        22: [22, 0, 35, 0, 0, 0],
    }
    monkeypatch.setattr(evidence, "_move_data", lambda session, move: rows[move])
    moves = [(76, 10), (45, 21), (73, 10), (22, 7)]
    before = deepcopy(moves)
    assert evidence.choose_supported_battle_move(None, moves) == (3, 22)
    assert moves == before


def test_move_choice_rejects_unsupported_or_depleted_moves(monkeypatch) -> None:
    import pytest

    from tests import _battle_turn_evidence as evidence

    monkeypatch.setattr(evidence, "_move_data", lambda session, move: [move, 39, 120, 0, 0, 0])
    with pytest.raises(ValueError, match="no legal supported existing move with PP"):
        evidence.choose_supported_battle_move(None, [(76, 10), (22, 64), (68, 20), (0, 0)])
