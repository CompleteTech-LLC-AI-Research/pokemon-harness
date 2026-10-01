"""Explicit versioned admission of the ordinary captured Red six-party state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pokered_harness.config import load_versions
from scripts.validate_fixture_manifest import _validate_assets, _validate_schema

SCENARIO = "normal-red-cerulean-six-party-v1"
ROOT = Path(__file__).resolve().parents[1]


def resolve_normal_red_assets(rom: Path, symbols: Path, fixture_root: Path) -> dict:
    """Fail before emulator launch on any asset or admission mismatch.

    Selection is explicit by versioned identity, not the legacy unique-kind
    resolver. Historical entries and their provenance remain untouched.
    """
    document = json.loads((ROOT / "release-evidence/scenarios" / f"{SCENARIO}.json").read_text())
    if document["schema_version"] != 1 or document["scenario_id"] != SCENARIO:
        raise ValueError("unsupported normal Red admission")
    row = document["fixture"]
    if row["id"] != SCENARIO or row["provenance"]["status"] != "captured":
        raise ValueError("normal Red admission must identify the captured scenario")
    _validate_schema(
        {
            "manifest_id": "pokered-harness.external-link-fixtures",
            "manifest_version": 1,
            "fixture_root": "operator-managed",
            "asset_policy": document["asset_policy"],
            "fixtures": [row],
        }
    )
    _validate_assets([row], fixture_root)
    pins = load_versions(ROOT / "VERSIONS.md")
    for path, recorded, expected in (
        (rom, row["expected_rom"]["sha1"], pins.sha1_for_path(row["expected_rom"]["path"])),
        (
            symbols,
            row["expected_symbols"]["sha1"],
            pins.symbol_sha1_for_path(row["expected_symbols"]["path"]),
        ),
    ):
        if recorded != expected or hashlib.sha1(path.read_bytes()).hexdigest() != recorded:
            raise ValueError(f"admission/VERSIONS/input mismatch: {path}")
    state_path = fixture_root / row["path"]
    return {
        "rom": rom,
        "sym": symbols,
        "state": state_path.read_bytes(),
        "family": "red",
        "pins": {
            "expected_rom_sha1": row["expected_rom"]["sha1"],
            "expected_symbol_sha1": row["expected_symbols"]["sha1"],
            "expected_pyboy_version": pins.pyboy_version,
            "expected_pyboy_revision": pins.pyboy_revision,
        },
        "provenance": {"fixture": str(state_path), "registry": row},
    }
