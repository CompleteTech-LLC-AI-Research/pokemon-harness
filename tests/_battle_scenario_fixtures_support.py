"""Shared helpers for the battle-scenario catalog/producer test suite.

Extracted from ``tests/test_battle_scenario_fixtures.py`` (#197).  This module
holds the fake pins/session doubles, catalog/manifest loaders, and the fixture
builders shared across the behavior-named test modules.
"""

from __future__ import annotations

import copy
import hashlib
import json
import types
import typing
from pathlib import Path

from scripts import produce_battle_scenario as producer

ROOT = Path(__file__).resolve().parents[1]


CATALOG_PATH = ROOT / "release-evidence" / "battle-scenarios.json"


MANIFEST_PATH = ROOT / "release-evidence" / "fixture-manifest.json"


class _FakePins:
    """Minimal stand-in for ``pokered_harness.config.VersionsConfig``."""

    def __init__(self, rom_sha1: str | None, sym_sha1: str | None) -> None:
        self._rom_sha1 = rom_sha1
        self._sym_sha1 = sym_sha1

    def sha1_for_path(self, path: str | Path) -> str | None:
        return self._rom_sha1

    def symbol_sha1_for_path(self, path: str | Path) -> str | None:
        return self._sym_sha1


def _load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _scenario(catalog: dict, scenario_id: str) -> dict:
    return next(item for item in catalog["scenarios"] if item["scenario_id"] == scenario_id)


class _FakeSymbols:
    """Symbol table stand-in exposing only the verified ``wLinkState`` byte."""

    def __init__(self, *, link_state: int, present: bool) -> None:
        self._present = present
        self._values = {"wLinkState": link_state}

    def __contains__(self, name: object) -> bool:
        return self._present and isinstance(name, str) and name in self._values

    def addr_of(self, name: str) -> int:
        if name not in self._values:
            raise KeyError(f"unknown symbol: {name!r}")
        return self._values[name]

    def read_u8(self, memory: dict[int, int], name: str) -> int:
        return memory[self.addr_of(name)] & 0xFF


class _FakeSession:
    """Scripted stand-in for a bounded capture session: no ROM, no PyBoy."""

    _DELTAS: typing.ClassVar[dict[str, tuple[int, int]]] = {
        "up": (0, -1),
        "down": (0, 1),
        "left": (-1, 0),
        "right": (1, 0),
    }

    def __init__(
        self,
        *,
        link_state: int = 0,
        map_id: int = 64,
        start_x: int = 8,
        start_y: int = 7,
        party_count: int = 0,
        active_slot: int | None = None,
        payload: bytes = b"fake-bounded-capture-state",
        symbols_present: bool = True,
    ) -> None:
        self.symbols = _FakeSymbols(link_state=link_state, present=symbols_present)
        self._pyboy = types.SimpleNamespace(memory={link_state: link_state})
        self.map_id = map_id
        self.x = start_x
        self.y = start_y
        self.party = types.SimpleNamespace(count=party_count, active_slot=active_slot)
        self.payload = payload
        self.ticks = 0
        self.presses: list[tuple[str, int]] = []
        self.loaded: list[bytes] = []
        self.closed = False

    def load_state(self, data: bytes) -> None:
        self.loaded.append(bytes(data))

    def step(self, count: int = 1, **_kwargs: object) -> None:
        self.ticks += count

    def press(self, button: str, *, duration: int = 1) -> None:
        self.presses.append((button, duration))
        dx, dy = self._DELTAS[button]
        self.x += dx
        self.y += dy

    def read_game_state(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            overworld=types.SimpleNamespace(map_id=self.map_id, x=self.x, y=self.y),
            party=self.party,
        )

    def save_state(self) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


def _writable_scenario(scenario_id: str = "red_color_ordinary") -> dict:
    """A declared scenario whose fixture bytes are not yet pinned.

    Clearing the pinned hashes also drops the provenance to ``partial``: an
    entry that no longer declares the bytes its provenance claims to have
    verified must not keep claiming ``verified``.
    """
    scenario = copy.deepcopy(_scenario(_load_catalog(), scenario_id))
    scenario["fixture"].update({"sha1": None, "sha256": None, "size_bytes": None})
    scenario["provenance"]["status"] = "partial"
    return scenario


def _capture_kwargs(tmp_path: Path, scenario: dict, **overrides: object) -> dict:
    input_fixture = tmp_path / "source.state"
    input_fixture.write_bytes(b"source-state-bytes")
    kwargs: dict = {
        "scenario": scenario,
        "rom": tmp_path / "pokemon-red-color.gb",
        "sym": tmp_path / "pokemon-red.sym",
        "input_fixture": input_fixture,
        "output": tmp_path / "out.state",
        "role": "listen",
        "bounds": {"max_frames": 100000, "max_wall_seconds": 180.0, "max_inputs": 64},
        "report": None,
        "repo_root": tmp_path,
        "runtime": "source",
        "python": None,
        "plan": {"rom_sha1": "a" * 40, "sym_sha1": "b" * 40, "input_fixture_sha1": None},
    }
    kwargs.update(overrides)
    return kwargs


def _staged_files(tmp_path: Path) -> list[Path]:
    return [path for path in tmp_path.iterdir() if ".partial-" in path.name]


def _measured_record() -> dict:
    """A capture record satisfying the measured-runtime contract exactly."""
    return {
        "producer": "scripts/produce_battle_scenario.py",
        "producer_sha1": "a" * 40,
        "runtime": "source",
        "runtime_measurement": "measured",
        "runtime_identity": {"mode": "source", "measured": True, "executable": "/usr/bin/python3"},
        "python": None,
        "role": "listen",
        "captured_at_utc": "2026-09-21T00:00:00+00:00",
        "wall_seconds": 0.5,
        "declared_bounds": {"max_frames": 1, "max_wall_seconds": 1.0, "max_inputs": 1},
        "inputs_used": 1,
        "frames_used": 1,
        "input_sequence": ["step:1"],
        "observed_boundary": {"map_id": 64, "link_state_raw": 0},
        "rom_sha1": "b" * 40,
        "sym_sha1": "c" * 40,
        "output": {"path": "out.state", "size_bytes": 12, "sha1": "d" * 40, "sha256": "e" * 64},
        "reproduction": {"matches": False},
    }


def _run_with_fake_session(prepared: dict, **overrides: object) -> dict:
    """Invoke ``run`` with the real capture path and an injected fake session."""
    kwargs: dict = {
        "scenario_id": "red_color_ordinary",
        "catalog": prepared["catalog"],
        "rom": prepared["rom"],
        "sym": prepared["sym"],
        "input_fixture": prepared["input_fixture"],
        "output": prepared["out"],
        "report": prepared.get("report"),
        "repo_root": prepared["root"],
        "pins": prepared["pins"],
        "capture": lambda **capture_kwargs: producer.capture_battle_scenario(
            **capture_kwargs, session_factory=lambda **_kwargs: _FakeSession()
        ),
    }
    kwargs.update(overrides)
    return producer.run(**kwargs)


def _prepared_run(tmp_path: Path) -> dict:
    """Prepare a runnable partial scenario with stand-in assets under ``tmp_path``."""
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    scenario["fixture"].update({"sha1": None, "sha256": None, "size_bytes": None})
    scenario["provenance"]["status"] = "partial"
    declared_producer = tmp_path / "scripts" / "produce_cable_club_fixture.py"
    declared_producer.parent.mkdir(parents=True, exist_ok=True)
    declared_producer.write_text("# declared producer stand-in\n", encoding="utf-8")
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    source = tmp_path / "source.state"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    source.write_bytes(b"source-state-bytes")
    scenario["game"]["rom_sha1"] = hashlib.sha1(rom.read_bytes()).hexdigest()
    scenario["game"]["sym_sha1"] = hashlib.sha1(sym.read_bytes()).hexdigest()
    return {
        "catalog": catalog,
        "scenario": scenario,
        "rom": rom,
        "sym": sym,
        "input_fixture": source,
        "pins": _FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
    }


def _verified_run_inputs(tmp_path: Path, *, payload: bytes = b"fake-bounded-capture-state") -> dict:
    """Prepare a verified scenario whose pins match a ``_FakeSession`` payload."""
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    scenario["fixture"].update(
        {
            "sha1": hashlib.sha1(payload).hexdigest(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    )
    producer_file = tmp_path / scenario["provenance"]["producer"]
    producer_file.parent.mkdir(parents=True, exist_ok=True)
    producer_file.write_bytes(b"original producer bytes")
    scenario["provenance"]["producer_sha1"] = hashlib.sha1(b"original producer bytes").hexdigest()
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    source = tmp_path / "source.state"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    source.write_bytes(b"source-state-bytes")
    scenario["game"]["rom_sha1"] = hashlib.sha1(rom.read_bytes()).hexdigest()
    scenario["game"]["sym_sha1"] = hashlib.sha1(sym.read_bytes()).hexdigest()
    return {
        "catalog": catalog,
        "scenario": scenario,
        "producer_file": producer_file,
        "rom": rom,
        "sym": sym,
        "input_fixture": source,
        "pins": _FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
    }


class _InterruptingSession(_FakeSession):
    """A capture session that raises ``KeyboardInterrupt`` at a chosen advance."""

    def __init__(self, *, interrupt_after: int, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._interrupt_after = interrupt_after

    def step(self, count: int = 1, **kwargs: object) -> None:
        if self.ticks >= self._interrupt_after:
            raise KeyboardInterrupt
        super().step(count, **kwargs)
