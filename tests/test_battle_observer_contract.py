"""ROM-free proofs for the observer's hook-order and settlement contract."""

from __future__ import annotations

import re
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


# --- #595: verified KO-return / terminal continuation hooks (N1, N2) ----------

_PROVIDER = {
    "red": {
        "select": 0x5564,
        "exchange": 0x5605,
        "load": 0x3725,
        "text": 0x5A4C,
        "printer": 0x3C49,
        "player": 0x5952,
        "enemy": 0x69D3,
    },
    "yellow": {
        "select": 0x56D6,
        "exchange": 0x5777,
        "load": 0x371B,
        "text": 0x5BBE,
        "printer": 0x3C36,
        "player": 0x5AC4,
        "enemy": 0x6B59,
    },
}
_HOOK_SHORT = {
    "player_ko": "player_action_ko_return",
    "enemy_ko": "enemy_action_ko_return",
    "victory": "terminal_victory",
    "blackout": "terminal_blackout",
}
_BYTE_CONTROL = {  # hook -> (index, replacement byte)
    "player_action_ko_return": (6, 0xC0),
    "enemy_action_ko_return": (6, 0xC0),
    "terminal_victory": (4, 0xD1),
    "terminal_blackout": (9, 0xD1),
}


class _Mem(dict):
    def __missing__(self, key):
        return 0


class _FakePyboy:
    def __init__(self, memory):
        self.memory, self.registered, self.callbacks = memory, [], {}

    def hook_register(self, bank, address, callback, context):
        self.registered.append((bank, address))
        self.callbacks.setdefault((bank, address), []).append(callback)


class _FakeSymbols:
    def __init__(self, table):
        self.table = table

    def bank_addr(self, name):
        return self.table[name]

    def __contains__(self, name):
        return name in self.table

    def addr_of(self, name):
        return self.table[name][1]


class _FakeSession:
    def __init__(self, table, memory):
        self.symbols, self._pyboy = _FakeSymbols(table), _FakePyboy(memory)


def _continuation_session(version, mutate=None):
    pins = _PROVIDER["yellow" if version == "yellow" else "red"]
    table = {
        "SelectEnemyMove": (15, pins["select"]),
        "LinkBattleExchangeData": (15, pins["exchange"]),
        "LoadScreenTilesFromBuffer1": (0, pins["load"]),
        "wSerialExchangeNybbleReceiveData": (0, 0xCC3E),
        "FullyParalyzedText": (15, pins["text"]),
        "PrintText": (0, pins["printer"]),
        "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed": (15, pins["player"]),
        "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed": (15, pins["enemy"]),
    }
    memory = _Mem()

    def put(bank, address, data):
        for index, value in enumerate(data):
            memory[bank, address + index] = value

    le = lambda value: value.to_bytes(2, "little")
    put(
        15,
        pins["select"] + 10,
        b"\xcd" + le(pins["exchange"]) + b"\xcd" + le(pins["load"]) + b"\xfa" + le(0xCC3E),
    )
    for key in ("player", "enemy"):
        put(15, pins[key] - 6, b"\x21" + le(pins["text"]) + b"\xcd" + le(pins["printer"]))
    for name, label, offset, pinned in evidence._CONTINUATION_PINS:
        address, signature = pinned["yellow" if version == "yellow" else "red"]
        table[label] = (15, address - offset)
        put(15, address, bytes.fromhex(signature))
    if mutate is not None:
        mutate(table, memory)
    return _FakeSession(table, memory)


@pytest.mark.parametrize("version", ("red", "blue", "yellow"))
def test_continuation_locations_pin_terminal_and_ko_hooks_for_every_version(version) -> None:
    session = _continuation_session(version)
    locations = evidence.continuation_locations(session, version)
    assert [name for name, _bank, _address in locations] == [
        "post_exchange",
        "local_fully_paralyzed",
        "enemy_fully_paralyzed",
        "player_action_ko_return",
        "enemy_action_ko_return",
        "terminal_victory",
        "terminal_blackout",
    ]
    wanted = (
        (0x593A, 0x69D3, 0x46BB, 0x489C)
        if version == "yellow"
        else (0x57C8, 0x684D, 0x4699, 0x4837)
    )
    assert [(bank, address) for _name, bank, address in locations[3:]] == [(15, a) for a in wanted]
    owned = evidence.install_continuation_hooks(session, object(), version=version)
    assert owned == [(bank, address) for _name, bank, address in locations]
    assert session._pyboy.registered == owned and len(owned) == 7


def _drift_cases():
    for version in ("red", "blue", "yellow"):
        for short, hook in _HOOK_SHORT.items():
            for kind in ("byte", "address", "bank"):
                yield pytest.param(version, hook, kind, id=f"{version}-{short}-{kind}")


@pytest.mark.parametrize("version, hook, kind", tuple(_drift_cases()))
def test_continuation_locations_reject_terminal_and_ko_signature_drift(version, hook, kind) -> None:
    _name, label, offset, pinned = next(x for x in evidence._CONTINUATION_PINS if x[0] == hook)
    address, signature = pinned["yellow" if version == "yellow" else "red"]
    data = bytes.fromhex(signature)

    def mutate(table, memory):
        if kind == "byte":
            index, replacement = _BYTE_CONTROL[hook]
            assert memory[15, address + index] == data[index] != replacement
            memory[15, address + index] = replacement
        elif kind == "address":
            table[label] = (15, address - offset + 1)
            for index, value in enumerate(data):
                memory[15, address + 1 + index] = value
        else:
            table[label] = (14, address - offset)
            for index, value in enumerate(data):
                memory[14, address + index] = value

    session = _continuation_session(version, mutate)
    message = (
        f"{hook} "
        + {"byte": "signature bytes", "address": "address", "bank": "bank"}[kind]
        + " mismatch"
    )
    with pytest.raises(ValueError, match=rf"^{re.escape(message)}$"):
        evidence.install_continuation_hooks(session, object(), version=version)
    assert session._pyboy.registered == []


# --- #595: local readiness and wait contract (N6 readiness, N6 local) ----------


def _ko_rows(*, terminal=True, cleanup=True):
    from tests.test_battle_turn_evidence import _ko_pair_without_terminal, _terminal_pair

    rows = _ko_pair_without_terminal()
    if terminal:
        for row, full in zip(rows, _terminal_pair(), strict=True):
            row["terminal"] = full["terminal"]
            if cleanup:
                row["cleanup"] = full["cleanup"]
    return rows


@pytest.mark.parametrize(
    "case, row, expected",
    (
        pytest.param(
            "errored",
            {"settled": False, "unsupported_reason": "battle observation X: ValueError: e"},
            False,
            id="errored",
        ),
        pytest.param("unsettled", {"settled": False}, False, id="unsettled"),
        pytest.param("non-dict", None, False, id="non-dict"),
        pytest.param(
            "nonko-settled",
            {"settled": True, "turn": {"local": {"hp": 5}, "enemy": {"hp": 5}}},
            True,
            id="nonko-settled",
        ),
        pytest.param(
            "ko-no-terminal",
            {
                "settled": True,
                "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}},
                "terminal": None,
                "cleanup": None,
            },
            False,
            id="ko-no-terminal",
        ),
        pytest.param(
            "ko-terminal-no-cleanup",
            {
                "settled": True,
                "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}},
                "terminal": {"boundary": "EndOfBattle"},
                "cleanup": None,
            },
            False,
            id="ko-terminal-no-cleanup",
        ),
        pytest.param(
            "ko-cleanup-still-in-battle",
            {
                "settled": True,
                "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}},
                "terminal": {"boundary": "EndOfBattle"},
                "cleanup": {"is_in_battle": 2},
            },
            False,
            id="ko-cleanup-still-in-battle",
        ),
        pytest.param(
            "ko-terminal-cleanup",
            {
                "settled": True,
                "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}},
                "terminal": {"boundary": "EndOfBattle"},
                "cleanup": {"is_in_battle": 0},
            },
            True,
            id="ko-terminal-cleanup",
        ),
    ),
)
def test_battle_evidence_ready_contract(case, row, expected) -> None:
    from tests._pyboy_link_session_roms_battle_support import battle_evidence_ready

    assert battle_evidence_ready(row) is expected


class _ScriptedLink:
    def __init__(self):
        self.calls = 0

    def step_interleaved(self, frames, chunk_cycles=None):
        self.calls += 1


class _ScriptedObserver:
    def __init__(self, link, rows_for_calls):
        self.link, self.rows_for_calls = link, rows_for_calls

    def snapshot(self):
        return self.rows_for_calls(self.link.calls)


@pytest.mark.parametrize(
    "case",
    (
        "local-never-ready",
        "local-nonko-ready",
        "local-ko-cleanup-arrives",
        "local-ko-never-cleanup",
    ),
)
def test_local_battle_wait_contract(case) -> None:
    from tests._pyboy_link_session_roms_battle_support import _wait_for_settled_battle_evidence

    link = _ScriptedLink()
    nonko = [_row(), _row(reverse=True)]
    plans = {
        "local-never-ready": lambda calls: [
            {"schema_version": evidence.SCHEMA_VERSION, "settled": False} for _ in range(2)
        ],
        "local-nonko-ready": lambda calls: nonko,
        "local-ko-cleanup-arrives": lambda calls: _ko_rows(cleanup=calls >= 2),
        "local-ko-never-cleanup": lambda calls: _ko_rows(terminal=False),
    }
    rows_for = plans[case]
    counters = {
        "_battle_evidence": [
            _ScriptedObserver(link, lambda calls, i=i: rows_for(calls)[i]) for i in (0, 1)
        ]
    }
    if case in ("local-never-ready", "local-ko-never-cleanup"):
        expected = (
            "['settled snapshot is missing']"
            if case == "local-never-ready"
            else "['KO outcome lacks EndOfBattle evidence']"
        )
        with pytest.raises(AssertionError) as caught:
            _wait_for_settled_battle_evidence(link, counters)
        assert str(caught.value).startswith(f"battle settlement evidence failed: {expected}; rows=")
        assert link.calls == 120
    else:
        _wait_for_settled_battle_evidence(link, counters)
        assert link.calls == (0 if case == "local-nonko-ready" else 2)


# --- #595: real-observer two-peer KO/terminal flows (N3, N4) -------------------
# Canonical scripts: step keys name a hook event (suffix "#n" = write variant); each peer runs
# against its own fake session through the production installers (see _run_flow).
_WRAM = """
red: wPlayerMonNumber=cc2f wSerialExchangeNybbleReceiveData=cc3e
    wSerialExchangeNybbleSendData=cc42 wPlayerSelectedMove=ccdc wBattleResult=cf0b
    wEnemyMonSpecies=cfe5 wEnemyMonHP=cfe6 wEnemyMonPartyPos=cfe8 wEnemyMonStatus=cfe9
    wEnemyMonMoves=cfed wEnemyMonMaxHP=cff4 wEnemyMonPP=cffe wBattleMonSpecies=d014
    wBattleMonHP=d015 wBattleMonStatus=d018 wBattleMonMoves=d01c wBattleMonMaxHP=d023
    wBattleMonPP=d02d wIsInBattle=d057 wMoveMissed=d05f wDamage=d0d7 wLinkState=d12b
    wPartyCount=d163 wPartySpecies=d164 wPartyMons=d16b wCurMap=d35e hWhoseTurn=fff3
    wEnemySelectedMove=ccdd
yellow: wPlayerMonNumber=cc2f wSerialExchangeNybbleReceiveData=cc3e
    wSerialExchangeNybbleSendData=cc42 wPlayerSelectedMove=ccdc wBattleResult=cf0b
    wEnemyMonSpecies=cfe4 wEnemyMonHP=cfe5 wEnemyMonPartyPos=cfe7 wEnemyMonStatus=cfe8
    wEnemyMonMoves=cfec wEnemyMonMaxHP=cff3 wEnemyMonPP=cffd wBattleMonSpecies=d013
    wBattleMonHP=d014 wBattleMonStatus=d017 wBattleMonMoves=d01b wBattleMonMaxHP=d022
    wBattleMonPP=d02c wIsInBattle=d056 wMoveMissed=d05e wDamage=d0d6 wLinkState=d12a
    wPartyCount=d162 wPartySpecies=d163 wPartyMons=d16a wCurMap=d35d hWhoseTurn=fff3
    wEnemySelectedMove=ccdd
"""
_HOOKS = """
red: MainInBattleLoop=0f:4233 LinkBattleExchangeData=0f:5605 post_exchange=0f:5574
    ExecutePlayerMove=0f:565e PlayerCanExecuteMove=0f:56b0 DecrementPP=1a:4000
    ApplyDamageToEnemyPokemon=0f:6142 ApplyAttackToEnemyPokemonDone=0f:619d
    player_action_ko_return=0f:57c8 HandleEnemyMonFainted=0f:4525 terminal_victory=0f:4699
    EndOfBattle=04:77aa ReturnToCableClubRoom=01:577d ExecuteEnemyMove=0f:66bc
    EnemyCanExecuteMove=0f:672b ApplyDamageToPlayerPokemon=0f:6200
    ApplyAttackToPlayerPokemonDone=0f:625b enemy_action_ko_return=0f:684d
    HandlePlayerMonFainted=0f:4700 terminal_blackout=0f:4837 ExecuteEnemyMoveDone=0f:688c
    ExecutePlayerMoveDone=0f:580a
yellow: MainInBattleLoop=0f:4249 LinkBattleExchangeData=0f:5777 post_exchange=0f:56e6
    ExecutePlayerMove=0f:57d0 PlayerCanExecuteMove=0f:5822 DecrementPP=3d:42db
    ApplyDamageToEnemyPokemon=0f:62b4 ApplyAttackToEnemyPokemonDone=0f:630f
    player_action_ko_return=0f:593a HandleEnemyMonFainted=0f:453b terminal_victory=0f:46bb
    EndOfBattle=04:7765 ReturnToCableClubRoom=01:581e ExecuteEnemyMove=0f:6842
    EnemyCanExecuteMove=0f:68b1 ApplyDamageToPlayerPokemon=0f:6372
    ApplyAttackToPlayerPokemonDone=0f:63cd enemy_action_ko_return=0f:69d3
    HandlePlayerMonFainted=0f:471d terminal_blackout=0f:489c ExecuteEnemyMoveDone=0f:6a12
    ExecutePlayerMoveDone=0f:597c
"""
_STEPS = """
MainInBattleLoop | - > -
LinkBattleExchangeData | - > -
post_exchange | wSerialExchangeNybbleSendData=0 wSerialExchangeNybbleReceiveData=0
    wPlayerSelectedMove=1 > -
ExecutePlayerMove | hWhoseTurn=0 > -
PlayerCanExecuteMove | - > -
DecrementPP | hWhoseTurn=0 > wBattleMonPP=9,0,0,0
ApplyDamageToEnemyPokemon | wEnemyMonHP=0,35 wDamage=0,35 > wEnemyMonHP=0,0
ApplyAttackToEnemyPokemonDone | wMoveMissed=0 > -
player_action_ko_return | hWhoseTurn=0 wMoveMissed=0 > -
HandleEnemyMonFainted | - > -
ROM_STORE_wBattleResult | - > wBattleResult=0
terminal_victory | - > -
EndOfBattle | - > -
ReturnToCableClubRoom | wIsInBattle=0 wLinkState=1 wCurMap=240 > -
ExecuteEnemyMove | hWhoseTurn=1 > -
EnemyCanExecuteMove | - > -
ApplyDamageToPlayerPokemon | wBattleMonHP=0,35 wDamage=0,35 > wBattleMonHP=0,0
ApplyAttackToPlayerPokemonDone | wMoveMissed=0 > -
enemy_action_ko_return | hWhoseTurn=1 wMoveMissed=0 > -
HandlePlayerMonFainted | - > -
ROM_STORE_wBattleResult#2 | - > wBattleResult=1
terminal_blackout | - > -
ApplyDamageToPlayerPokemon#2 | wBattleMonHP=0,40 wDamage=0,5 > wBattleMonHP=0,35
ExecuteEnemyMoveDone | wMoveMissed=0 > -
ApplyDamageToEnemyPokemon#2 | wEnemyMonHP=0,40 wDamage=0,5 > wEnemyMonHP=0,35
ExecutePlayerMoveDone | wMoveMissed=0 > -
player_action_ko_return#2 | hWhoseTurn=0 wMoveMissed=0 wEnemyMonHP=0,0 > -
player_action_ko_return#3 | hWhoseTurn=1 wMoveMissed=0 > -
player_action_ko_return#4 | hWhoseTurn=0 wMoveMissed=1 > -
ApplyDamageToEnemyPokemon#3 | wEnemyMonHP=0,35 wDamage=0,34 > wEnemyMonHP=0,1
"""
_SCRIPTS = """
S00: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S01: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return HandlePlayerMonFainted ROM_STORE_wBattleResult#2 terminal_blackout
    EndOfBattle ReturnToCableClubRoom
S02: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon#2 ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return ExecuteEnemyMoveDone ExecutePlayerMove PlayerCanExecuteMove
    DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S03: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon#2 ApplyAttackToEnemyPokemonDone
    player_action_ko_return ExecutePlayerMoveDone ExecuteEnemyMove EnemyCanExecuteMove
    ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone enemy_action_ko_return
    HandlePlayerMonFainted ROM_STORE_wBattleResult#2 terminal_blackout EndOfBattle
    ReturnToCableClubRoom
S04: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory EndOfBattle
    ReturnToCableClubRoom
S05: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP player_action_ko_return#2 HandleEnemyMonFainted
    ROM_STORE_wBattleResult terminal_victory EndOfBattle ReturnToCableClubRoom
S06: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon player_action_ko_return
    HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory EndOfBattle
    ReturnToCableClubRoom
S07: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return#3 HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S08: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return#4 HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S09: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon#3 ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S10: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return player_action_ko_return HandleEnemyMonFainted
    ROM_STORE_wBattleResult terminal_victory EndOfBattle ReturnToCableClubRoom
S11: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S12: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    terminal_victory EndOfBattle ReturnToCableClubRoom
S13: MainInBattleLoop terminal_blackout ROM_STORE_wBattleResult#2 EndOfBattle
    ReturnToCableClubRoom
S14: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult MainInBattleLoop
S15: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle
S16: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return HandlePlayerMonFainted ROM_STORE_wBattleResult#2 terminal_blackout
S17: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return HandlePlayerMonFainted ROM_STORE_wBattleResult terminal_blackout
    EndOfBattle ReturnToCableClubRoom
"""
_N3 = """
red-peer0-attacker-first red 0 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
red-peer0-attacker-second red 0 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
red-peer1-attacker-first red 1 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
red-peer1-attacker-second red 1 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
yellow-peer0-attacker-first yellow 0 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
yellow-peer0-attacker-second yellow 0 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
yellow-peer1-attacker-first yellow 1 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
yellow-peer1-attacker-second yellow 1 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
"""
_N4 = """
ko-flow-without-ko-hook | S04 | S01 | A | terminal_victory:terminal branch before settled
    action evidence | = | -,-,-,-,10,10 | 8,10,11,12,-,12
ko-completion-without-damage | S05 | S01 | A | player_action_ko_return:KO completion without
    damage application | = | -,-,-,-,7,7 | 8,10,11,12,-,12
ko-completion-pending-damage | S06 | S01 | A | player_action_ko_return:KO completion with
    pending damage application | = | -,-,-,-,8,8 | 8,10,11,12,-,12
ko-completion-wrong-turn-owner | S07 | S01 | A | player_action_ko_return:KO completion turn
    mismatch | = | -,-,-,-,9,9 | 8,10,11,12,-,12
ko-completion-miss-flag-contradiction | S08 | S01 | A | player_action_ko_return:KO completion
    contradicts miss flag | = | -,-,-,-,9,9 | 8,10,11,12,-,12
ko-hook-nonzero-target-ignored | S09 | S01 | A | terminal_victory:terminal branch before
    settled action evidence | = | -,-,-,-,11,11 | 8,10,11,12,-,12
ko-completion-duplicate | S10 | S01 | A | player_action_ko_return:KO completion repeated | = |
    9,-,-,-,10,10 | 8,10,11,12,-,12
ko-missing-pp-decrement | S11 | S01 | A | terminal_victory:executed move lacks exactly one
    local PP decrement | = | 8,-,-,-,10,10 | 8,10,11,12,-,12
duplicate-terminal-hook-after-settlement | S12 | S01 | - | - | - | 9,11,13,14,-,14 |
    8,10,11,12,-,12
blackout-before-exchange | S00 | S13 | - | - | settled snapshot is missing | 9,11,12,13,-,13 |
    -,-,3,4,-,4
nonterminal-ko-replacement | S14 | S01 | - | - | KO outcome lacks EndOfBattle evidence |
    9,11,-,-,-,11 | 8,10,11,12,-,12
missing-cleanup | S15 | S01 | - | - | battle cleanup is incomplete | 9,11,12,-,-,12 |
    8,10,11,12,-,12
ko-turn-without-end-of-battle-on-peer | S00 | S16 | - | - | KO outcome lacks EndOfBattle
    evidence | 9,11,12,13,-,13 | 8,10,-,-,-,10
noncomplementary-result-0-0 | S00 | S17 | - | - | battle terminal results are not
    complementary | 9,11,12,13,-,13 | 8,10,11,12,-,12
"""


def _blocks(text):
    lines = []
    for raw in text.strip().splitlines():
        if raw[:1] == " " and lines:
            lines[-1] += " " + raw.strip()
        else:
            lines.append(raw.strip())
    return lines


def _writes(text):
    pairs = [item.split("=") for item in ([] if text == "-" else text.split())]
    return [(symbol, [int(part) for part in data.split(",")]) for symbol, data in pairs]


def _parse_flow_tables():
    tables = {"wram": {}, "hooks": {}, "steps": {}, "scripts": {}}
    for kind, text in (("wram", _WRAM), ("hooks", _HOOKS)):
        for line in _blocks(text):
            version, _, items = line.partition(": ")
            tables[kind][version] = dict(item.split("=") for item in items.split())
    for line in _blocks(_STEPS):
        key, _, body = line.partition(" | ")
        before, _, after = body.partition(" > ")
        tables["steps"][key] = (_writes(before), _writes(after))
    for line in _blocks(_SCRIPTS):
        name, _, keys = line.partition(": ")
        tables["scripts"][name] = keys.split()
    return tables


_FLOW = _parse_flow_tables()
_N3_CASES = tuple(tuple(line.split()) for line in _blocks(_N3))
_N4_CASES = tuple(tuple(part.strip() for part in line.split(" | ")) for line in _blocks(_N4))
_CONTINUATION_EVENTS = {"post_exchange", *(pin[0] for pin in evidence._CONTINUATION_PINS)}
_PEER_MONS = (((25, 40), (54, 35)), ((54, 35), (25, 40)))  # attacker, defender: (local, enemy)


def _poke(session, symbol, data):
    address = session.symbols.addr_of(symbol)
    for offset, value in enumerate(data):
        where = address + offset
        session._pyboy.memory[(1, where) if 0xD000 <= where <= 0xDFFF else where] = value


def _peer_session(version, local, enemy):
    """Independent fake session (own symbols, memory and hook recorder) in the plan's start state."""
    session = _continuation_session(version)
    table, memory = session.symbols.table, session._pyboy.memory
    for symbol, address in _FLOW["wram"][version].items():
        table[symbol] = (0, int(address, 16))
    for name, where in _FLOW["hooks"][version].items():
        if name not in _CONTINUATION_EVENTS:
            table[name] = tuple(int(part, 16) for part in where.split(":"))
    table["Moves"] = (14, 0x4000)
    for offset, value in enumerate((1, 0, 40, 100, 35, 0)):
        memory[14, 0x4000 + offset] = value
    record = bytearray(44)
    record[0], record[1:3], record[8:12] = local[0], bytes((0, local[1])), bytes((1, 0, 0, 0))
    record[29:33], record[34:36] = bytes((10, 0, 0, 0)), bytes((0, local[1]))
    start = {
        "wPartyCount": [1],
        "wPartySpecies": [local[0], 255],
        "wPartyMons": list(record),
        "wBattleMonSpecies": [local[0]],
        "wBattleMonHP": [0, local[1]],
        "wBattleMonMaxHP": [0, local[1]],
        "wBattleMonStatus": [0],
        "wBattleMonMoves": [1, 0, 0, 0],
        "wBattleMonPP": [10, 0, 0, 0],
        "wPlayerMonNumber": [0],
        "wEnemyMonSpecies": [enemy[0]],
        "wEnemyMonHP": [0, enemy[1]],
        "wEnemyMonMaxHP": [0, enemy[1]],
        "wEnemyMonStatus": [0],
        "wEnemyMonMoves": [1, 0, 0, 0],
        "wEnemyMonPP": [10, 0, 0, 0],
        "wEnemyMonPartyPos": [0],
        "wBattleResult": [0],
        "wMoveMissed": [0],
        "wDamage": [0, 0],
        "hWhoseTurn": [0],
        "wIsInBattle": [2],
        "wLinkState": [4],
        "wCurMap": [240],
    }
    for symbol, data in start.items():
        _poke(session, symbol, data)
    return session


def _play(version, session, observer, script):
    """Run one peer's script only through the hook callbacks recorded at the pinned locations."""
    completed = None
    for key in _FLOW["scripts"][script]:
        before, after = _FLOW["steps"][key]
        name = key.partition("#")[0]
        for symbol, data in before:
            _poke(session, symbol, data)
        if not name.startswith("ROM_STORE"):
            where = tuple(int(part, 16) for part in _FLOW["hooks"][version][name].split(":"))
            callbacks = session._pyboy.callbacks.get(where, [])
            assert callbacks, f"{name} has no registered callback at {where}"
            was_done = observer.ko_completed
            for callback in callbacks:
                callback(None)
            if observer.ko_completed and not was_done:
                completed = observer.sequence
        for symbol, data in after:
            _poke(session, symbol, data)
    return completed


def _run_flow(version, scripts, peer=0):
    """Production installer on two independent sessions; returns role-ordered results and rows."""
    from tests._pyboy_link_session_roms_battle_support import _install_battle_diag_counters

    order = (0, 1) if peer == 0 else (1, 0)
    sessions = [_peer_session(version, *_PEER_MONS[role]) for role in order]
    observers = _install_battle_diag_counters(*sessions, versions=(version, version))
    observers = observers["_battle_evidence"]
    completed = [None, None]
    for position, role in enumerate(order):
        completed[role] = _play(version, sessions[position], observers[position], scripts[role])
    by_role = [observers[order.index(role)] for role in (0, 1)]
    return by_role, completed, [observer.snapshot() for observer in observers]


def _check_peer(observer, completion, anchors, error=None):
    done, settled, ended, cleaned, failed, visits = (
        None if part == "-" else int(part) for part in anchors.split(",")
    )
    row = observer.snapshot()
    assert (observer.sequence, completion) == (visits, done)
    if failed is not None:
        assert observer.error == error and observer.sequence == failed and row["settled"] is False
        return
    assert observer.error is None and row["settled"] is (settled is not None)
    if settled is not None:
        assert row["settled_seq"] == settled
    assert (row.get("terminal") or {}).get("seq") == ended
    assert (row.get("cleanup") or {}).get("seq") == cleaned


@pytest.mark.parametrize("case", _N3_CASES, ids=[case[0] for case in _N3_CASES])
def test_peer_ko_flow_settles_before_end_of_battle(case) -> None:
    _name, version, peer, attacker, defender, a_anchors, d_anchors = case
    observers, completed, rows = _run_flow(version, (attacker, defender), int(peer))
    assert verify_battle_turns(rows) == []
    for observer, completion, anchors in zip(observers, completed, (a_anchors, d_anchors)):
        _check_peer(observer, completion, anchors)


@pytest.mark.parametrize("case", _N4_CASES, ids=[case[0] for case in _N4_CASES])
def test_ko_boundary_negative_controls(case) -> None:
    _name, attacker, defender, bad, error, verdict, a_anchors, d_anchors = case
    hook, _, cause = error.partition(":")
    error = f"battle observation {hook}: ValueError: {cause}" if bad != "-" else None
    expected = [] if verdict == "-" else [error if verdict == "=" else verdict]
    observers, completed, rows = _run_flow("red", (attacker, defender))
    assert verify_battle_turns(rows) == expected
    for role, (observer, completion, anchors) in enumerate(
        zip(observers, completed, (a_anchors, d_anchors))
    ):
        _check_peer(observer, completion, anchors, error if bad == "AD"[role] else None)
