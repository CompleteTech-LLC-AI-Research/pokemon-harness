"""Asset-free checks of actual TCP peer battle/menu orchestration methods."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _tcp_trade_peer_drive_battle as battle_driver
from tests import _tcp_trade_peer_drive_support as drive_support
from tests._tcp_trade_peer_drive_battle import _PeerDriveBattleMixin
from tests._tcp_trade_peer_drive_support import _PeerDriveSupportMixin
from tests._tcp_trade_peer_link_menu import _TRADE_DIAG_SYMBOLS

_CURSOR, _MAX_ITEM, _WATCHED_KEYS, _SERIAL_STATUS = range(4)


class _Memory(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cursor_reads: list[int] = []

    def __getitem__(self, key):
        if key == _CURSOR and self.cursor_reads:
            return self.cursor_reads.pop(0)
        return super().__getitem__(key)


class _Session:
    def __init__(self, owner, *, cursor: int | None = 0):
        self.owner = owner
        memory = {_MAX_ITEM: 1, _WATCHED_KEYS: 1, _SERIAL_STATUS: 2}
        if cursor is not None:
            memory[_CURSOR] = cursor
        self._pyboy = SimpleNamespace(memory=_Memory(memory))
        self.symbols = SimpleNamespace(addr_of=self._address)
        self.steps: list[int] = []
        self.presses: list[tuple[str, int]] = []
        self.tick = 0

    @staticmethod
    def _address(name: str) -> int:
        return {
            "wCurrentMenuItem": _CURSOR,
            "wMaxMenuItem": _MAX_ITEM,
            "wMenuWatchedKeys": _WATCHED_KEYS,
            "hSerialConnectionStatus": _SERIAL_STATUS,
        }[name]

    def press(self, button: str, duration: int = 1) -> None:
        self.presses.append((button, duration))
        self.owner.events.append(("press", button, duration))
        if button == "down" and _CURSOR in self._pyboy.memory:
            self._pyboy.memory[_CURSOR] = self._pyboy.memory[_CURSOR] + 1
        elif button == "up" and _CURSOR in self._pyboy.memory:
            self._pyboy.memory[_CURSOR] = max(0, self._pyboy.memory[_CURSOR] - 1)
        elif button == "a":
            if len([key for key, _duration in self.presses if key == "a"]) == 1:
                self._pyboy.memory[_CURSOR] = 1
                self._pyboy.memory[_MAX_ITEM] = 1
            else:
                self._pyboy.memory[_CURSOR] = 1
                self._pyboy.memory[_MAX_ITEM] = 4

    def step(self, frames: int) -> None:
        self.steps.append(frames)
        self.tick += frames
        if frames == 40 and self.owner.cursor_after_settle is not None:
            self._pyboy.memory.cursor_reads = [1, self.owner.cursor_after_settle]
        if self.owner.stage == "turn-loop":
            self.owner.turn_steps += 1
            if self.owner.turn_steps >= 2 and not self.owner.settle_after_step:
                self.owner.counters["EndOfBattle"][0] = 1
            if self.owner.settle_after_step and self.owner.turn_steps >= 1:
                self.owner.observer.settled = True

    def current_tick(self) -> int:
        return self.tick

    def read_game_state(self):
        return SimpleNamespace(overworld=SimpleNamespace(map_id=0xF0))


class _Observer:
    def __init__(self, owner):
        self.owner = owner
        self.settled = False
        self.reads = 0

    def snapshot(self):
        self.reads += 1
        return {"settled": self.settled}


class _Backend:
    def __init__(self, owner):
        self.owner = owner
        self.announced: list[int] = []
        self.polled: list[int] = []

    def announce_sync(self, *, sync_id: int) -> None:
        self.announced.append(sync_id)
        if sync_id == 14:
            self.owner.deadline = time.monotonic() + 0.01

    def poll_peer_sync(self, *, sync_id: int) -> bool:
        self.polled.append(sync_id)
        if sync_id == 112:
            return True
        if sync_id == 14 and self.owner.stop_after_second_poll and self.polled.count(14) == 2:
            raise _StopAtPeerPoll
        return sync_id == 14 and self.owner.peer_marker

    def wait_for_wire_idle(self, **_kwargs) -> None:
        return None

    def service_pending_edges(self, **_kwargs) -> int:
        return 0

    def debug_snapshot(self) -> dict:
        return {}


class _StopAtPeerPoll(Exception):
    pass


class _StopAtSelection(Exception):
    pass


class _BattleHarness(_PeerDriveBattleMixin, _PeerDriveSupportMixin):
    def __init__(
        self,
        *,
        cursor: int | None = 0,
        cursor_after_settle: int | None = None,
        settle_after_step: bool = False,
        peer_marker: bool = False,
        stop_after_second_poll: bool = False,
        stop_at_selection: bool = False,
    ):
        self.args = SimpleNamespace(role="local")
        self.deadline = time.monotonic() + 5.0
        self.link_menu_max = 1
        self.counters = defaultdict(lambda: [0])
        for name in _TRADE_DIAG_SYMBOLS:
            self.counters[name]
        for name in (
            "CableClub_DoBattleOrTrade",
            "DisplayLinkBattleVersusTextBox",
            "BattleTransition",
            "MainInBattleLoop",
            "DisplayBattleMenu",
            "DisplayBattleMenu.leftColumn_WaitForInput",
            "MoveSelectionMenu",
            "MoveSelectionMenu.menuset",
        ):
            self.counters[name][0] = 1
        self.session = _Session(self, cursor=cursor)
        self.backend = _Backend(self)
        self.link = SimpleNamespace(
            _network_backend=self.backend,
            _network_is_internal_clock=True,
            _network_frame_barrier=False,
            set_network_frame_barrier=lambda _enabled: None,
        )
        self.observer = _Observer(self)
        self.battle_observer = self.observer
        self.battle_turn_announced = False
        self.peer_battle_turn_ready = False
        self.drive_status = "running"
        self.drive_error = None
        self.stage = "menu"
        self.turn_steps = 0
        self.cursor_after_settle = cursor_after_settle
        self.settle_after_step = settle_after_step
        self.peer_marker = peer_marker
        self.stop_after_second_poll = stop_after_second_poll
        self.stop_at_selection = stop_at_selection
        self.syncs: list[int] = []
        self.events: list[tuple] = []

    def log(self, _message: str) -> None:
        return None

    def shot(self, _phase: str) -> None:
        return None

    def state_snapshot(self) -> dict:
        return {"map_id": 0xF0}

    def cpu_snapshot(self) -> dict:
        return {}

    def cooperative_sync(self, *, sync_id: int, **_kwargs) -> None:
        self.syncs.append(sync_id)
        self.events.append(("sync", sync_id))

    def wait_for_link_menu_selection_exchange(self, **_kwargs) -> None:
        if self.stop_at_selection:
            raise _StopAtSelection

    def choose_first_usable_battle_move(self) -> int:
        self.stage = "turn-loop"
        return 1

    def peer_shutdown_sync(self, **_kwargs) -> None:
        return None


def _patch_sync_boundary(monkeypatch) -> None:
    monkeypatch.setattr(battle_driver, "_hold_at_sync_boundary", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize("case", ("cursor-unavailable", "invalid-range", "required-key-missing"))
def test_peer_battle_menu_waits_for_rom_input_readiness(case: str) -> None:
    cursor = None if case == "cursor-unavailable" else (2 if case == "invalid-range" else 0)
    harness = _BattleHarness(cursor=cursor)
    if case == "required-key-missing":
        harness.session._pyboy.memory[_WATCHED_KEYS] = 0
    with pytest.raises(RuntimeError, match="menu did not become input-ready"):
        harness.wait_for_menu_ready(
            label="test menu",
            min_item=0,
            max_item=1,
            expected_max=1,
            required_keys=1,
            timeout=0.002,
        )
    assert harness.session.steps
    assert set(harness.session.steps) == {2}
    assert harness.session.presses == []


def test_peer_battle_menu_direction_uses_one_shot_session_press(monkeypatch) -> None:
    harness = _BattleHarness(cursor=0)
    harness.move_menu_to_item(
        target=1,
        min_item=0,
        max_item=1,
        label="test menu",
        input_duration=2,
        settle_frames=4,
    )
    assert harness.session.presses == [("down", 2)]
    assert harness.session.steps == [4]


@pytest.mark.parametrize(
    "case",
    ("already-target", "stale-cursor", "changed-cursor", "invalid-cursor"),
)
def test_peer_battle_menu_navigation_rechecks_rom_cursor_before_commit(
    case: str, monkeypatch
) -> None:
    cursor = None if case == "invalid-cursor" else (1 if case == "already-target" else 0)
    after_settle = {"stale-cursor": 0, "changed-cursor": 2}.get(case)
    harness = _BattleHarness(
        cursor=cursor,
        cursor_after_settle=after_settle,
        stop_at_selection=case == "already-target",
    )
    _patch_sync_boundary(monkeypatch)
    if case == "invalid-cursor":
        ticks = iter(range(1000))
        monkeypatch.setattr(
            drive_support,
            "time",
            SimpleNamespace(monotonic=lambda: next(ticks)),
        )
        with pytest.raises(RuntimeError, match="battle LinkMenu did not become input-ready"):
            harness._battle()
        assert harness.session.presses == []
        assert 117 not in harness.syncs
        return

    if case == "already-target":
        with pytest.raises(_StopAtSelection):
            harness._battle()
        assert harness.session.presses == [("a", 4)]
        assert harness.events.index(("sync", 117)) < harness.events.index(("press", "a", 4))
        return

    with pytest.raises(RuntimeError, match="ordinary LinkMenu input did not select COLOSSEUM"):
        harness._battle()
    assert harness.session.presses == [("down", 12)]
    assert 117 not in harness.syncs
    assert 117 not in harness.backend.announced
    assert all(button != "a" for button, _duration in harness.session.presses)


@pytest.mark.parametrize("case", ("damage-only", "peer-marker-only"))
def test_peer_battle_driver_does_not_finish_on_damage_or_peer_marker(
    case: str, monkeypatch
) -> None:
    harness = _BattleHarness(peer_marker=case == "peer-marker-only")
    harness.counters["ApplyDamageToEnemyPokemon"][0] = int(case == "damage-only")
    _patch_sync_boundary(monkeypatch)
    harness._battle()
    assert harness.turn_steps == 2
    assert 14 not in harness.backend.announced
    assert harness.battle_turn_announced is False
    assert harness.peer_battle_turn_ready is False
    assert harness.backend.polled.count(14) == 0


def test_peer_battle_driver_waits_for_settled_peer_sync(monkeypatch) -> None:
    harness = _BattleHarness(settle_after_step=True, stop_after_second_poll=True)
    _patch_sync_boundary(monkeypatch)
    with pytest.raises(_StopAtPeerPoll):
        harness._battle()
    assert harness.battle_turn_announced is True
    assert harness.backend.announced.count(14) == 1
    assert harness.backend.polled.count(14) == 2
    assert harness.turn_steps == 2
    assert 20 in harness.session.steps


def test_importing_battle_driver_with_asset_paths_does_not_construct_pyboy() -> None:
    script = """
import importlib
import pyboy

def forbidden_constructor(*args, **kwargs):
    raise AssertionError("import attempted to construct PyBoy")

pyboy.PyBoy = forbidden_constructor
importlib.import_module("tests._tcp_trade_peer_drive")
"""
    env = os.environ.copy()
    env.update(
        POKERED_ROM_PATH="/missing/red.gb",
        POKERED_SYM_PATH="/missing/red.sym",
        POKERED_PEER_ROM_PATH="/missing/blue.gb",
        POKERED_PEER_SYM_PATH="/missing/blue.sym",
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
