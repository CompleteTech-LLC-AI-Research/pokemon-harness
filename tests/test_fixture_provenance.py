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
_EXPECTED_FIXTURE_ROWS = {
    "red-color-ordinary": ("red/cable_club.state", "red", "color", "ordinary"),
    "red-vanilla-ordinary": (
        "red/cable_club-vanilla.state",
        "red",
        "vanilla",
        "ordinary",
    ),
    "red-color-battle": ("red/cable_club-battle.state", "red", "color", "battle"),
    "red-vanilla-battle": (
        "red/cable_club-battle-vanilla.state",
        "red",
        "vanilla",
        "battle",
    ),
    "blue-color-ordinary": ("blue/cable_club.state", "blue", "color", "ordinary"),
    "blue-vanilla-ordinary": (
        "blue/cable_club-vanilla.state",
        "blue",
        "vanilla",
        "ordinary",
    ),
    "blue-color-battle": ("blue/cable_club-battle.state", "blue", "color", "battle"),
    "blue-vanilla-battle": (
        "blue/cable_club-battle-vanilla.state",
        "blue",
        "vanilla",
        "battle",
    ),
    "yellow-cgb-ordinary": ("yellow/cable_club.state", "yellow", "cgb", "ordinary"),
    "yellow-cgb-battle": ("yellow/cable_club-battle.state", "yellow", "cgb", "battle"),
}
_BATTLE_RECIPES = {
    "red-color-battle": "red_color",
    "red-vanilla-battle": "red_gb",
    "blue-color-battle": "blue_color",
    "blue-vanilla-battle": "blue_gb",
    "yellow-cgb-battle": "yellow",
}
_ORDINARY_SOURCE_PREFIXES = {
    "red": "external walkthrough_red/milestones/cerulean_pc.state;",
    "blue": "external walkthrough_blue/milestones/cerulean_pc.state;",
    "yellow": "external walkthrough_yellow/milestones/cerulean_pc.state;",
}
_VERIFIED_FIXTURE_IDS = frozenset(
    {
        "red-color-ordinary",
        "red-color-battle",
        "blue-color-ordinary",
        "blue-color-battle",
        "yellow-cgb-ordinary",
        "yellow-cgb-battle",
    }
)
_VANILLA_FIXTURE_IDS = frozenset(
    {
        "red-vanilla-ordinary",
        "red-vanilla-battle",
        "blue-vanilla-ordinary",
        "blue-vanilla-battle",
    }
)


def _load_manifest() -> dict:
    return json.loads(
        (ROOT / "release-evidence" / "fixture-manifest.json").read_text(encoding="utf-8")
    )


def _source_digests(source_state: str) -> tuple[str, str]:
    match = _SOURCE_DIGEST_RE.search(source_state)
    assert match is not None, source_state
    return match.group("sha1").lower(), match.group("sha256").lower()


def test_battle_fixture_generator_covers_each_supported_rom_variant() -> None:
    assert set(VARIANTS) == {"red_gb", "red_color", "blue_gb", "blue_color", "yellow"}
    assert {config["output"] for config in VARIANTS.values()} == {
        "cable_club-battle.state",
        "cable_club-battle-vanilla.state",
    }


def test_release_manifest_records_all_external_fixture_bytes_and_provenance() -> None:
    document = _load_manifest()
    fixtures = document["fixtures"]
    assert len(fixtures) == 10
    assert all(fixture["repository_distributed"] is False for fixture in fixtures)
    assert all("provenance" in fixture for fixture in fixtures)
    assert all("source_state" in fixture["provenance"] for fixture in fixtures)


def test_manifest_has_exact_supported_fixture_matrix_and_status_boundary() -> None:
    document = _load_manifest()
    fixtures = fixture_manifest._validate_schema(document)

    actual_rows = {
        fixture["id"]: (
            fixture["path"],
            fixture["version"],
            fixture["variant"],
            fixture["kind"],
        )
        for fixture in fixtures
    }
    assert actual_rows == _EXPECTED_FIXTURE_ROWS
    assert {
        fixture["id"] for fixture in fixtures if fixture["provenance"]["status"] == "verified"
    } == _VERIFIED_FIXTURE_IDS
    assert {
        fixture["id"] for fixture in fixtures if fixture["provenance"]["status"] == "partial"
    } == _VANILLA_FIXTURE_IDS
    assert {fixture["id"] for fixture in fixtures if fixture["kind"] == "battle"} == set(
        _BATTLE_RECIPES
    )


def test_ordinary_provenance_uses_matching_recipe_and_external_source() -> None:
    document = _load_manifest()

    for fixture in document["fixtures"]:
        if fixture["kind"] != "ordinary":
            continue

        recipe = producer._VERSIONS[(fixture["version"], fixture["variant"])]
        provenance = fixture["provenance"]
        assert provenance["producer"] == "scripts/produce_cable_club_fixture.py"
        assert provenance["source_state"].startswith(_ORDINARY_SOURCE_PREFIXES[fixture["version"]])
        assert str(recipe["rom"]) == fixture["expected_rom"]["path"].removeprefix("rom/")
        assert str(recipe["sym"]) == fixture["expected_symbols"]["path"].removeprefix("rom/")
        assert recipe["out_name"] == Path(fixture["path"]).name


def test_battle_provenance_binds_each_derived_state_to_ordinary_input() -> None:
    document = _load_manifest()
    by_id = {fixture["id"]: fixture for fixture in document["fixtures"]}

    for battle_id, recipe_key in _BATTLE_RECIPES.items():
        battle = by_id[battle_id]
        ordinary_id = battle_id.replace("-battle", "-ordinary")
        ordinary = by_id[ordinary_id]
        battle_provenance = battle["provenance"]
        recipe = VARIANTS[recipe_key]

        assert battle_provenance["producer"] == ("scripts/prepare_battle_cable_club_fixtures.py")
        assert _source_digests(battle_provenance["source_state"]) == (
            ordinary["sha1"],
            ordinary["sha256"],
        )
        assert f"external {ordinary['path']};" in battle_provenance["source_state"]
        assert recipe["fixture_version"] == battle["version"]
        assert recipe["source"] == Path(ordinary["path"]).name
        assert recipe["output"] == Path(battle["path"]).name
        assert str(recipe["rom"]) == battle["expected_rom"]["path"].removeprefix("rom/")
        assert str(recipe["symbols"]) == battle["expected_symbols"]["path"].removeprefix("rom/")
        assert f"--variants {recipe_key}" in battle_provenance["capture_command_template"]


def test_vanilla_provenance_remains_fail_closed_through_derived_states() -> None:
    document = _load_manifest()
    by_id = {fixture["id"]: fixture for fixture in document["fixtures"]}

    for version in ("red", "blue"):
        ordinary = by_id[f"{version}-vanilla-ordinary"]
        battle = by_id[f"{version}-vanilla-battle"]
        ordinary_provenance = ordinary["provenance"]
        battle_provenance = battle["provenance"]

        assert ordinary_provenance["status"] == "partial"
        assert battle_provenance["status"] == "partial"
        assert all(
            ordinary_provenance[field] is None
            for field in ("runtime_identity", "captured_at_utc", "verification_method")
        )
        assert "not proven to be vanilla-ROM captured" in ordinary_provenance["source_state"]
        assert "ordinary source provenance is partial" in battle_provenance["source_state"]


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
