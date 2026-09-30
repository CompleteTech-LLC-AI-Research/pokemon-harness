"""Bounded boot and save-state replay checks for every pinned ROM variant.

The matrix uses only the operator-managed ROM and symbol inputs. It does not
consume link fixtures or write emulated RAM directly. Direct pytest runs skip
when an optional BYO asset is absent; the production gate must inspect these
five rows and reject missing required assets.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from pokered_harness.config import load_versions
from pokered_harness.session import Session, sha1_of_file
from tests._rom_assets import PROJECT_ROOT, rom_path, sym_path

pytestmark = pytest.mark.real_rom


_ROM_VARIANTS = (
    pytest.param("red-stock", "red", False, id="red-stock"),
    pytest.param("red-color", "red", True, id="red-color"),
    pytest.param("blue-stock", "blue", False, id="blue-stock"),
    pytest.param("blue-color", "blue", True, id="blue-color"),
    pytest.param("yellow", "yellow", False, id="yellow"),
)


def _asset_hashes(rom: Path, symbols: Path) -> tuple[str, str]:
    return sha1_of_file(rom), sha1_of_file(symbols)


def _state_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime_paths() -> dict[str, str | None]:
    import pyboy
    import pyboy.core.serial as pyboy_serial

    import pokered_harness.link.serial_core as harness_serial

    return {
        "python": sys.executable,
        "pyboy_module": str(pyboy.__file__) if pyboy.__file__ else None,
        "pyboy_serial_module": (str(pyboy_serial.__file__) if pyboy_serial.__file__ else None),
        "harness_serial_module": (
            str(harness_serial.__file__) if harness_serial.__file__ else None
        ),
    }


@pytest.mark.parametrize(("label", "version", "color"), _ROM_VARIANTS)
def test_pinned_rom_boot_save_load_replay(
    label: str, version: str, color: bool, record_property
) -> None:
    rom = rom_path(version, color=color)
    symbols = sym_path(version)
    missing = [str(path) for path in (rom, symbols) if not path.is_file()]
    if missing:
        pytest.skip(f"{label} ROM/symbol assets are not present: {', '.join(missing)}")

    pins = load_versions(PROJECT_ROOT / "VERSIONS.md")
    expected_rom_sha1 = pins.sha1_for_path(rom)
    expected_symbol_sha1 = pins.sha1_for_symbol_path(symbols)
    assert expected_rom_sha1 is not None, f"missing ROM pin for {rom}"
    assert expected_symbol_sha1 is not None, f"missing symbol pin for {symbols}"

    before_hashes = _asset_hashes(rom, symbols)
    assert before_hashes == (expected_rom_sha1, expected_symbol_sha1)

    session: Session | None = None
    runtime_paths: dict[str, str | None] = {}
    checkpoint_hash = expected_replay_hash = replay_hash = None
    teardown_status: str | None = None
    try:
        session = Session.from_files(
            rom,
            symbols,
            expected_rom_sha1=expected_rom_sha1,
            expected_symbol_sha1=expected_symbol_sha1,
            expected_pyboy_version=pins.pyboy_version,
        )
        runtime_paths = _runtime_paths()
        session.step(120)
        state = session.read_game_state()
        assert isinstance(state.overworld.map_id, int)

        checkpoint = session.save_state()
        checkpoint_hash = _state_hash(checkpoint)

        session.step(120)
        expected_replay = session.save_state()
        expected_replay_hash = _state_hash(expected_replay)
        assert expected_replay_hash != checkpoint_hash

        session.load_state(checkpoint)
        session.step(120)
        replay = session.save_state()
        replay_hash = _state_hash(replay)
        assert replay_hash == expected_replay_hash
    finally:
        try:
            if session is not None:
                session.close(save=False)
                teardown_status = "closed"
        finally:
            after_hashes = _asset_hashes(rom, symbols)
            assert after_hashes == before_hashes

    record_property(
        "rom_boot",
        json.dumps(
            {
                "phase": "boot/advance/replay",
                "label": label,
                "actual_rom_sha1": before_hashes[0],
                "actual_symbol_sha1": before_hashes[1],
                **runtime_paths,
                "checkpoint_sha256": checkpoint_hash,
                "expected_replay_sha256": expected_replay_hash,
                "replay_sha256": replay_hash,
                "teardown_status": teardown_status,
            },
            sort_keys=True,
        ),
    )
