"""Asset-free checks for the external fixture provenance contract."""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath

import pytest

from pokered_harness.config import load_versions
from scripts import produce_cable_club_fixture as producer
from scripts import validate_fixture_manifest as fixture_manifest
from scripts.prepare_battle_cable_club_fixtures import VARIANTS

ROOT = Path(__file__).resolve().parents[1]
_SOURCE_DIGEST_RE = re.compile(
    r"SHA-1 (?P<sha1>[0-9a-f]{40}); SHA-256 (?P<sha256>[0-9a-f]{64})",
    re.IGNORECASE,
)


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


def test_manifest_validator_requires_complete_verified_provenance() -> None:
    manifest = json.loads(
        (ROOT / "release-evidence" / "fixture-manifest.json").read_text(encoding="utf-8")
    )
    provenance = manifest["fixtures"][0]["provenance"]
    provenance["status"] = "verified"
    provenance["runtime_identity"] = None

    with pytest.raises(ValueError, match="runtime_identity.*non-empty string"):
        fixture_manifest._validate_schema(manifest)


def test_manifest_validator_rejects_drive_relative_asset_paths() -> None:
    manifest = json.loads(
        (ROOT / "release-evidence" / "fixture-manifest.json").read_text(encoding="utf-8")
    )
    manifest["fixtures"][0]["expected_rom"]["path"] = "C:rom/pokemon-red.gb"

    with pytest.raises(ValueError, match="must be relative"):
        fixture_manifest._validate_schema(manifest)


def test_manifest_validator_rejects_symlinked_fixture_bytes(tmp_path) -> None:
    fixture_root = tmp_path / "fixtures"
    red_root = fixture_root / "red"
    red_root.mkdir(parents=True)
    target = red_root / "target.state"
    target.write_bytes(b"state")
    linked = red_root / "cable_club.state"
    try:
        linked.symlink_to(target.name)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="regular file"):
        fixture_manifest._validate_assets(
            [
                {
                    "path": "red/cable_club.state",
                    "size_bytes": 5,
                    "sha1": "c" * 40,
                    "sha256": "d" * 64,
                }
            ],
            fixture_root,
        )


def test_manifest_pins_and_provenance_boundaries_match_tracked_contract() -> None:
    manifest = json.loads(
        (ROOT / "release-evidence" / "fixture-manifest.json").read_text(encoding="utf-8")
    )
    versions = load_versions(ROOT / "VERSIONS.md")
    by_id = {fixture["id"]: fixture for fixture in manifest["fixtures"]}

    for fixture in manifest["fixtures"]:
        expected_rom = fixture["expected_rom"]
        expected_symbols = fixture["expected_symbols"]
        assert versions.sha1_for_path(expected_rom["path"]) == expected_rom["sha1"]
        assert versions.symbol_sha1_for_path(expected_symbols["path"]) == expected_symbols["sha1"]

        provenance = fixture["provenance"]
        producer_path = Path(provenance["producer"])
        assert not producer_path.is_absolute()
        assert not PureWindowsPath(provenance["producer"]).is_absolute()
        assert ".." not in producer_path.parts
        assert (ROOT / producer_path).is_file()

        for value in (
            fixture["path"],
            expected_rom["path"],
            expected_symbols["path"],
            provenance["producer"],
            provenance["source_state"],
            provenance["capture_command_template"],
        ):
            assert not Path(value).is_absolute()
            assert not PureWindowsPath(value).is_absolute()

        assert _SOURCE_DIGEST_RE.search(provenance["source_state"])
        if provenance["status"] == "verified":
            assert all(
                isinstance(provenance[field], str) and provenance[field].strip()
                for field in ("runtime_identity", "captured_at_utc", "verification_method")
            )

        if fixture["variant"] == "vanilla":
            assert provenance["status"] == "partial"

        if fixture["kind"] == "battle":
            ordinary_id = f"{fixture['version']}-{fixture['variant']}-ordinary"
            ordinary = by_id[ordinary_id]
            if ordinary["provenance"]["status"] != "verified":
                assert provenance["status"] != "verified"


def test_fixture_producer_does_not_enable_hash_bypass() -> None:
    source = (ROOT / "scripts" / "produce_cable_club_fixture.py").read_text(encoding="utf-8")
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
