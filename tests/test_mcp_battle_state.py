"""ROM-free MCP resource/epoch exposure for the additive battle state.

Split from ``tests/test_mcp_battle_state.py`` (#122); every node ID, assertion,
and fixture is unchanged.  The parser/phase truth table now lives in
``tests/test_mcp_battle_state_parse.py`` and shared helpers in
``tests/_mcp_battle_state_support.py``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading

import pytest

from pokered_harness.events import EventBus
from pokered_harness.mcp_server import (
    LinkState,
    _resource_specs,
    dispatch_tool,
    read_resource,
)
from pokered_harness.session import Session
from pokered_harness.state.battle import BattlePhase
from tests._mcp_battle_state_support import (
    _BATTLE_END_HOOK,
    _CURRENT_MENU_ITEM,
    _MENU_OPEN_HOOKS,
    _PLAYER_STAT_MOD_BASE,
    ROOT,
    _session,
    _symbols,
    _write_enemy,
)
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy


def test_game_state_resource_contains_additive_battle_fields():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # trainer battle
    session._pyboy.memory[0xC005] = 1  # wActionResultOrTookBattleTurn
    _write_enemy(session._pyboy.memory)
    body = json.loads(read_resource(session, "pokered://game-state"))
    battle = body["battle"]
    assert battle["phase"] == int(BattlePhase.ACTION_RESOLUTION)
    assert battle["phase_valid"] is True
    assert "wMoveMenuType" in battle["phase_evidence"]
    assert battle["enemy_mon_valid"] is True
    assert battle["enemy_mon"]["hp"] == 32
    assert battle["enemy_mon"]["max_hp"] == 40
    assert battle["enemy_mon"]["pp"] == [20, 25, 0, 0]
    # The raw byte is always exposed; an unended battle never confirms it.
    assert battle["raw_battle_result"] == 0
    assert battle["terminal_result"] is None


def test_game_state_resource_exposes_menu_and_transient_fields():
    session = _session()
    mem = session._pyboy.memory
    mem[0xC000] = 2  # trainer battle
    mem[0xC004] = 1  # wPlayerMoveListIndex
    mem[_CURRENT_MENU_ITEM] = 2  # wCurrentMenuItem
    for offset, raw in enumerate((7, 8, 7, 7, 7, 7)):
        mem[_PLAYER_STAT_MOD_BASE + offset] = raw
    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    battle = json.loads(read_resource(session, "pokered://game-state"))["battle"]
    assert battle["phase"] == int(BattlePhase.COMMAND_SELECTION)
    assert battle["menu_open"] is True
    assert battle["menu_evidence"][:1] == ["SelectMenuItem"]
    assert battle["player_move_list_index"] == 1
    assert battle["current_menu_item"] == 2
    assert battle["player_stat_stages"] == {
        "attack": 0,
        "defense": 1,
        "speed": 0,
        "special": 0,
        "accuracy": 0,
        "evasion": 0,
        "valid": True,
    }


def test_game_state_resource_embeds_snapshot_epoch():
    session = _session()
    dispatch_tool(session, "step", {"count": 4})
    body = json.loads(read_resource(session, "pokered://game-state"))
    assert body["epoch"] == {
        "tick": 4,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": session.session_id,
    }


def test_session_read_state_snapshot_pairs_state_with_epoch():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    snapshot = session.read_state_snapshot()
    assert snapshot.state.battle is not None
    assert snapshot.epoch == session.read_epoch()
    assert snapshot.epoch.session_id == session.session_id


def test_replacement_sessions_have_distinct_identities():
    first = _session()
    second = _session()
    assert first.session_id != second.session_id
    assert read_resource(first, "pokered://state-epoch") != read_resource(
        second, "pokered://state-epoch"
    )


def test_session_identities_are_distinct_across_process_restarts():
    """Two fresh processes must not report the same first-session identity.

    A bare per-process counter restarts at 1 in every process, so a client
    comparing epochs across a server restart could accept a stale snapshot.
    """
    code = (
        "import json;"
        "from pokered_harness.events import EventBus;"
        "from pokered_harness.session import Session;"
        "from pokered_harness.symbols.loader import load_sym_text;"
        "from tests.conftest import DictMemory;"
        "from tests.fakes import FakePyBoy;"
        "s = Session(pyboy=FakePyBoy(DictMemory()),"
        " symbols=load_sym_text('00:C000 wIsInBattle'), event_bus=EventBus());"
        "print(json.dumps({'session_id': s.session_id, 'epoch': "
        "s.read_epoch().session_id}))"
    )
    runs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"},
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        for _ in range(2)
    ]
    for run in runs:
        assert run["session_id"] == run["epoch"]
    assert runs[0]["session_id"] != runs[1]["session_id"], runs


def test_peer_game_state_resource_embeds_peer_epoch():
    primary = _session()
    peer = _session()
    peer._pyboy.memory[0xC000] = 1  # wild battle on the peer
    link = LinkState(peer_session=peer)
    body = json.loads(read_resource(primary, "pokered://peer-game-state", link=link))
    assert body["battle"]["raw_is_in_battle"] == 1
    assert body["epoch"] == {
        "tick": 0,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": peer.session_id,
    }


def test_state_epoch_resource_tracks_session_tick():
    session = _session()
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 0,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": session.session_id,
    }
    dispatch_tool(session, "step", {"count": 3})
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 3,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": session.session_id,
    }


def test_state_epoch_reset_generation_advances_on_reset_tick():
    session = _session()
    dispatch_tool(session, "step", {"count": 3})
    session.reset_tick(3)
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 3,
        "load_generation": 0,
        "reset_generation": 1,
        "session_id": session.session_id,
    }


def test_state_epoch_load_generation_advances_on_load_state():
    session = _session()
    dispatch_tool(session, "step", {"count": 5})
    payload = base64.b64encode(b"SNAPSHOT").decode("ascii")
    dispatch_tool(session, "load_state", {"data": payload})
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 5,
        "load_generation": 1,
        "reset_generation": 0,
        "session_id": session.session_id,
    }


def test_read_game_state_keeps_ambiguous_zero_unknown_on_battle_end():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # battle active
    active = session.read_game_state()
    assert active.battle is not None
    assert active.battle.terminal_result is None
    # EndOfBattle clears wIsInBattle but leaves wBattleResult at zero for a
    # win; a blackout or escape also leaves/clears zero, so the outcome stays
    # unknown even though the battle is observed to end.
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None
    assert ended.battle.raw_battle_result == 0
    # Confirmation is edge-triggered: a later overworld read is inactive.
    later = session.read_game_state()
    assert later.battle is not None
    assert later.battle.phase is BattlePhase.INACTIVE
    assert later.battle.terminal_result is None


def test_read_game_state_confirms_surviving_nonzero_outcome():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC001] = 1  # player-faint marker while active
    assert session.read_game_state().battle.terminal_result is None
    # The ROM's own EndOfBattle is what attributes the surviving byte to this
    # end; the session samples the outcome bytes at that routine's entry.
    session._pyboy.fire(*_BATTLE_END_HOOK)
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 1  # survived teardown
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.terminal_result == 1


def test_read_game_state_requires_rom_observed_end_to_promote_result():
    # Without the EndOfBattle hook a client-timed read cannot attribute a
    # surviving byte to this end, so promotion stays disabled.
    mem = DictMemory({0xC000: 2, 0xC001: 1})
    session = Session(pyboy=FakePyBoy(mem), symbols=_symbols(), event_bus=EventBus())
    assert session.enable_battle_end_observation() is True
    assert session.read_game_state().battle.terminal_result is None
    # No hook fired: the falling edge alone must not promote the stale byte.
    session._pyboy.memory[0xC000] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


def test_unsampled_escape_does_not_promote_stale_player_faint_result():
    """An escape that finishes between two reads must not confirm a faint.

    ``SwitchAndTeleportEffect``/``ItemUsePokeDoll`` set ``wEscapedFromBattle``
    and ``EndOfBattle`` clears it on the way out without touching
    ``wBattleResult``.  A player faint earlier in the same battle leaves a
    ``1`` there, so a reader that only compares snapshots would promote that
    stale byte as if the escape had been a loss.
    """
    session = _session()
    session._pyboy.memory[0xC000] = 1  # live wild battle
    session._pyboy.memory[0xC001] = 1  # earlier player-faint result byte
    assert session.read_game_state().battle.terminal_result is None
    # Escape flag is set and then cleared again entirely between reads; the
    # session's EndOfBattle hook still samples both bytes for this end.
    session._pyboy.memory[0xC006] = 1  # wEscapedFromBattle
    session._pyboy.fire(*_BATTLE_END_HOOK)
    session._pyboy.memory[0xC000] = 0  # EndOfBattle clears wIsInBattle
    session._pyboy.memory[0xC006] = 0  # ... and wEscapedFromBattle
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


@pytest.mark.parametrize("transition", ["load", "reset"])
def test_pending_end_sample_cannot_cross_an_epoch(transition):
    """A sampled ``EndOfBattle`` outcome must not outlive its epoch.

    ``load_state`` and ``reset_tick`` start a new observation epoch and drop the
    battle lifecycle, but the pending ``EndOfBattle`` sample was taken while the
    *previous* emulated instant was running.  If it survived, a later read that
    merely crosses the active->inactive transition would promote the old
    epoch's outcome -- reporting a battle end this epoch never observed.
    """
    session = _session()
    try:
        session._pyboy.memory[0xC000] = 2  # live link battle
        session._pyboy.memory[0xC001] = 1  # wBattleResult: player loss
        session.read_game_state()
        session._pyboy.fire(*_BATTLE_END_HOOK)
        if transition == "load":
            session.load_state(b"state loaded after EndOfBattle entry")
        else:
            session.reset_tick(0)
        # The new epoch is already past EndOfBattle entry, so its routine never
        # runs again and the pre-epoch sample must not be attributed to it.
        session._pyboy.memory[0xC001] = 0
        assert session.read_game_state().battle.terminal_result is None
        session._pyboy.memory[0xC000] = 0
        ended = session.read_game_state()
        assert ended.battle is not None
        assert ended.battle.raw_battle_result == 0
        assert ended.battle.terminal_result is None
    finally:
        session.close()


def test_read_game_state_blackout_zero_does_not_confirm_win():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC001] = 1  # player-faint handler ran while active
    session.read_game_state()
    # ResetStatusAndHalveMoneyOnBlackout zeroes both bytes before the
    # overworld read; the captured faint byte cannot be turned into a win.
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


def test_read_game_state_escape_zero_does_not_confirm_win():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC006] = 1  # wEscapedFromBattle observed while active
    session.read_game_state()
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    session._pyboy.memory[0xC006] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


def test_load_state_resets_battle_lifecycle():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # active battle observed
    assert session.read_game_state().battle is not None
    # The loaded state is an overworld snapshot; its zero must not be
    # qualified by the pre-load active-battle history.
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    payload = base64.b64encode(b"OVERWORLD").decode("ascii")
    dispatch_tool(session, "load_state", {"data": payload})
    loaded = session.read_game_state()
    assert loaded.battle is not None
    assert loaded.battle.phase is BattlePhase.INACTIVE
    assert loaded.battle.phase_valid is True
    assert loaded.battle.terminal_result is None


def test_reset_tick_resets_battle_lifecycle():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session.read_game_state()
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    session.reset_tick(0)
    loaded = session.read_game_state()
    assert loaded.battle is not None
    assert loaded.battle.phase is BattlePhase.INACTIVE
    assert loaded.battle.terminal_result is None


class _BlockingMemory(DictMemory):
    """DictMemory that parks the first read of ``block_addr`` on an event."""

    def __init__(
        self, *, block_addr: int, entered: threading.Event, release: threading.Event
    ) -> None:
        super().__init__()
        self._block_addr = block_addr
        self._entered = entered
        self._release = release
        self._blocked = False

    def __getitem__(self, key):
        if key == self._block_addr and not self._blocked:
            self._blocked = True
            self._entered.set()
            if not self._release.wait(timeout=5):
                raise AssertionError("snapshot parse was never released")
        return super().__getitem__(key)


def test_state_snapshot_holds_owner_lock_across_parse_and_epoch():
    entered = threading.Event()
    release = threading.Event()
    memory = _BlockingMemory(block_addr=0xC000, entered=entered, release=release)
    session = Session(pyboy=FakePyBoy(memory), symbols=_symbols(), event_bus=EventBus())
    captured: dict[str, object] = {}

    def snapshot() -> None:
        captured["snapshot"] = session.read_state_snapshot()

    def step() -> None:
        session.step(1)
        captured["stepped"] = True

    snapshot_thread = threading.Thread(target=snapshot)
    snapshot_thread.start()
    assert entered.wait(timeout=5)
    step_thread = threading.Thread(target=step)
    step_thread.start()
    # The owner lock is held across parsing and epoch capture, so a
    # concurrent step cannot advance the tick mid-snapshot.
    assert "stepped" not in captured
    release.set()
    snapshot_thread.join(timeout=5)
    step_thread.join(timeout=5)
    assert not snapshot_thread.is_alive() and not step_thread.is_alive()
    result = captured["snapshot"]
    assert result.epoch.tick == 0
    assert result.epoch.session_id == session.session_id


def test_resource_specs_advertise_state_epoch():
    names = {spec.name for spec in _resource_specs()}
    assert "State Epoch" in names
    uris = {str(spec.uri) for spec in _resource_specs()}
    assert "pokered://state-epoch" in uris
