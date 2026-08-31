"""Asset-free checks for the external fixture provenance contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import produce_cable_club_fixture as producer
from scripts.prepare_battle_cable_club_fixtures import VARIANTS

ROOT = Path(__file__).resolve().parents[1]


def test_battle_fixture_generator_covers_each_supported_rom_variant() -> None:
    assert set(VARIANTS) == {"red_gb", "red_color", "blue_gb", "blue_color", "yellow"}
    assert {config["output"] for config in VARIANTS.values()} == {
        "cable_club-battle.state",
        "cable_club-battle-vanilla.state",
    }


def test_release_manifest_records_all_external_fixture_bytes_and_provenance() -> None:
    manifest_path = ROOT / "release-evidence" / "fixture-manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixtures = document["fixtures"]
    assert len(fixtures) == 10
    assert all(fixture["repository_distributed"] is False for fixture in fixtures)
    assert all("provenance" in fixture for fixture in fixtures)
    assert all("source_state" in fixture["provenance"] for fixture in fixtures)


def test_fixture_producer_does_not_enable_hash_bypass() -> None:
    source = (ROOT / "scripts" / "produce_cable_club_fixture.py").read_text(
        encoding="utf-8"
    )
    assert "POKERED_SKIP_SHA1" not in source
    assert producer._DEFAULT_TIMEOUT_SECONDS > 0
    assert producer._DEFAULT_MAX_MOVEMENT_STEPS > 0


def test_fixture_producer_rejects_unbounded_configuration(tmp_path) -> None:
    source = tmp_path / "source.state"
    rom = tmp_path / "pokemon-red.gb"
    sym = tmp_path / "pokemon-red.sym"
    out = tmp_path / "out.state"

    with pytest.raises(ValueError, match="timeout_seconds"):
        producer.produce(source, rom, sym, out, timeout_seconds=0)
    with pytest.raises(ValueError, match="max_movement_steps"):
        producer.produce(source, rom, sym, out, max_movement_steps=0)
