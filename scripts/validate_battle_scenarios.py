"""Validate the declared battle-scenario catalog.

The catalog in ``release-evidence/battle-scenarios.json`` is the public
contract that mechanics issues use to declare a replayable scenario before
they add real-ROM evidence.  This validator checks the catalog shape, unique
scenario IDs, required fields, finite positive capture bounds, provenance
rules, and that every referenced fixture path/hash matches
``release-evidence/fixture-manifest.json``.

``--schema-only`` performs the asset-free validation used by local CI.  A
release run additionally passes ``--fixture-root`` so the external fixture
bytes are present and their size, SHA-1, and SHA-256 match the catalog pins.
The repository never distributes those bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

_SCENARIO_ID_PATTERN = r"^[a-z0-9]+(_[a-z0-9]+)*$"
_SCENARIO_ID_RE = re.compile(_SCENARIO_ID_PATTERN)
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_STATUSES = {"verified", "derived", "partial", "unknown"}
_ROLE_SUFFIXES = ("__listen", "__connect")
_ROLES = {"listen", "connect"}
_ROM_VERSIONS = {"red", "blue", "yellow"}
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "release-evidence" / "battle-scenarios.json"
_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "release-evidence" / "fixture-manifest.json"


def _error(message: str) -> ValueError:
    return ValueError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _error(message)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _error(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise _error(f"{label} is not valid JSON: {path}: {exc}") from exc

    _require(isinstance(document, dict), f"{label} root must be an object")
    return document


def _validate_relative_path(value: Any, field: str) -> None:
    _require(isinstance(value, str) and value, f"{field} must be a non-empty string")
    path = Path(value)
    windows_path = PureWindowsPath(value)
    _require(
        not path.is_absolute() and not windows_path.is_absolute() and not windows_path.drive,
        f"{field} must be relative: {value!r}",
    )
    _require("\\" not in value, f"{field} must use POSIX separators: {value!r}")
    _require(".." not in path.parts, f"{field} must not escape its root: {value!r}")


def _validate_text(value: Any, field: str) -> None:
    _require(isinstance(value, str) and value.strip(), f"{field} must be a non-empty string")


def base_scenario_id(scenario_id: str) -> str:
    """Return ``scenario_id`` without an optional ``__listen``/``__connect`` suffix."""
    for suffix in _ROLE_SUFFIXES:
        if scenario_id.endswith(suffix):
            return scenario_id[: -len(suffix)]
    return scenario_id


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_scenario_id(value: Any, field: str) -> None:
    _require(isinstance(value, str) and value, f"{field} is required")
    base = base_scenario_id(value)
    _require(
        bool(_SCENARIO_ID_RE.fullmatch(base)), f"{field} does not match {_SCENARIO_ID_PATTERN}"
    )
    _require(
        f"{base}__listen" == value or f"{base}__connect" == value or base == value,
        f"{field} has an unsupported role suffix",
    )


def _validate_sha1(value: Any, field: str) -> None:
    _require(isinstance(value, str) and _SHA1_RE.fullmatch(value), f"{field} is invalid")


def _validate_sha256(value: Any, field: str) -> None:
    _require(isinstance(value, str) and _SHA256_RE.fullmatch(value), f"{field} is invalid")


def _validate_game(game: Any, field: str) -> None:
    _require(isinstance(game, dict), f"{field} must be an object")
    _require(game.get("version") in _ROM_VERSIONS, f"{field}.version is invalid")
    _validate_text(game.get("variant"), f"{field}.variant")
    _validate_relative_path(game.get("rom_path"), f"{field}.rom_path")
    _validate_sha1(game.get("rom_sha1"), f"{field}.rom_sha1")
    _validate_relative_path(game.get("sym_path"), f"{field}.sym_path")
    _validate_sha1(game.get("sym_sha1"), f"{field}.sym_sha1")


def _validate_battle(battle: Any, field: str) -> None:
    _require(isinstance(battle, dict), f"{field} must be an object")
    _validate_text(battle.get("type"), f"{field}.type")
    _validate_text(battle.get("mode"), f"{field}.mode")
    roles = battle.get("roles")
    _require(isinstance(roles, list) and roles, f"{field}.roles must be a non-empty list")
    _require(all(role in _ROLES for role in roles), f"{field}.roles has an unknown role")
    _require(len(roles) == len(set(roles)), f"{field}.roles has duplicates")
    paths = battle.get("paths")
    _require(isinstance(paths, list), f"{field}.paths must be a list")
    for index, path in enumerate(paths):
        _validate_relative_path(path, f"{field}.paths[{index}]")


def _validate_capture_boundary(boundary: Any, field: str) -> None:
    _require(isinstance(boundary, dict), f"{field} must be an object")
    for key in ("stage", "when", "region", "link_state"):
        _validate_text(boundary.get(key), f"{field}.{key}")
    map_id = boundary.get("map_id")
    _require(_is_int(map_id) and map_id >= 0, f"{field}.map_id must be a non-negative integer")


def _validate_party(party: Any, field: str) -> None:
    _require(isinstance(party, dict), f"{field} must be an object")
    count = party.get("count")
    _require(count is None or (_is_int(count) and count >= 0), f"{field}.count is invalid")
    active_slot = party.get("active_slot")
    _require(
        active_slot is None or (_is_int(active_slot) and active_slot >= 0),
        f"{field}.active_slot is invalid",
    )
    if count is not None and active_slot is not None:
        _require(active_slot < count, f"{field}.active_slot must be less than count")
    mons = party.get("mons")
    _require(isinstance(mons, list), f"{field}.mons must be a list")
    _require(all(isinstance(mon, dict) for mon in mons), f"{field}.mons entries must be objects")


def _validate_inventory(inventory: Any, field: str) -> None:
    _require(
        inventory is None or isinstance(inventory, list),
        f"{field} must be null or a list",
    )
    if isinstance(inventory, list):
        _require(
            all(isinstance(item, dict) for item in inventory),
            f"{field} entries must be objects",
        )


def _validate_opponent(opponent: Any, field: str) -> None:
    _require(opponent is None or isinstance(opponent, dict), f"{field} must be null or an object")


def _validate_runtime(runtime: Any, field: str) -> None:
    _require(isinstance(runtime, dict), f"{field} must be an object")
    python = runtime.get("python")
    _require(
        python is None or (isinstance(python, str) and python.strip()), f"{field}.python is invalid"
    )
    _validate_text(runtime.get("pyboy_version"), f"{field}.pyboy_version")
    revision = runtime.get("pyboy_revision")
    _require(
        revision is None or (isinstance(revision, str) and revision.strip()),
        f"{field}.pyboy_revision is invalid",
    )
    role = runtime.get("role")
    _require(role is None or role in _ROLES, f"{field}.role is invalid")


def _validate_bounds(bounds: Any, field: str) -> None:
    _require(isinstance(bounds, dict), f"{field} must be an object")
    for key in ("max_frames", "max_inputs"):
        value = bounds.get(key)
        _require(
            _is_int(value) and value > 0,
            f"{field}.{key} must be a positive integer",
        )
    wall = bounds.get("max_wall_seconds")
    _require(
        isinstance(wall, (int, float)) and not isinstance(wall, bool),
        f"{field}.max_wall_seconds must be a number",
    )
    _require(
        math.isfinite(float(wall)) and float(wall) > 0,
        f"{field}.max_wall_seconds must be finite and greater than zero",
    )


def _validate_fixture(fixture: Any, field: str) -> None:
    _require(isinstance(fixture, dict), f"{field} must be an object")
    _validate_relative_path(fixture.get("path"), f"{field}.path")
    _require(
        fixture.get("repository_distributed") is False,
        f"{field}.repository_distributed must be false",
    )
    size = fixture.get("size_bytes")
    _require(_is_int(size) and size >= 0, f"{field}.size_bytes is invalid")
    _validate_sha1(fixture.get("sha1"), f"{field}.sha1")
    _validate_sha256(fixture.get("sha256"), f"{field}.sha256")
    _validate_text(fixture.get("fixture_id"), f"{field}.fixture_id")


def _validate_provenance(provenance: Any, field: str) -> None:
    _require(isinstance(provenance, dict), f"{field} must be an object")
    status = provenance.get("status")
    _require(status in _PROVENANCE_STATUSES, f"{field}.status is invalid")
    _validate_text(provenance.get("producer"), f"{field}.producer")
    _validate_text(provenance.get("capture_command_template"), f"{field}.capture_command_template")

    source_fixture_id = provenance.get("source_fixture_id")
    _require(
        source_fixture_id is None
        or (isinstance(source_fixture_id, str) and source_fixture_id.strip()),
        f"{field}.source_fixture_id must be null or a non-empty string",
    )
    input_fixture_sha1 = provenance.get("input_fixture_sha1")
    _require(
        input_fixture_sha1 is None
        or (isinstance(input_fixture_sha1, str) and _SHA1_RE.fullmatch(input_fixture_sha1)),
        f"{field}.input_fixture_sha1 is invalid",
    )
    input_sequence = provenance.get("input_sequence")
    _require(
        input_sequence is None
        or (isinstance(input_sequence, str) and input_sequence.strip())
        or (isinstance(input_sequence, list) and input_sequence),
        f"{field}.input_sequence must be null, a non-empty string, or a non-empty list",
    )
    for key in ("runtime_identity", "captured_at_utc", "verification_method"):
        value = provenance.get(key)
        _require(
            value is None or (isinstance(value, str) and value.strip()),
            f"{field}.{key} must be null or a non-empty string",
        )

    if status == "verified":
        for key in ("runtime_identity", "captured_at_utc", "verification_method"):
            _require(
                isinstance(provenance.get(key), str) and provenance[key].strip(),
                f"{field}.{key} is required for verified provenance",
            )
    if status == "derived":
        _require(
            isinstance(source_fixture_id, str) and source_fixture_id.strip(),
            f"{field}.source_fixture_id is required for derived provenance",
        )
        _require(
            (isinstance(input_sequence, str) and input_sequence.strip())
            or (isinstance(input_sequence, list) and input_sequence),
            f"{field}.input_sequence transformation is required for derived provenance",
        )


def _validate_evidence_class(evidence: Any, field: str) -> None:
    _require(isinstance(evidence, dict), f"{field} must be an object")
    expectations = evidence.get("expectations")
    _require(isinstance(expectations, list), f"{field}.expectations must be a list")
    _require(
        all(isinstance(item, str) and item.strip() for item in expectations),
        f"{field}.expectations entries must be non-empty strings",
    )
    authored_tests = evidence.get("authored_tests")
    _require(isinstance(authored_tests, list), f"{field}.authored_tests must be a list")
    _require(
        all(isinstance(item, str) and item.strip() for item in authored_tests),
        f"{field}.authored_tests entries must be non-empty strings",
    )
    _require("real_rom_execution" in evidence, f"{field}.real_rom_execution is required")
    real = evidence["real_rom_execution"]
    _require(
        real is None
        or (isinstance(real, str) and real.strip())
        or (isinstance(real, list) and real)
        or isinstance(real, dict),
        f"{field}.real_rom_execution must be null or recorded evidence",
    )


def _validate_scenario(scenario: Any, index: int) -> str:
    prefix = f"scenarios[{index}]"
    _require(isinstance(scenario, dict), f"{prefix} must be an object")
    scenario_id = scenario.get("scenario_id")
    _validate_scenario_id(scenario_id, f"{prefix}.scenario_id")
    _validate_game(scenario.get("game"), f"{prefix}.game")
    _validate_battle(scenario.get("battle"), f"{prefix}.battle")
    _validate_capture_boundary(scenario.get("capture_boundary"), f"{prefix}.capture_boundary")
    _validate_party(scenario.get("party"), f"{prefix}.party")
    _validate_inventory(scenario.get("inventory"), f"{prefix}.inventory")
    _validate_opponent(scenario.get("opponent"), f"{prefix}.opponent")
    _validate_runtime(scenario.get("runtime"), f"{prefix}.runtime")
    required = scenario.get("required_prior_actions")
    _require(isinstance(required, list), f"{prefix}.required_prior_actions must be a list")
    _require(
        all(isinstance(item, str) and item.strip() for item in required),
        f"{prefix}.required_prior_actions entries must be non-empty strings",
    )
    _validate_bounds(scenario.get("capture_bounds"), f"{prefix}.capture_bounds")
    _validate_fixture(scenario.get("fixture"), f"{prefix}.fixture")
    _validate_provenance(scenario.get("provenance"), f"{prefix}.provenance")
    _validate_evidence_class(scenario.get("evidence_class"), f"{prefix}.evidence_class")
    return scenario_id


def _manifest_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fixtures = manifest.get("fixtures")
    _require(isinstance(fixtures, list) and fixtures, "fixture manifest fixtures must be non-empty")
    return {fixture["id"]: fixture for fixture in fixtures}


# The battle catalog models the battle-relevant state kinds (``ordinary`` and
# ``battle``): each scenario is a one-turn pairing source.  The six-member
# ``slots`` rows are trade-acceptance fixtures whose party records are pairwise
# distinct so the sender/receiver slot rows are falsifiable; they are not battle
# pairing sources, so they are not required to appear in this catalog even
# though the fixture manifest pins and byte-validates them.
_BATTLE_CATALOG_KINDS = frozenset({"ordinary", "battle"})


def _validate_cross_references(scenarios: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    manifest_by_id = _manifest_index(manifest)
    referenced: set[str] = set()
    for index, scenario in enumerate(scenarios):
        prefix = f"scenarios[{index}]"
        fixture = scenario["fixture"]
        fixture_id = fixture["fixture_id"]
        _require(
            fixture_id in manifest_by_id, f"{prefix}.fixture.fixture_id is not in the manifest"
        )
        _require(fixture_id not in referenced, f"duplicate fixture reference: {fixture_id}")
        referenced.add(fixture_id)
        manifest_fixture = manifest_by_id[fixture_id]
        for key in ("path", "size_bytes", "sha1", "sha256"):
            _require(
                fixture[key] == manifest_fixture[key],
                f"{prefix}.fixture.{key} disagrees with the manifest for {fixture_id}",
            )
        expected_rom = manifest_fixture["expected_rom"]
        expected_symbols = manifest_fixture["expected_symbols"]
        game = scenario["game"]
        _require(
            game["rom_path"] == expected_rom["path"] and game["rom_sha1"] == expected_rom["sha1"],
            f"{prefix}.game ROM pin disagrees with the manifest for {fixture_id}",
        )
        _require(
            game["sym_path"] == expected_symbols["path"]
            and game["sym_sha1"] == expected_symbols["sha1"],
            f"{prefix}.game symbol pin disagrees with the manifest for {fixture_id}",
        )

        provenance = scenario["provenance"]
        source_fixture_id = provenance.get("source_fixture_id")
        input_fixture_sha1 = provenance.get("input_fixture_sha1")
        if source_fixture_id is not None:
            _require(
                source_fixture_id in manifest_by_id,
                f"{prefix}.provenance.source_fixture_id is not in the manifest",
            )
            manifest_source = manifest_by_id[source_fixture_id]
            _require(
                input_fixture_sha1 == manifest_source["sha1"],
                f"{prefix}.provenance.input_fixture_sha1 disagrees with manifest fixture "
                f"{source_fixture_id}",
            )
        else:
            _require(
                input_fixture_sha1 is None,
                f"{prefix}.provenance.input_fixture_sha1 requires a source_fixture_id",
            )

    required = {
        fixture_id
        for fixture_id, fixture in manifest_by_id.items()
        if fixture.get("kind") in _BATTLE_CATALOG_KINDS
    }
    missing = sorted(required - referenced)
    _require(not missing, f"catalog is missing manifest fixtures: {', '.join(missing)}")


def _validate_coverage_dimension(document: dict[str, Any]) -> None:
    """Validate the additive coverage/effects dimension.

    The #87 scenario contract remains the primary shape check. The coverage
    catalog is validated here too so a release run cannot silently accept an
    incomplete or self-contradictory coverage declaration. Import the shared
    validator by path when this module is executed as a standalone script.
    """
    try:
        from scripts import coverage_report
    except ModuleNotFoundError:
        import importlib.util

        path = Path(__file__).resolve().parent / "coverage_report.py"
        spec = importlib.util.spec_from_file_location("_coverage_report", path)
        if spec is None or spec.loader is None:
            raise _error("could not load the coverage report validator") from None
        coverage_report = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = coverage_report
        spec.loader.exec_module(coverage_report)
    try:
        coverage_report.validate_catalog(document)
    except ValueError as exc:
        raise _error(f"coverage dimension is invalid: {exc}") from exc


def _validate_schema(document: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    _require(
        document.get("catalog_id") == "pokered-harness.battle-scenarios",
        "unexpected catalog_id",
    )
    _require(document.get("catalog_version") == 1, "unsupported catalog_version")
    _require(
        document.get("scenario_id_pattern") == _SCENARIO_ID_PATTERN,
        "scenario_id_pattern does not match the validator",
    )
    policy = document.get("asset_policy")
    _require(isinstance(policy, dict), "asset_policy must be an object")
    _require(policy.get("repository_distributed") is False, "state assets must remain external")

    scenarios = document.get("scenarios")
    _require(isinstance(scenarios, list) and scenarios, "scenarios must be a non-empty list")

    seen_ids: set[str] = set()
    for index, scenario in enumerate(scenarios):
        scenario_id = _validate_scenario(scenario, index)
        _require(scenario_id not in seen_ids, f"duplicate scenario id: {scenario_id}")
        seen_ids.add(scenario_id)

    _validate_cross_references(scenarios, manifest)
    _validate_coverage_dimension(document)
    return scenarios


def _hash_file(path: Path) -> tuple[str, str]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


def _validate_fixture_assets(scenarios: list[dict[str, Any]], fixture_root: Path) -> None:
    try:
        resolved_root = fixture_root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _error(f"fixture root could not be resolved: {fixture_root}: {exc}") from exc
    if not resolved_root.is_dir():
        raise _error(f"fixture root not found: {fixture_root}")

    for scenario in scenarios:
        fixture = scenario["fixture"]
        candidate = resolved_root / Path(fixture["path"])
        try:
            path = candidate.resolve(strict=False)
            path.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _error(f"fixture path escapes fixture root: {fixture['path']!r}") from exc
        if candidate.is_symlink():
            raise _error(f"fixture must be a regular file, not a symlink: {fixture['path']}")
        if not path.is_file():
            raise _error(f"fixture missing: {path}")
        actual_size = path.stat().st_size
        if actual_size != fixture["size_bytes"]:
            raise _error(
                f"fixture size mismatch for {fixture['path']}: "
                f"expected {fixture['size_bytes']}, got {actual_size}"
            )
        actual_sha1, actual_sha256 = _hash_file(path)
        if actual_sha1 != fixture["sha1"]:
            raise _error(
                f"fixture SHA-1 mismatch for {fixture['path']}: "
                f"expected {fixture['sha1']}, got {actual_sha1}"
            )
        if actual_sha256 != fixture["sha256"]:
            raise _error(
                f"fixture SHA-256 mismatch for {fixture['path']}: "
                f"expected {fixture['sha256']}, got {actual_sha256}"
            )


def _default_fixture_root() -> Path:
    configured = os.environ.get("POKERED_FIXTURE_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "link"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_CATALOG_PATH)
    parser.add_argument("--manifest", type=Path, default=_MANIFEST_PATH)
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=None,
        help="external tests/fixtures/link directory; required for byte validation",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="validate the catalog without accepting missing external assets",
    )
    args = parser.parse_args(argv)

    try:
        document = _load_json(args.catalog, "battle-scenario catalog")
        manifest = _load_json(args.manifest, "fixture manifest")
        scenarios = _validate_schema(document, manifest)
        if not args.schema_only:
            _validate_fixture_assets(
                scenarios, (args.fixture_root or _default_fixture_root()).resolve()
            )
    except (OSError, ValueError) as exc:
        print(f"battle-scenario validation failed: {exc}", file=sys.stderr)
        return 2

    mode = "schema" if args.schema_only else "byte"
    print(f"battle-scenario {mode} validation passed: {len(scenarios)} scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
