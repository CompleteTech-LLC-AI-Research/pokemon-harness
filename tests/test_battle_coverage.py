"""ROM-free tests for the battle coverage/effects catalog and report.

These tests never instantiate PyBoy or load a ROM/state. They validate the
additive coverage/effects dimension, exercise the coverage report's fail-closed
accounting with synthetic records, and prove that a complete one-turn pairing
matrix is never reported as complete while expanded mechanics remain
unverified.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import coverage_report as coverage
from scripts import tcp_link_matrix as matrix
from scripts import validate_battle_scenarios as validator
from tests import _tier_config

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "release-evidence" / "battle-scenarios.json"
MANIFEST_PATH = ROOT / "release-evidence" / "fixture-manifest.json"

_EXPECTED_RUNTIMES = ["source", "cython"]
_TERMINAL_FAILURES = ("skipped", "xfailed", "timed_out", "not_run", "failed", "error")


@pytest.fixture()
def catalog() -> dict:
    return coverage.load_catalog(CATALOG_PATH)


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _pinned_revision(catalog: dict) -> str:
    return catalog["coverage"]["evidence_policy"]["expected_pyboy_revision"]


def _passing_document(
    catalog: dict,
    *,
    runtimes: tuple[str, ...] = ("source", "cython"),
    revision: str | None = None,
    status: str = "passed",
) -> dict:
    revision = revision or _pinned_revision(catalog)
    runtime_blocks = [
        {
            "mode": mode,
            "runtime": {
                "pyboy_mode": mode,
                "pyboy_version": "2.7.0",
                "pyboy_revision": revision,
            },
        }
        for mode in runtimes
    ]
    records = [
        {"nodeid": case["selector"], "runtime": mode, "status": status}
        for case in coverage.one_turn_pairing_cases(catalog)
        for mode in runtimes
    ]
    return {
        "run_id": "synthetic-coverage-test",
        "commit": "a" * 40,
        "runtimes": runtime_blocks,
        "records": records,
    }


def _case_report(report: dict, selector: str, runtime: str) -> dict:
    return next(
        case
        for case in report["cases"]
        if case["selector"] == selector and case["runtime"] == runtime
    )


def test_catalog_shape_still_validates_and_declares_nineteen_pairing_cases(catalog: dict) -> None:
    scenarios = validator._validate_schema(catalog, _manifest())
    assert len(scenarios) == 10
    coverage.validate_catalog(catalog)

    cases = coverage.one_turn_pairing_cases(catalog)
    assert len(cases) == 19
    assert {case["selector"] for case in cases} == set(matrix.STRICT_BATTLE_NODEIDS)
    for case in cases:
        assert case["runtimes"] == _EXPECTED_RUNTIMES
        assert set(case["roles"]) == {"listen", "connect"}
        assert case["fixture_id"] in {
            scenario["fixture"]["fixture_id"] for scenario in catalog["scenarios"]
        }


def test_report_states_one_turn_scope_separately_from_expanded_mechanics(catalog: dict) -> None:
    report = coverage.build_report(catalog)

    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["dimensions"]["expanded_mechanics"]["status"] == "PLANNED_UNVERIFIED"
    text = coverage.render_text(report).lower()
    assert "one-turn" in text
    assert "expanded mechanics" in text


def test_effect_families_cover_every_pinned_effect_id(catalog: dict) -> None:
    families = coverage.effect_families(catalog)

    assert len(families) == 87
    assert [family["effect_id"] for family in families] == list(range(87))
    for family in families:
        assert family["scope"] in coverage._SCOPES
        assert isinstance(family["reason"], str) and family["reason"].strip()
        assert isinstance(family["move_ids"], list)
        assert family["scope"] != "tested"


def test_effect_families_account_for_every_move_and_reserved_slot(catalog: dict) -> None:
    families = coverage.effect_families(catalog)

    all_moves = sorted(move for family in families for move in family["move_ids"])
    assert all_moves == list(range(1, 166))
    for family in families:
        if family["move_ids"]:
            assert family["scope"] == "planned_unverified"
            assert family["kind"] == "effect"
        else:
            assert family["scope"] == "deliberately_excluded"
    reserved = {family["effect_id"] for family in families if family["kind"] == "const_skip"}
    assert reserved == {72, 73, 74, 75, 78}


def test_current_selector_admits_0_6_44_and_excludes_counter(catalog: dict) -> None:
    selector = catalog["move_effects"]["current_selector"]
    families = {family["effect_id"]: family for family in coverage.effect_families(catalog)}

    assert selector["admitted_effect_ids"] == [0, 6, 44]
    assert [entry["move_id"] for entry in selector["excluded_moves"]] == [68]
    assert selector["excluded_moves"][0]["name"] == "COUNTER"
    assert selector["excluded_moves"][0]["effect_id"] == 0
    for effect_id in (0, 6, 44):
        assert families[effect_id]["scope"] == "planned_unverified"


def test_missing_required_case_is_reported_incomplete(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    mutated["coverage"]["required_cases"] = [
        case
        for case in mutated["coverage"]["required_cases"]
        if case["case_id"] != "battle_local_red_red"
    ]

    with pytest.raises(coverage.CoverageError, match="tcp_link_matrix"):
        coverage.validate_catalog(mutated)
    report = coverage.build_report(mutated)
    assert report["overall"] == "INCOMPLETE"
    assert any(
        "missing required one_turn_pairing case" in problem for problem in report["problems"]
    )


def test_missing_required_runtime_is_reported_incomplete(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    target = mutated["coverage"]["required_cases"][0]
    target["runtimes"] = ["source"]

    report = coverage.build_report(mutated, None)
    assert report["overall"] == "INCOMPLETE"
    assert any("missing required runtime 'cython'" in problem for problem in report["problems"])


def test_missing_required_role_is_reported_incomplete(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    target = mutated["coverage"]["required_cases"][0]
    target["roles"] = ["listen"]

    report = coverage.build_report(mutated, None)
    assert report["overall"] == "INCOMPLETE"
    assert any("missing required role 'connect'" in problem for problem in report["problems"])


def test_unknown_fixture_is_reported(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    mutated["coverage"]["required_cases"][0]["fixture_id"] = "not-a-real-fixture"

    with pytest.raises(coverage.CoverageError, match="fixture_id is unknown"):
        coverage.validate_catalog(mutated)
    report = coverage.build_report(mutated, None)
    assert report["overall"] == "INCOMPLETE"
    assert any("unknown fixture" in problem for problem in report["problems"])


def test_duplicate_result_is_incomplete(catalog: dict) -> None:
    document = _passing_document(catalog)
    duplicated = document["records"][0]
    document["records"].append(dict(duplicated))
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["statuses"].get("duplicate") == 1


@pytest.mark.parametrize("status", _TERMINAL_FAILURES)
def test_non_pass_outcomes_are_incomplete_and_never_pass(catalog: dict, status: str) -> None:
    document = _passing_document(catalog, status=status)
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert all(case["status"] != "tested" for case in report["cases"])


def test_collection_failure_is_reported_incomplete(catalog: dict) -> None:
    document = _passing_document(catalog)
    document["collection_errors"] = [{"nodeid": "tests/broken.py", "reason": "boom"}]
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert any("collection failure" in problem for problem in report["problems"])


def test_mismatched_commit_is_reported_incomplete(catalog: dict) -> None:
    document = _passing_document(catalog)
    document["commit"] = "b" * 40
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert any("commit mismatch" in problem for problem in report["problems"])
    sample = _case_report(report, coverage.one_turn_pairing_cases(catalog)[0]["selector"], "source")
    assert sample["status"] == "mismatched"


def test_mismatched_build_is_reported_incomplete(catalog: dict) -> None:
    document = _passing_document(catalog, revision="0" * 40)
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    sample = _case_report(report, coverage.one_turn_pairing_cases(catalog)[0]["selector"], "source")
    assert sample["status"] == "mismatched"
    assert "revision" in sample["reason"]


def test_required_cases_need_both_runtime_results(catalog: dict) -> None:
    document = _passing_document(catalog, runtimes=("source",))
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    assert _case_report(report, selector, "source")["status"] == "tested"
    assert _case_report(report, selector, "cython")["status"] == "missing"
    assert report["overall"] == "INCOMPLETE"


def test_complete_pairing_does_not_hide_unverified_mechanics(catalog: dict) -> None:
    document = _passing_document(catalog)
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["summary"]["tested"] == 38
    assert report["dimensions"]["one_turn_pairing"] == {
        "status": "COMPLETE",
        "checks": 38,
        "complete": 38,
    }
    assert report["dimensions"]["expanded_mechanics"]["status"] == "PLANNED_UNVERIFIED"
    assert report["overall"] == "INCOMPLETE"


def test_unrelated_partial_runs_are_not_merged(catalog: dict) -> None:
    document = _passing_document(catalog)
    document["runtimes"][1]["runtime"]["pyboy_revision"] = "f" * 40
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert any("refusing to merge" in problem for problem in report["problems"])


def test_gate_json_matrix_rows_are_consumed_without_double_counting(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    document = {
        "runtime": {
            "pyboy_mode": "source",
            "pyboy_version": "2.7.0",
            "pyboy_revision": _pinned_revision(catalog),
        },
        "tiers": [
            {
                "name": "battle",
                "case_results": [
                    {"nodeid": selector, "status": "PASS", "partial": False, "reason": ""}
                ],
                "failure_details": [
                    {"nodeid": selector, "outcome": "failed", "reason": "stale teardown"}
                ],
            }
        ],
    }
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results)
    sample = _case_report(report, selector, "source")
    assert sample["status"] == "tested"
    assert report["summary"]["statuses"].get("duplicate") is None


def test_gate_timeout_row_is_incomplete(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    document = {
        "runtime": {
            "pyboy_mode": "source",
            "pyboy_version": "2.7.0",
            "pyboy_revision": _pinned_revision(catalog),
        },
        "tiers": [
            {
                "name": "battle",
                "case_results": [
                    {
                        "nodeid": selector,
                        "status": "TIMEOUT",
                        "partial": True,
                        "reason": "deadline",
                    }
                ],
            }
        ],
    }
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results)
    sample = _case_report(report, selector, "source")
    assert sample["status"] == "partial"
    assert report["overall"] == "INCOMPLETE"


def test_junit_results_are_never_treated_as_runtime_evidence(catalog: dict, tmp_path: Path) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    module, _, name = selector.partition("::")
    junit = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="1">'
        f'<testcase classname="{module.replace("/", ".")}" name="{name}" time="0.1">'
        "</testcase></testsuite></testsuites>"
    )
    path = tmp_path / "results.xml"
    path.write_text(junit, encoding="utf-8")

    results = coverage.load_results(path)
    assert results.runtimes == {}
    report = coverage.build_report(catalog, results)
    assert report["overall"] == "INCOMPLETE"
    assert all(case["status"] != "tested" for case in report["cases"])


def test_cli_no_results_is_declaration_only(catalog: dict, capsys: pytest.CaptureFixture) -> None:
    status = coverage.main(["--catalog", str(CATALOG_PATH), "--no-results"])
    captured = capsys.readouterr()
    assert status == 1
    assert "INCOMPLETE" in captured.out


def test_internal_tested_sentinel_is_not_a_terminal_pass(catalog: dict) -> None:
    document = _passing_document(catalog, status="tested")
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert all(case["status"] != "tested" for case in report["cases"])
    assert report["summary"]["statuses"].get("unaccepted") == 38


def test_only_accepted_outcomes_can_complete_a_dimension(catalog: dict) -> None:
    accepted = coverage.result_set_from_document(_passing_document(catalog, status="passed"))
    completed = coverage.build_report(catalog, accepted, expected_commit="a" * 40)
    assert completed["dimensions"]["one_turn_pairing"]["status"] == "COMPLETE"

    for status in ("tested", "ok", "pass", "xpassed"):
        results = coverage.result_set_from_document(_passing_document(catalog, status=status))
        report = coverage.build_report(catalog, results, expected_commit="a" * 40)
        assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE", status
        assert report["summary"]["tested"] == 0, status


def test_accepted_outcomes_declaration_is_enforced(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    mutated["coverage"]["evidence_policy"]["accepted_outcomes"] = ["tested"]
    with pytest.raises(coverage.CoverageError, match="sentinel"):
        coverage.validate_catalog(mutated)

    mutated["coverage"]["evidence_policy"]["accepted_outcomes"] = []
    with pytest.raises(coverage.CoverageError, match="accepted_outcomes"):
        coverage.validate_catalog(mutated)


def test_sentinel_is_filtered_even_when_catalog_declares_it(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    mutated["coverage"]["evidence_policy"]["accepted_outcomes"] = ["tested"]
    results = coverage.result_set_from_document(_passing_document(catalog, status="tested"))

    report = coverage.build_report(mutated, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"


def test_record_commit_is_accepted_when_present(catalog: dict) -> None:
    document = _passing_document(catalog)
    document.pop("commit")
    for record in document["records"]:
        record["commit"] = "a" * 40
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["run"]["commit_source"] == "records"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "COMPLETE"


def test_declared_commit_without_result_commit_is_explicitly_unidentified(catalog: dict) -> None:
    document = _passing_document(catalog)
    document.pop("commit")
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert report["run"]["commit_source"] is None
    assert report["run"]["commit_status"].startswith("unidentified")
    assert any("carries no commit" in problem for problem in report["problems"])
    sample = _case_report(report, coverage.one_turn_pairing_cases(catalog)[0]["selector"], "source")
    assert sample["status"] == "unidentified"
    assert "commit" in sample["reason"]


def test_battle_coverage_module_is_classified_as_unit() -> None:
    assert "test_battle_coverage.py" in _tier_config.UNIT_MODULES
