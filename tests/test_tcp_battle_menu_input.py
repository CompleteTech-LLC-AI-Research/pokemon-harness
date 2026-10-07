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
    def __init__(self, owner, *, cursor: int | None = 0, max_item: int = 1):
        self.owner = owner
        memory = {_MAX_ITEM: max_item, _WATCHED_KEYS: 1, _SERIAL_STATUS: 2}
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
        self.owner.events.append(("step", frames))
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
        link_menu_max: int = 1,
        cursor_after_settle: int | None = None,
        settle_after_step: bool = False,
        peer_marker: bool = False,
        stop_after_second_poll: bool = False,
        stop_at_selection: bool = False,
    ):
        self.args = SimpleNamespace(role="local")
        self.deadline = time.monotonic() + 5.0
        self.link_menu_max = link_menu_max
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
        self.session = _Session(self, cursor=cursor, max_item=link_menu_max)
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
            max_item=harness.link_menu_max,
            expected_max=harness.link_menu_max,
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
        link_menu_max=2 if case == "changed-cursor" else 1,
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
        for max_item in (2, 3):
            valid_harness = _BattleHarness(
                cursor=2,
                link_menu_max=max_item,
                stop_at_selection=True,
            )
            assert valid_harness.link_menu_max == max_item
            assert valid_harness.session._pyboy.memory[_MAX_ITEM] == max_item
            with pytest.raises(_StopAtSelection):
                valid_harness._battle()
            assert valid_harness.session.presses == [("up", 12), ("a", 4)]
            assert valid_harness.session.steps == [60, 40]
            assert valid_harness.events == [
                ("sync", 11),
                ("step", 60),
                ("press", "up", 12),
                ("step", 40),
                ("sync", 117),
                ("press", "a", 4),
            ]
        for max_item in (2, 3):
            for invalid_cursor in (None, 255):
                invalid_harness = _BattleHarness(
                    cursor=invalid_cursor,
                    link_menu_max=max_item,
                )
                assert invalid_harness.link_menu_max == max_item
                assert invalid_harness.session._pyboy.memory[_MAX_ITEM] == max_item
                with pytest.raises(
                    RuntimeError, match="battle LinkMenu did not become input-ready"
                ):
                    invalid_harness._battle()
                assert invalid_harness.session.presses == []
                assert invalid_harness.syncs == [11]
                assert 117 not in invalid_harness.backend.announced
                assert all(
                    event[0] != "press" or event[1] not in {"up", "down", "a"}
                    for event in invalid_harness.events
                )
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
    press_index = harness.events.index(("press", "down", 12))
    assert harness.events[press_index:] == [
        ("press", "down", 12),
        ("step", 40),
    ]


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
import builtins
import importlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import pyboy
from tests import _rom_assets

project_root = Path.cwd().resolve()
asset_paths = {
    _rom_assets.rom_path("yellow"),
    _rom_assets.sym_path("yellow"),
    _rom_assets.fixture_path("yellow"),
}
asset_keys = {
    os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(path))))
    for path in asset_paths
}
asset_checks = set()
asset_reads = []

def path_key(path):
    try:
        value = os.fspath(path)
    except TypeError:
        return None
    return os.path.normcase(os.path.abspath(os.fsdecode(value)))

original_is_file = Path.is_file
def asset_ready_is_file(path):
    key = path_key(path)
    if key in asset_keys:
        asset_checks.add(key)
        return True
    return original_is_file(path)

Path.is_file = asset_ready_is_file

def reject_asset_content(path):
    key = path_key(path)
    if key in asset_keys:
        asset_reads.append(key)
        raise AssertionError("configured ROM/SYM/state content read: " + key)

original_builtin_open = builtins.open
def guarded_builtin_open(path, *args, **kwargs):
    reject_asset_content(path)
    return original_builtin_open(path, *args, **kwargs)

original_io_open = io.open
def guarded_io_open(path, *args, **kwargs):
    reject_asset_content(path)
    return original_io_open(path, *args, **kwargs)

original_os_open = os.open
def guarded_os_open(path, *args, **kwargs):
    reject_asset_content(path)
    return original_os_open(path, *args, **kwargs)

builtins.open = guarded_builtin_open
io.open = guarded_io_open
os.open = guarded_os_open
descriptor = os.open(os.devnull, os.O_RDONLY)
with builtins.open(descriptor, "rb", closefd=True) as descriptor_stream:
    descriptor_stream.read(0)

def forbidden_constructor(*args, **kwargs):
    raise AssertionError("import attempted to construct PyBoy")

pyboy.PyBoy = forbidden_constructor
rom_support = importlib.import_module("tests._pyboy_link_session_roms_support")
assert rom_support._fixtures_ready is True

rom_tests_path = project_root / "tests" / "test_pyboy_link_session_roms.py"
spec = importlib.util.spec_from_file_location(
    "tests._asset_ready_real_rom_probe", rom_tests_path
)
assert spec is not None and spec.loader is not None
rom_tests = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rom_tests
spec.loader.exec_module(rom_tests)
assert rom_tests._fixtures_available("yellow") is True
assert Path(rom_tests.__file__).resolve() == rom_tests_path

driver = importlib.import_module("tests._tcp_trade_peer_drive")
driver_modules = [
    driver,
    importlib.import_module("tests._tcp_trade_peer_drive_battle"),
    importlib.import_module("tests._tcp_trade_peer_drive_support"),
]
assert all(
    Path(module.__file__).resolve().is_relative_to(project_root)
    for module in driver_modules
)
assert asset_checks == asset_keys
assert asset_reads == []
"""
    env = os.environ.copy()
    asset_probe_root = Path(__file__).resolve().parents[1] / ".asset-ready-import-probe"
    env.update(
        POKERED_ROM_PATH="/missing/red.gb",
        POKERED_SYM_PATH="/missing/red.sym",
        POKERED_PEER_ROM_PATH="/missing/blue.gb",
        POKERED_PEER_SYM_PATH="/missing/blue.sym",
        POKERED_ROM_ROOT=str(asset_probe_root / "rom"),
        POKERED_FIXTURE_ROOT=str(asset_probe_root / "fixtures"),
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
