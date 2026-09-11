"""ROM-free checks for remote turn observation and fail-closed verification."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tests import _tcp_trade_peer as peer


_MATRIX_PATH = Path(__file__).resolve().parents[1] / "scripts" / "tcp_link_matrix.py"
_SPEC = importlib.util.spec_from_file_location("battle_evidence_matrix", _MATRIX_PATH)
matrix = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = matrix
_SPEC.loader.exec_module(matrix)


class ReadOnlyMemory:
    def __init__(self, data):
        self.data = bytes(data)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        raise AssertionError("Battle observation must never write game memory")


def make_session(side=0, *, faint=False, result=0, in_battle=2):
    player = {"Species": 25, "HP": 257, "MaxHP": 400, "Status": 8}
    enemy = {"Species": 54, "HP": 0 if faint else 18, "MaxHP": 35, "Status": 0}
    if side:
        player, enemy = enemy, player
    values = {"wIsInBattle": in_battle, "wBattleResult": result}
    values.update({"wBattleMon" + key: value for key, value in player.items()})
    values.update({"wEnemyMon" + key: value for key, value in enemy.items()})
    data = bytearray()
    addresses = {}
    for name, value in values.items():
        addresses[name] = len(data)
        data.extend(value.to_bytes(2 if name.endswith("HP") else 1, "big"))

    class Symbols:
        def __contains__(self, name):
            return name in peer._TRADE_DIAG_SYMBOLS or name in addresses

        def bank_addr(self, name):
            return 0, name

        def addr_of(self, name):
            return addresses[name]

    hooks = {}
    presses = []
    return SimpleNamespace(
        symbols=Symbols(),
        _pyboy=SimpleNamespace(
            memory=ReadOnlyMemory(data),
            hook_register=lambda bank, addr, callback, ctx: hooks.update({addr: callback}),
        ),
        hooks=hooks,
        presses=presses,
        press=lambda button, duration: presses.append((button, duration)),
    )


def install_observer(session):
    counters = {name: [0] for name in peer._TRADE_DIAG_SYMBOLS}
    observer = peer._BattleTurnObserver(session, counters)
    for name in counters:
        peer._install_hook(session, name, counters[name], observer.observe)
    return observer, counters


def record_turn(side=0, *, boundary="MainInBattleLoop", cleanup=True, result=0):
    session = make_session(side, faint="Fainted" in boundary, result=result)
    observer, counters = install_observer(session)
    for name in (
        "DisplayLinkBattleVersusTextBox", "MainInBattleLoop",
        "LinkBattleExchangeData", "ExecuteEnemyMove" if side else "ExecutePlayerMove",
        boundary,
    ):
        session.hooks[name](None)
    if boundary == "EndOfBattle" and cleanup:
        # Simulate ROM-owned cleanup by replacing the immutable fake view.
        session._pyboy.memory = make_session(side, result=result, in_battle=0)._pyboy.memory
        observer.observe_cleanup()
    return {**{name: count[0] for name, count in counters.items()}, "battle_turn": deepcopy(observer.snapshot)}


def test_real_hook_callbacks_capture_mirrored_status_turn_without_damage():
    results = [record_turn(0), record_turn(1)]
    assert [result["PlayerCalcMoveDamage"] for result in results] == [0, 0]
    assert results[0]["battle_turn"]["player"]["hp"] == 257  # big-endian RAM read
    assert matrix._verify_battle_turns(results) == []


def test_initial_loop_and_damage_entry_do_not_capture_outcome():
    session = make_session()
    observer, counters = install_observer(session)
    for name in ("MainInBattleLoop", "LinkBattleExchangeData", "PlayerCalcMoveDamage"):
        session.hooks[name](None)
    assert observer.snapshot is None
    session.hooks["ExecutePlayerMove"](None)
    assert observer.snapshot is None
    session.hooks["MainInBattleLoop"](None)
    first = deepcopy(observer.snapshot)
    assert first["exchange_count"] == 1
    session.hooks["LinkBattleExchangeData"](None)
    session.hooks["MainInBattleLoop"](None)
    assert observer.snapshot == first  # first qualifying outcome stays frozen


def test_execution_without_initial_battle_loop_cannot_capture():
    session = make_session(faint=True)
    observer, _ = install_observer(session)
    for name in ("LinkBattleExchangeData", "ExecutePlayerMove", "HandleEnemyMonFainted"):
        session.hooks[name](None)
    assert observer.snapshot is None


@pytest.mark.parametrize("events", [
    ("MainInBattleLoop", "ExecutePlayerMove", "LinkBattleExchangeData", "MainInBattleLoop"),
    ("MainInBattleLoop", "LinkBattleExchangeData", "ExecutePlayerMove", "LinkBattleExchangeData", "MainInBattleLoop"),
])
def test_execution_must_follow_the_current_exchange(events):
    session = make_session()
    observer, _ = install_observer(session)
    for name in events:
        session.hooks[name](None)
    assert observer.snapshot is None
    session.hooks["ExecutePlayerMove"](None)
    session.hooks["MainInBattleLoop"](None)
    assert observer.ready


def test_faint_before_opponent_execution_is_verified():
    results = [
        record_turn(0, boundary="HandleEnemyMonFainted"),
        record_turn(1, boundary="HandlePlayerMonFainted"),
    ]
    assert results[0]["ExecuteEnemyMove"] == results[1]["ExecutePlayerMove"] == 0
    assert matrix._verify_battle_turns(results) == []


@pytest.mark.parametrize("results", [(0, 1), (1, 0), (2, 2)])
def test_terminal_outcomes_require_complementary_results_and_cleanup(results):
    rows = [record_turn(side, boundary="EndOfBattle", result=results[side]) for side in (0, 1)]
    assert matrix._verify_battle_turns(rows) == []
    rows[0]["battle_turn"]["cleanup_complete"] = False
    assert "cleanup is incomplete" in matrix._verify_battle_turns(rows)[0]


@pytest.mark.parametrize("results", [[], [{}], [{}, {}], [None, {}]])
def test_absent_evidence_fails_closed(results):
    assert matrix._verify_battle_turns(results)


@pytest.mark.parametrize("path,value", [
    (("battle_turn",), None),
    (("battle_turn", "schema"), True),
    (("battle_turn", "boundary"), []),
    (("battle_turn", "in_battle"), 0),
    (("battle_turn", "player", "hp"), True),
    (("battle_turn", "player", "hp"), -1),
    (("battle_turn", "player", "hp"), 401),
    (("battle_turn", "player", "max_hp"), 0),
    (("battle_turn", "player", "species"), 0),
    (("battle_turn", "player", "status"), 256),
    (("battle_turn", "exchange_count"), 0),
    (("battle_turn", "exchange_count"), "1"),
    (("battle_turn", "player_execute_count"), 0),
    (("battle_turn", "loop_count"), 1),
    (("LinkBattleExchangeData",), 0),
    (("ExecutePlayerMove",), 0),
    (("DisplayLinkBattleVersusTextBox",), True),
])
def test_malformed_or_unproven_snapshot_fails_closed(path, value):
    results = [record_turn(0), record_turn(1)]
    target = results[0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert matrix._verify_battle_turns(results)


@pytest.mark.parametrize("field,value", [("hp", 256), ("max_hp", 401), ("species", 54), ("status", 0)])
def test_mismatched_combatants_fail(field, value):
    results = [record_turn(0), record_turn(1)]
    results[0]["battle_turn"]["player"][field] = value
    assert matrix._verify_battle_turns(results) == ["battle peers disagree on settled combatant state"]


def test_identical_state_from_different_exchanges_fails():
    results = [record_turn(0), record_turn(1)]
    results[1]["battle_turn"]["exchange_count"] = 2
    results[1]["battle_turn"]["execution_exchange_count"] = 2
    results[1]["LinkBattleExchangeData"] = 2
    assert matrix._verify_battle_turns(results) == ["battle snapshots describe different move exchanges"]


def test_terminal_entry_without_cleanup_is_not_ready():
    session = make_session()
    observer, _ = install_observer(session)
    for name in ("MainInBattleLoop", "LinkBattleExchangeData", "ExecutePlayerMove", "EndOfBattle"):
        session.hooks[name](None)
    observer.observe_cleanup()
    assert not observer.ready
    session._pyboy.memory = make_session(in_battle=0)._pyboy.memory
    observer.observe_cleanup()
    assert observer.ready
    assert observer.snapshot["in_battle"] == 2
    assert observer.snapshot["post_cleanup_in_battle"] == 0


def test_terminal_result_disagreement_fails():
    results = [record_turn(side, boundary="EndOfBattle", result=0) for side in (0, 1)]
    assert matrix._verify_battle_turns(results) == ["battle terminal results are not complementary"]


def test_driver_keeps_ticking_after_local_boundary_without_another_move(monkeypatch):
    session = make_session()
    observer, _ = install_observer(session)
    ticks = []
    announcements = []
    shots = []
    monkeypatch.setattr(peer.time, "monotonic", lambda: len(ticks))

    def step(frames):
        ticks.append(frames)
        if len(ticks) == 1:
            for name in ("MainInBattleLoop", "LinkBattleExchangeData", "ExecutePlayerMove", "MainInBattleLoop"):
                session.hooks[name](None)

    session.step = step
    backend = SimpleNamespace(
        announce_sync=lambda *, sync_id: announcements.append(sync_id),
        poll_peer_sync=lambda *, sync_id: len(ticks) >= 3,
    )
    peer._drive_battle_to_settled_turn(
        session, backend, observer, deadline=10, shot=shots.append, log=lambda message: None,
    )
    assert ticks == [20, 20, 20]
    assert session.presses == [("a", 4)]
    assert announcements == [14]
    assert shots == ["05_battle_settled"]


def test_damage_entry_and_peer_marker_cannot_end_driver(monkeypatch):
    session = make_session()
    observer, _ = install_observer(session)
    for name in ("MainInBattleLoop", "LinkBattleExchangeData", "PlayerCalcMoveDamage"):
        session.hooks[name](None)
    ticks = []
    session.step = ticks.append
    monkeypatch.setattr(peer.time, "monotonic", lambda: len(ticks))
    backend = SimpleNamespace(
        announce_sync=lambda **kwargs: pytest.fail("Damage entry cannot announce settlement"),
        poll_peer_sync=lambda **kwargs: True,
    )
    with pytest.raises(RuntimeError, match="not observed on both peers before deadline"):
        peer._drive_battle_to_settled_turn(
            session, backend, observer, deadline=3,
            shot=lambda phase: None, log=lambda message: None,
        )
    assert ticks == [20, 20, 20]


def test_matrix_row_rejects_hook_only_success_even_with_all_screenshots(tmp_path, monkeypatch):
    peers = []
    for side, role in enumerate(("listen", "connect")):
        result = record_turn(side)
        result.pop("battle_turn")
        result["PlayerCalcMoveDamage"] = 1
        peers.append(matrix.PeerRun(role, "red_color", 0, "", "", result))
        for phase in matrix.BATTLE_PHASES:
            (tmp_path / f"row.{phase}.{role}.red_color.png").write_bytes(b"png")
    monkeypatch.setattr(matrix, "_verify_palette", lambda **kwargs: ({}, []))
    verification = matrix._verify_run(
        repo_root=tmp_path, mode="battle", run_dir=tmp_path,
        label="row", peers=peers, timed_out=False,
    )
    assert not verification["ok"]
    assert any("settled snapshot is missing" in error for error in verification["errors"])
