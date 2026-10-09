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
    "red": dict(select=0x5564, exchange=0x5605, load=0x3725, text=0x5A4C, printer=0x3C49,
                player=0x5952, enemy=0x69D3),
    "yellow": dict(select=0x56D6, exchange=0x5777, load=0x371B, text=0x5BBE, printer=0x3C36,
                   player=0x5AC4, enemy=0x6B59),
}
_HOOK_SHORT = {
    "player_ko": "player_action_ko_return", "enemy_ko": "enemy_action_ko_return",
    "victory": "terminal_victory", "blackout": "terminal_blackout",
}
_BYTE_CONTROL = {  # hook -> (index, replacement byte)
    "player_action_ko_return": (6, 0xC0), "enemy_action_ko_return": (6, 0xC0),
    "terminal_victory": (4, 0xD1), "terminal_blackout": (9, 0xD1),
}


class _FakePyboy:
    def __init__(self, memory):
        self.memory, self.registered = memory, []

    def hook_register(self, bank, address, callback, context):
        self.registered.append((bank, address))


class _FakeSymbols:
    def __init__(self, table):
        self.table = table

    def bank_addr(self, name):
        return self.table[name]

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
    memory = {}

    def put(bank, address, data):
        for index, value in enumerate(data):
            memory[bank, address + index] = value

    le = lambda value: value.to_bytes(2, "little")  # noqa: E731
    put(15, pins["select"] + 10, b"\xcd" + le(pins["exchange"]) + b"\xcd" + le(pins["load"])
        + b"\xfa" + le(0xCC3E))
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
        "post_exchange", "local_fully_paralyzed", "enemy_fully_paralyzed",
        "player_action_ko_return", "enemy_action_ko_return", "terminal_victory", "terminal_blackout",
    ]
    wanted = (0x593A, 0x69D3, 0x46BB, 0x489C) if version == "yellow" else (
        0x57C8, 0x684D, 0x4699, 0x4837)
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
    message = f"{hook} " + {"byte": "signature bytes", "address": "address", "bank": "bank"}[kind] + " mismatch"
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
        pytest.param("errored", {"settled": False, "unsupported_reason": "battle observation X: ValueError: e"}, False, id="errored"),
        pytest.param("unsettled", {"settled": False}, False, id="unsettled"),
        pytest.param("non-dict", None, False, id="non-dict"),
        pytest.param("nonko-settled", {"settled": True, "turn": {"local": {"hp": 5}, "enemy": {"hp": 5}}}, True, id="nonko-settled"),
        pytest.param("ko-no-terminal", {"settled": True, "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}}, "terminal": None, "cleanup": None}, False, id="ko-no-terminal"),
        pytest.param("ko-terminal-no-cleanup", {"settled": True, "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}}, "terminal": {"boundary": "EndOfBattle"}, "cleanup": None}, False, id="ko-terminal-no-cleanup"),
        pytest.param("ko-cleanup-still-in-battle", {"settled": True, "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}}, "terminal": {"boundary": "EndOfBattle"}, "cleanup": {"is_in_battle": 2}}, False, id="ko-cleanup-still-in-battle"),
        pytest.param("ko-terminal-cleanup", {"settled": True, "turn": {"local": {"hp": 5}, "enemy": {"hp": 0}}, "terminal": {"boundary": "EndOfBattle"}, "cleanup": {"is_in_battle": 0}}, True, id="ko-terminal-cleanup"),
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


@pytest.mark.parametrize("case", ("local-never-ready", "local-nonko-ready", "local-ko-cleanup-arrives", "local-ko-never-cleanup"))
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
    counters = {"_battle_evidence": [
        _ScriptedObserver(link, lambda calls, i=i: rows_for(calls)[i]) for i in (0, 1)
    ]}
    if case in ("local-never-ready", "local-ko-never-cleanup"):
        expected = (
            "['settled snapshot is missing']" if case == "local-never-ready"
            else "['KO outcome lacks EndOfBattle evidence']"
        )
        with pytest.raises(AssertionError) as caught:
            _wait_for_settled_battle_evidence(link, counters)
        assert str(caught.value).startswith(f"battle settlement evidence failed: {expected}; rows=")
        assert link.calls == 120
    else:
        _wait_for_settled_battle_evidence(link, counters)
        assert link.calls == (0 if case == "local-nonko-ready" else 2)
