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

import sys
from pathlib import Path

# Support loading this facade directly as a script or by file path, where the
# repository root is not already importable as the ``scripts`` namespace.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._coverage_report_build import (
    _accepted_outcomes,
    _check_case_effect,
    _check_case_inputs,
    _declared_case_effects,
    _dimension_status,
    _endpoint_hash_requirements,
    _evaluate_case,
    _evidence_policy,
    _expanded_required_runtimes,
    _family_is_verified,
    _incomplete_label,
    _move_effects_summary,
    _observed_endpoint_hash,
    _parse_args,
    _runtime_collection_gate,
    _verified_mechanics_case_ids,
    build_report,
    main,
    render_text,
)
from scripts._coverage_report_catalog import (
    _authoritative_selectors,
    _canonical_battle_scenarios,
    _case_declaration_error,
    _case_endpoint_scenarios,
    _coverage,
    _declared_fixture_sha1,
    _declared_input_hashes,
    _mandatory_runtimes,
    _profile_version,
    _scenario_fixture_ids,
    _scenarios_by_fixture_id,
    _selector_endpoint_versions,
    _validate_case_fixture,
    _validate_move_effects,
    _validate_tested_family_evidence,
    dimensions,
    effect_families,
    load_catalog,
    one_turn_pairing_cases,
    required_cases,
    validate_catalog,
)
from scripts._coverage_report_model import (
    CollectionEvidence,
    Outcome,
    ResultSet,
    _asset_version,
    _canonical_asset_version,
    _collect_collection_errors,
    _collect_collections,
    _collect_outcome_records,
    _collect_outcomes,
    _document_input_hashes,
    _effects_from_text,
    _enclosing_partial,
    _finalize_identity,
    _gate_input_hashes,
    _mode_for,
    _normalise_outcome,
    _parse_junit,
    _record_block_conflicts,
    _record_effects,
    _record_input_hashes,
    _runtime_identity,
    _status_from_gate,
    load_results,
    result_set_from_document,
)
from scripts._coverage_report_schema import (
    _CASE_SENTINEL,
    _CATALOG_STATUS,
    _COLLECTION_PASS,
    _DEFAULT_ACCEPTED_OUTCOMES,
    _EFFECT_TEXT_KEYS,
    _ENCLOSING_PASS,
    _GATE_STATUS,
    _INCOMPLETE_STATUSES,
    _LINK_BATTLE_TYPE,
    _LINK_BATTLE_VARIANTS,
    _MANDATORY_RUNTIMES_BY_COVERAGE_VERSION,
    _MATRIX,
    _MOVE_EFFECT_RE,
    _PINNED_EFFECT_IDS,
    _PINNED_MOVE_EFFECTS,
    _PINNED_UNUSED_EFFECT_IDS,
    _REPO,
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

__all__ = [
    "CATALOG_PATH",
    "COVERAGE_DIMENSION",
    "EXPANDED_DIMENSION",
    "_CASE_SENTINEL",
    "_CATALOG_STATUS",
    "_COLLECTION_PASS",
    "_DEFAULT_ACCEPTED_OUTCOMES",
    "_EFFECT_TEXT_KEYS",
    "_ENCLOSING_PASS",
    "_GATE_STATUS",
    "_INCOMPLETE_STATUSES",
    "_LINK_BATTLE_TYPE",
    "_LINK_BATTLE_VARIANTS",
    "_MANDATORY_RUNTIMES_BY_COVERAGE_VERSION",
    "_MATRIX",
    "_MOVE_EFFECT_RE",
    "_PINNED_EFFECT_IDS",
    "_PINNED_MOVE_EFFECTS",
    "_PINNED_UNUSED_EFFECT_IDS",
    "_REPO",
    "_REQUIRED_DIMENSIONS",
    "_ROLES",
    "_RUNTIMES",
    "_SCOPES",
    "_TRANSPORTS",
    "_VERSIONS",
    "CollectionEvidence",
    "CoverageError",
    "Outcome",
    "ResultSet",
    "_accepted_outcomes",
    "_asset_version",
    "_authoritative_selectors",
    "_canonical_asset_version",
    "_canonical_battle_scenarios",
    "_case_declaration_error",
    "_case_endpoint_scenarios",
    "_check_case_effect",
    "_check_case_inputs",
    "_collect_collection_errors",
    "_collect_collections",
    "_collect_outcome_records",
    "_collect_outcomes",
    "_coverage",
    "_declared_case_effects",
    "_declared_fixture_sha1",
    "_declared_input_hashes",
    "_dimension_status",
    "_document_input_hashes",
    "_effects_from_text",
    "_enclosing_partial",
    "_endpoint_hash_requirements",
    "_evaluate_case",
    "_evidence_policy",
    "_expanded_required_runtimes",
    "_family_is_verified",
    "_finalize_identity",
    "_gate_input_hashes",
    "_incomplete_label",
    "_mandatory_runtimes",
    "_mode_for",
    "_move_effects_summary",
    "_normalise_outcome",
    "_observed_endpoint_hash",
    "_parse_args",
    "_parse_junit",
    "_profile_version",
    "_record_block_conflicts",
    "_record_effects",
    "_record_input_hashes",
    "_runtime_collection_gate",
    "_runtime_identity",
    "_scenario_fixture_ids",
    "_scenarios_by_fixture_id",
    "_selector_endpoint_versions",
    "_status_from_gate",
    "_validate_case_fixture",
    "_validate_move_effects",
    "_validate_tested_family_evidence",
    "_verified_mechanics_case_ids",
    "build_report",
    "dimensions",
    "effect_families",
    "load_catalog",
    "load_results",
    "main",
    "normalize_nodeid",
    "one_turn_pairing_cases",
    "render_text",
    "required_cases",
    "result_set_from_document",
    "validate_catalog",
]


if __name__ == "__main__":
    raise SystemExit(main())
