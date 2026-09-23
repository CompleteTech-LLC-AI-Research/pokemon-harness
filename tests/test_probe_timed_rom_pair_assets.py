"""Asset/fixture resolution and admission checks for the timed probe."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_timed_rom_pair as probe


@pytest.mark.parametrize("version", ["blue_color", "yellow"])
def test_asset_resolution_uses_external_roots_and_canonical_pins(monkeypatch, tmp_path, version):
    from pokered_harness import config
    from scripts import validate_fixture_manifest as manifest

    family = version.split("_")[0]
    name = "pokemon-blue-color.gb" if family == "blue" else "pokemon-yellow.gbc"
    rom_root = tmp_path / "external-roms"
    fixture_root = tmp_path / "external-fixtures"
    monkeypatch.setenv("POKERED_ROM_ROOT", str(rom_root))
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", str(fixture_root))
    state, rom, symbols = b"synthetic-state", b"synthetic-rom", b"synthetic-symbols"
    sha1 = lambda data: hashlib.sha1(data).hexdigest()
    row = {
        "version": family,
        "kind": "ordinary",
        "variant": "color",
        "provenance": {"status": "verified"},
        "path": f"{family}/cable_club.state",
        "size_bytes": len(state),
        "sha1": sha1(state),
        "sha256": hashlib.sha256(state).hexdigest(),
        "expected_rom": {"path": f"rom/{family}/{name}", "sha1": sha1(rom)},
        "expected_symbols": {"path": f"rom/{family}/pokemon-{family}.sym", "sha1": sha1(symbols)},
    }
    contents = {
        rom_root / family / name: rom,
        rom_root / family / f"pokemon-{family}.sym": symbols,
        fixture_root / family / "cable_club.state": state,
    }
    monkeypatch.setattr(Path, "read_bytes", lambda path: contents[path])
    monkeypatch.setattr(manifest, "_load_manifest", lambda path: [row])
    monkeypatch.setattr(manifest, "_validate_schema", lambda rows: rows)
    monkeypatch.setattr(
        config,
        "load_versions",
        lambda path: SimpleNamespace(
            sha1_for_path=lambda path: sha1(rom),
            symbol_sha1_for_path=lambda path: sha1(symbols),
            pyboy_version="test-version",
            pyboy_revision="test-revision",
        ),
    )
    assets = probe.resolve_assets(version, tmp_path)
    assert assets["rom"] == rom_root / family / name
    assert assets["sym"] == rom_root / family / f"pokemon-{family}.sym"
    assert assets["state"] == state
    assert assets["family"] == family
    assert assets["pins"]["expected_rom_sha1"] == sha1(rom)
    contents[fixture_root / family / "cable_club.state"] = b"corrupted"
    with pytest.raises(ValueError, match="fixture bytes"):
        probe.resolve_assets(version, tmp_path)


def test_fixture_kind_selects_the_manifest_kind_from_the_admitted_basename():
    """The basename is the whole selector, so every admitted name maps once."""

    assert probe._fixture_kind("cable_club.state") == "ordinary"
    assert probe._fixture_kind("cable_club-vanilla.state") == "ordinary"
    assert probe._fixture_kind("cable_club-battle.state") == "battle"
    assert probe._fixture_kind("cable_club-battle-vanilla.state") == "battle"
    assert probe._fixture_kind("cable_club-slots.state") == "slots"


def test_asset_resolution_admits_the_six_slot_fixture_kind(monkeypatch, tmp_path):
    """A ``slots`` request resolves the ``slots`` row and fails closed otherwise."""

    from pokered_harness import config
    from scripts import validate_fixture_manifest as manifest

    family, version = "red", "red_color"
    rom_root = tmp_path / "external-roms"
    fixture_root = tmp_path / "external-fixtures"
    monkeypatch.setenv("POKERED_ROM_ROOT", str(rom_root))
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", str(fixture_root))
    state, rom, symbols = b"slots-state", b"synthetic-rom", b"synthetic-symbols"
    sha1 = lambda data: hashlib.sha1(data).hexdigest()  # test helper

    def row_for(basename):
        return {
            "id": "red-color-slots",
            "version": family,
            "kind": "slots",
            "variant": "color",
            "provenance": {"status": "verified"},
            "path": f"{family}/{basename}",
            "size_bytes": len(state),
            "sha1": sha1(state),
            "sha256": hashlib.sha256(state).hexdigest(),
            "expected_rom": {
                "path": f"rom/{family}/pokemon-red-color.gb",
                "sha1": sha1(rom),
            },
            "expected_symbols": {
                "path": f"rom/{family}/pokemon-red.sym",
                "sha1": sha1(symbols),
            },
        }

    contents = {
        rom_root / family / "pokemon-red-color.gb": rom,
        rom_root / family / "pokemon-red.sym": symbols,
        fixture_root / family / "cable_club-slots.state": state,
    }
    monkeypatch.setattr(Path, "read_bytes", lambda path: contents[path])
    monkeypatch.setattr(
        config,
        "load_versions",
        lambda path: SimpleNamespace(
            sha1_for_path=lambda path: sha1(rom),
            symbol_sha1_for_path=lambda path: sha1(symbols),
            pyboy_version="test-version",
            pyboy_revision="test-revision",
        ),
    )
    rows = [row_for("cable_club-slots.state")]
    monkeypatch.setattr(manifest, "_load_manifest", lambda path: rows)
    monkeypatch.setattr(manifest, "_validate_schema", lambda value: value)

    assets = probe.resolve_assets(version, tmp_path, fixture="cable_club-slots.state")
    assert assets["state"] == state
    assert assets["provenance"]["registry"]["kind"] == "slots"

    # A slots key that resolves to a differently named row must still fail
    # closed rather than silently admitting the wrong bytes.
    rows[0] = row_for("cable_club-slots-alt.state")
    with pytest.raises(ValueError, match="not the registered 'slots' row"):
        probe.resolve_assets(version, tmp_path, fixture="cable_club-slots.state")
