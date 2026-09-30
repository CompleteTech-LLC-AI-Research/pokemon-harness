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
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts._coverage_report_catalog import (
    _authoritative_selectors,
    _canonical_battle_scenarios,
    _case_declaration_error,
    _case_endpoint_scenarios,
    _declared_input_hashes,
    _mandatory_runtimes,
    _scenario_fixture_ids,
    _scenarios_by_fixture_id,
    dimensions,
    effect_families,
    load_catalog,
    required_cases,
    validate_catalog,
)
from scripts._coverage_report_model import (
    Outcome,
    ResultSet,
    load_results,
)
from scripts._coverage_report_schema import (
    _CASE_SENTINEL,
    _CATALOG_STATUS,
    _COLLECTION_PASS,
    _DEFAULT_ACCEPTED_OUTCOMES,
    _INCOMPLETE_STATUSES,
    _REQUIRED_DIMENSIONS,
    CATALOG_PATH,
    COVERAGE_DIMENSION,
    EXPANDED_DIMENSION,
    CoverageError,
    normalize_nodeid,
)


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
