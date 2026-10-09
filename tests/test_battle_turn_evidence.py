"""ROM-free failure cases for strict, read-only battle settlement evidence."""

from __future__ import annotations

import contextlib
import json
from copy import deepcopy

import pytest

from tests._battle_turn_evidence import MAX_SEQUENCE, verify_battle_turns


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
        "schema_version": 2,
        "settled": True,
        "exchange_seq": 2,
        "settled_seq": 4,
        "baseline": {"local": local_before, "enemy": enemy_before},
        "turn": {
            "exchange_ordinal": 1,
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


@pytest.mark.parametrize(
    "case",
    (
        pytest.param("negative-hp", id="negative-hp"),
        pytest.param("boolean-hp", id="boolean-hp"),
        pytest.param("above-max-hp", id="above-max-hp"),
        pytest.param("zero-max-hp", id="zero-max-hp"),
        pytest.param("invalid-species", id="invalid-species"),
        pytest.param("status-above-max", id="status-above-max"),
        pytest.param("absent-empty", id="absent-empty"),
        pytest.param("absent-single", id="absent-single"),
        pytest.param("absent-unproven-pair", id="absent-unproven-pair"),
        pytest.param("absent-nonmapping", id="absent-nonmapping"),
        pytest.param("hook-and-screenshot-only", id="hook-and-screenshot-only"),
    ),
)
def test_verifier_rejects_invalid_or_unproven_evidence(case: str) -> None:
    expected: str
    if case.startswith("absent-") or case == "hook-and-screenshot-only":
        if case == "absent-empty":
            rows, expected = [], "exactly two battle peer rows are required"
        elif case == "absent-single":
            rows, expected = [_row()], "exactly two battle peer rows are required"
        elif case == "absent-nonmapping":
            rows, expected = [None, None], "settled snapshot is missing"
        elif case == "hook-and-screenshot-only":
            diagnostic = {"hook_counts": {"LinkBattleExchangeData": 1}, "_shots": ["battle"]}
            rows, expected = [diagnostic, deepcopy(diagnostic)], "settled snapshot is missing"
        else:
            rows, expected = [{}, {}], "settled snapshot is missing"
    else:
        rows = [_row(), _row(reverse=True)]
        mon = rows[0]["baseline"]["local"]
        if case == "negative-hp":
            mon["hp"], expected = -1, "invalid active HP"
        elif case == "boolean-hp":
            mon["hp"], expected = True, "invalid active HP"
        elif case == "above-max-hp":
            mon["hp"], expected = mon["max_hp"] + 1, "active HP exceeds max HP"
        elif case == "zero-max-hp":
            mon["max_hp"], expected = 0, "invalid active max HP"
        elif case == "invalid-species":
            mon["species"], expected = 0, "invalid active species"
        else:
            mon["status"], expected = 256, "invalid active status"

    before = deepcopy(rows)
    errors = verify_battle_turns(rows)
    assert errors and expected in errors[0]
    assert rows == before


@pytest.mark.parametrize(
    "field",
    (
        pytest.param("species", id="species"),
        pytest.param("max_hp", id="max-hp"),
        pytest.param("status", id="status"),
    ),
)
def test_peer_snapshot_mismatch_rejects_supported_turn(field: str) -> None:
    rows = [_row(), _row(reverse=True)]
    peer_mon = rows[1]["baseline"]["local"]
    peer_turn_mon = rows[1]["turn"]["local"]
    replacement = {"species": 73, "max_hp": 36, "status": 1}[field]
    peer_mon[field] = replacement
    peer_turn_mon[field] = replacement

    before = deepcopy(rows)
    errors = verify_battle_turns(rows)
    assert errors == ["battle peers disagree on settled baseline combatant state"]
    assert rows == before

    if field in ("species", "max_hp"):
        turn_only_rows = [_row(), _row(reverse=True)]
        original_baseline = deepcopy(turn_only_rows[1]["baseline"]["local"])
        turn_mon = turn_only_rows[1]["turn"]["local"]
        turn_mon[field] = {"species": 73, "max_hp": 36}[field]
        assert turn_mon["hp"] <= turn_mon["max_hp"]
        assert turn_only_rows[1]["baseline"]["local"] == original_baseline

        turn_only_before = deepcopy(turn_only_rows)
        assert verify_battle_turns(turn_only_rows) == [
            "active combatant identity changed during turn"
        ]
        assert turn_only_rows == turn_only_before
        return

    turn_only_rows = _paralysis_rows(attacker="local")
    turn_only_rows[1]["turn"]["local"]["status"] = turn_only_rows[1]["baseline"]["local"]["status"]
    assert turn_only_rows[0]["turn"]["enemy"]["status"] == 64
    assert turn_only_rows[1]["turn"]["local"]["status"] == 0

    turn_only_before = deepcopy(turn_only_rows)
    assert verify_battle_turns(turn_only_rows) == [
        "battle peers disagree on settled turn combatant state"
    ]
    assert turn_only_rows == turn_only_before


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
    left, right = _row(), _row(reverse=True)
    left.update(exchange_seq=1, settled_seq=3)
    right.update(exchange_seq=5, settled_seq=9)
    left["turn"].update(exchange_ordinal=MAX_SEQUENCE, exchange_seq=1, settled_seq=3)
    right["turn"].update(exchange_ordinal=MAX_SEQUENCE, exchange_seq=5, settled_seq=9)
    left["hook_counts"] = {"LinkBattleExchangeData": 1, "post_exchange": 1}
    right["hook_counts"] = {"LinkBattleExchangeData": 7, "post_exchange": 4}
    assert left["hook_counts"] != right["hook_counts"]
    assert verify_battle_turns([left, right]) == []


def test_tcp_diagnostic_wrapper_keeps_complete_settlement_envelope() -> None:
    """The TCP peer nests a complete observer snapshot beside its counters."""
    rows = [{"battle_turn": _row()}, {"battle_turn": _row(reverse=True)}]
    decoded = json.loads(json.dumps(rows))
    assert verify_battle_turns(decoded) == []


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
    assert observer.exchange["exchange_ordinal"] == 1
    assert observer.exchange["enemy_move_id"] == 1


def test_invalid_wire_slot_after_exchange_still_fails_closed(monkeypatch) -> None:
    observer, memory = _observer(monkeypatch)
    memory["wSerialExchangeNybbleSendData"] = 0
    observer.observe("LinkBattleExchangeData")
    observer.observe("post_exchange")
    assert "invalid receive slot: 4" in observer.error
    assert observer.exchange is None


def test_missing_selected_local_move_after_exchange_fails_closed(monkeypatch) -> None:
    observer, memory = _observer(monkeypatch)
    memory.update(
        wSerialExchangeNybbleSendData=0, wSerialExchangeNybbleReceiveData=0, wPlayerSelectedMove=0
    )
    observer.observe("LinkBattleExchangeData")
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


def test_different_exchange_ordinal_fails_even_when_otherwise_coherent() -> None:
    left, right = _row(), _row(reverse=True)
    left["turn"]["exchange_ordinal"] = 1
    right["turn"]["exchange_ordinal"] = 2
    errors = verify_battle_turns([left, right])
    assert errors == ["battle peers disagree on move exchange ordinal"]


@pytest.mark.parametrize(
    "ordinal",
    (
        pytest.param(None, id="none"),
        pytest.param(0, id="zero"),
        pytest.param(True, id="bool"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="string"),
        pytest.param(MAX_SEQUENCE + 1, id="above-max"),
    ),
)
def test_invalid_exchange_ordinal_fails_closed(ordinal) -> None:
    values = (0, -1) if type(ordinal) is int and ordinal == 0 else (ordinal,)
    for value in values:
        row = _row()
        row["turn"]["exchange_ordinal"] = value
        errors = verify_battle_turns([row, _row(reverse=True)])
        assert errors and errors[0].startswith("invalid exchange ordinal:")


def test_missing_exchange_ordinal_fails_closed() -> None:
    row = _row()
    del row["turn"]["exchange_ordinal"]
    errors = verify_battle_turns([row, _row(reverse=True)])
    assert errors and errors[0].startswith("invalid exchange ordinal:")


def test_schema_one_evidence_fails_closed() -> None:
    for invalid_version in (1, True):
        full_row = _row()
        full_row["schema_version"] = invalid_version
        nested_row = {"battle_turn": _row()}
        nested_row["battle_turn"]["schema_version"] = invalid_version
        for rows in (
            [full_row, _row(reverse=True)],
            [nested_row, {"battle_turn": _row(reverse=True)}],
        ):
            assert verify_battle_turns(rows) == ["invalid battle evidence schema"]


@pytest.mark.parametrize(
    "stage",
    (
        pytest.param("before-first-continuation", id="before-first-continuation"),
        pytest.param("after-first-capture", id="after-first-capture"),
    ),
)
def test_duplicate_exchange_entry_before_settlement_fails_closed(monkeypatch, stage) -> None:
    observer, memory = _observer(monkeypatch)
    observer.observe("LinkBattleExchangeData")
    first_ordinal = observer.pending_exchange_ordinal
    first_exchange = None
    if stage == "after-first-capture":
        memory["wSerialExchangeNybbleSendData"] = 0
        memory["wSerialExchangeNybbleReceiveData"] = 0
        observer.observe("post_exchange")
        first_exchange = deepcopy(observer.exchange)
    observer.observe("LinkBattleExchangeData")
    assert observer.error is not None
    assert "duplicate move exchange entry before settlement" in observer.error
    assert observer.turn is None
    if stage == "before-first-continuation":
        assert observer.exchange is None
        assert observer.pending_exchange_ordinal == first_ordinal == 1
    else:
        assert observer.exchange == first_exchange
        assert observer.exchange["exchange_ordinal"] == 1
    observer.observe("post_exchange")
    assert observer.turn is None
    assert observer.exchange == first_exchange


def test_post_exchange_without_entry_fails_closed(monkeypatch) -> None:
    observer, memory = _observer(monkeypatch)
    memory["wSerialExchangeNybbleSendData"] = 0
    memory["wSerialExchangeNybbleReceiveData"] = 0
    observer.observe("post_exchange")
    assert "without an unconsumed move exchange entry" in observer.error
    assert observer.exchange is None
    assert observer.turn is None
    assert observer.snapshot()["settled"] is False

    observer, memory = _observer(monkeypatch)
    memory["wSerialExchangeNybbleSendData"] = 0
    memory["wSerialExchangeNybbleReceiveData"] = 0
    observer.observe("LinkBattleExchangeData")
    observer.observe("post_exchange")
    first_exchange = deepcopy(observer.exchange)
    assert first_exchange["exchange_ordinal"] == 1
    assert observer.pending_exchange_ordinal is None
    observer.observe("post_exchange")
    assert "without an unconsumed move exchange entry" in observer.error
    assert observer.exchange == first_exchange
    assert observer.turn is None
    assert observer.snapshot()["settled"] is False


def test_exchange_ordinal_counter_overflow_fails_closed(monkeypatch) -> None:
    observer, _memory = _observer(monkeypatch)
    observer._exchange_ordinal_counter = MAX_SEQUENCE
    observer.observe("LinkBattleExchangeData")
    assert "move exchange ordinal exhausted" in observer.error
    assert observer._exchange_ordinal_counter == MAX_SEQUENCE
    assert observer.pending_exchange_ordinal is None
    assert observer.exchange is None
    assert observer.turn is None
    assert observer.snapshot()["settled"] is False


def test_postsettlement_entry_preserves_frozen_turn(monkeypatch) -> None:
    from tests import _battle_turn_evidence as evidence

    row = _row()
    observer, memory = _observer(monkeypatch)
    memory.update(
        wSerialExchangeNybbleSendData=0,
        wSerialExchangeNybbleReceiveData=0,
        wBattleResult=0,
        wCurMap=0xF0,
        wLinkState=1,
        wIsInBattle=0,
    )
    observer.observe("LinkBattleExchangeData")
    observer.observe("post_exchange")
    monkeypatch.setattr(
        evidence,
        "_combatants",
        lambda session: deepcopy({side: row["turn"][side] for side in ("local", "enemy")}),
    )
    observer.actions = deepcopy(row["turn"]["actions"])
    observer.pp_entries = 1
    observer.sequence = 4
    observer.observe("MainInBattleLoop")
    assert observer.error is None
    assert observer.turn is not None
    frozen_turn = deepcopy(observer.turn)
    assert frozen_turn is not None

    observer.observe("LinkBattleExchangeData")
    observer.observe("MainInBattleLoop")
    observer.observe("EndOfBattle")
    observer.observe("ReturnToCableClubRoom")
    assert observer.error is None
    assert observer.turn == frozen_turn
    assert observer.turn["exchange_ordinal"] == 1
    assert observer.terminal["boundary"] == "EndOfBattle"
    assert observer.cleanup == {
        "seq": observer.sequence,
        "map": 0xF0,
        "link_state": 1,
        "is_in_battle": 0,
    }
    assert observer.counts["LinkBattleExchangeData"] == 2
    assert observer.snapshot()["settled"] is True

    observed = observer.snapshot()
    peer = _row(reverse=True)
    peer["terminal"] = {
        "seq": observer.terminal["seq"],
        "result": 1,
        "send": 0,
        "receive": 0,
        "boundary": "EndOfBattle",
    }
    peer["cleanup"] = {
        "seq": observer.cleanup["seq"],
        "map": 0xF0,
        "link_state": 1,
        "is_in_battle": 0,
    }
    rows = [observed, peer]
    before = deepcopy(rows)
    assert verify_battle_turns(rows) == []
    assert rows == before
    missing_cleanup = deepcopy(observed)
    missing_cleanup["cleanup"] = None
    assert verify_battle_turns([missing_cleanup, peer]) == ["battle cleanup is incomplete"]


def _observed_faint_row(monkeypatch, side: str, actor_hp: int, result: int) -> dict:
    from tests import _battle_turn_evidence as evidence

    base = _row(reverse=side == "enemy")
    observer, memory = _observer(monkeypatch)
    observer.baseline = deepcopy(base["baseline"])
    final = deepcopy(base["turn"])
    for combatant in ("local", "enemy"):
        final[combatant]["hp"] = base["baseline"][combatant]["hp"]
    final["local"]["pp"] = list(base["baseline"]["local"]["pp"])
    final[side]["hp"] = actor_hp
    other = "enemy" if side == "local" else "local"
    before_hp = base["baseline"][side]["hp"]
    observer.actions[other] = _action(before_hp, actor_hp)
    if other == "local":
        final["local"]["pp"][0] -= 1
        observer.pp_entries = 1
    monkeypatch.setattr(
        evidence,
        "_combatants",
        lambda _session: {name: deepcopy(final[name]) for name in ("local", "enemy")},
    )
    memory.update(
        wSerialExchangeNybbleSendData=0,
        wSerialExchangeNybbleReceiveData=0,
        wBattleResult=result,
        wCurMap=0xF0,
        wLinkState=1,
        wIsInBattle=0,
    )
    observer.observe("LinkBattleExchangeData")
    observer.observe("post_exchange")
    observer.observe("HandlePlayerMonFainted" if side == "local" else "HandleEnemyMonFainted")
    observer.observe("MainInBattleLoop")
    if actor_hp == 0:
        observer.observe("EndOfBattle")
        observer.observe("ReturnToCableClubRoom")
    return observer.snapshot()


def _observed_faint_pair(monkeypatch, first_side: str, *, valid: bool) -> list[dict]:
    actor_hp = 0 if valid else 39
    return [
        _observed_faint_row(monkeypatch, first_side, actor_hp, 0),
        _observed_faint_row(
            monkeypatch,
            "enemy" if first_side == "local" else "local",
            actor_hp,
            1,
        ),
    ]


@pytest.mark.parametrize(
    "scenario",
    (
        pytest.param("local-valid-zero", id="local-valid-zero"),
        pytest.param("local-positive-actor", id="local-positive-actor"),
        pytest.param("local-opposite-zero", id="local-opposite-zero"),
        pytest.param("enemy-valid-zero", id="enemy-valid-zero"),
        pytest.param("enemy-positive-actor", id="enemy-positive-actor"),
        pytest.param("enemy-opposite-zero", id="enemy-opposite-zero"),
    ),
)
def test_observed_faint_skip_requires_actor_hp_zero(monkeypatch, scenario: str) -> None:
    first_side = "local" if scenario.startswith("local-") else "enemy"
    if scenario.endswith("opposite-zero"):
        rows = _observed_faint_pair(monkeypatch, first_side, valid=True)
        target_row = rows[0]
        target_side = "enemy" if first_side == "local" else "local"
        target_row["turn"][target_side]["hp"] = 0
        before = deepcopy(rows)
        assert verify_battle_turns(rows) == ["faint skip target is the wrong combatant"]
        assert rows == before
        return

    valid = scenario.endswith("valid-zero")
    rows = _observed_faint_pair(monkeypatch, first_side, valid=valid)
    before = deepcopy(rows)
    if valid:
        assert all(row["unsupported_reason"] is None for row in rows)
        assert all(
            row["turn"][
                first_side if index == 0 else ("enemy" if first_side == "local" else "local")
            ]["hp"]
            == 0
            for index, row in enumerate(rows)
        )
        assert verify_battle_turns(rows) == []
    else:
        assert all(
            row["unsupported_reason"] is not None
            and "faint skip actor is not fainted" in row["unsupported_reason"]
            for row in rows
        )
        assert all(row["settled"] is False for row in rows)
        assert verify_battle_turns(rows)
    assert rows == before


def _terminal_pair(results: tuple[object, object] = (0, 1)) -> list[dict]:
    rows = [_row(), _row(reverse=True)]
    for row, result in zip(rows, results, strict=True):
        row["terminal"] = {
            "seq": 6,
            "result": result,
            "send": 0,
            "receive": 0,
            "boundary": "EndOfBattle",
        }
        row["cleanup"] = {"seq": 7, "map": 0xF0, "link_state": 1, "is_in_battle": 0}
    return rows


def _ko_pair_without_terminal() -> list[dict]:
    left, right = _row(), _row(reverse=True)
    left["turn"]["enemy"]["hp"] = 0
    left["turn"]["actions"]["local"] = _action(35, 0)
    right["turn"]["local"]["hp"] = 0
    right["turn"]["actions"]["enemy"] = _action(35, 0)
    return [left, right]


@pytest.mark.parametrize(
    "results",
    (
        pytest.param((0, 1), id="result-0-peer-1"),
        pytest.param((1, 0), id="result-1-peer-0"),
        pytest.param((2, 2), id="legacy-draw-2-2"),
    ),
)
def test_terminal_result_pairs_accept_supported_contract(results) -> None:
    rows = _terminal_pair(results)
    before = deepcopy(rows)
    assert verify_battle_turns(rows) == []
    assert rows == before


@pytest.mark.parametrize(
    "case",
    (
        pytest.param(((0, 0), "battle terminal results are not complementary"), id="both-win-0-0"),
        pytest.param(((1, 1), "battle terminal results are not complementary"), id="both-loss-1-1"),
        pytest.param(
            ((255, 255), "battle terminal results are not complementary"),
            id="equal-unsupported-255",
        ),
        pytest.param(
            ((2, 3), "battle terminal results are not complementary"), id="unsupported-2-3"
        ),
        pytest.param(
            ((0, 2), "battle terminal results are not complementary"), id="unsupported-0-2"
        ),
        pytest.param(((True, 1), "invalid battle result"), id="boolean-result"),
        pytest.param((("0", 1), "invalid battle result"), id="string-result"),
        pytest.param((None, "battle terminal evidence is asymmetric"), id="peer-terminal-missing"),
    ),
)
def test_terminal_result_pairs_reject_invalid_contract(case) -> None:
    results, expected = case
    rows = _terminal_pair((0, 1) if results is None else results)
    if results is None:
        rows[1]["terminal"] = None
    before = deepcopy(rows)
    errors = verify_battle_turns(rows)
    assert errors and expected in errors[0]
    assert rows == before


@pytest.mark.parametrize(
    "case",
    (
        pytest.param(
            ("missing-terminal-after-ko", "KO outcome lacks EndOfBattle evidence"),
            id="missing-terminal-after-ko",
        ),
        pytest.param(("missing-boundary", "terminal boundary is invalid"), id="missing-boundary"),
        pytest.param(("wrong-boundary", "terminal boundary is invalid"), id="wrong-boundary"),
        pytest.param(
            ("terminal-at-settled-seq", "invalid terminal sequence"), id="terminal-at-settled-seq"
        ),
        pytest.param(
            ("terminal-before-settled", "invalid terminal sequence"), id="terminal-before-settled"
        ),
        pytest.param(
            ("boolean-terminal-seq", "invalid terminal sequence"), id="boolean-terminal-seq"
        ),
        pytest.param(
            ("terminal-seq-above-max", "invalid terminal sequence"), id="terminal-seq-above-max"
        ),
    ),
)
def test_terminal_boundary_and_sequence_fail_closed(case) -> None:
    scenario, expected = case
    rows = (
        _ko_pair_without_terminal() if scenario == "missing-terminal-after-ko" else _terminal_pair()
    )
    if scenario != "missing-terminal-after-ko":
        terminal = rows[0]["terminal"]
        if scenario == "missing-boundary":
            del terminal["boundary"]
        elif scenario == "wrong-boundary":
            terminal["boundary"] = "battle-complete"
        elif scenario == "terminal-at-settled-seq":
            terminal["seq"] = rows[0]["settled_seq"]
        elif scenario == "terminal-before-settled":
            terminal["seq"] = rows[0]["settled_seq"] - 1
        elif scenario == "boolean-terminal-seq":
            terminal["seq"] = True
        else:
            terminal["seq"] = MAX_SEQUENCE + 1
    before = deepcopy(rows)
    errors = verify_battle_turns(rows)
    assert errors and expected in errors[0]
    assert rows == before
    if scenario == "wrong-boundary":
        rows[0]["terminal"]["boundary"] = []
        assert verify_battle_turns(rows) == ["terminal boundary is invalid"]


@pytest.mark.parametrize(
    "case",
    (
        pytest.param(
            ("missing-bilateral-cleanup", "battle cleanup is incomplete"),
            id="missing-bilateral-cleanup",
        ),
        pytest.param(
            ("one-peer-cleanup-missing", "battle cleanup is incomplete"),
            id="one-peer-cleanup-missing",
        ),
        pytest.param(("still-in-battle", "battle cleanup is incomplete"), id="still-in-battle"),
        pytest.param(("wrong-map", "unexpected state"), id="wrong-map"),
        pytest.param(("wrong-link-state", "unexpected state"), id="wrong-link-state"),
        pytest.param(
            ("cleanup-seq-equal-terminal", "invalid cleanup sequence"),
            id="cleanup-seq-equal-terminal",
        ),
        pytest.param(
            ("cleanup-seq-before-terminal", "invalid cleanup sequence"),
            id="cleanup-seq-before-terminal",
        ),
        pytest.param(
            ("cleanup-seq-above-max", "invalid cleanup sequence"), id="cleanup-seq-above-max"
        ),
    ),
)
def test_terminal_requires_complete_ordered_cleanup_by_default(case) -> None:
    scenario, expected = case
    rows = _terminal_pair()
    if scenario == "missing-bilateral-cleanup":
        rows[0]["cleanup"] = rows[1]["cleanup"] = None
    elif scenario == "one-peer-cleanup-missing":
        rows[1]["cleanup"] = None
    elif scenario == "still-in-battle":
        rows[0]["cleanup"]["is_in_battle"] = 1
    elif scenario == "wrong-map":
        rows[0]["cleanup"]["map"] = 0xF1
    elif scenario == "wrong-link-state":
        rows[0]["cleanup"]["link_state"] = 0
    elif scenario == "cleanup-seq-equal-terminal":
        rows[0]["cleanup"]["seq"] = rows[0]["terminal"]["seq"]
    elif scenario == "cleanup-seq-before-terminal":
        rows[0]["cleanup"]["seq"] = rows[0]["terminal"]["seq"] - 1
    else:
        rows[0]["cleanup"]["seq"] = MAX_SEQUENCE + 1
    before = deepcopy(rows)
    errors = verify_battle_turns(rows)
    assert errors and expected in errors[0]
    assert rows == before
    if scenario == "cleanup-seq-above-max":
        float_control = deepcopy(_terminal_pair())
        float_control[0]["cleanup"]["seq"] = 7.0
        float_control[1]["cleanup"]["seq"] = 7.0
        assert verify_battle_turns(float_control) == ["invalid cleanup sequence: 7.0"]


def test_terminal_hp_divergence_fails_after_valid_cleanup() -> None:
    rows = _terminal_pair()
    local_attack = rows[0]["turn"]["actions"]["local"]
    sample = local_attack["damage_samples"][0]
    rows[0]["turn"]["enemy"]["hp"] = 33
    sample.update(after_hp=33, damage=2)

    before = deepcopy(rows)
    assert verify_battle_turns(rows) == ["battle peers disagree on settled turn combatant state"]
    assert rows == before


@pytest.mark.parametrize(
    "case",
    (pytest.param("equal-seq", id="equal-seq"), pytest.param("earlier-seq", id="earlier-seq")),
)
def test_terminal_sequence_must_follow_settlement(case) -> None:
    """A terminal at or before the settled sequence is rejected (synthetic validator rows)."""
    rows = _terminal_pair()
    sequence = rows[0]["settled_seq"] - (0 if case == "equal-seq" else 1)
    for row in rows:
        row["terminal"]["seq"] = sequence
        row["cleanup"]["seq"] = sequence + 1
    assert verify_battle_turns(rows) == [f"invalid terminal sequence: {sequence}"]


@contextlib.contextmanager
def source_mutant(monkeypatch, target, name: str, anchor: str, replacement: str):
    """In-memory mutant of ``target.name`` (class method or module function).

    Exactly one ``anchor`` fragment of the live source is replaced and compiled against the real
    module globals; the original binding is restored on exit, also when the body fails.
    """
    import inspect
    import sys
    import types

    original = getattr(target, name)
    is_module = isinstance(target, types.ModuleType)
    source = inspect.getsource(original)
    assert source.count(anchor) == 1, f"mutation anchor for {name} must occur exactly once"
    text = ("" if is_module else "class _Holder:\n") + source.replace(anchor, replacement)
    scope: dict = {}
    module = target if is_module else sys.modules[target.__module__]
    exec(compile(text, f"<mutant {name}>", "exec"), vars(module), scope)  # noqa: S102
    mutant = scope[name] if is_module else scope["_Holder"].__dict__[name]
    assert mutant is not original and mutant.__code__.co_code != original.__code__.co_code
    try:
        with monkeypatch.context() as patch:
            patch.setattr(target, name, mutant)
            assert getattr(target, name) is mutant
            yield original
    finally:
        assert getattr(target, name) is original
