"""ROM-free proofs for the observer's hook-order and settlement contract."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tests import _battle_turn_evidence as evidence
from tests.test_battle_turn_evidence import _observer, _row, verify_battle_turns


def _prepared_observer(monkeypatch, *, status_actor: str = "local"):
    observer, memory = _observer(monkeypatch)
    monkeypatch.setattr(evidence, "_read", lambda _session, name, size=1: memory[name])
    baseline = deepcopy(_row()["baseline"])
    baseline["local"]["moves"][0] = 85 if status_actor == "local" else 1
    baseline["enemy"]["moves"][0] = 85 if status_actor == "enemy" else 1
    baseline["local"]["pp"] = [10, 0, 0, 0]
    baseline["enemy"]["pp"] = [10, 0, 0, 0]
    observer.baseline = deepcopy(baseline)
    memory.update(
        wSerialExchangeNybbleSendData=0,
        wSerialExchangeNybbleReceiveData=0,
        wPlayerSelectedMove=baseline["local"]["moves"][0],
        wBattleMonHP=[0, 40],
        wEnemyMonHP=[0, 35],
        wDamage=[0, 1],
        wMoveMissed=0,
        hWhoseTurn=0,
        wBattleResult=0,
        wCurMap=0xF0,
        wLinkState=1,
        wIsInBattle=0,
    )
    state = deepcopy(baseline)
    monkeypatch.setattr(evidence, "_combatants", lambda _session: deepcopy(state))
    monkeypatch.setattr(
        evidence,
        "_move_data",
        lambda _session, move: bytes([move, 6 if move == 85 else 0, 40, 100, 35, 0]),
    )
    return observer, memory, state


def _complete_turn(observer, memory, state, *, status_actor: str = "local") -> dict:
    observer.observe("LinkBattleExchangeData")
    observer.observe("post_exchange")
    for side in ("local", "enemy"):
        observer.observe("PlayerCanExecuteMove" if side == "local" else "EnemyCanExecuteMove")
        observer.observe("ExecutePlayerMove" if side == "local" else "ExecuteEnemyMove")
        if side == "local":
            memory["hWhoseTurn"] = 0
            observer.observe("DecrementPP")
        target = "enemy" if side == "local" else "local"
        before_hp = state[target]["hp"]
        memory["wEnemyMonHP" if target == "enemy" else "wBattleMonHP"] = [
            before_hp >> 8,
            before_hp & 0xFF,
        ]
        damage = 0 if side == status_actor else 1
        memory["wDamage"] = [0, damage]
        observer.observe(
            "ApplyDamageToEnemyPokemon" if target == "enemy" else "ApplyDamageToPlayerPokemon"
        )
        state[target]["hp"] -= damage
        if side == status_actor:
            state[target]["status"] = 64
        memory["wEnemyMonHP" if target == "enemy" else "wBattleMonHP"] = [
            state[target]["hp"] >> 8,
            state[target]["hp"] & 0xFF,
        ]
        observer.observe(
            "ApplyAttackToEnemyPokemonDone"
            if target == "enemy"
            else "ApplyAttackToPlayerPokemonDone"
        )
        state[side]["pp"][0] = 9
        memory["wMoveMissed"] = 0
        observer.observe("ExecutePlayerMoveDone" if side == "local" else "ExecuteEnemyMoveDone")
    observer.observe("MainInBattleLoop")
    assert observer.error is None
    assert observer.turn is not None
    return observer.snapshot()


def _opposite_snapshot(snapshot: dict) -> dict:
    peer = deepcopy(snapshot)
    for phase in ("baseline", "turn"):
        peer[phase]["local"], peer[phase]["enemy"] = (
            peer[phase]["enemy"],
            peer[phase]["local"],
        )
    turn = peer["turn"]
    for left, right in (
        ("send", "receive"),
        ("local_move_id", "enemy_move_id"),
        ("local_move_effect", "enemy_move_effect"),
    ):
        turn[left], turn[right] = turn[right], turn[left]
    turn["move_data"]["local"], turn["move_data"]["enemy"] = (
        turn["move_data"]["enemy"],
        turn["move_data"]["local"],
    )
    turn["actions"]["local"], turn["actions"]["enemy"] = (
        turn["actions"]["enemy"],
        turn["actions"]["local"],
    )
    turn["pp"] = {
        "before": list(peer["baseline"]["local"]["pp"]),
        "after": list(turn["local"]["pp"]),
        "decrement_entries": 1,
    }
    return peer


@pytest.mark.parametrize(
    "case",
    (
        pytest.param("one-peer-boundary", id="one-peer-boundary"),
        pytest.param("entry-without-action", id="entry-without-action"),
        pytest.param("execution-before-baseline", id="execution-before-baseline"),
        pytest.param("damage-before-execution", id="damage-before-execution"),
    ),
)
def test_observer_rejects_partial_hook_sequences(case: str, monkeypatch) -> None:
    observer, memory = _observer(monkeypatch)
    monkeypatch.setattr(evidence, "_read", lambda _session, name, size=1: memory[name])
    if case == "one-peer-boundary":
        peer_observer, peer_memory = _observer(monkeypatch)
        memories_by_session = {
            id(observer.session): memory,
            id(peer_observer.session): peer_memory,
        }

        def read_for_session(session, name, size=1):
            return memories_by_session[id(session)][name]

        monkeypatch.setattr(evidence, "_read", read_for_session)
    memory.update(wSerialExchangeNybbleSendData=0, wSerialExchangeNybbleReceiveData=0)
    if case == "execution-before-baseline":
        observer.baseline = None
        observer.observe("PlayerCanExecuteMove")
        observer.observe("ExecutePlayerMove")
        assert observer.error is None
        assert observer.counts["PlayerCanExecuteMove"] == 1
        assert observer.counts["ExecutePlayerMove"] == 1
        assert observer.exchange is None
        assert observer.turn is None
        before_exchange = observer.snapshot()
        assert before_exchange["settled"] is False
        assert before_exchange["turn"] is None

        observer.observe("LinkBattleExchangeData")
        observer.observe("post_exchange")
        assert "before battle baseline" in observer.error
        assert observer.exchange is None
        after_error = observer.snapshot()
        assert after_error["settled"] is False
        assert after_error["exchange_seq"] is None
        assert after_error["turn"] is None
        return

    observer.observe("MainInBattleLoop")
    if case == "one-peer-boundary":
        peer_observer.observe("MainInBattleLoop")
        observer.observe("LinkBattleExchangeData")
        observer.observe("post_exchange")
        observer.observe("MainInBattleLoop")
    elif case == "entry-without-action":
        observer.observe("LinkBattleExchangeData")
        observer.observe("post_exchange")
        observer.observe("MainInBattleLoop")
    else:
        observer.observe("LinkBattleExchangeData")
        observer.observe("post_exchange")
        memory.update(wEnemyMonHP=[0, 35], wDamage=[0, 1])
        observer.observe("ApplyDamageToEnemyPokemon")
        assert "damage application before action execution" in observer.error

    if case == "one-peer-boundary":
        snapshot = observer.snapshot()
        peer_snapshot = peer_observer.snapshot()
        assert observer.error is None
        assert peer_observer.error is None
        assert snapshot["turn"] is None, "partial one-peer observer produced a turn"
        assert snapshot["settled"] is False
        assert snapshot["unsupported_reason"] is None
        assert peer_snapshot["turn"] is None
        assert peer_snapshot["settled"] is False
        assert peer_snapshot["unsupported_reason"] is None
        assert verify_battle_turns([snapshot, peer_snapshot]) == ["settled snapshot is missing"]

        complete_observer, complete_memory, complete_state = _prepared_observer(
            monkeypatch, status_actor="enemy"
        )
        complete_snapshot = _complete_turn(
            complete_observer,
            complete_memory,
            complete_state,
            status_actor="enemy",
        )
        assert complete_snapshot["settled"] is True
        assert complete_snapshot["turn"]["actions"]["local"]["damage_samples"][0]["damage"] == 1

        partial_observer, _, _ = _prepared_observer(monkeypatch)
        partial_observer.observe("MainInBattleLoop")
        partial_snapshot = partial_observer.snapshot()
        assert partial_observer.error is None
        assert partial_observer.counts["MainInBattleLoop"] == 1
        assert partial_snapshot["turn"] is None
        assert partial_snapshot["settled"] is False
        assert verify_battle_turns([complete_snapshot, partial_snapshot]) == [
            "settled snapshot is missing"
        ]
    elif case == "entry-without-action":
        snapshot = observer.snapshot()
        assert observer.error is None
        assert snapshot["settled"] is False
        assert snapshot["turn"] is None
        assert verify_battle_turns([snapshot, None]) == ["settled snapshot is missing"]


@pytest.mark.parametrize("side", ("local", "enemy"), ids=("local", "enemy"))
def test_observer_captures_supported_status_without_hp_damage(side: str, monkeypatch) -> None:
    observer, memory, state = _prepared_observer(monkeypatch, status_actor=side)
    snapshot = _complete_turn(observer, memory, state, status_actor=side)
    target = "enemy" if side == "local" else "local"
    assert snapshot["turn"][target]["status"] == 64
    action = snapshot["turn"]["actions"][side]
    assert action["damage_samples"] == [
        {
            "before_hp": snapshot["baseline"][target]["hp"],
            "damage": 0,
            "after_hp": snapshot["baseline"][target]["hp"],
            "move_missed": 0,
        }
    ]
    assert snapshot["settled"] is True
    assert verify_battle_turns([snapshot, _opposite_snapshot(snapshot)]) == []


def test_terminal_cleanup_gate_preserves_frozen_turn(monkeypatch) -> None:
    observer, memory, state = _prepared_observer(monkeypatch)
    _complete_turn(observer, memory, state)
    frozen = deepcopy(observer.turn)
    observer.observe("EndOfBattle")
    assert observer.error is None
    assert observer.snapshot()["settled"] is True
    peer = _opposite_snapshot(observer.snapshot())
    peer["terminal"]["result"] = 1
    assert verify_battle_turns([observer.snapshot(), peer]) == ["battle cleanup is incomplete"]

    observer.observe("ReturnToCableClubRoom")
    peer["cleanup"] = deepcopy(observer.cleanup)
    assert verify_battle_turns([observer.snapshot(), peer]) == []
    observer.observe("LinkBattleExchangeData")
    observer.observe("MainInBattleLoop")
    observer.observe("ReturnToCableClubRoom")
    assert observer.error is None
    assert observer.turn == frozen
    assert observer.snapshot()["settled"] is True
