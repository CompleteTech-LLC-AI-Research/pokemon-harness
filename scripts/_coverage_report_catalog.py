"""Catalog loading, dimension queries, and declaration validation (#130).

Split from ``scripts/coverage_report.py`` with no behavior change."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts._coverage_report_schema import (
    _CASE_SENTINEL,
    _INCOMPLETE_STATUSES,
    _LINK_BATTLE_TYPE,
    _LINK_BATTLE_VARIANTS,
    _MANDATORY_RUNTIMES_BY_COVERAGE_VERSION,
    _MATRIX,
    _PINNED_EFFECT_IDS,
    _PINNED_MOVE_EFFECTS,
    _PINNED_UNUSED_EFFECT_IDS,
    _REQUIRED_DIMENSIONS,
    _ROLES,
    _RUNTIMES,
    _SCOPES,
    _TRANSPORTS,
    _VERSIONS,
    CATALOG_PATH,
    COVERAGE_DIMENSION,
    EXPANDED_DIMENSION,
    CoverageError,
    normalize_nodeid,
)


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
