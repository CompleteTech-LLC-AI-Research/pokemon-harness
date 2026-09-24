"""ROM-free schema/provenance validation for the battle-scenario catalog."""

from __future__ import annotations

import copy
import math

import pytest

from scripts import merge_battle_boundary_scenarios as boundary_merge
from scripts import produce_battle_scenario as producer
from scripts import validate_battle_scenarios as validator
from tests._battle_scenario_fixtures_support import (
    _load_catalog,
    _load_manifest,
    _scenario,
)


def test_catalog_validates_against_the_fixture_manifest() -> None:
    manifest = _load_manifest()
    scenarios = validator._validate_schema(_load_catalog(), manifest)
    # One scenario per admitted manifest row: the six canonical/vanilla
    # ordinary and battle rows plus the eighteen captured boundary rows.
    assert len(scenarios) == 28
    expected_ids = {
        fixture["id"]
        for fixture in manifest["fixtures"]
        if fixture["kind"] in validator._BATTLE_CATALOG_KINDS
    }
    assert {scenario["fixture"]["fixture_id"] for scenario in scenarios} == expected_ids
    # The six-member ``slots`` rows are trade-acceptance fixtures, not battle
    # pairing sources, so the catalog deliberately does not model them.
    slots_ids = {fixture["id"] for fixture in manifest["fixtures"] if fixture["kind"] == "slots"}
    assert slots_ids and slots_ids.isdisjoint(expected_ids)


def test_catalog_copies_provenance_statuses_truthfully_and_has_no_execution_claim() -> None:
    catalog = _load_catalog()
    manifest = _load_manifest()
    observed = {
        scenario["scenario_id"]: scenario["provenance"]["status"]
        for scenario in catalog["scenarios"]
    }
    expected = {
        fixture["id"].replace("-", "_"): fixture["provenance"]["status"]
        for fixture in manifest["fixtures"]
        if fixture["kind"] in validator._BATTLE_CATALOG_KINDS
    }
    assert observed == expected
    assert sum(status == "verified" for status in observed.values()) == 6
    assert sum(status == "partial" for status in observed.values()) == 4
    assert sum(status == "captured" for status in observed.values()) == 18
    assert all(
        scenario["evidence_class"]["real_rom_execution"] is None
        for scenario in catalog["scenarios"]
    )


def test_scenario_ids_match_the_pattern_and_are_unique() -> None:
    catalog = _load_catalog()
    ids = [scenario["scenario_id"] for scenario in catalog["scenarios"]]
    assert len(ids) == len(set(ids))
    for scenario_id in ids:
        validator._validate_scenario_id(scenario_id, "scenario_id")
        assert validator.base_scenario_id(scenario_id) == scenario_id


@pytest.mark.parametrize(
    "scenario_id",
    [
        "red_color",
        "red_color_2",
        "red_color__listen",
        "red_color__connect",
    ],
)
def test_scenario_id_regex_accepts_declared_and_role_suffixed_ids(scenario_id: str) -> None:
    validator._validate_scenario_id(scenario_id, "scenario_id")


@pytest.mark.parametrize(
    "scenario_id",
    [
        "",
        "Red_Color",
        "red-color",
        "red__",
        "red color",
        "__listen",
        "red__listen__connect",
    ],
)
def test_scenario_id_regex_rejects_malformed_ids(scenario_id: str) -> None:
    with pytest.raises(ValueError, match="scenario_id"):
        validator._validate_scenario_id(scenario_id, "scenario_id")


def test_duplicate_scenario_ids_are_rejected() -> None:
    catalog = _load_catalog()
    duplicated = copy.deepcopy(catalog)
    duplicated["scenarios"].append(copy.deepcopy(duplicated["scenarios"][0]))
    with pytest.raises(ValueError, match="duplicate scenario id"):
        validator._validate_schema(duplicated, _load_manifest())
    with pytest.raises(producer.ScenarioRefusal, match="duplicate scenario id"):
        producer.scenario_index(duplicated)


def test_verified_provenance_requires_identity_timestamp_and_method() -> None:
    provenance = {
        "status": "verified",
        "producer": "scripts/produce_cable_club_fixture.py",
        "source_fixture_id": None,
        "input_sequence": None,
        "capture_command_template": "python scripts/produce_cable_club_fixture.py",
        "runtime_identity": None,
        "captured_at_utc": None,
        "verification_method": None,
    }
    with pytest.raises(ValueError, match="runtime_identity is required"):
        validator._validate_provenance(provenance, "provenance")


def test_derived_provenance_requires_source_and_transformation() -> None:
    provenance = {
        "status": "derived",
        "producer": "scripts/prepare_battle_cable_club_fixtures.py",
        "source_fixture_id": None,
        "input_sequence": None,
        "capture_command_template": "python scripts/prepare_battle_cable_club_fixtures.py",
        "runtime_identity": None,
        "captured_at_utc": None,
        "verification_method": None,
    }
    with pytest.raises(ValueError, match="source_fixture_id is required"):
        validator._validate_provenance(provenance, "provenance")

    provenance["source_fixture_id"] = "red-color-ordinary"
    with pytest.raises(ValueError, match="transformation is required"):
        validator._validate_provenance(provenance, "provenance")

    provenance["input_sequence"] = "copy lead record into six party slots"
    validator._validate_provenance(provenance, "provenance")


def test_partial_and_unknown_provenance_stay_visible() -> None:
    base = {
        "producer": "scripts/produce_cable_club_fixture.py",
        "source_fixture_id": None,
        "input_sequence": None,
        "capture_command_template": "python scripts/produce_cable_club_fixture.py",
        "runtime_identity": None,
        "captured_at_utc": None,
        "verification_method": None,
    }
    for status in ("partial", "unknown"):
        validator._validate_provenance({**base, "status": status}, "provenance")


def test_captured_provenance_requires_the_full_chain_and_input_binding() -> None:
    """A captured row must carry the verified fields plus its input pin."""
    base = {
        "status": "captured",
        "producer": "scripts/produce_battle_state_fixtures.py",
        "source_fixture_id": "red-color-battle",
        "input_fixture_sha1": "a" * 40,
        "input_sequence": "real-play Cable Club link drive to the faint boundary",
        "capture_command_template": "python scripts/produce_battle_state_fixtures.py",
        "runtime_identity": "Python 3.11.2; PyBoy 2.7.0 cython runtime",
        "captured_at_utc": "2026-09-18T04:38:03Z",
        "verification_method": "reload-validated against the boundary invariants",
    }
    validator._validate_provenance(base, "provenance")

    # ``captured`` is as strict as ``verified``: every recorded identity field
    # is mandatory, and the drive must name and pin the admitted input.
    for field in ("runtime_identity", "captured_at_utc", "verification_method"):
        with pytest.raises(ValueError, match=f"{field} is required for captured provenance"):
            validator._validate_provenance({**base, field: None}, "provenance")
    with pytest.raises(ValueError, match="source_fixture_id is required"):
        validator._validate_provenance({**base, "source_fixture_id": None}, "provenance")
    with pytest.raises(ValueError, match="input_fixture_sha1 is required"):
        validator._validate_provenance({**base, "input_fixture_sha1": None}, "provenance")
    with pytest.raises(ValueError, match="input_sequence is required"):
        validator._validate_provenance({**base, "input_sequence": None}, "provenance")


def test_boundary_scenario_bounds_are_the_real_replay_geometry() -> None:
    """The declared bounds must be the drive constants the tests actually use."""
    from scripts import production_gate
    from tests import test_mcp_battle_phase_rom as boundary_tests

    bounds = boundary_merge._bounds()
    assert bounds["max_frames"] == boundary_tests.BOUNDARY_DRIVE_BUDGET
    assert bounds["max_wall_seconds"] == production_gate.DEFAULT_TIMEOUT_SECONDS["local"]
    # One input per owner at most every ``BOUNDARY_INPUT_SPACING`` frames.
    assert bounds["max_inputs"] == 2 * (boundary_tests.BOUNDARY_DRIVE_BUDGET // 8 + 1)
    producer.validate_bounds(**bounds)


def test_every_admitted_boundary_fixture_is_declared() -> None:
    """No admitted boundary row may be missing its catalog scenario."""
    catalog = _load_catalog()
    manifest = _load_manifest()
    declared = {
        scenario["fixture"]["fixture_id"]: scenario
        for scenario in catalog["scenarios"]
        if scenario["battle"]["type"] == boundary_merge._BOUNDARY_BATTLE_TYPE
    }
    expected = {row["id"] for row in manifest["fixtures"] if row["kind"] == "boundary"}
    assert set(declared) == expected
    assert len(declared) == 18

    for row in manifest["fixtures"]:
        if row["kind"] != "boundary":
            continue
        scenario = declared[row["id"]]
        # The scenario is derived from the manifest row, not restated by hand.
        assert scenario["fixture"]["sha1"] == row["sha1"]
        assert scenario["fixture"]["sha256"] == row["sha256"]
        assert scenario["fixture"]["size_bytes"] == row["size_bytes"]
        assert scenario["game"]["rom_sha1"] == row["expected_rom"]["sha1"]
        assert scenario["game"]["sym_sha1"] == row["expected_symbols"]["sha1"]
        assert scenario["provenance"]["status"] == row["provenance"]["status"] == "captured"
        assert scenario["provenance"]["runtime_identity"] == row["provenance"]["runtime_identity"]
        # A captured row names and pins the admitted battle input it was driven
        # from, and declares the boundary kind the producer recorded.
        assert scenario["provenance"]["source_fixture_id"] == (
            f"{row['version']}-{row['variant']}-battle"
        )
        assert scenario["capture_boundary"]["stage"] == row["provenance"]["boundary"]


def test_boundary_scenario_merge_refreshes_in_place_without_reshuffling() -> None:
    """Re-running the declaration must be idempotent and preserve order."""
    catalog = _load_catalog()
    manifest = _load_manifest()
    ids = [scenario["scenario_id"] for scenario in catalog["scenarios"]]
    assert len(ids) == len(set(ids)) == 28

    # The file stays grouped by game, with each version's committed
    # ordinary/battle rows first and its boundary rows appended after them.
    committed = [
        "red_color_ordinary",
        "red_vanilla_ordinary",
        "red_color_battle",
        "red_vanilla_battle",
        "blue_color_ordinary",
        "blue_vanilla_ordinary",
        "blue_color_battle",
        "blue_vanilla_battle",
        "yellow_cgb_ordinary",
        "yellow_cgb_battle",
    ]
    boundary_ids = [scenario_id for scenario_id in ids if scenario_id not in committed]
    assert len(boundary_ids) == 18
    for version in ("red", "blue", "yellow"):
        block = [scenario_id for scenario_id in ids if scenario_id.startswith(version)]
        # The committed rows appear in their original relative order, and every
        # boundary row of that version follows the last committed one.
        assert [scenario_id for scenario_id in block if scenario_id in committed] == [
            scenario_id for scenario_id in committed if scenario_id.startswith(version)
        ]
        last_committed = max(
            index for index, scenario_id in enumerate(block) if scenario_id in committed
        )
        assert all(scenario_id not in committed for scenario_id in block[last_committed + 1 :])
    # The canonical settled-battle scenario per version is still the one the
    # coverage dimension resolves its pairing cases from.
    from scripts import coverage_report

    canonical = coverage_report._canonical_battle_scenarios(catalog)
    assert {version: item["scenario_id"] for version, item in canonical.items()} == {
        "red": "red_color_battle",
        "blue": "blue_color_battle",
        "yellow": "yellow_cgb_battle",
    }
    assert validator._validate_schema(catalog, manifest)


@pytest.mark.parametrize("value", [0, -1, math.inf, -math.inf, math.nan, True, "4"])
def test_capture_bounds_reject_non_finite_and_non_positive_values(value: object) -> None:
    with pytest.raises(ValueError):
        validator._validate_bounds(
            {"max_frames": value, "max_wall_seconds": 1.0, "max_inputs": 1},
            "capture_bounds",
        )
    with pytest.raises(producer.ScenarioRefusal):
        producer.validate_bounds(max_frames=value, max_wall_seconds=1.0, max_inputs=1)


def test_producer_bounds_reject_non_finite_wall_clock() -> None:
    for value in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(producer.ScenarioRefusal, match="max_wall_seconds"):
            producer.validate_bounds(max_frames=1, max_wall_seconds=value, max_inputs=1)


def test_catalog_cross_reference_rejects_fixture_hash_drift() -> None:
    catalog = copy.deepcopy(_load_catalog())
    catalog["scenarios"][0]["fixture"]["sha1"] = "0" * 40
    with pytest.raises(ValueError, match="fixture.sha1 disagrees with the manifest"):
        validator._validate_schema(catalog, _load_manifest())


def test_catalog_cross_reference_rejects_unknown_and_missing_fixtures() -> None:
    catalog = copy.deepcopy(_load_catalog())
    catalog["scenarios"][0]["fixture"]["fixture_id"] = "not-a-real-fixture"
    with pytest.raises(ValueError, match="not in the manifest"):
        validator._validate_schema(catalog, _load_manifest())

    catalog = copy.deepcopy(_load_catalog())
    catalog["scenarios"] = catalog["scenarios"][:-1]
    with pytest.raises(ValueError, match="missing manifest fixtures"):
        validator._validate_schema(catalog, _load_manifest())


def test_catalog_cross_reference_rejects_unknown_source_fixture() -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    scenario["provenance"]["source_fixture_id"] = "not-a-real-fixture"
    with pytest.raises(ValueError, match="source_fixture_id is not in the manifest"):
        validator._validate_schema(catalog, _load_manifest())


def test_catalog_cross_reference_rejects_source_input_hash_drift() -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    scenario["provenance"]["input_fixture_sha1"] = "0" * 40
    with pytest.raises(ValueError, match="input_fixture_sha1 disagrees with manifest fixture"):
        validator._validate_schema(catalog, _load_manifest())


def test_missing_required_scenario_is_blocked_not_pass() -> None:
    catalog = _load_catalog()
    assert producer.scenario_status(catalog, "not_declared") == "BLOCKED"
    with pytest.raises(producer.ScenarioBlocked):
        producer.find_scenario(catalog, "not_declared")
    assert producer.scenario_status(catalog, "not_declared") != "PASS"
    assert producer.scenario_status(catalog, "red_color_battle") == "READY"
