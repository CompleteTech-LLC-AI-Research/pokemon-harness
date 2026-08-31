"""Asset-free checks for the external fixture provenance contract."""

from __future__ import annotations

import json
from pathlib import Path

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
