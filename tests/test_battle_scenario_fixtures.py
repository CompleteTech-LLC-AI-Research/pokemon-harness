"""ROM-free tests for the battle-scenario catalog and producer contract.

These tests never instantiate PyBoy or load a ROM/state.  They validate the
declared catalog, recompute fixture hashes from temporary bytes, and exercise
the producer's refusal/bounds logic by injection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from scripts import validate_battle_scenarios as validator

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


def test_catalog_validates_against_the_fixture_manifest() -> None:
    manifest = _load_manifest()
    scenarios = validator._validate_schema(_load_catalog(), manifest)
    assert len(scenarios) == 10
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


def test_producer_refuses_unpinned_rom_and_symbol() -> None:
    with pytest.raises(producer.ScenarioRefusal, match="ROM is not pinned"):
        producer.resolve_pins(
            _FakePins(None, "b" * 40), "rom/red/unknown.gb", "rom/red/pokemon-red.sym"
        )
    with pytest.raises(producer.ScenarioRefusal, match="symbol file is not pinned"):
        producer.resolve_pins(
            _FakePins("a" * 40, None), "rom/red/pokemon-red.gb", "rom/red/unknown.sym"
        )


def test_producer_refuses_wrong_resolved_rom_pin(tmp_path: Path) -> None:
    catalog = _load_catalog()
    scenario = _scenario(catalog, "red_color_battle")
    with pytest.raises(producer.ScenarioRefusal, match="disagrees with scenario"):
        producer.run(
            scenario_id="red_color_battle",
            catalog=catalog,
            rom="rom/red/pokemon-red.gb",
            sym="rom/red/pokemon-red.sym",
            output=tmp_path / "out.state",
            pins=_FakePins("f" * 40, scenario["game"]["sym_sha1"]),
        )


def test_producer_refuses_input_fixture_sha_mismatch(tmp_path: Path) -> None:
    payload = b"declared input fixture bytes"
    target = tmp_path / "input.state"
    target.write_bytes(payload)
    expected = hashlib.sha1(payload).hexdigest()
    assert producer.verify_input_fixture(target, expected) == expected
    with pytest.raises(producer.ScenarioRefusal, match="input fixture SHA-1 mismatch"):
        producer.verify_input_fixture(target, "0" * 40)


def test_producer_refuses_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "out.state"
    output.write_bytes(b"existing")
    with pytest.raises(producer.ScenarioRefusal, match="refusing to overwrite"):
        producer.ensure_output_available(output)


def test_producer_capture_stub_refuses_without_writing(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    input_fixture = tmp_path / "input.state"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    input_fixture.write_bytes(b"temporary input state bytes")
    scenario["game"]["rom_sha1"] = hashlib.sha1(rom.read_bytes()).hexdigest()
    scenario["game"]["sym_sha1"] = hashlib.sha1(sym.read_bytes()).hexdigest()
    scenario["provenance"]["input_fixture_sha1"] = hashlib.sha1(
        input_fixture.read_bytes()
    ).hexdigest()
    output = tmp_path / "out.state"
    report = tmp_path / "report.json"
    with pytest.raises(producer.CaptureNotAvailable, match="capture requires a controlled ROM run"):
        producer.run(
            scenario_id="red_color_battle",
            catalog=catalog,
            rom=rom,
            sym=sym,
            input_fixture=input_fixture,
            output=output,
            report=report,
            pins=_FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
        )
    assert not output.exists()
    assert not report.exists()


def test_producer_requires_declared_input_fixture(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    scenario["game"]["rom_sha1"] = hashlib.sha1(rom.read_bytes()).hexdigest()
    scenario["game"]["sym_sha1"] = hashlib.sha1(sym.read_bytes()).hexdigest()
    with pytest.raises(producer.ScenarioRefusal, match="input fixture is required"):
        producer.run(
            scenario_id="red_color_battle",
            catalog=catalog,
            rom=rom,
            sym=sym,
            output=tmp_path / "out.state",
            pins=_FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
        )


def test_producer_refuses_missing_or_substituted_assets(tmp_path: Path) -> None:
    with pytest.raises(producer.ScenarioRefusal, match="ROM not found"):
        producer.verify_pinned_assets(
            tmp_path / "absent.gb", tmp_path / "absent.sym", "a" * 40, "b" * 40
        )
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    with pytest.raises(producer.ScenarioRefusal, match="ROM SHA-1 mismatch"):
        producer.verify_pinned_assets(rom, sym, "a" * 40, "b" * 40)
    producer.verify_pinned_assets(
        rom,
        sym,
        hashlib.sha1(rom.read_bytes()).hexdigest(),
        hashlib.sha1(sym.read_bytes()).hexdigest(),
    )


def test_producer_source_never_enables_hash_bypass() -> None:
    source = (ROOT / "scripts" / "produce_battle_scenario.py").read_text(encoding="utf-8")
    assert "POKERED_SKIP_SHA1" not in source


def test_producer_role_resolution_and_suffixes() -> None:
    catalog = _load_catalog()
    scenario = _scenario(catalog, "red_color_battle")
    assert producer.resolve_role(scenario, "red_color_battle", None) == "listen"
    assert producer.resolve_role(scenario, "red_color_battle", "connect") == "connect"
    with pytest.raises(producer.ScenarioRefusal, match="requires role"):
        producer.resolve_role(scenario, "red_color_battle__connect", "listen")
    with pytest.raises(producer.ScenarioRefusal, match="not allowed"):
        producer.resolve_role(scenario, "red_color_battle", "spectator")


def test_validator_asset_check_recomputes_hashes_from_bytes(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    fixture_root = tmp_path / "fixtures"
    fixture_path = fixture_root / scenario["fixture"]["path"]
    fixture_path.parent.mkdir(parents=True)
    payload = b"temporary fixture bytes for hash recomputation"
    fixture_path.write_bytes(payload)
    scenario["fixture"]["size_bytes"] = len(payload)
    scenario["fixture"]["sha1"] = hashlib.sha1(payload).hexdigest()
    scenario["fixture"]["sha256"] = hashlib.sha256(payload).hexdigest()

    validator._validate_fixture_assets([scenario], fixture_root)

    fixture_path.write_bytes(b"X" * len(payload))
    with pytest.raises(ValueError, match="SHA-1 mismatch"):
        validator._validate_fixture_assets([scenario], fixture_root)


def test_validator_reports_missing_fixture_bytes(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    missing_root = tmp_path / "fixtures"
    missing_root.mkdir()
    with pytest.raises(ValueError, match="fixture missing"):
        validator._validate_fixture_assets([scenario], missing_root)


def test_validator_cli_schema_only_accepts_catalog_and_rejects_drift(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    good = tmp_path / "good.json"
    good.write_text(json.dumps(catalog), encoding="utf-8")
    assert validator.main(["--catalog", str(good), "--schema-only"]) == 0

    catalog["scenarios"][0]["fixture"]["sha256"] = "0" * 64
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(catalog), encoding="utf-8")
    assert validator.main(["--catalog", str(bad), "--schema-only"]) == 2


def test_producer_cli_refusals_and_bounds(tmp_path: Path) -> None:
    rom = ROOT / "rom" / "red" / "pokemon-red-color.gb"
    sym = ROOT / "rom" / "red" / "pokemon-red.sym"
    output = tmp_path / "out.state"

    assert (
        producer.main(
            [
                "--scenario",
                "not_declared",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
            ]
        )
        == 3
    )

    output.write_bytes(b"existing")
    assert (
        producer.main(
            [
                "--scenario",
                "red_color_battle",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert output.read_bytes() == b"existing"

    output.unlink()
    assert (
        producer.main(
            [
                "--scenario",
                "red_color_battle",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
                "--max-wall-seconds",
                "inf",
            ]
        )
        == 2
    )

    # A recognized suffix is not proof the asset exists. With the declared ROM
    # absent, the producer now refuses before capture instead of reaching the
    # stub, so the missing asset can never be recorded as pinned.
    assert (
        producer.main(
            [
                "--scenario",
                "red_color_battle",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()


def test_producer_report_writer_round_trips(tmp_path: Path) -> None:
    report = tmp_path / "nested" / "report.json"
    producer.write_report(report, {"scenario_id": "red_color_battle"})
    assert json.loads(report.read_text(encoding="utf-8")) == {"scenario_id": "red_color_battle"}
