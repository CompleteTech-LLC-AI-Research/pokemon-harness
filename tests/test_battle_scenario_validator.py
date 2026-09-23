"""ROM-free validator/staging checks for the battle-scenario catalog."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from scripts import validate_battle_scenarios as validator
from tests._battle_scenario_fixtures_support import (
    _load_catalog,
    _scenario,
)


def test_all_shipped_scenarios_pass_the_new_screening() -> None:
    """The stricter screening must not narrow the ten declared scenarios."""
    catalog = _load_catalog()
    for scenario in catalog["scenarios"]:
        scenario_id = scenario["scenario_id"]
        producer.validate_scenario_metadata(scenario, scenario_id)
        producer._assert_supported_conditions(scenario, scenario_id)


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
