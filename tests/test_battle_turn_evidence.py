"""ROM-free regressions for the authentic battle acceptance driver."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests import test_pyboy_link_session_roms as battle


def paired_snapshots():
    player = {"species": 25, "hp": 27, "max_hp": 40, "status": 0}
    enemy = {"species": 54, "hp": 18, "max_hp": 35, "status": 8}
    return [
        {"boundary": "MainInBattleLoop", "result": 0, "cleanup_complete": False,
         "in_battle": 2, "player": player, "enemy": enemy},
        {"boundary": "MainInBattleLoop", "result": 0, "cleanup_complete": False,
         "in_battle": 2, "player": deepcopy(enemy), "enemy": deepcopy(player)},
    ]


def test_damage_entry_and_one_peer_boundary_do_not_prove_settlement():
    counters = {"PlayerCalcMoveDamage": [1, 1], "settled_turn_boundaries": [None, None]}
    assert not battle._battle_turn_is_settled(counters)
    counters["settled_turn_boundaries"][0] = paired_snapshots()[0]
    assert not battle._battle_turn_is_settled(counters)


def test_mirrored_next_turn_snapshots_prove_settlement():
    assert battle._battle_turn_is_settled({"settled_turn_boundaries": paired_snapshots()})


@pytest.mark.parametrize("field,value", [("hp", 26), ("status", 0), ("species", 12), ("max_hp", 99)])
def test_divergent_next_turn_snapshots_fail(field, value):
    snapshots = paired_snapshots()
    snapshots[1]["player"][field] = value
    assert not battle._battle_turn_is_settled({"settled_turn_boundaries": snapshots})


@pytest.mark.parametrize("hp,max_hp", [(-1, 40), (41, 40), (1, 0)])
def test_mirrored_invalid_hp_does_not_prove_settlement(hp, max_hp):
    snapshots = paired_snapshots()
    for mon in (snapshots[0]["player"], snapshots[1]["enemy"]):
        mon.update(hp=hp, max_hp=max_hp)
    assert not battle._battle_turn_is_settled({"settled_turn_boundaries": snapshots})


def test_boundary_capture_requires_exchange_and_execution(monkeypatch):
    class Symbols:
        def __contains__(self, name):
            return name in battle._BATTLE_DIAG_SYMBOLS

        def bank_addr(self, name):
            return 0, name

    sessions = []
    for _ in range(2):
        hooks = {}
        sessions.append(SimpleNamespace(
            symbols=Symbols(),
            _pyboy=SimpleNamespace(hook_register=lambda bank, addr, callback, ctx, hooks=hooks: hooks.update({addr: callback})),
            hooks=hooks,
        ))
    snapshots = paired_snapshots()
    monkeypatch.setattr(battle, "_battle_turn_snapshot", lambda session: snapshots[sessions.index(session)])
    counters = battle._install_battle_diag_counters(*sessions)
    for side, session in enumerate(sessions):
        hook = session.hooks["MainInBattleLoop"]
        hook(None)
        counters["PlayerCalcMoveDamage"][side] = 1
        hook(None)
        assert counters["settled_turn_boundaries"][side] is None
        counters["LinkBattleExchangeData"][side] = 1
        hook(None)
        assert counters["settled_turn_boundaries"][side] is None
        counters["ExecutePlayerMove"][side] = 1
        hook(None)
        assert counters["settled_turn_boundaries"][side] == snapshots[side]
    assert battle._battle_turn_is_settled(counters)


def test_driver_continues_after_damage_and_honors_chunk_budget(monkeypatch):
    monkeypatch.setattr(battle, "_LINK_CHUNK_CYCLES", 16)
    counters = {name: [1, 1] for name in battle._BATTLE_DIAG_SYMBOLS}
    counters["settled_turn_boundaries"] = [None, None]
    calls = []
    presses = [[], []]
    sessions = [SimpleNamespace(
        symbols=SimpleNamespace(addr_of=lambda name: 0),
        _pyboy=SimpleNamespace(memory=[2]),
        press=lambda button, duration, side=side: presses[side].append(button),
    ) for side in range(2)]

    def step(frames, *, chunk_cycles):
        calls.append((frames, chunk_cycles))
        side = len(calls) - 1
        counters["settled_turn_boundaries"][side] = paired_snapshots()[side]

    diag = battle._drive_complete_battle_turn(
        *sessions, SimpleNamespace(step_interleaved=step), counters=counters,
        battle_budget_frames=100, step_frames=20,
    )
    assert calls == [(20, 16), (20, 16)]
    assert presses == [[], ["a"]]
    assert diag["turn_settled"]


def test_colosseum_navigation_honors_chunk_budget(monkeypatch):
    monkeypatch.setattr(battle, "_LINK_CHUNK_CYCLES", 16)
    monkeypatch.setattr(battle, "_drive_two_sessions_to_link_menu", lambda *args: {
        "counters": {"LinkMenu": [1, 1]}, "frames_used": 100,
    })
    calls = []

    def step(frames, *, chunk_cycles):
        calls.append((frames, chunk_cycles))

    def state():
        return SimpleNamespace(overworld=SimpleNamespace(
            map_id=battle.COLOSSEUM_MAP_ID if len(calls) >= 3 else 0,
        ))

    session = SimpleNamespace(
        symbols=SimpleNamespace(addr_of=lambda name: 0),
        _pyboy=SimpleNamespace(memory=[1]),
        press=lambda *args, **kwargs: None,
        read_game_state=state,
    )
    diag = battle._drive_past_link_menu_to_colosseum(
        session, session, SimpleNamespace(step_interleaved=step),
    )
    assert calls == [(60, 16), (40, 16), (20, 16)]
    assert diag["final_map_a"] == battle.COLOSSEUM_MAP_ID


def test_faint_before_opponent_action_is_a_settled_turn():
    snapshots = paired_snapshots()
    snapshots[0]["boundary"] = "HandleEnemyMonFainted"
    snapshots[1]["boundary"] = "HandlePlayerMonFainted"
    snapshots[0]["enemy"]["hp"] = snapshots[1]["player"]["hp"] = 0
    counters = {
        "LinkBattleExchangeData": [1, 1],
        "ExecutePlayerMove": [1, 0], "ExecuteEnemyMove": [0, 1],
        "PlayerCalcMoveDamage": [0, 0],
        "settled_turn_boundaries": snapshots,
    }
    assert battle._battle_turn_is_settled(counters)


def test_faint_hook_with_nonzero_hp_is_not_settled():
    snapshots = paired_snapshots()
    snapshots[0]["boundary"] = "HandleEnemyMonFainted"
    assert not battle._battle_turn_is_settled({"settled_turn_boundaries": snapshots})


@pytest.mark.parametrize("results", [(0, 1), (1, 0), (2, 2)])
def test_terminal_turn_requires_cleanup_and_mirrored_results(results):
    snapshots = paired_snapshots()
    for side, snapshot in enumerate(snapshots):
        snapshot.update(boundary="EndOfBattle", result=results[side])
    # Zero HP is valid in final battle state.
    snapshots[0]["enemy"]["hp"] = snapshots[1]["player"]["hp"] = 0
    counters = {"settled_turn_boundaries": snapshots}
    assert not battle._battle_turn_is_settled(counters)
    sessions = [SimpleNamespace(
        symbols=SimpleNamespace(addr_of=lambda name: 0),
        _pyboy=SimpleNamespace(memory=[state]),
    ) for state in (0, 2)]
    battle._observe_battle_cleanup(*sessions, counters)
    assert not battle._battle_turn_is_settled(counters)
    sessions[1]._pyboy.memory = [0]
    battle._observe_battle_cleanup(*sessions, counters)
    assert battle._battle_turn_is_settled(counters)


@pytest.mark.parametrize("results", [(0, 0), (1, 1), (0, 2), (2, 1), (255, 255)])
def test_contradictory_terminal_results_fail(results):
    snapshots = paired_snapshots()
    for side, snapshot in enumerate(snapshots):
        snapshot.update(boundary="EndOfBattle", result=results[side], cleanup_complete=True)
    assert not battle._battle_turn_is_settled({"settled_turn_boundaries": snapshots})


def test_terminal_hp_divergence_fails_even_after_cleanup():
    snapshots = paired_snapshots()
    for side, snapshot in enumerate(snapshots):
        snapshot.update(boundary="EndOfBattle", result=side, cleanup_complete=True)
    snapshots[0]["enemy"]["hp"] -= 1
    assert not battle._battle_turn_is_settled({"settled_turn_boundaries": snapshots})


def test_importing_driver_does_not_construct_pyboy_with_assets_present(monkeypatch):
    import importlib.util
    from pathlib import Path
    import pyboy

    def unexpected_emulator(*args, **kwargs):
        pytest.fail("Importing battle helpers must not construct an emulator")

    monkeypatch.setattr(pyboy, "PyBoy", unexpected_emulator)
    # Force the previous import-time probe's asset condition to be true.
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    spec = importlib.util.spec_from_file_location("battle_import_probe", battle.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._fixtures_ready
