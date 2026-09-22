"""Generate a functional-coverage report from the battle-scenario catalog.

The catalog in ``release-evidence/battle-scenarios.json`` deliberately keeps two
separate coverage dimensions:

* ``one_turn_pairing`` -- the 19 strict battle entry points, each required once
  per declared runtime; and
* ``expanded_mechanics`` -- every move-effect family in the pinned game tables,
  mapped to ``tested`` / ``planned_unverified`` / ``deliberately_excluded``.

This command combines the declared catalog with actual collected and terminal
records (a production-gate JSON/XML path, or an explicit ``--no-results`` mode).
Declaration, enum, or table presence is never execution evidence. A required
case is only ``tested`` when a single terminal ``passed`` record for the
declared runtime with matching runtime build exists. Missing, skipped, xfailed,
timed-out, unrun, partial, duplicated, or mismatched results are reported as
``INCOMPLETE`` and can never be confused with a complete pairing matrix.

The report refuses to merge unrelated partial runs: it accepts one results
source, records that source's identity, and flags differing commits or runtime
builds instead of synthesising a clean gate.
"""

from __future__ import annotations

import argparse
import json
import re
import runpy
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_MATRIX = runpy.run_path(str(_REPO / "scripts" / "tcp_link_matrix.py"))
CATALOG_PATH = _REPO / "release-evidence" / "battle-scenarios.json"
COVERAGE_DIMENSION = "one_turn_pairing"
EXPANDED_DIMENSION = "expanded_mechanics"
_REQUIRED_DIMENSIONS = (COVERAGE_DIMENSION, EXPANDED_DIMENSION)
_RUNTIMES = ("source", "cython")
_ROLES = ("listen", "connect")
_VERSIONS = ("red", "blue", "yellow")
_TRANSPORTS = ("local", "remote")
_SCOPES = ("tested", "planned_unverified", "deliberately_excluded")
# Tool-owned complete effect inventory for effects_version 1: the pinned pret
# move-effect constants 0..86. Never derived from the supplied family list.
_PINNED_EFFECT_IDS: tuple[int, ...] = tuple(range(87))
# Tool-owned exact move-to-effect mapping for effects_version 1, derived from the
# pinned pret `data/moves/moves.asm` and `constants/move_effect_constants.asm`
# (Red/Blue revision fbcf7d0e). Index i is move id i+1 (move ids 1..165); the
# catalog may never reassign, omit, or duplicate a move.
_PINNED_MOVE_EFFECTS: tuple[int, ...] = (
    0,
    0,
    29,
    29,
    0,
    16,
    4,
    5,
    6,
    0,
    0,
    38,
    39,
    50,
    0,
    0,
    0,
    28,
    43,
    42,
    0,
    0,
    37,
    44,
    0,
    45,
    37,
    22,
    37,
    0,
    29,
    38,
    0,
    36,
    42,
    48,
    27,
    48,
    19,
    2,
    77,
    29,
    19,
    31,
    18,
    28,
    32,
    49,
    41,
    86,
    69,
    4,
    4,
    46,
    0,
    0,
    0,
    5,
    5,
    76,
    70,
    68,
    80,
    0,
    0,
    48,
    37,
    0,
    41,
    0,
    3,
    3,
    84,
    13,
    0,
    39,
    66,
    67,
    32,
    27,
    20,
    41,
    42,
    6,
    6,
    67,
    6,
    0,
    0,
    38,
    39,
    66,
    76,
    71,
    32,
    10,
    52,
    0,
    81,
    28,
    41,
    82,
    59,
    15,
    56,
    11,
    15,
    22,
    49,
    11,
    11,
    51,
    64,
    25,
    65,
    47,
    26,
    83,
    9,
    7,
    0,
    36,
    33,
    33,
    31,
    34,
    0,
    42,
    17,
    39,
    29,
    70,
    53,
    22,
    56,
    45,
    67,
    8,
    66,
    29,
    3,
    32,
    39,
    57,
    70,
    0,
    32,
    22,
    41,
    85,
    51,
    0,
    7,
    29,
    44,
    56,
    0,
    31,
    10,
    24,
    0,
    40,
    0,
    79,
    48,
)
# Effect slots the pinned table assigns no move (const_skip padding and unused
# constants); these must be declared empty and deliberately excluded.
_PINNED_UNUSED_EFFECT_IDS: tuple[int, ...] = (
    1,
    12,
    14,
    21,
    23,
    30,
    35,
    54,
    55,
    58,
    60,
    61,
    62,
    63,
    72,
    73,
    74,
    75,
    78,
)
# Tool-owned mandatory runtimes keyed by coverage scope version. The catalog
# declares requirements, but these pins are data the tool owns: a catalog edit
# can never remove a runtime the coverage scope version requires.
_MANDATORY_RUNTIMES_BY_COVERAGE_VERSION: dict[int, tuple[str, ...]] = {
    1: ("source", "cython"),
}
# Report status for a case whose catalog declaration is structurally invalid.
# It is never a terminal pytest outcome and can never count as tested.
_CATALOG_STATUS = "catalog"
# Internal report sentinel for a fully-evidenced required case. It is never a
# terminal pytest outcome and must stay distinct from any result status.
_CASE_SENTINEL = "tested"
# Fallback only; the catalog's coverage.evidence_policy.accepted_outcomes is
# authoritative and is enforced when classifying terminal records.
_DEFAULT_ACCEPTED_OUTCOMES = ("passed",)
_INCOMPLETE_STATUSES = frozenset(
    {
        "missing",
        "duplicate",
        "mismatched",
        "unidentified",
        "unaccepted",
        "partial",
        "collection",
        "skipped",
        "xfailed",
        "xpassed",
        "failed",
        "error",
        "timed_out",
        "interrupted",
        "not_run",
        _CATALOG_STATUS,
    }
)
_GATE_STATUS = {
    "PASS": "passed",
    "FAIL": "failed",
    "TIMEOUT": "timed_out",
    "INTERRUPTED": "interrupted",
    "NOT_STARTED": "not_run",
}
# A production-gate collection entry passes only with an explicitly successful
# status; every other status (FAIL, INTERRUPTED, NOT_STARTED) fails closed.
_COLLECTION_PASS = frozenset({"pass", "passed"})
# The one_turn_pairing cases are settled link battles. A declared case fixture
# must be a link-battle fixture for the listen endpoint's game.
_LINK_BATTLE_TYPE = "link_battle"
_LINK_BATTLE_VARIANTS = {"red": "color", "blue": "color", "yellow": "cgb"}
# Enclosing status values that still mean "not a clean terminal pass". Any
# tier/block status that maps to something outside this set marks every
# contained case as partial rather than silently qualifying the dimension.
_ENCLOSING_PASS = frozenset({"passed"})
_MOVE_EFFECT_RE = re.compile(r"[\"'](?:local|enemy)_move_effect[\"']\s*:\s*(\d+)")
_EFFECT_TEXT_KEYS = ("output_tail", "system_out", "stdout")


class CoverageError(ValueError):
    """Raised when the catalog or a supplied result cannot be interpreted."""


def normalize_nodeid(nodeid: str) -> str:
    """Normalise a pytest node ID for comparison."""
    path, separator, test_name = str(nodeid).partition("::")
    normalized_path = path.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    return f"{normalized_path}::{test_name}" if separator else normalized_path


def load_catalog(path: str | Path = CATALOG_PATH) -> dict[str, Any]:
    """Load a scenario catalog, requiring an object with a ``scenarios`` list."""
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoverageError(f"catalog not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise CoverageError(f"catalog is not valid JSON: {target}: {exc}") from exc
    if not isinstance(document, dict):
        raise CoverageError("catalog root must be an object")
    if not isinstance(document.get("scenarios"), list) or not document["scenarios"]:
        raise CoverageError("catalog.scenarios must be a non-empty list")
    return document


def _coverage(catalog: dict[str, Any]) -> dict[str, Any]:
    coverage = catalog.get("coverage")
    if not isinstance(coverage, dict):
        raise CoverageError("catalog.coverage must be an object")
    return coverage


def _mandatory_runtimes(catalog: dict[str, Any]) -> tuple[str, ...]:
    """Return the tool-owned runtimes every required case must satisfy.

    The mandatory runtimes are keyed to the declared coverage scope version and
    are not read from the catalog's dimension declarations, so removing a
    runtime from the catalog can never weaken the scope requirement.
    """
    coverage = catalog.get("coverage")
    version = coverage.get("coverage_version") if isinstance(coverage, dict) else None
    mandatory = _MANDATORY_RUNTIMES_BY_COVERAGE_VERSION.get(version)
    return mandatory if mandatory is not None else _RUNTIMES


def dimensions(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = _coverage(catalog).get("dimensions")
    if not isinstance(raw, list):
        raise CoverageError("catalog.coverage.dimensions must be a list")
    result: dict[str, dict[str, Any]] = {}
    for index, dimension in enumerate(raw):
        if not isinstance(dimension, dict):
            raise CoverageError(f"coverage.dimensions[{index}] must be an object")
        dimension_id = dimension.get("dimension_id")
        if not isinstance(dimension_id, str) or not dimension_id:
            raise CoverageError(f"coverage.dimensions[{index}].dimension_id is required")
        result[dimension_id] = dimension
    return result


def required_cases(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _coverage(catalog).get("required_cases")
    if not isinstance(raw, list):
        raise CoverageError("catalog.coverage.required_cases must be a list")
    return [case for case in raw if isinstance(case, dict)]


def one_turn_pairing_cases(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the declared one-turn pairing cases (the strict battle rows)."""
    return [
        case for case in required_cases(catalog) if case.get("dimension_id") == COVERAGE_DIMENSION
    ]


def effect_families(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    move_effects = catalog.get("move_effects")
    if not isinstance(move_effects, dict):
        raise CoverageError("catalog.move_effects must be an object")
    raw = move_effects.get("families")
    if not isinstance(raw, list):
        raise CoverageError("catalog.move_effects.families must be a list")
    families = [family for family in raw if isinstance(family, dict)]
    if len(families) != len(raw):
        raise CoverageError("catalog.move_effects.families entries must be objects")
    return families


def _scenario_fixture_ids(catalog: dict[str, Any]) -> set[str]:
    return set(_scenarios_by_fixture_id(catalog))


def _scenarios_by_fixture_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scenarios: dict[str, dict[str, Any]] = {}
    for scenario in catalog.get("scenarios", []):
        if not isinstance(scenario, dict):
            continue
        fixture = scenario.get("fixture")
        if isinstance(fixture, dict) and isinstance(fixture.get("fixture_id"), str):
            scenarios.setdefault(fixture["fixture_id"], scenario)
    return scenarios


def _profile_version(profile: str) -> str:
    """Map a strict profile name (for example ``red_color``) to its version."""
    return profile.removesuffix("_color")


def _selector_endpoint_versions() -> dict[str, tuple[str, str]]:
    """Map every authoritative battle selector to its ordered endpoint versions."""
    matrix = _MATRIX
    mapping: dict[str, tuple[str, str]] = {}
    for left, right in matrix["SUPPORTED_VERSION_PAIRS"]:
        mapping[
            "tests/test_pyboy_link_session_roms.py::"
            f"test_pair_completes_battle_turn[{left}-{right}]"
        ] = (left, right)
    for listener, connector in matrix["REMOTE_STRICT_PROFILE_PAIRS"]:
        mapping[
            "tests/test_pyboy_link_session_subprocess.py::"
            f"test_subprocess_pair_resolves_battle_turn_over_tcp"
            f"[{listener}-listen-{connector}-connect]"
        ] = (_profile_version(listener), _profile_version(connector))
    mapping["tests/test_pyboy_link_session_roms.py::test_red_yellow_battle_turn_is_resolved"] = (
        "red",
        "yellow",
    )
    return mapping


def _canonical_battle_scenarios(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the canonical link-battle scenario for each game version."""
    battles: dict[str, dict[str, Any]] = {}
    for scenario in catalog.get("scenarios", []):
        if not isinstance(scenario, dict):
            continue
        game = scenario.get("game") if isinstance(scenario.get("game"), dict) else {}
        battle = scenario.get("battle") if isinstance(scenario.get("battle"), dict) else {}
        version = game.get("version")
        if battle.get("type") != _LINK_BATTLE_TYPE or version not in _VERSIONS:
            continue
        if game.get("variant") == _LINK_BATTLE_VARIANTS.get(version):
            battles.setdefault(version, scenario)
    return battles


def _case_endpoint_scenarios(
    catalog: dict[str, Any], case: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Resolve the canonical battle scenario for each declared endpoint role."""
    roles = case.get("roles")
    versions = case.get("game_versions")
    if not isinstance(roles, list) or not isinstance(versions, list):
        return {}
    if len(roles) != len(versions) or set(roles) != set(_ROLES):
        return {}
    battles = _canonical_battle_scenarios(catalog)
    resolved: dict[str, dict[str, Any]] = {}
    for role, version in zip(roles, versions, strict=True):
        scenario = battles.get(version)
        if scenario is not None:
            resolved[role] = scenario
    return resolved


def _validate_case_fixture(
    prefix: str,
    case: dict[str, Any],
    scenario: dict[str, Any],
    battle_by_version: dict[str, dict[str, Any]],
) -> None:
    """Reject a case whose endpoint assignments disagree with its selector.

    The declared case's ``roles`` and ``game_versions`` are parallel endpoint
    assignments. Both endpoints must match the ordered versions encoded by the
    authoritative selector, the fixture must belong to the listen endpoint's
    game, be a link battle, carry both endpoint roles, and use the canonical
    variant for that game. The connect endpoint must resolve to a canonical
    link-battle scenario as well. A wrong-game, wrong-variant, or swapped
    endpoint assignment must fail closed rather than silently qualify the
    pairing matrix.
    """
    fixture_id = case.get("fixture_id")
    versions = case.get("game_versions")
    roles = case.get("roles")
    if not isinstance(versions, list) or not isinstance(roles, list):
        return
    if len(versions) != len(roles):
        raise CoverageError(
            f"{prefix}.game_versions and roles must assign both endpoints: "
            f"{len(versions)} versions vs {len(roles)} roles"
        )
    if set(roles) != set(_ROLES) or len(set(roles)) != len(_ROLES):
        raise CoverageError(
            f"{prefix}.roles must assign both listen and connect endpoints: {roles!r}"
        )
    endpoint = dict(zip(roles, versions, strict=True))
    selector = case.get("selector")
    if isinstance(selector, str):
        expected = _selector_endpoint_versions().get(normalize_nodeid(selector))
        if expected is not None and tuple(versions) != expected:
            raise CoverageError(
                f"{prefix}.game_versions {versions!r} do not match selector "
                f"{selector!r} endpoints {expected!r}; both endpoint assignments "
                "must agree with the authoritative selector"
            )
    listen_version = endpoint["listen"]
    connect_version = endpoint["connect"]
    for role, version in (("listen", listen_version), ("connect", connect_version)):
        canonical = battle_by_version.get(version)
        if canonical is None:
            raise CoverageError(
                f"{prefix}.{role} endpoint {version!r} has no canonical "
                f"{_LINK_BATTLE_TYPE!r} scenario in the catalog"
            )
    game = scenario.get("game") if isinstance(scenario.get("game"), dict) else {}
    battle = scenario.get("battle") if isinstance(scenario.get("battle"), dict) else {}
    if game.get("version") != listen_version:
        raise CoverageError(
            f"{prefix}.fixture_id {fixture_id!r} is a {game.get('version')!r} fixture "
            f"but the listen endpoint is {listen_version!r}"
        )
    expected_variant = _LINK_BATTLE_VARIANTS.get(listen_version)
    if expected_variant is not None and game.get("variant") != expected_variant:
        raise CoverageError(
            f"{prefix}.fixture_id {fixture_id!r} uses variant {game.get('variant')!r}, "
            f"expected {expected_variant!r} for the {listen_version!r} listen endpoint"
        )
    if battle.get("type") != _LINK_BATTLE_TYPE:
        raise CoverageError(
            f"{prefix}.fixture_id {fixture_id!r} is not a {_LINK_BATTLE_TYPE!r} fixture "
            f"(battle type {battle.get('type')!r})"
        )
    scenario_roles = battle.get("roles")
    if not isinstance(scenario_roles, list) or not set(roles).issubset(set(scenario_roles)):
        raise CoverageError(
            f"{prefix}.fixture_id {fixture_id!r} does not assign both case roles: "
            f"fixture roles {scenario_roles!r}, case roles {roles!r}"
        )
    canonical_listen = battle_by_version.get(listen_version, {})
    canonical_fixture = canonical_listen.get("fixture", {}).get("fixture_id")
    if canonical_fixture is not None and fixture_id != canonical_fixture:
        raise CoverageError(
            f"{prefix}.fixture_id {fixture_id!r} is not the canonical "
            f"{listen_version!r} battle fixture {canonical_fixture!r}"
        )


def _declared_input_hashes(scenario: dict[str, Any] | None) -> dict[str, str]:
    """Return declared ROM/symbol/fixture hashes keyed by observed-input aliases."""
    hashes: dict[str, str] = {}
    if not isinstance(scenario, dict):
        return hashes
    fixture = scenario.get("fixture")
    if isinstance(fixture, dict):
        sha1 = fixture.get("sha1")
        if isinstance(sha1, str) and sha1:
            hashes["fixture"] = sha1.lower()
            fixture_id = fixture.get("fixture_id")
            if isinstance(fixture_id, str) and fixture_id:
                hashes[fixture_id.lower()] = sha1.lower()
    game = scenario.get("game")
    if isinstance(game, dict):
        for alias, field in (
            ("rom", "rom_sha1"),
            ("rom_sha1", "rom_sha1"),
            ("sym", "sym_sha1"),
            ("symbol", "sym_sha1"),
            ("sym_sha1", "sym_sha1"),
        ):
            value = game.get(field)
            if isinstance(value, str) and value:
                hashes.setdefault(alias, value.lower())
    return hashes


def _declared_fixture_sha1(scenario: dict[str, Any] | None) -> str | None:
    return _declared_input_hashes(scenario).get("fixture")


def _authoritative_selectors() -> frozenset[str]:
    return frozenset(_MATRIX["STRICT_BATTLE_NODEIDS"])


def _case_declaration_error(
    prefix: str,
    case: dict[str, Any],
    known_dimensions: dict[str, dict[str, Any]],
    fixtures: set[str],
    scenarios_by_fixture: dict[str, dict[str, Any]],
    battle_by_version: dict[str, dict[str, Any]],
) -> str | None:
    """Return the first structural catalog error for one case, or ``None``.

    This mirrors the per-case checks in :func:`validate_catalog` so a report can
    refuse to credit a case whose declaration is invalid. Runtime completeness
    is enforced separately against the tool-owned mandatory runtimes.
    """
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        return f"{prefix}.case_id is required"
    dimension_id = case.get("dimension_id")
    if dimension_id not in known_dimensions:
        return f"{prefix}.dimension_id is unknown: {dimension_id!r}"
    if case.get("operation") != "battle":
        return f"{prefix}.operation must be battle"
    if case.get("transport") not in _TRANSPORTS:
        return f"{prefix}.transport is invalid"
    versions = case.get("game_versions")
    if not isinstance(versions, list) or not versions:
        return f"{prefix}.game_versions must be a non-empty list"
    if any(version not in _VERSIONS for version in versions):
        return f"{prefix}.game_versions has an unknown version"
    roles = case.get("roles")
    if not isinstance(roles, list) or not roles:
        return f"{prefix}.roles must be a non-empty list"
    if any(role not in _ROLES for role in roles):
        return f"{prefix}.roles has an unknown role"
    case_runtimes = case.get("runtimes")
    if not isinstance(case_runtimes, list) or not case_runtimes:
        return f"{prefix}.runtimes must be a non-empty list"
    if any(runtime not in _RUNTIMES for runtime in case_runtimes):
        return f"{prefix}.runtimes has an unknown runtime"
    fixture_id = case.get("fixture_id")
    if fixture_id not in fixtures:
        return f"{prefix}.fixture_id is unknown: {fixture_id!r}"
    try:
        _validate_case_fixture(prefix, case, scenarios_by_fixture[fixture_id], battle_by_version)
    except CoverageError as exc:
        return str(exc)
    declared_effect = case.get("effect_id")
    if declared_effect is not None and (
        isinstance(declared_effect, bool) or not isinstance(declared_effect, int)
    ):
        return f"{prefix}.effect_id must be an integer"
    if not isinstance(case.get("oracle"), str) or not case["oracle"].strip():
        return f"{prefix}.oracle is required"
    selector = case.get("selector")
    if not isinstance(selector, str) or "::" not in selector:
        return f"{prefix}.selector must be a pytest node ID"
    if case.get("status") != "required":
        return f"{prefix}.status must be required"
    return None


def validate_catalog(catalog: dict[str, Any]) -> None:
    """Validate the additive coverage/effects dimension, raising on defects."""
    coverage = _coverage(catalog)
    if coverage.get("coverage_version") != 1:
        raise CoverageError("unsupported coverage_version")
    known_dimensions = dimensions(catalog)
    for required_dimension in _REQUIRED_DIMENSIONS:
        if required_dimension not in known_dimensions:
            raise CoverageError(f"missing required coverage dimension: {required_dimension}")

    policy = coverage.get("evidence_policy")
    if not isinstance(policy, dict):
        raise CoverageError("catalog.coverage.evidence_policy must be an object")
    accepted = policy.get("accepted_outcomes")
    if (
        not isinstance(accepted, list)
        or not accepted
        or any(not isinstance(item, str) or not item.strip() for item in accepted)
    ):
        raise CoverageError(
            "coverage.evidence_policy.accepted_outcomes must be a non-empty list of strings"
        )
    if _CASE_SENTINEL in accepted:
        raise CoverageError(
            f"accepted_outcomes must not use the internal {_CASE_SENTINEL!r} sentinel; "
            "it is not a terminal pytest outcome"
        )
    forbidden = sorted(set(accepted) & _INCOMPLETE_STATUSES)
    if forbidden:
        raise CoverageError(
            "coverage.evidence_policy.accepted_outcomes must never authorize "
            f"non-passing outcomes: {', '.join(forbidden)}"
        )

    fixtures = _scenario_fixture_ids(catalog)
    scenarios_by_fixture = _scenarios_by_fixture_id(catalog)
    battle_by_version = _canonical_battle_scenarios(catalog)
    mandatory_runtimes = _mandatory_runtimes(catalog)
    version = coverage.get("coverage_version")
    seen_case_ids: set[str] = set()
    seen_selectors: set[tuple[str, str]] = set()
    role_coverage: dict[str, set[str]] = {dimension_id: set() for dimension_id in known_dimensions}
    declared_pairing: set[str] = set()

    for index, case in enumerate(required_cases(catalog)):
        prefix = f"coverage.required_cases[{index}]"
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise CoverageError(f"{prefix}.case_id is required")
        if case_id in seen_case_ids:
            raise CoverageError(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)
        error = _case_declaration_error(
            prefix,
            case,
            known_dimensions,
            fixtures,
            scenarios_by_fixture,
            battle_by_version,
        )
        if error is not None:
            raise CoverageError(error)
        dimension_id = case.get("dimension_id")
        case_runtimes = case.get("runtimes")
        missing_runtimes = [
            runtime for runtime in mandatory_runtimes if runtime not in case_runtimes
        ]
        if missing_runtimes:
            raise CoverageError(
                f"{prefix}.runtimes omits mandatory runtime(s) for coverage_version "
                f"{version!r}: {', '.join(missing_runtimes)}"
            )
        selector = case.get("selector")
        normalized = normalize_nodeid(selector)
        if (dimension_id, normalized) in seen_selectors:
            raise CoverageError(f"duplicate case selector: {selector}")
        seen_selectors.add((dimension_id, normalized))
        role_coverage.setdefault(dimension_id, set()).update(case.get("roles"))
        if dimension_id == COVERAGE_DIMENSION:
            declared_pairing.add(normalized)

    for dimension_id, dimension in known_dimensions.items():
        required_roles = dimension.get("required_roles")
        if not isinstance(required_roles, list) or not required_roles:
            raise CoverageError(f"dimension {dimension_id!r} must declare required_roles")
        if required_roles and role_coverage.get(dimension_id):
            missing_roles = sorted(set(required_roles) - role_coverage[dimension_id])
            if missing_roles:
                raise CoverageError(
                    f"dimension {dimension_id!r} has no case for roles: {', '.join(missing_roles)}"
                )
        dimension_runtimes = dimension.get("required_runtimes")
        dimension_runtimes = dimension_runtimes if isinstance(dimension_runtimes, list) else []
        missing_runtimes = [
            runtime for runtime in mandatory_runtimes if runtime not in dimension_runtimes
        ]
        if missing_runtimes:
            raise CoverageError(
                f"dimension {dimension_id!r} omits mandatory runtime(s) for "
                f"coverage_version {version!r}: {', '.join(missing_runtimes)}"
            )

    expected_pairing = _authoritative_selectors()
    if declared_pairing != expected_pairing:
        missing = sorted(expected_pairing - declared_pairing)
        extra = sorted(declared_pairing - expected_pairing)
        raise CoverageError(
            "one_turn_pairing declaration does not match tcp_link_matrix "
            f"(missing={missing}, undeclared={extra})"
        )

    _validate_move_effects(catalog)


def _validate_move_effects(catalog: dict[str, Any]) -> None:
    move_effects = catalog.get("move_effects")
    if not isinstance(move_effects, dict):
        raise CoverageError("catalog.move_effects must be an object")
    if move_effects.get("effects_version") != 1:
        raise CoverageError("unsupported effects_version")
    families = effect_families(catalog)
    if move_effects.get("family_count") != len(families):
        raise CoverageError("family_count does not match the family list")
    # The complete effect inventory is tool-owned: effects_version 1 covers the
    # pinned pret move-effect constants 0..86.  It must never be derived from
    # the supplied list, or a truncated list would define its own sufficiency.
    expected_ids = list(_PINNED_EFFECT_IDS)
    if [family.get("effect_id") for family in families] != expected_ids:
        raise CoverageError("effect families must cover every pinned effect id exactly once")
    expanded_cases = {
        case.get("case_id"): case
        for case in required_cases(catalog)
        if case.get("dimension_id") == EXPANDED_DIMENSION
    }
    planned = 0
    excluded = 0
    for family in families:
        scope = family.get("scope")
        if scope not in _SCOPES:
            raise CoverageError(f"effect {family.get('effect_id')} has an unknown scope")
        if not isinstance(family.get("reason"), str) or not family["reason"].strip():
            raise CoverageError(f"effect {family.get('effect_id')} requires a reason")
        move_ids = family.get("move_ids")
        if not isinstance(move_ids, list):
            raise CoverageError(f"effect {family.get('effect_id')} require a move_ids list")
        if scope == "planned_unverified":
            planned += 1
        elif scope == "deliberately_excluded":
            excluded += 1
        else:
            _validate_tested_family_evidence(family, expanded_cases)

    declared_moves: dict[int, int] = {}
    for family in families:
        effect_id = family.get("effect_id")
        for move_id in family.get("move_ids", []):
            if not isinstance(move_id, int) or isinstance(move_id, bool):
                raise CoverageError(f"effect {effect_id} has a non-integer move id")
            if move_id in declared_moves:
                raise CoverageError(f"move {move_id} is mapped to multiple effects")
            declared_moves[move_id] = effect_id
    expected_moves = {index + 1: effect for index, effect in enumerate(_PINNED_MOVE_EFFECTS)}
    if declared_moves != expected_moves:
        missing = sorted(set(expected_moves) - set(declared_moves))
        extra = sorted(set(declared_moves) - set(expected_moves))
        wrong = sorted(
            move
            for move in set(declared_moves) & set(expected_moves)
            if declared_moves[move] != expected_moves[move]
        )
        raise CoverageError(
            "move-to-effect mapping does not match the pinned table "
            f"(missing={missing[:8]}, extra={extra[:8]}, wrong={wrong[:8]})"
        )
    for family in families:
        effect_id = family.get("effect_id")
        if not family.get("move_ids"):
            if effect_id not in _PINNED_UNUSED_EFFECT_IDS:
                raise CoverageError(
                    f"effect {effect_id} declares no moves but the pinned table assigns moves to it"
                )
            if family.get("scope") != "deliberately_excluded":
                raise CoverageError(f"unused effect {effect_id} must be deliberately_excluded")
        elif effect_id in _PINNED_UNUSED_EFFECT_IDS:
            raise CoverageError(f"unused effect {effect_id} must not declare moves")

    if move_effects.get("planned_unverified_count") != planned:
        raise CoverageError("planned_unverified_count does not match the family list")
    if move_effects.get("deliberately_excluded_count") != excluded:
        raise CoverageError("deliberately_excluded_count does not match the family list")

    selector = move_effects.get("current_selector")
    if not isinstance(selector, dict):
        raise CoverageError("catalog.move_effects.current_selector must be an object")
    if selector.get("admitted_effect_ids") != [0, 6, 44]:
        raise CoverageError("current_selector must admit effects 0, 6, and 44")
    excluded_moves = selector.get("excluded_moves")
    if not isinstance(excluded_moves, list):
        raise CoverageError("current_selector.excluded_moves must be a list")
    excluded_ids = {entry.get("move_id") for entry in excluded_moves if isinstance(entry, dict)}
    if 68 not in excluded_ids:
        raise CoverageError("current_selector must record the Counter (move 68) exclusion")


def _validate_tested_family_evidence(
    family: dict[str, Any], expanded_cases: dict[str, dict[str, Any]]
) -> None:
    """Require a ``tested`` family to name declared mechanics cases.

    A truthy string is declaration presence, not evidence. Every referenced
    case must be a declared ``expanded_mechanics`` case for this same effect so
    the report can later require its terminal passing result before counting the
    family as tested.
    """
    effect_id = family.get("effect_id")
    evidence = family.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(item, str) or not item.strip() for item in evidence)
    ):
        raise CoverageError(
            f"effect {effect_id} claims tested without a list of declared mechanics case IDs"
        )
    for case_id in evidence:
        case = expanded_cases.get(case_id)
        if case is None:
            raise CoverageError(
                f"effect {effect_id} claims tested with undeclared mechanics case {case_id!r}"
            )
        declared_effect = case.get("effect_id")
        if declared_effect != effect_id:
            raise CoverageError(
                f"effect {effect_id} claims case {case_id!r} declared for effect {declared_effect!r}"
            )


@dataclass
class Outcome:
    """One terminal test outcome for a single node ID and runtime."""

    nodeid: str
    status: str
    runtime: str
    reason: str = ""
    partial: bool = False
    commit: str | None = None
    run_id: str | None = None
    input_hashes: dict[str, str] = field(default_factory=dict)
    effects: tuple[int, ...] = ()


@dataclass
class CollectionEvidence:
    """One pytest collection record for a declared runtime."""

    runtime: str
    name: str
    status: str
    nodeids: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class ResultSet:
    """Normalised collected/terminal records from one results source."""

    source_path: str | None = None
    source_kind: str = "unknown"
    run_id: str | None = None
    commit: str | None = None
    partial: bool = False
    runtimes: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)
    collection_failures: list[str] = field(default_factory=list)
    collections: list[CollectionEvidence] = field(default_factory=list)
    block_run_ids: set[str] = field(default_factory=set)
    block_commits: set[str] = field(default_factory=set)
    missing_commit: bool = False
    notes: list[str] = field(default_factory=list)
    identity_conflicts: list[str] = field(default_factory=list)
    merge_conflict: bool = False

    def lookup(self, runtime: str, selector: str) -> list[Outcome]:
        target = normalize_nodeid(selector)
        return [
            outcome
            for outcome in self.outcomes
            if outcome.runtime == runtime and normalize_nodeid(outcome.nodeid) == target
        ]


def _status_from_gate(status: Any) -> str:
    if isinstance(status, str) and status in _GATE_STATUS:
        return _GATE_STATUS[status]
    if isinstance(status, str) and status:
        return status.lower()
    return "error"


def _runtime_identity(block: dict[str, Any]) -> dict[str, Any]:
    runtime = block.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _effects_from_text(text: str) -> tuple[int, ...]:
    """Extract observed ``local``/``enemy`` move effects from retained output."""
    return tuple(int(match.group(1)) for match in _MOVE_EFFECT_RE.finditer(text))


def _record_effects(record: dict[str, Any]) -> tuple[int, ...]:
    """Return the observed move effects carried by a result record.

    A normalised record may declare ``effect_id``/``effect`` directly. Gate and
    JUnit evidence instead retain the strict battle test's printed settlement
    rows, so the same normalisation path also scans the retained output text.
    """
    explicit = record.get("effect_id", record.get("effect"))
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return (explicit,)
    effects: list[int] = []
    for key in _EFFECT_TEXT_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value:
            effects.extend(_effects_from_text(value))
    return tuple(effects)


def _enclosing_partial(record: dict[str, Any]) -> bool:
    """Return whether an enclosing container marks every child non-terminal.

    An explicit ``partial`` marker or any enclosing status that is not a clean
    terminal pass propagates to every contained case so a timed-out or partial
    tier can never qualify a dimension.
    """
    if bool(record.get("partial", False)):
        return True
    status = record.get("status")
    if isinstance(status, str) and status.strip():
        return _status_from_gate(status) not in _ENCLOSING_PASS
    return False


def _normalise_outcome(
    record: dict[str, Any] | None,
    *,
    nodeid: str,
    status: str,
    runtime: str,
    inherited_partial: bool,
    inherited_commit: str | None,
    inherited_run_id: str | None,
    inherited_hashes: dict[str, str],
) -> Outcome:
    """Build one :class:`Outcome`, preserving partial and identity provenance.

    Every supported input format funnels through this single normaliser so a
    record-level ``partial`` marker, commit, run identity, input hashes, and
    observed move effect behave the same for ``records``, ``tests``, gate
    ``tiers``/``case_results``, and JUnit testcases.
    """
    source = record if isinstance(record, dict) else {}
    commit = source.get("commit")
    if not (isinstance(commit, str) and commit):
        commit = inherited_commit
    run_id = source.get("run_id")
    if not (isinstance(run_id, str) and run_id):
        run_id = inherited_run_id
    input_hashes = dict(inherited_hashes)
    input_hashes.update(_record_input_hashes(source))
    return Outcome(
        nodeid=nodeid,
        status=status,
        runtime=runtime,
        reason=str(source.get("reason", "")),
        partial=bool(source.get("partial", False)) or inherited_partial,
        commit=commit,
        run_id=run_id,
        input_hashes=input_hashes,
        effects=_record_effects(source),
    )


def _mode_for(block: dict[str, Any]) -> str:
    mode = block.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    identity = _runtime_identity(block)
    mode = identity.get("pyboy_mode")
    if isinstance(mode, str) and mode:
        return mode
    return "unknown"


def _collect_collection_errors(raw: Any, result: ResultSet) -> None:
    if not isinstance(raw, list):
        return
    for entry in raw:
        if isinstance(entry, dict):
            result.collection_errors.append(
                f"{entry.get('nodeid', '<collection>')}: {entry.get('reason', '')}"
            )
        elif isinstance(entry, str):
            result.collection_errors.append(entry)


def _collect_collections(raw: Any, runtime: str, result: ResultSet) -> None:
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        name = name if isinstance(name, str) and name else "collection"
        status = entry.get("status")
        status = status if isinstance(status, str) and status else "error"
        raw_nodeids = entry.get("nodeids")
        nodeids = (
            tuple(item for item in raw_nodeids if isinstance(item, str))
            if isinstance(raw_nodeids, list)
            else ()
        )
        reason = entry.get("reason")
        reason = reason if isinstance(reason, str) else ""
        result.collections.append(
            CollectionEvidence(
                runtime=runtime,
                name=name,
                status=status,
                nodeids=nodeids,
                reason=reason,
            )
        )
        if status.lower() not in _COLLECTION_PASS:
            result.collection_failures.append(f"{name} ({runtime}): {reason or status}")


def result_set_from_document(
    document: Any,
    *,
    source_path: str | None = None,
) -> ResultSet:
    """Normalise a gate/evidence/normalised JSON document into a result set."""
    if not isinstance(document, dict):
        raise CoverageError("results root must be an object")
    result = ResultSet(source_path=source_path, source_kind="json")
    result.run_id = document.get("run_id") if isinstance(document.get("run_id"), str) else None
    result.commit = document.get("commit") if isinstance(document.get("commit"), str) else None
    result.partial = _enclosing_partial(document)
    document_input_hashes = _document_input_hashes(document)
    _collect_collection_errors(document.get("collection_errors"), result)

    raw_runtimes = document.get("runtimes")
    if isinstance(raw_runtimes, list):
        blocks = [block for block in raw_runtimes if isinstance(block, dict)]
        block_modes = {_mode_for(block) for block in blocks}
        default_mode = next(iter(block_modes)) if len(block_modes) == 1 else "unknown"
        for mode in sorted(block_modes):
            _collect_collections(document.get("collections"), mode, result)
        block_run_ids: set[str] = set()
        block_commits: set[str] = set()
        fingerprints: dict[str, set[tuple[Any, Any, str]]] = {}
        block_partial_by_mode: dict[str, bool] = {}
        any_partial = result.partial
        for block in blocks:
            mode = _mode_for(block)
            identity = _runtime_identity(block)
            block_run = block.get("run_id")
            block_run = block_run if isinstance(block_run, str) and block_run else None
            block_commit = block.get("commit")
            block_commit = block_commit if isinstance(block_commit, str) and block_commit else None
            block_partial = _enclosing_partial(block) or result.partial
            block_partial_by_mode[mode] = block_partial_by_mode.get(mode, False) or block_partial
            if block_run:
                block_run_ids.add(block_run)
            if block_commit:
                block_commits.add(block_commit)
            if block_partial:
                any_partial = True
            fingerprints.setdefault(mode, set()).add(
                (
                    block_run,
                    block_commit,
                    json.dumps(identity, sort_keys=True, default=str),
                )
            )
            if identity:
                result.runtimes[mode] = {**identity, "mode": mode}
            elif mode not in result.runtimes:
                result.runtimes[mode] = {"mode": mode}
            _collect_collection_errors(block.get("collection_errors"), result)
            _collect_collections(block.get("collections"), mode, result)
            _collect_outcomes(
                block,
                mode,
                result,
                block_run=block_run,
                block_commit=block_commit,
                block_partial=block_partial,
                default_input_hashes=document_input_hashes,
            )
        _record_block_conflicts(result, block_run_ids, block_commits, fingerprints, any_partial)
        raw_records = document.get("records")
        if isinstance(raw_records, list):
            _collect_outcome_records(
                raw_records,
                default_mode,
                result,
                block_run=result.run_id,
                block_commit=result.commit,
                block_partial=result.partial,
                default_input_hashes=document_input_hashes,
                runtime_partial=block_partial_by_mode,
            )
        _finalize_identity(result)
        return result

    if not (
        isinstance(document.get("tiers"), list)
        or isinstance(document.get("runtime"), dict)
        or isinstance(document.get("records"), list)
        or isinstance(document.get("tests"), list)
    ):
        result.notes.append("results source contains no runtime blocks")
        return result

    mode = _mode_for(document)
    identity = _runtime_identity(document)
    if identity:
        result.runtimes[mode] = {**identity, "mode": mode}
    elif mode not in result.runtimes:
        result.runtimes[mode] = {"mode": mode}
    _collect_collections(document.get("collections"), mode, result)
    _collect_outcomes(
        document,
        mode,
        result,
        block_run=result.run_id,
        block_commit=result.commit,
        block_partial=result.partial,
        default_input_hashes=document_input_hashes,
    )
    _finalize_identity(result)
    return result


def _record_block_conflicts(
    result: ResultSet,
    block_run_ids: set[str],
    block_commits: set[str],
    fingerprints: dict[str, set[tuple[Any, Any, str]]],
    any_partial: bool,
) -> None:
    result.block_run_ids = set(block_run_ids)
    result.block_commits = set(block_commits)
    if any_partial:
        result.identity_conflicts.append(
            "results source contains partial blocks; refusing to merge partial runs"
        )
    for mode, prints in sorted(fingerprints.items()):
        if len(prints) > 1:
            result.identity_conflicts.append(
                f"results source merges conflicting block identities for runtime {mode!r}"
            )


def _finalize_identity(result: ResultSet) -> None:
    record_commits = {outcome.commit for outcome in result.outcomes if outcome.commit}
    record_run_ids = {outcome.run_id for outcome in result.outcomes if outcome.run_id}
    all_commits = set(record_commits) | set(result.block_commits)
    if result.commit:
        all_commits.add(result.commit)
    all_run_ids = set(record_run_ids) | set(result.block_run_ids)
    if result.run_id:
        all_run_ids.add(result.run_id)
    if len(all_commits) > 1:
        result.identity_conflicts.append(
            "results source mixes "
            f"{len(all_commits)} commit identities across document, blocks, and records"
        )
    if len(all_run_ids) > 1:
        result.identity_conflicts.append(
            "results source mixes "
            f"{len(all_run_ids)} run identities across document, blocks, and records"
        )
    if result.outcomes and not all_commits:
        result.missing_commit = True
    if result.identity_conflicts:
        result.merge_conflict = True


def _record_input_hashes(record: dict[str, Any]) -> dict[str, str]:
    """Extract observed input hashes from a record or document, if declared."""
    hashes: dict[str, str] = {}
    raw = record.get("input_hashes")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and key and isinstance(value, str) and value:
                hashes[key.lower()] = value.lower()
    fixture_sha1 = record.get("fixture_sha1")
    if isinstance(fixture_sha1, str) and fixture_sha1:
        hashes.setdefault("fixture", fixture_sha1.lower())
    return hashes


def _asset_version(label: str) -> str | None:
    """Return the game version encoded in a gate asset label, if any."""
    head = re.split(r"[\s\-_]+", label.strip().lower(), maxsplit=1)[0]
    return head if head in _VERSIONS else None


def _canonical_asset_version(kind: Any, label: str) -> str | None:
    """Map a gate asset to its canonical version without stock shadowing.

    A strict battle row uses the canonical ``red-color``/``blue-color``/
    ``yellow`` ROM and fixture, not the stock or vanilla ROM that may share a
    version prefix. Stock and vanilla assets must not override the canonical
    pin, so they are ignored here.
    """
    normalized = label.strip().lower()
    if kind == "rom":
        if normalized == "yellow":
            return "yellow"
        for version in ("red", "blue"):
            if normalized == f"{version}-color":
                return version
        return None
    if kind == "symbol":
        return normalized if normalized in _VERSIONS else None
    if kind == "fixture":
        return _asset_version(normalized)
    return None


def _gate_input_hashes(document: dict[str, Any]) -> dict[str, str]:
    """Extract version-scoped observed input hashes from a gate report.

    A production gate records the operator ROM, symbol, and fixture assets it
    actually opened. Those hashes describe one process, so they are exposed as
    document defaults keyed by version and consumed by every contained record.
    """
    hashes: dict[str, str] = {}
    assets = document.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            status = asset.get("status")
            if isinstance(status, str) and status.lower() not in ("ok", "pass", "passed"):
                continue
            sha1 = asset.get("actual_sha1", asset.get("expected_sha1"))
            label = asset.get("label")
            if not (isinstance(sha1, str) and sha1 and isinstance(label, str) and label):
                continue
            kind = asset.get("kind")
            version = _canonical_asset_version(kind, label)
            if version is None:
                continue
            if kind == "rom":
                hashes.setdefault(f"rom:{version}", sha1.lower())
            elif kind == "symbol":
                hashes.setdefault(f"sym:{version}", sha1.lower())
            elif kind == "fixture":
                hashes.setdefault(f"input_fixture:{version}", sha1.lower())
    manifest = document.get("fixture_manifest")
    if isinstance(manifest, dict):
        entries = manifest.get("fixtures", manifest.get("entries"))
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fixture_id = entry.get("id", entry.get("fixture_id"))
                sha1 = entry.get("sha1")
                if isinstance(fixture_id, str) and fixture_id and isinstance(sha1, str) and sha1:
                    hashes.setdefault(f"fixture:{fixture_id.lower()}", sha1.lower())
    return hashes


def _document_input_hashes(document: dict[str, Any]) -> dict[str, str]:
    """Merge document-level gate assets and any explicit input hash declaration."""
    merged = _gate_input_hashes(document)
    merged.update(_record_input_hashes(document))
    return merged


def _collect_outcome_records(
    records: list[Any],
    default_mode: str,
    result: ResultSet,
    *,
    block_run: str | None = None,
    block_commit: str | None = None,
    block_partial: bool = False,
    default_input_hashes: dict[str, str] | None = None,
    runtime_partial: dict[str, bool] | None = None,
) -> None:
    for record in records:
        if not isinstance(record, dict):
            continue
        nodeid = record.get("nodeid")
        if not isinstance(nodeid, str) or not nodeid:
            continue
        runtime = record.get("runtime")
        runtime = runtime if isinstance(runtime, str) and runtime else default_mode
        inherited_partial = block_partial or bool((runtime_partial or {}).get(runtime, False))
        result.outcomes.append(
            _normalise_outcome(
                record,
                nodeid=nodeid,
                status=str(record.get("status", "error")).lower(),
                runtime=runtime,
                inherited_partial=inherited_partial,
                inherited_commit=block_commit,
                inherited_run_id=block_run,
                inherited_hashes=dict(default_input_hashes or {}),
            )
        )


def _collect_outcomes(
    block: dict[str, Any],
    mode: str,
    result: ResultSet,
    *,
    block_run: str | None = None,
    block_commit: str | None = None,
    block_partial: bool = False,
    default_input_hashes: dict[str, str] | None = None,
) -> None:
    seen_nodeids: set[str] = set()
    inherited_hashes = dict(default_input_hashes or {})
    inherited_hashes.update(_record_input_hashes(block))
    records = block.get("records")
    if isinstance(records, list):
        _collect_outcome_records(
            records,
            mode,
            result,
            block_run=block_run,
            block_commit=block_commit,
            block_partial=block_partial,
            default_input_hashes=inherited_hashes,
        )
        seen_nodeids.update(
            normalize_nodeid(record["nodeid"])
            for record in records
            if isinstance(record, dict) and isinstance(record.get("nodeid"), str)
        )
    tests = block.get("tests")
    if isinstance(tests, list):
        for record in tests:
            if not isinstance(record, dict):
                continue
            nodeid = record.get("nodeid")
            if not isinstance(nodeid, str) or not nodeid:
                continue
            status = str(record.get("outcome", "error")).lower()
            if record.get("was_xfail") and status == "skipped":
                status = "xfailed"
            elif record.get("was_xfail") and status == "passed":
                status = "xpassed"
            result.outcomes.append(
                _normalise_outcome(
                    record,
                    nodeid=nodeid,
                    status=status,
                    runtime=mode,
                    inherited_partial=block_partial,
                    inherited_commit=block_commit,
                    inherited_run_id=block_run,
                    inherited_hashes=inherited_hashes,
                )
            )
            seen_nodeids.add(normalize_nodeid(nodeid))
    tiers = block.get("tiers")
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            tier_partial = _enclosing_partial(tier)
            if tier_partial:
                result.identity_conflicts.append(
                    "results source contains a partial or non-passing tier; "
                    "every contained case is treated as partial"
                )
            for case in tier.get("case_results", []):
                if not isinstance(case, dict):
                    continue
                nodeid = case.get("nodeid")
                if not isinstance(nodeid, str) or not nodeid:
                    continue
                result.outcomes.append(
                    _normalise_outcome(
                        case,
                        nodeid=nodeid,
                        status=_status_from_gate(case.get("status")),
                        runtime=mode,
                        inherited_partial=block_partial or tier_partial,
                        inherited_commit=block_commit,
                        inherited_run_id=block_run,
                        inherited_hashes=inherited_hashes,
                    )
                )
                seen_nodeids.add(normalize_nodeid(nodeid))
            for detail in tier.get("failure_details", []):
                if not isinstance(detail, dict):
                    continue
                nodeid = detail.get("nodeid")
                if not isinstance(nodeid, str) or not nodeid:
                    continue
                if normalize_nodeid(nodeid) in seen_nodeids:
                    continue
                outcome = str(detail.get("outcome", "failed")).lower()
                if outcome not in ("xfailed", "xpassed"):
                    outcome = outcome or "failed"
                result.outcomes.append(
                    _normalise_outcome(
                        detail,
                        nodeid=nodeid,
                        status=outcome,
                        runtime=mode,
                        inherited_partial=block_partial or tier_partial,
                        inherited_commit=block_commit,
                        inherited_run_id=block_run,
                        inherited_hashes=inherited_hashes,
                    )
                )


def _parse_junit(text: str, source_path: str) -> ResultSet:
    result = ResultSet(source_path=source_path, source_kind="junit-xml")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CoverageError(f"invalid JUnit XML: {exc}") from exc
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname") or ""
        name = testcase.get("name") or ""
        if classname:
            module = classname.replace(".", "/")
            if not module.endswith(".py"):
                module = f"{module}.py"
            nodeid = f"{module}::{name}"
        else:
            nodeid = name
        status = _DEFAULT_ACCEPTED_OUTCOMES[0]
        reason = ""
        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None:
            status = "failed"
            reason = failure.get("message") or ""
        elif error is not None:
            status = "error"
            reason = error.get("message") or ""
        elif skipped is not None:
            marker = f"{skipped.get('type', '')} {skipped.get('message', '')}".lower()
            status = "xfailed" if "xfail" in marker else "skipped"
            reason = skipped.get("message") or skipped.get("type") or ""
        system_out = testcase.findtext("system-out") or ""
        result.outcomes.append(
            _normalise_outcome(
                {"reason": reason, "output_tail": system_out},
                nodeid=nodeid,
                status=status,
                runtime="unknown",
                inherited_partial=False,
                inherited_commit=None,
                inherited_run_id=None,
                inherited_hashes={},
            )
        )
    result.notes.append("JUnit results carry no runtime/build identity")
    _finalize_identity(result)
    return result


def load_results(path: str | Path) -> ResultSet:
    """Load a gate report, JUnit XML, or normalised JSON results file."""
    target = Path(path)
    if not target.is_file():
        raise CoverageError(f"results source not found: {target}")
    text = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".xml" or text.lstrip().startswith("<"):
        return _parse_junit(text, str(target))
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CoverageError(f"results source is not valid JSON: {target}: {exc}") from exc
    return result_set_from_document(document, source_path=str(target))


def _evidence_policy(catalog: dict[str, Any]) -> dict[str, Any]:
    coverage = catalog.get("coverage")
    if not isinstance(coverage, dict):
        return {}
    policy = coverage.get("evidence_policy")
    return policy if isinstance(policy, dict) else {}


def _accepted_outcomes(policy: dict[str, Any]) -> tuple[str, ...]:
    """Return the catalog-declared terminal outcomes that may count as PASS.

    The catalog is authoritative. A malformed declaration falls back to the
    default accepted outcome so the report stays fail-closed rather than
    silently accepting an unknown status.
    """
    accepted = policy.get("accepted_outcomes")
    if (
        isinstance(accepted, list)
        and accepted
        and all(isinstance(item, str) and item.strip() for item in accepted)
    ):
        # Never allow the internal sentinel or a non-passing outcome to act as
        # a terminal pass, even if a malformed catalog declares it and bypasses
        # validation.
        filtered = tuple(
            item for item in accepted if item != _CASE_SENTINEL and item not in _INCOMPLETE_STATUSES
        )
        if filtered:
            return filtered
    return _DEFAULT_ACCEPTED_OUTCOMES


def _incomplete_label(status: str) -> str:
    """Map an unaccepted status to a safe incomplete report label."""
    if status in _INCOMPLETE_STATUSES and status != _CASE_SENTINEL:
        return status
    return "unaccepted"


def _runtime_collection_gate(
    results: ResultSet | None, runtime: str, selector: str
) -> tuple[bool, str]:
    """Return whether a runtime's collection evidence qualifies one selector.

    A production-gate collection record must be explicitly successful, the
    declared selector must appear in the collected node IDs, and a scope with
    no collection evidence at all cannot qualify. Any globally recorded
    collection error or failure fails every scope closed.
    """
    if results is None:
        return True, ""
    if results.collection_errors or results.collection_failures:
        return False, "collection evidence failed; this scope cannot qualify"
    evidence = [item for item in results.collections if item.runtime == runtime]
    if not evidence:
        return False, f"no collection evidence for runtime {runtime!r}"
    collected: set[str] = set()
    for item in evidence:
        if item.status.lower() not in _COLLECTION_PASS:
            return (
                False,
                f"collection {item.name!r} for runtime {runtime!r} did not pass ({item.status})",
            )
        collected.update(normalize_nodeid(nodeid) for nodeid in item.nodeids)
    if normalize_nodeid(selector) not in collected:
        return False, f"selector {selector!r} was not collected for runtime {runtime!r}"
    return True, ""


def _evaluate_case(
    runtime: str,
    records: list[Outcome],
    results: ResultSet | None,
    expected_commit: str | None,
    policy: dict[str, Any],
    selector: str = "",
) -> tuple[str, str]:
    accepted = _accepted_outcomes(policy)
    if not records:
        return "missing", f"no result for runtime {runtime}"
    if len(records) > 1:
        return "duplicate", f"{len(records)} results for runtime {runtime}"
    record = records[0]
    if record.partial:
        return "partial", f"partial {record.status} record"
    if record.status not in accepted:
        label = _incomplete_label(record.status)
        return label, record.reason or (
            f"result status {record.status!r} is not an accepted outcome {list(accepted)!r}"
        )
    if results is not None and results.merge_conflict:
        return (
            "mismatched",
            (
                "results source mixes partial or multiple run/commit identities; "
                "refusing to merge into a complete claim"
            ),
        )

    identity: dict[str, Any] = {}
    if results is not None:
        identity = results.runtimes.get(runtime, {})
        if runtime not in results.runtimes:
            return "unidentified", f"runtime/build identity unavailable for {runtime}"
    mode = identity.get("pyboy_mode") or identity.get("mode")
    if isinstance(mode, str) and mode and mode != runtime:
        return "mismatched", f"runtime mode {mode!r} does not match required {runtime!r}"
    expected_revision = policy.get("expected_pyboy_revision")
    actual_revision = identity.get("pyboy_revision")
    if expected_revision:
        if not actual_revision:
            return "unidentified", "PyBoy revision unavailable for the declared runtime"
        if actual_revision != expected_revision:
            return (
                "mismatched",
                f"PyBoy revision {actual_revision!r} does not match pinned {expected_revision!r}",
            )
    expected_version = policy.get("expected_pyboy_version")
    actual_version = identity.get("pyboy_version")
    if expected_version and actual_version and actual_version != expected_version:
        return (
            "mismatched",
            f"PyBoy version {actual_version!r} does not match pinned {expected_version!r}",
        )
    result_commit = record.commit or (results.commit if results else None)
    if not result_commit:
        return (
            "unidentified",
            (
                "result commit unavailable: the results source carries no per-record "
                "or top-level commit, so exact commit provenance cannot be confirmed"
            ),
        )
    if expected_commit and result_commit != expected_commit:
        return (
            "mismatched",
            f"result commit {result_commit!r} does not match declared {expected_commit!r}",
        )
    collected, collection_reason = _runtime_collection_gate(results, runtime, selector)
    if not collected:
        return "collection", collection_reason
    return _CASE_SENTINEL, ""


def _expanded_required_runtimes(catalog: dict[str, Any]) -> tuple[str, ...]:
    """Return the tool-owned runtimes every expanded_mechanics case must meet.

    The mandatory runtimes come from the coverage scope version rather than the
    catalog declaration, so a catalog edit that removes a runtime cannot make a
    family tested with partial evidence.
    """
    return _mandatory_runtimes(catalog)


def _verified_mechanics_case_ids(
    case_reports: list[dict[str, Any]],
    required_runtimes: tuple[str, ...] = (),
) -> dict[str, bool]:
    """Return whether each declared mechanics case has all terminal passes.

    A case is only verified when it carries a terminal ``tested`` record for
    every runtime the dimension requires.  A case whose declaration or
    evidence omits a required runtime can never mark its family ``tested``,
    even if the runtimes that are present all passed.
    """
    reports_by_case: dict[str, list[dict[str, Any]]] = {}
    for report in case_reports:
        if report.get("dimension_id") != EXPANDED_DIMENSION:
            continue
        case_id = report.get("case_id")
        if isinstance(case_id, str):
            reports_by_case.setdefault(case_id, []).append(report)
    required = set(required_runtimes)
    verified: dict[str, bool] = {}
    for case_id, reports in reports_by_case.items():
        present = {str(report.get("runtime")) for report in reports}
        if required and not required <= present:
            verified[case_id] = False
            continue
        verified[case_id] = bool(reports) and all(
            report["status"] == "tested" for report in reports
        )
    return verified


def _declared_case_effects(catalog: dict[str, Any]) -> dict[str, Any]:
    """Map each declared mechanics case_id to its declared effect_id."""
    try:
        cases = required_cases(catalog)
    except CoverageError:
        return {}
    return {
        case.get("case_id"): case.get("effect_id")
        for case in cases
        if isinstance(case.get("case_id"), str)
    }


def _family_is_verified(
    family: dict[str, Any],
    verified_cases: dict[str, bool],
    declared_effects: dict[str, Any] | None = None,
) -> bool:
    evidence = family.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    family_effect = family.get("effect_id")
    for case_id in evidence:
        if not verified_cases.get(case_id, False):
            return False
        if declared_effects is not None and declared_effects.get(case_id) != family_effect:
            return False
    return True


def _dimension_status(
    dimension_id: str,
    case_reports: list[dict[str, Any]],
    catalog: dict[str, Any],
    *,
    catalog_valid: bool = True,
) -> dict[str, Any]:
    if dimension_id == EXPANDED_DIMENSION:
        families = []
        try:
            families = effect_families(catalog)
        except CoverageError:
            families = []
        verified_cases = _verified_mechanics_case_ids(
            case_reports, _expanded_required_runtimes(catalog)
        )
        declared_effects = _declared_case_effects(catalog)
        # A catalog-invalid or duplicate/truncated family declaration can never
        # earn tested credit: the tool pins the complete effect inventory.
        if not catalog_valid:
            verified_cases = {}
        tested = 0
        planned = 0
        excluded = 0
        seen_effects: set[Any] = set()
        for family in families:
            effect_id = family.get("effect_id")
            if effect_id in seen_effects:
                continue
            seen_effects.add(effect_id)
            scope = family.get("scope")
            if scope == "deliberately_excluded":
                excluded += 1
            elif scope == "tested" and _family_is_verified(
                family, verified_cases, declared_effects
            ):
                tested += 1
            else:
                planned += 1
        if planned and tested:
            status = "PARTIAL"
        elif planned:
            status = "PLANNED_UNVERIFIED"
        elif tested:
            status = "COMPLETE"
        else:
            status = "DELIBERATELY_EXCLUDED"
        if not catalog_valid:
            status = "INCOMPLETE"
            tested = 0
        return {
            "status": status,
            "families": len(families),
            "tested": tested,
            "planned_unverified": planned,
            "deliberately_excluded": excluded,
        }
    relevant = [report for report in case_reports if report["dimension_id"] == dimension_id]
    incomplete = [report for report in relevant if report["status"] != "tested"]
    if not relevant:
        return {"status": "MISSING", "checks": 0, "complete": 0}
    return {
        "status": "INCOMPLETE" if incomplete else "COMPLETE",
        "checks": len(relevant),
        "complete": len(relevant) - len(incomplete),
    }


def _endpoint_hash_requirements(
    catalog: dict[str, Any], case: dict[str, Any]
) -> list[tuple[str, str, str, tuple[str, ...], str]]:
    """Return ``(label, role, kind, accepted_values, version)`` for each endpoint."""
    requirements: list[tuple[str, str, str, tuple[str, ...], str]] = []
    for role, scenario in _case_endpoint_scenarios(catalog, case).items():
        game = scenario.get("game") if isinstance(scenario.get("game"), dict) else {}
        fixture = scenario.get("fixture") if isinstance(scenario.get("fixture"), dict) else {}
        provenance = (
            scenario.get("provenance") if isinstance(scenario.get("provenance"), dict) else {}
        )
        version = game.get("version")
        if not isinstance(version, str):
            continue
        if isinstance(game.get("rom_sha1"), str) and game["rom_sha1"]:
            requirements.append(
                (f"{role} ROM ({version})", role, "rom", (game["rom_sha1"].lower(),), version)
            )
        if isinstance(game.get("sym_sha1"), str) and game["sym_sha1"]:
            requirements.append(
                (f"{role} symbol ({version})", role, "sym", (game["sym_sha1"].lower(),), version)
            )
        fixture_values: list[str] = []
        if isinstance(fixture.get("sha1"), str) and fixture["sha1"]:
            fixture_values.append(fixture["sha1"].lower())
        if (
            isinstance(provenance.get("input_fixture_sha1"), str)
            and provenance["input_fixture_sha1"]
        ):
            fixture_values.append(provenance["input_fixture_sha1"].lower())
        if fixture_values:
            requirements.append(
                (
                    f"{role} fixture ({version})",
                    role,
                    "fixture",
                    tuple(dict.fromkeys(fixture_values)),
                    version,
                )
            )
    return requirements


def _observed_endpoint_hash(
    observed: dict[str, str], role: str, kind: str, version: str
) -> tuple[str | None, str | None]:
    keys = [
        f"{role}_{kind}",
        f"{role}_{kind}_sha1",
        f"{version}_{kind}",
        f"{version}_{kind}_sha1",
        f"{kind}:{version}",
    ]
    if kind == "fixture":
        keys.extend((f"{role}_input_fixture", f"input_fixture:{version}"))
    if role == "listen":
        keys.extend((kind, f"{kind}_sha1", "input_fixture"))
    for key in keys:
        value = observed.get(key)
        if isinstance(value, str) and value:
            return key, value.lower()
    return None, None


def _check_case_inputs(
    catalog: dict[str, Any], case: dict[str, Any], observed: dict[str, str]
) -> tuple[str, str] | None:
    """Fail closed unless every declared endpoint ROM/symbol/fixture hash matches."""
    requirements = _endpoint_hash_requirements(catalog, case)
    if not requirements:
        return None
    missing: list[str] = []
    mismatched: list[str] = []
    for label, role, kind, accepted, version in requirements:
        _, value = _observed_endpoint_hash(observed, role, kind, version)
        if value is None:
            missing.append(label)
        elif value not in accepted:
            mismatched.append(f"{label}: observed {value!r} not in {sorted(accepted)}")
    if mismatched:
        return "mismatched", "observed input hash mismatch for " + "; ".join(mismatched)
    if missing:
        return "unidentified", "missing observed input hashes for " + ", ".join(missing)
    return None


def _check_case_effect(case: dict[str, Any], record: Outcome) -> tuple[str, str] | None:
    """Fail closed unless the observed move effect matches the declared effect."""
    declared = case.get("effect_id")
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None
    effects = record.effects
    if not effects:
        return (
            "unidentified",
            f"no observed move effect for declared effect {declared}",
        )
    if any(effect != declared for effect in effects):
        return (
            "mismatched",
            f"observed move effects {sorted(set(effects))} do not match declared effect {declared}",
        )
    return None


def build_report(
    catalog: dict[str, Any],
    results: ResultSet | None = None,
    *,
    expected_commit: str | None = None,
    catalog_path: str | None = None,
) -> dict[str, Any]:
    """Build a coverage report; never merge partial runs into a clean gate."""
    document = deepcopy(catalog)
    problems: list[str] = []
    catalog_valid = True
    try:
        validate_catalog(document)
    except CoverageError as exc:
        problems.append(f"catalog: {exc}")
        catalog_valid = False

    known_dimensions: dict[str, dict[str, Any]] = {}
    try:
        known_dimensions = dimensions(document)
    except CoverageError:
        known_dimensions = {}

    for required_dimension in _REQUIRED_DIMENSIONS:
        if required_dimension not in known_dimensions:
            problems.append(f"missing coverage dimension: {required_dimension}")

    cases: list[dict[str, Any]] = []
    try:
        cases = required_cases(document)
    except CoverageError:
        cases = []

    fixtures = _scenario_fixture_ids(document)
    scenarios_by_fixture = _scenarios_by_fixture_id(document)
    battle_by_version = _canonical_battle_scenarios(document)
    for case in cases:
        fixture_id = case.get("fixture_id")
        if fixture_id not in fixtures:
            problems.append(
                f"case {case.get('case_id')!r} references unknown fixture {fixture_id!r}"
            )

    case_catalog_errors: dict[str, str] = {}
    catalog_error_dimensions: set[str] = set()
    for index, case in enumerate(cases):
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in case_catalog_errors:
            continue
        error = _case_declaration_error(
            f"coverage.required_cases[{index}]",
            case,
            known_dimensions,
            fixtures,
            scenarios_by_fixture,
            battle_by_version,
        )
        if error is not None:
            case_catalog_errors[case_id] = error
            dimension_id = case.get("dimension_id")
            if isinstance(dimension_id, str):
                catalog_error_dimensions.add(dimension_id)

    policy = _evidence_policy(document)
    declared_pairing = {
        normalize_nodeid(case.get("selector", ""))
        for case in cases
        if case.get("dimension_id") == COVERAGE_DIMENSION
    }
    expected_pairing = _authoritative_selectors()
    for selector in sorted(expected_pairing - declared_pairing):
        problems.append(f"missing required one_turn_pairing case: {selector}")
    for selector in sorted(declared_pairing - expected_pairing):
        problems.append(f"undeclared one_turn_pairing case: {selector}")

    incomplete_dimensions: set[str] = set()
    for case in cases:
        dimension_id = case.get("dimension_id")
        dimension = known_dimensions.get(dimension_id, {})
        case_runtimes = case.get("runtimes")
        case_runtimes = case_runtimes if isinstance(case_runtimes, list) else []
        case_roles = case.get("roles")
        case_roles = case_roles if isinstance(case_roles, list) else []
        for runtime in _mandatory_runtimes(document):
            if runtime not in case_runtimes:
                problems.append(
                    f"case {case.get('case_id')!r} is missing required runtime {runtime!r}"
                )
                if isinstance(dimension_id, str):
                    incomplete_dimensions.add(dimension_id)
        for role in dimension.get("required_roles", []):
            if role not in case_roles:
                problems.append(f"case {case.get('case_id')!r} is missing required role {role!r}")
                if isinstance(dimension_id, str):
                    incomplete_dimensions.add(dimension_id)

    if results is not None:
        for entry in (*results.collection_errors, *results.collection_failures):
            problems.append(f"collection failure: {entry}")
        if results.partial:
            problems.append(
                "results source is marked partial; refusing to qualify any scope as complete"
            )
        problems.extend(results.identity_conflicts)
        if expected_commit and results.commit and results.commit != expected_commit:
            problems.append(
                f"commit mismatch: declared {expected_commit!r}, result {results.commit!r}"
            )
        if results.missing_commit:
            problems.append(
                "results source carries no document, block, or record commit; exact commit "
                "provenance is required regardless of --commit (fail-closed)"
            )
        if (
            expected_commit
            and not results.commit
            and not any(outcome.commit for outcome in results.outcomes)
        ):
            problems.append(
                f"results source carries no commit; cannot confirm declared commit "
                f"{expected_commit!r} (fail-closed); retain a result with an exact commit"
            )
        distinct_commits = {
            identity.get("pyboy_revision")
            for identity in results.runtimes.values()
            if identity.get("pyboy_revision")
        }
        if len(distinct_commits) > 1:
            problems.append("results source mixes runtime builds; refusing to merge partial runs")

    case_reports: list[dict[str, Any]] = []
    summary: Counter[str] = Counter()
    for case in cases:
        case_id = case.get("case_id")
        selectors = case.get("selector")
        if not isinstance(selectors, str):
            continue
        scenario = scenarios_by_fixture.get(case.get("fixture_id"))
        declared_hashes = _declared_input_hashes(scenario)
        declared_sha1 = declared_hashes.get("fixture")
        catalog_error = case_catalog_errors.get(case_id) if isinstance(case_id, str) else None
        for runtime in case.get("runtimes", []):
            if not isinstance(runtime, str):
                continue
            records = results.lookup(runtime, selectors) if results is not None else []
            status, reason = _evaluate_case(
                runtime, records, results, expected_commit, policy, selectors
            )
            observed_hashes = dict(records[0].input_hashes) if len(records) == 1 else {}
            if catalog_error is not None:
                status, reason = _CATALOG_STATUS, catalog_error
            elif status == _CASE_SENTINEL:
                input_result = _check_case_inputs(document, case, observed_hashes)
                if input_result is not None:
                    status, reason = input_result
                    problems.append(f"case {case.get('case_id')!r} ({runtime}): {reason}")
            if status == _CASE_SENTINEL and len(records) == 1:
                effect_result = _check_case_effect(case, records[0])
                if effect_result is not None:
                    status, reason = effect_result
                    problems.append(f"case {case.get('case_id')!r} ({runtime}): {reason}")
            summary[status] += 1
            case_reports.append(
                {
                    "case_id": case.get("case_id"),
                    "dimension_id": case.get("dimension_id"),
                    "selector": selectors,
                    "runtime": runtime,
                    "transport": case.get("transport"),
                    "game_versions": case.get("game_versions"),
                    "roles": case.get("roles"),
                    "fixture_id": case.get("fixture_id"),
                    "declared_fixture_sha1": declared_sha1,
                    "observed_input_hashes": observed_hashes,
                    "status": status,
                    "tested": status == "tested",
                    "reason": reason,
                }
            )

    dimension_reports = {
        dimension_id: _dimension_status(
            dimension_id, case_reports, document, catalog_valid=catalog_valid
        )
        for dimension_id in known_dimensions
    }
    if not catalog_valid or incomplete_dimensions or catalog_error_dimensions:
        for dimension_id, dimension_report in dimension_reports.items():
            if dimension_id in catalog_error_dimensions or (
                dimension_report.get("status") == "COMPLETE"
                and (not catalog_valid or dimension_id in incomplete_dimensions)
            ):
                dimension_report["status"] = "INCOMPLETE"
    if results is None:
        problems.append("no results supplied; declaration-only report")

    overall = "COMPLETE"
    if problems or not catalog_valid:
        overall = "INCOMPLETE"
    if results is None or any(report["status"] in _INCOMPLETE_STATUSES for report in case_reports):
        overall = "INCOMPLETE"
    pairing = dimension_reports.get(COVERAGE_DIMENSION, {}).get("status")
    mechanics = dimension_reports.get(EXPANDED_DIMENSION, {}).get("status")
    if pairing != "COMPLETE" or mechanics not in ("COMPLETE",):
        overall = "INCOMPLETE"

    record_commit_available = results is not None and any(
        outcome.commit for outcome in results.outcomes
    )
    if results is not None and results.commit:
        commit_source = "result"
    elif record_commit_available:
        commit_source = "records"
    else:
        commit_source = None
    run: dict[str, Any] = {
        "expected_commit": expected_commit,
        "result_commit": results.commit if results is not None else None,
        "commit_source": commit_source,
        "source_path": results.source_path if results is not None else None,
        "source_kind": results.source_kind if results is not None else "none",
        "run_id": results.run_id if results is not None else None,
        "runtimes": results.runtimes if results is not None else {},
    }
    if results is not None and results.notes:
        run["notes"] = list(results.notes)
    if results is not None:
        run["partial"] = results.partial
        run["collections"] = [
            {
                "runtime": item.runtime,
                "name": item.name,
                "status": item.status,
                "collected": len(item.nodeids),
            }
            for item in results.collections
        ]
    if (expected_commit or results is not None) and commit_source is None and results is not None:
        run["commit_status"] = "unidentified: results source carries no commit"

    return {
        "catalog_id": document.get("catalog_id"),
        "catalog_version": document.get("catalog_version"),
        "coverage_version": (
            document.get("coverage", {}).get("coverage_version")
            if isinstance(document.get("coverage"), dict)
            else None
        ),
        "effects_version": (
            document.get("move_effects", {}).get("effects_version")
            if isinstance(document.get("move_effects"), dict)
            else None
        ),
        "catalog_path": catalog_path,
        "run": run,
        "dimensions": dimension_reports,
        "summary": {
            "checks": len(case_reports),
            "tested": summary.get("tested", 0),
            "incomplete": sum(
                count for status, count in summary.items() if status in _INCOMPLETE_STATUSES
            ),
            "statuses": dict(sorted(summary.items())),
        },
        "cases": case_reports,
        "move_effects": _move_effects_summary(document, case_reports, catalog_valid=catalog_valid),
        "problems": problems,
        "overall": overall,
    }


def _move_effects_summary(
    catalog: dict[str, Any],
    case_reports: list[dict[str, Any]] | None = None,
    *,
    catalog_valid: bool = True,
) -> dict[str, Any]:
    try:
        families = effect_families(catalog)
    except CoverageError:
        return {"families": [], "current_selector": {}}
    move_effects = catalog.get("move_effects", {})
    verified_cases = _verified_mechanics_case_ids(
        case_reports or [], _expanded_required_runtimes(catalog)
    )
    declared_effects = _declared_case_effects(catalog)
    if not catalog_valid:
        verified_cases = {}
    verified_tested = sum(
        1
        for family in families
        if family.get("scope") == "tested"
        and _family_is_verified(family, verified_cases, declared_effects)
    )
    return {
        "families": families,
        "planned_unverified": sum(
            1 for family in families if family.get("scope") == "planned_unverified"
        ),
        "deliberately_excluded": sum(
            1 for family in families if family.get("scope") == "deliberately_excluded"
        ),
        "tested": verified_tested,
        "declared_tested": sum(1 for family in families if family.get("scope") == "tested"),
        "current_selector": move_effects.get("current_selector", {})
        if isinstance(move_effects, dict)
        else {},
    }


def render_text(report: dict[str, Any]) -> str:
    """Render a readable report that keeps the two dimensions distinct."""
    lines = [
        "Battle-scenario functional coverage report",
        (
            f"catalog: {report.get('catalog_id')} v{report.get('catalog_version')} "
            f"(coverage v{report.get('coverage_version')}, effects "
            f"v{report.get('effects_version')})"
        ),
    ]
    run = report.get("run", {})
    lines.append(
        "run: "
        f"kind={run.get('source_kind')} path={run.get('source_path') or '(none)'} "
        f"run_id={run.get('run_id') or '(none)'} "
        f"commit={run.get('result_commit') or run.get('expected_commit') or '(unknown)'} "
        f"commit_source={run.get('commit_source') or '(none)'}"
    )
    if run.get("commit_status"):
        lines.append(f"  commit_status: {run['commit_status']}")
    for mode, identity in sorted((run.get("runtimes") or {}).items()):
        lines.append(
            f"  runtime {mode}: build={identity.get('pyboy_revision') or '(unknown)'} "
            f"python={identity.get('python_version') or '(unknown)'}"
        )
    lines.append("dimensions (separate scopes):")
    pairing = report["dimensions"].get(COVERAGE_DIMENSION, {})
    lines.append(
        f"  one_turn_pairing: {pairing.get('status', 'MISSING')} "
        f"[one-turn settled battle pairing; checks={pairing.get('checks', 0)} "
        f"complete={pairing.get('complete', 0)}]"
    )
    mechanics = report["dimensions"].get(EXPANDED_DIMENSION, {})
    lines.append(
        f"  expanded_mechanics: {mechanics.get('status', 'MISSING')} "
        "[expanded mechanics / move effects; "
        f"families={mechanics.get('families', 0)} "
        f"tested={mechanics.get('tested', 0)} "
        f"planned_unverified={mechanics.get('planned_unverified', 0)} "
        f"deliberately_excluded={mechanics.get('deliberately_excluded', 0)}]"
    )
    summary = report.get("summary", {})
    lines.append(
        f"required checks: {summary.get('checks', 0)} "
        f"tested={summary.get('tested', 0)} incomplete={summary.get('incomplete', 0)}"
    )
    selector = report.get("move_effects", {}).get("current_selector", {})
    admitted = selector.get("admitted_effect_ids", [])
    excluded = [entry.get("move_id") for entry in selector.get("excluded_moves", [])]
    lines.append(f"current selector: admits effects {admitted}; excludes moves {excluded}")
    if report.get("problems"):
        lines.append("problems:")
        lines.extend(f"  - {problem}" for problem in report["problems"])
    lines.append("case results:")
    for case in report.get("cases", []):
        status = case["status"].upper()
        lines.append(
            f"  {status:12} runtime={case['runtime']} selector={case['selector']}"
            + (f" reason={case['reason']}" if case.get("reason") else "")
        )
    lines.append(f"overall: {report.get('overall')}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="gate report JSON, JUnit XML, or normalised JSON results path",
    )
    parser.add_argument(
        "--no-results",
        action="store_true",
        help="explicit declaration-only mode; no execution evidence is claimed",
    )
    parser.add_argument(
        "--commit",
        default=None,
        help="declared harness commit that a supplied result must match",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.results is not None and args.no_results:
        print("--results and --no-results are mutually exclusive", file=sys.stderr)
        return 2
    try:
        catalog = load_catalog(args.catalog)
        results = None
        if not args.no_results:
            if args.results is None:
                print("provide --results or --no-results", file=sys.stderr)
                return 2
            results = load_results(args.results)
    except (OSError, CoverageError) as exc:
        print(f"coverage report failed: {exc}", file=sys.stderr)
        return 2

    report = build_report(
        catalog,
        results,
        expected_commit=args.commit,
        catalog_path=str(args.catalog),
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report["overall"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
