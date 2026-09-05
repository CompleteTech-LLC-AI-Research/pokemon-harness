"""Fixture-free canonical boot/state checks, not intro or gameplay acceptance."""

from dataclasses import asdict
from pathlib import Path

import pytest

from pokered_harness.config import load_versions
from pokered_harness.session import Session
from pokered_harness.state import GameState
from tests._rom_assets import rom_path, sym_path


@pytest.mark.parametrize(
    "version,color",
    [
        pytest.param("red", True, id="red-color"),
        pytest.param("blue", True, id="blue-color"),
        pytest.param("yellow", False, id="yellow"),
    ],
)
def test_canonical_rom_boot_state_roundtrip(version: str, color: bool) -> None:
    rom = rom_path(version, color=color)
    symbols = sym_path(version)
    missing = [str(path) for path in (rom, symbols) if not path.is_file()]
    if missing:
        pytest.skip("Missing BYO ROM/SYM assets: " + ", ".join(missing))

    pins = load_versions(Path(__file__).resolve().parents[1] / "VERSIONS.md")
    # Select documented paths independently of the configured asset root's name.
    rom_sha1 = pins.sha1_for_path(Path("rom") / version / rom.name)
    symbol_sha1 = pins.symbol_sha1_for_path(Path("rom") / version / symbols.name)
    assert rom_sha1 is not None, f"Missing canonical {version} ROM SHA-1 pin"
    assert symbol_sha1 is not None, f"Missing canonical {version} SYM SHA-1 pin"
    assert pins.pyboy_version, "Missing PyBoy version pin"
    assert pins.pyboy_revision, "Missing PyBoy revision pin"

    session = Session.from_files(
        rom,
        symbols,
        expected_rom_sha1=rom_sha1,
        expected_symbol_sha1=symbol_sha1,
        expected_pyboy_version=pins.pyboy_version,
        expected_pyboy_revision=pins.pyboy_revision,
    )
    try:
        initial_tick = session.current_tick()
        session.step(120)
        assert session.current_tick() == initial_tick + 120
        state = session.read_game_state()
        assert isinstance(state, GameState)
        assert set(asdict(state)) == {
            "overworld",
            "menu",
            "text",
            "battle",
            "party",
            "bag",
            "progress",
            "validity",
        }
        # Early boot can legitimately report partial/unknown game values.
        assert state.validity.status in {"valid", "partial", "unknown"}
        assert isinstance(state.validity.missing_symbols, tuple)
        assert isinstance(state.validity.unknown_fields, tuple)
        assert isinstance(state.validity.invalid_fields, tuple)
        if state.overworld is not None:
            for value in (state.overworld.map_id, state.overworld.x, state.overworld.y):
                assert isinstance(value, int) and 0 <= value <= 255

        saved = session.save_state()
        assert isinstance(saved, bytes) and saved
        assert session.save_state() == saved
        session.step(1)
        assert session.current_tick() == initial_tick + 121
        session.load_state(saved)
        # GameState excludes the external tick, which load_state does not reset.
        assert session.read_game_state() == state
    finally:
        session.close(save=False)
