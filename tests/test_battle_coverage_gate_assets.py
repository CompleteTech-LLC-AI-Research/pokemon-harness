"""Gate-asset, junit and tier-propagation accounting for battle coverage (#143).

Split from ``tests/test_battle_coverage.py`` for #143 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module, except the self-referential unit-classification test, whose
assertion is repointed to the five replacement modules (see #143 evidence).
Synthetic report documents only; never instantiates PyBoy or loads a ROM/state.
"""

import copy
from pathlib import Path

import pytest

from scripts import coverage_report as coverage
from tests import _tier_config
from tests._battle_coverage_support import (
    _case_input_hashes,
    _case_report,
    _mechanics_case,
    _mechanics_status,
    _passing_collections,
    _passing_document,
    _pinned_revision,
)


def test_gate_output_settlement_rows_verify_the_declared_effect(catalog: dict) -> None:
    selector = _mechanics_case(catalog)["selector"]
    case = _mechanics_case(catalog)
    settlement = (
        "  local battle settlement: ["
        "{'turn': {'local_move_effect': 0, 'enemy_move_effect': 0}}, "
        "{'turn': {'local_move_effect': 0, 'enemy_move_effect': 0}}]"
    )
    document = {
        "commit": "a" * 40,
        "runtime": {
            "pyboy_mode": "source",
            "pyboy_version": "2.7.0",
            "pyboy_revision": _pinned_revision(catalog),
        },
        "input_hashes": _case_input_hashes(catalog, case),
        "collections": _passing_collections([selector]),
        "tiers": [
            {
                "name": "battle",
                "case_results": [
                    {
                        "nodeid": selector,
                        "status": "PASS",
                        "partial": False,
                        "reason": "",
                        "output_tail": settlement,
                    }
                ],
            }
        ],
    }
    results = coverage.result_set_from_document(document)
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "tested"


def test_junit_system_out_is_parsed_for_the_declared_effect(catalog: dict, tmp_path: Path) -> None:
    case = _mechanics_case(catalog)
    selector = case["selector"]
    module, _, name = selector.partition("::")
    classname = (module.removesuffix(".py")).replace("/", ".")
    junit = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="1">'
        f'<testcase classname="{classname}" name="{name}" time="0.1">'
        "<system-out>'local_move_effect': 0 'enemy_move_effect': 0</system-out>"
        "</testcase></testsuite></testsuites>"
    )
    path = tmp_path / "mechanisms.xml"
    path.write_text(junit, encoding="utf-8")

    results = coverage.load_results(path)
    outcome = results.lookup("unknown", selector)[0]
    assert outcome.effects == (0, 0)


def test_connector_endpoint_must_match_the_selector(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    target = next(
        case
        for case in mutated["coverage"]["required_cases"]
        if case["case_id"] == "battle_local_red_red"
    )
    target["game_versions"] = ["red", "yellow"]

    with pytest.raises(coverage.CoverageError, match="endpoints"):
        coverage.validate_catalog(mutated)
    report = coverage.build_report(mutated)
    assert report["overall"] == "INCOMPLETE"


def test_both_endpoint_hashes_are_required(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    case = coverage.one_turn_pairing_cases(catalog)[0]
    hashes = _case_input_hashes(catalog, case)
    hashes.pop("connect_rom_sha1")
    document = _passing_document(catalog)
    document["records"][0]["input_hashes"] = hashes
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    sample = _case_report(report, selector, "source")
    assert sample["status"] == "unidentified"
    assert "connect" in sample["reason"]


def test_record_level_partial_in_tests_is_not_ignored(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    case = coverage.one_turn_pairing_cases(catalog)[0]
    document = {
        "commit": "a" * 40,
        "runtime": {
            "pyboy_mode": "source",
            "pyboy_version": "2.7.0",
            "pyboy_revision": _pinned_revision(catalog),
        },
        "input_hashes": _case_input_hashes(catalog, case),
        "collections": _passing_collections([selector]),
        "tests": [
            {"nodeid": selector, "outcome": "passed", "partial": True, "reason": "timed out"}
        ],
    }
    results = coverage.result_set_from_document(document)
    report = coverage.build_report(catalog, results)

    sample = _case_report(report, selector, "source")
    assert sample["status"] == "partial"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"


def test_enclosing_partial_tier_propagates_to_contained_cases(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    case = coverage.one_turn_pairing_cases(catalog)[0]
    document = {
        "commit": "a" * 40,
        "runtime": {
            "pyboy_mode": "source",
            "pyboy_version": "2.7.0",
            "pyboy_revision": _pinned_revision(catalog),
        },
        "input_hashes": _case_input_hashes(catalog, case),
        "collections": _passing_collections([selector]),
        "tiers": [
            {
                "name": "battle",
                "partial": True,
                "status": "TIMEOUT",
                "case_results": [
                    {"nodeid": selector, "status": "PASS", "partial": False, "reason": ""}
                ],
            }
        ],
    }
    results = coverage.result_set_from_document(document)
    report = coverage.build_report(catalog, results)

    sample = _case_report(report, selector, "source")
    assert sample["status"] == "partial"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0


def test_enclosing_non_pass_tier_status_propagates(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    case = coverage.one_turn_pairing_cases(catalog)[0]
    document = {
        "commit": "a" * 40,
        "runtime": {
            "pyboy_mode": "source",
            "pyboy_version": "2.7.0",
            "pyboy_revision": _pinned_revision(catalog),
        },
        "input_hashes": _case_input_hashes(catalog, case),
        "collections": _passing_collections([selector]),
        "tiers": [
            {
                "name": "battle",
                "status": "INTERRUPTED",
                "case_results": [
                    {"nodeid": selector, "status": "PASS", "partial": False, "reason": ""}
                ],
            }
        ],
    }
    results = coverage.result_set_from_document(document)
    report = coverage.build_report(catalog, results)

    sample = _case_report(report, selector, "source")
    assert sample["status"] == "partial"
    assert report["summary"]["tested"] == 0


@pytest.mark.parametrize("status", ["TIMEOUT", "INTERRUPTED"])
def test_enclosing_document_status_propagates(catalog: dict, status: str) -> None:
    document = _passing_document(catalog)
    document["status"] = status
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    assert _case_report(report, selector, "source")["status"] == "partial"
    assert report["summary"]["tested"] == 0
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"


@pytest.mark.parametrize("status", ["TIMEOUT", "INTERRUPTED"])
def test_enclosing_runtime_block_status_propagates(catalog: dict, status: str) -> None:
    document = _passing_document(catalog)
    for block in document["runtimes"]:
        block["status"] = status
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    assert _case_report(report, selector, "source")["status"] == "partial"
    assert report["summary"]["tested"] == 0
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"


def test_dual_runtime_gate_assets_satisfy_endpoint_hashes(catalog: dict) -> None:
    case = _mechanics_case(catalog)
    selector = case["selector"]
    red = coverage._canonical_battle_scenarios(catalog)["red"]
    blocks = [
        {
            "mode": mode,
            "runtime": {
                "pyboy_mode": mode,
                "pyboy_version": "2.7.0",
                "pyboy_revision": _pinned_revision(catalog),
            },
            "collections": _passing_collections([selector]),
            "tiers": [
                {
                    "name": "battle",
                    "status": "PASS",
                    "case_results": [
                        {
                            "nodeid": selector,
                            "status": "PASS",
                            "partial": False,
                            "reason": "",
                            "output_tail": "'local_move_effect': 0 'enemy_move_effect': 0",
                        }
                    ],
                }
            ],
        }
        for mode in ("source", "cython")
    ]
    document = {
        "run_id": "gate",
        "commit": "a" * 40,
        "runtimes": blocks,
        "assets": [
            {"kind": "rom", "label": "red-stock", "status": "ok", "actual_sha1": "0" * 40},
            {
                "kind": "rom",
                "label": "red-color",
                "status": "ok",
                "actual_sha1": red["game"]["rom_sha1"],
            },
            {
                "kind": "symbol",
                "label": "red",
                "status": "ok",
                "actual_sha1": red["game"]["sym_sha1"],
            },
            {
                "kind": "fixture",
                "label": "red cable-club",
                "status": "ok",
                "actual_sha1": red["provenance"]["input_fixture_sha1"],
            },
        ],
    }
    results = coverage.result_set_from_document(document)
    report = coverage.build_report(catalog, results, expected_commit="a" * 40)

    assert _mechanics_status(catalog, report, "source")["status"] == "tested"
    assert _mechanics_status(catalog, report, "cython")["status"] == "tested"
    assert report["dimensions"]["expanded_mechanics"]["tested"] == 1


def test_gate_asset_hashes_are_canonical_and_version_scoped(catalog: dict) -> None:
    document = {
        "assets": [
            {"kind": "rom", "label": "red-stock", "status": "ok", "actual_sha1": "a" * 40},
            {"kind": "rom", "label": "red-color", "status": "ok", "actual_sha1": "b" * 40},
            {"kind": "symbol", "label": "red", "status": "ok", "actual_sha1": "c" * 40},
            {"kind": "fixture", "label": "red cable-club", "status": "ok", "actual_sha1": "d" * 40},
        ]
    }
    hashes = coverage._gate_input_hashes(document)

    assert hashes["rom:red"] == "b" * 40
    assert hashes["sym:red"] == "c" * 40
    assert hashes["input_fixture:red"] == "d" * 40


def test_tier_case_level_commit_is_preserved(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    case = coverage.one_turn_pairing_cases(catalog)[0]
    document = {
        "commit": "a" * 40,
        "runtime": {
            "pyboy_mode": "source",
            "pyboy_version": "2.7.0",
            "pyboy_revision": _pinned_revision(catalog),
        },
        "input_hashes": _case_input_hashes(catalog, case),
        "collections": _passing_collections([selector]),
        "tiers": [
            {
                "name": "battle",
                "status": "PASS",
                "case_results": [
                    {
                        "nodeid": selector,
                        "status": "PASS",
                        "partial": False,
                        "reason": "",
                        "commit": "b" * 40,
                    }
                ],
            }
        ],
    }
    results = coverage.result_set_from_document(document)
    report = coverage.build_report(catalog, results)

    assert any("commit identities" in problem for problem in report["problems"])
    assert report["summary"]["tested"] == 0


def test_battle_coverage_module_is_classified_as_unit() -> None:
    for module in (
        "test_battle_coverage_accounting.py",
        "test_battle_coverage_catalog.py",
        "test_battle_coverage_gate_assets.py",
        "test_battle_coverage_identity.py",
        "test_battle_coverage_mechanics.py",
    ):
        assert module in _tier_config.UNIT_MODULES
