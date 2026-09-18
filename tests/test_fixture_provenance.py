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
# Boundary fixtures are real-play drives captured by
# ``scripts/produce_battle_state_fixtures.py``: a forced-replacement party
# menu, the last ROM command boundary before the deciding knockout, and the
# terminal return itself.  Each named fixture has a ``-peer`` sibling captured
# at the same emulated instant.
_BOUNDARY_SLUGS = {
    "battle-faint": "cable_club-battle-faint",
    "battle-faint-peer": "cable_club-battle-faint-peer",
    "battle-pre-terminal": "cable_club-pre-terminal",
    "battle-pre-terminal-peer": "cable_club-pre-terminal-peer",
    "battle-terminal-return": "cable_club-terminal",
    "battle-terminal-return-peer": "cable_club-terminal-peer",
}
_FORMAT_BY_VERSION = {"red": "color", "blue": "color", "yellow": "cgb"}
_EXPECTED_FIXTURE_ROWS |= {
    f"{version}-{variant}-{slug}": (
        f"{version}/{stem}.state",
        version,
        variant,
        "boundary",
    )
    for version, variant in _FORMAT_BY_VERSION.items()
    for slug, stem in _BOUNDARY_SLUGS.items()
}
_CAPTURED_FIXTURE_IDS = frozenset(
    f"{version}-{variant}-{slug}"
    for version, variant in _FORMAT_BY_VERSION.items()
    for slug in _BOUNDARY_SLUGS
)
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
    assert len(fixtures) == 28
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
    assert {
        fixture["id"] for fixture in fixtures if fixture["provenance"]["status"] == "captured"
    } == _CAPTURED_FIXTURE_IDS
    assert {fixture["id"] for fixture in fixtures if fixture["kind"] == "boundary"} == (
        _CAPTURED_FIXTURE_IDS
    )
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


def test_boundary_provenance_binds_each_captured_pair_to_admitted_battle_input() -> None:
    document = _load_manifest()
    by_id = {fixture["id"]: fixture for fixture in document["fixtures"]}

    for fixture in document["fixtures"]:
        if fixture["kind"] != "boundary":
            continue
        source = by_id[f"{fixture['version']}-{fixture['variant']}-battle"]
        provenance = fixture["provenance"]

        assert provenance["status"] == "captured"
        assert provenance["producer"] == "scripts/produce_battle_state_fixtures.py"
        assert f"external {source['path']};" in provenance["source_state"]
        assert _source_digests(provenance["source_state"]) == (
            source["sha1"],
            source["sha256"],
        )
        assert all(
            isinstance(provenance[field], str) and provenance[field].strip()
            for field in ("runtime_identity", "captured_at_utc", "verification_method")
        )
        # The captured pair is pinned to the exact ROM/symbol pair of the
        # admitted battle input it was driven from, and stays external.
        assert fixture["expected_rom"] == source["expected_rom"]
        assert fixture["expected_symbols"] == source["expected_symbols"]
        assert fixture["repository_distributed"] is False

    # Every named boundary fixture has a peer sibling captured at the same
    # emulated instant, so a consumer can reload the pair consistently.
    for fixture_id in _CAPTURED_FIXTURE_IDS:
        if fixture_id.endswith("-peer"):
            continue
        primary = by_id[fixture_id]
        counterpart = by_id[f"{fixture_id}-peer"]
        # Only the pre-terminal and terminal-return pairs carry a replay
        # capture; every pair still shares one capture instant.
        assert counterpart.get("capture") == primary.get("capture"), fixture_id
        captured_at = primary["provenance"]["captured_at_utc"]
        assert counterpart["provenance"]["captured_at_utc"] == captured_at, fixture_id


def test_captured_boundary_rows_pin_the_replayable_terminal_turn() -> None:
    document = _load_manifest()
    by_id = {fixture["id"]: fixture for fixture in document["fixtures"]}

    for version, variant in _FORMAT_BY_VERSION.items():
        boundary_capture = by_id[f"{version}-{variant}-battle-pre-terminal"]["capture"]
        terminal_capture = by_id[f"{version}-{variant}-battle-terminal-return"]["capture"]

        # ``cable_club-pre-terminal`` is the last ROM command boundary before
        # the deciding knockout, so both rows describe the same driven turn,
        # which is the plan the MCP terminal-return scenario replays.
        plan = terminal_capture["terminal_turn_plan"]
        assert type(boundary_capture["boundary_turn"]) is int
        assert boundary_capture["terminal_turn"] == terminal_capture["terminal_turn"]
        assert boundary_capture["terminal_turn_plan"] == plan
        assert isinstance(plan, list) and len(plan) == 2
        for entry in plan:
            assert type(entry["slot"]) is int and 0 <= entry["slot"] < 4
            assert type(entry["move"]) is int and entry["move"] != 0
            assert isinstance(entry["policy"], str) and entry["policy"]

        # The terminal capture's own party summary proves the battle ended on
        # both sessions, so the admitted terminal bytes are a real return and
        # not a mid-battle snapshot.
        party = terminal_capture["party"]
        assert set(party) == {"primary", "peer"}
        for summary in party.values():
            assert summary["is_in_battle"] == 0
            assert summary["party_count"] == 6
            assert len(summary["party_hp"]) == 6
            assert summary["battle_result"] in (0, 1, 2)


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
