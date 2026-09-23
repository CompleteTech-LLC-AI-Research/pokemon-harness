"""Expanded-mechanics family dimensions and effect-linkage rules (#143).

Split from ``tests/test_battle_coverage.py`` for #143 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Synthetic report documents only; never instantiates PyBoy
or loads a ROM/state.
"""

import copy

import pytest

from scripts import coverage_report as coverage
from tests._battle_coverage_support import (
    _case_input_hashes,
    _dimension,
    _mechanics_case,
    _mechanics_document,
    _mechanics_status,
    _passing_document,
)


def test_expanded_mechanics_family_becomes_tested_only_with_dual_runtime_evidence(
    catalog: dict,
) -> None:
    results = coverage.result_set_from_document(_mechanics_document(catalog))
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "tested"
    assert _mechanics_status(catalog, report, "cython")["status"] == "tested"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 1
    assert report["dimensions"]["expanded_mechanics"]["planned_unverified"] == 67
    tested_families = [
        family for family in report["move_effects"]["families"] if family["effect_id"] == 0
    ]
    verified = coverage._verified_mechanics_case_ids(report["cases"])
    assert coverage._family_is_verified(tested_families[0], verified)


def test_mechanics_family_stays_unverified_without_both_runtimes(catalog: dict) -> None:
    results = coverage.result_set_from_document(_mechanics_document(catalog, runtimes=("source",)))
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "tested"
    assert _mechanics_status(catalog, report, "cython")["status"] == "missing"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0


def test_mechanics_family_stays_unverified_when_a_required_runtime_is_undeclared(
    catalog: dict,
) -> None:
    mutated = copy.deepcopy(catalog)
    case = next(
        item
        for item in mutated["coverage"]["required_cases"]
        if item["case_id"] == "mechanics_effect_0_no_additional_effect"
    )
    case["runtimes"] = [runtime for runtime in case["runtimes"] if runtime != "cython"]
    assert "cython" not in case["runtimes"]

    # Only source evidence is supplied, matching the now-incomplete declaration;
    # the dimension still requires both runtimes, so the family must not be tested.
    results = coverage.result_set_from_document(_mechanics_document(mutated, runtimes=("source",)))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert _mechanics_status(mutated, report, "source")["status"] == "tested"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0


def test_invalid_family_link_does_not_receive_tested_credit(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    family = next(item for item in mutated["move_effects"]["families"] if item["effect_id"] == 6)
    family["scope"] = "tested"
    family["evidence"] = ["mechanics_effect_0_no_additional_effect"]
    family["reason"] = "probe: effect 6 pointing at the effect-0 case"
    mutated["move_effects"]["planned_unverified_count"] = (
        mutated["move_effects"]["planned_unverified_count"] - 1
    )

    results = coverage.result_set_from_document(_mechanics_document(mutated))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    # The effect-6 family references a case declared for effect 0, so the catalog
    # is invalid and no family can receive tested credit.
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0
    assert report["move_effects"]["tested"] == 0


def test_truncated_effect_inventory_cannot_claim_completion(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    keep = next(item for item in mutated["move_effects"]["families"] if item["effect_id"] == 0)
    mutated["move_effects"]["families"] = [keep]
    mutated["move_effects"]["family_count"] = 1
    mutated["move_effects"]["planned_unverified_count"] = 1
    mutated["move_effects"]["deliberately_excluded_count"] = 0

    results = coverage.result_set_from_document(_mechanics_document(mutated))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0
    assert report["dimensions"]["expanded_mechanics"]["status"] != "COMPLETE"


def test_duplicate_effect_family_receives_no_tested_credit(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    duplicate = copy.deepcopy(
        next(item for item in mutated["move_effects"]["families"] if item["effect_id"] == 0)
    )
    mutated["move_effects"]["families"].append(duplicate)
    mutated["move_effects"]["family_count"] = len(mutated["move_effects"]["families"])

    results = coverage.result_set_from_document(_mechanics_document(mutated))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0
    assert report["move_effects"]["tested"] == 0


def test_move_reassignment_is_rejected(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    families = mutated["move_effects"]["families"]
    donor = next(item for item in families if item["effect_id"] == 0)
    recipient = next(item for item in families if item["effect_id"] == 29)
    recipient["move_ids"].append(donor["move_ids"].pop(0))

    with pytest.raises(coverage.CoverageError, match="move-to-effect mapping"):
        coverage.validate_catalog(mutated)


def test_move_deletion_is_rejected(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    donor = next(item for item in mutated["move_effects"]["families"] if item["effect_id"] == 0)
    donor["move_ids"].pop(0)

    with pytest.raises(coverage.CoverageError, match="move-to-effect mapping"):
        coverage.validate_catalog(mutated)


def test_unused_effect_slot_cannot_declare_a_move(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    families = mutated["move_effects"]["families"]
    donor = next(item for item in families if item["effect_id"] == 0)
    unused = next(item for item in families if item["effect_id"] == 1)
    unused["move_ids"].append(donor["move_ids"].pop(0))

    with pytest.raises(coverage.CoverageError):
        coverage.validate_catalog(mutated)


def test_mechanics_family_stays_unverified_with_wrong_effect(catalog: dict) -> None:
    results = coverage.result_set_from_document(_mechanics_document(catalog, effect=6))
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "mismatched"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0
    assert any("declared effect" in problem for problem in report["problems"])


def test_mechanics_family_stays_unverified_with_missing_effect(catalog: dict) -> None:
    results = coverage.result_set_from_document(_mechanics_document(catalog, effect=None))
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "unidentified"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0


@pytest.mark.parametrize("status", ["skipped", "failed", "timed_out", "not_run"])
def test_mechanics_family_stays_unverified_with_non_terminal_evidence(
    catalog: dict, status: str
) -> None:
    document = _mechanics_document(catalog, status=status)
    results = coverage.result_set_from_document(document)
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] != "tested"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0


def test_mechanics_family_stays_unverified_with_mismatched_hashes(catalog: dict) -> None:
    case = _mechanics_case(catalog)
    hashes = _case_input_hashes(catalog, case)
    hashes["listen_rom_sha1"] = "0" * 40
    results = coverage.result_set_from_document(_mechanics_document(catalog, hashes=hashes))
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "mismatched"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0


def test_mechanics_family_stays_unverified_with_missing_hashes(catalog: dict) -> None:
    results = coverage.result_set_from_document(_mechanics_document(catalog, hashes={}))
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "unidentified"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0


def test_mandatory_runtime_cannot_be_removed_from_mechanics_declaration(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    _mechanics_case(mutated)["runtimes"] = ["source"]
    _dimension(mutated, coverage.EXPANDED_DIMENSION)["required_runtimes"] = ["source"]

    with pytest.raises(coverage.CoverageError, match="mandatory runtime"):
        coverage.validate_catalog(mutated)

    results = coverage.result_set_from_document(_mechanics_document(mutated, runtimes=("source",)))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0
    assert report["dimensions"]["expanded_mechanics"]["status"] != "COMPLETE"
    assert report["overall"] == "INCOMPLETE"


def test_dimension_must_declare_every_mandatory_runtime(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    _dimension(mutated, coverage.EXPANDED_DIMENSION)["required_runtimes"] = ["source"]

    with pytest.raises(coverage.CoverageError, match="mandatory runtime"):
        coverage.validate_catalog(mutated)


def test_mandatory_runtime_cannot_be_removed_from_pairing_declaration(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    for case in mutated["coverage"]["required_cases"]:
        if case["dimension_id"] == coverage.COVERAGE_DIMENSION:
            case["runtimes"] = ["source"]
    _dimension(mutated, coverage.COVERAGE_DIMENSION)["required_runtimes"] = ["source"]

    with pytest.raises(coverage.CoverageError, match="mandatory runtime"):
        coverage.validate_catalog(mutated)

    results = coverage.result_set_from_document(_passing_document(catalog, runtimes=("source",)))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert report["dimensions"]["one_turn_pairing"]["status"] != "COMPLETE"
    assert report["overall"] == "INCOMPLETE"


def test_catalog_invalid_fixture_makes_mechanics_case_untested(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    _mechanics_case(mutated)["fixture_id"] = "yellow-cgb-ordinary"

    with pytest.raises(coverage.CoverageError):
        coverage.validate_catalog(mutated)
    results = coverage.result_set_from_document(_mechanics_document(mutated))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    for runtime in ("source", "cython"):
        assert _mechanics_status(mutated, report, runtime)["status"] != "tested"
    assert report["dimensions"]["expanded_mechanics"]["status"] == "INCOMPLETE"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0
    assert report["move_effects"]["tested"] == 0


def test_catalog_invalid_role_makes_mechanics_case_untested(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    case = _mechanics_case(mutated)
    case["roles"] = ["listen"]
    case["game_versions"] = ["red"]

    with pytest.raises(coverage.CoverageError):
        coverage.validate_catalog(mutated)
    results = coverage.result_set_from_document(_mechanics_document(mutated))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    for runtime in ("source", "cython"):
        assert _mechanics_status(mutated, report, runtime)["status"] != "tested"
    assert report["dimensions"]["expanded_mechanics"]["status"] == "INCOMPLETE"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 0
    assert report["move_effects"]["tested"] == 0
