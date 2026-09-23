"""Fail-closed accounting of cases, runtimes, roles, fixtures and evidence (#143).

Split from ``tests/test_battle_coverage.py`` for #143 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Synthetic report documents only; never instantiates PyBoy
or loads a ROM/state, and a complete one-turn pairing is never reported as
complete while expanded mechanics remain unverified.
"""

import copy
from pathlib import Path

import pytest

from scripts import coverage_report as coverage
from tests._battle_coverage_support import (
    _TERMINAL_FAILURES,
    CATALOG_PATH,
    _case_input_hashes,
    _case_report,
    _passing_collections,
    _passing_document,
    _pinned_revision,
    _selectors,
)


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


def test_missing_declared_runtime_makes_dimension_incomplete(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    for case in mutated["coverage"]["required_cases"]:
        case["runtimes"] = [runtime for runtime in case["runtimes"] if runtime != "cython"]

    results = coverage.result_set_from_document(_passing_document(catalog))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 19
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


def test_incompatible_fixture_is_rejected(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    target = next(
        case
        for case in mutated["coverage"]["required_cases"]
        if case["case_id"] == "battle_local_red_red"
    )
    target["fixture_id"] = "yellow-cgb-ordinary"

    with pytest.raises(coverage.CoverageError):
        coverage.validate_catalog(mutated)
    results = coverage.result_set_from_document(_passing_document(catalog))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert any("yellow-cgb-ordinary" in problem for problem in report["problems"])


@pytest.mark.parametrize(
    "fixture_id",
    ["yellow-cgb-battle", "red-vanilla-battle", "red-color-ordinary"],
)
def test_incompatible_fixture_version_variant_or_type_is_rejected(
    catalog: dict, fixture_id: str
) -> None:
    mutated = copy.deepcopy(catalog)
    target = next(
        case
        for case in mutated["coverage"]["required_cases"]
        if case["case_id"] == "battle_local_red_red"
    )
    target["fixture_id"] = fixture_id

    with pytest.raises(coverage.CoverageError):
        coverage.validate_catalog(mutated)


def test_declared_fixture_hash_and_observed_input_hash_are_retained(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    scenario = next(
        item for item in catalog["scenarios"] if item["fixture"]["fixture_id"] == "red-color-battle"
    )
    declared = scenario["fixture"]["sha1"]
    document = _passing_document(catalog)
    document["records"][0]["input_hashes"]["fixture"] = declared
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    sample = _case_report(report, selector, "source")
    assert sample["status"] == "tested"
    assert sample["declared_fixture_sha1"] == declared
    assert sample["observed_input_hashes"]["fixture"] == declared


def test_observed_input_hash_mismatch_is_rejected(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    document = _passing_document(catalog)
    document["records"][0]["input_hashes"]["listen_rom_sha1"] = "0" * 40
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    sample = _case_report(report, selector, "source")
    assert sample["status"] == "mismatched"
    assert sample["observed_input_hashes"]["listen_rom_sha1"] == "0" * 40
    assert any("observed input hash" in problem for problem in report["problems"])


def test_missing_observed_input_hashes_fail_closed(catalog: dict) -> None:
    selector = coverage.one_turn_pairing_cases(catalog)[0]["selector"]
    document = _passing_document(catalog)
    document["records"][0]["input_hashes"] = {}
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    sample = _case_report(report, selector, "source")
    assert sample["status"] == "unidentified"
    assert "missing observed input hashes" in sample["reason"]
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"


def test_duplicate_result_is_incomplete(catalog: dict) -> None:
    document = _passing_document(catalog)
    duplicated = document["records"][0]
    document["records"].append(dict(duplicated))
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    # The duplicated selector backs both its pairing case and the shared
    # expanded-mechanics case, so both runtimes' registrations are duplicates.
    assert report["summary"]["statuses"].get("duplicate") == 2


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
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert any("collection failure" in problem for problem in report["problems"])


def test_runtime_collection_failure_is_not_ignored(catalog: dict) -> None:
    document = _passing_document(catalog)
    document["runtimes"][0]["collections"][0]["status"] = "FAIL"
    document["runtimes"][0]["collections"][0]["reason"] = "collection crashed"
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] < 38
    assert any("collection failure" in problem for problem in report["problems"])
    sample = _case_report(report, coverage.one_turn_pairing_cases(catalog)[0]["selector"], "source")
    assert sample["status"] == "collection"


def test_runtime_collection_error_is_not_ignored(catalog: dict) -> None:
    document = _passing_document(catalog)
    document["runtimes"][0]["collection_errors"] = [
        {"nodeid": "tests/broken.py", "reason": "import boom"}
    ]
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert any("collection failure" in problem for problem in report["problems"])


def test_missing_collection_evidence_is_not_ignored(catalog: dict) -> None:
    document = _passing_document(catalog)
    for block in document["runtimes"]:
        block.pop("collections")
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    sample = _case_report(report, coverage.one_turn_pairing_cases(catalog)[0]["selector"], "source")
    assert "no collection evidence" in sample["reason"]


def test_uncollected_selector_is_incomplete(catalog: dict) -> None:
    selectors = _selectors(catalog)
    document = _passing_document(catalog)
    document["runtimes"][0]["collections"][0]["nodeids"] = selectors[1:]
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    sample = _case_report(report, selectors[0], "source")
    assert sample["status"] == "collection"
    assert "not collected" in sample["reason"]
    assert _case_report(report, selectors[1], "source")["status"] == "tested"


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
    case = coverage.one_turn_pairing_cases(catalog)[0]
    selector = case["selector"]
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
    assert report["summary"]["statuses"].get("unaccepted") == 40


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


def test_truthy_evidence_string_cannot_complete_mechanics(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    promoted = 0
    for family in mutated["move_effects"]["families"]:
        if family["scope"] == "planned_unverified":
            family["scope"] = "tested"
            family["evidence"] = "not-executed"
            promoted += 1
    mutated["move_effects"]["planned_unverified_count"] = 0
    assert promoted == 67

    results = coverage.result_set_from_document(_passing_document(catalog))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["expanded_mechanics"]["status"] != "COMPLETE"


def test_evidence_linked_but_unexecuted_mechanics_cannot_complete(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    case = copy.deepcopy(mutated["coverage"]["required_cases"][0])
    case["case_id"] = "mechanics_effect_0"
    case["dimension_id"] = coverage.EXPANDED_DIMENSION
    case["effect_id"] = 0
    case["selector"] = "tests/test_battle_mechanics.py::test_effect_0"
    mutated["coverage"]["required_cases"].append(case)

    for family in mutated["move_effects"]["families"]:
        if family["effect_id"] == 0:
            family["scope"] = "tested"
            family["evidence"] = ["mechanics_effect_0"]
        elif family["scope"] == "planned_unverified":
            family["scope"] = "deliberately_excluded"
    families = mutated["move_effects"]["families"]
    mutated["move_effects"]["planned_unverified_count"] = sum(
        1 for family in families if family["scope"] == "planned_unverified"
    )
    mutated["move_effects"]["deliberately_excluded_count"] = sum(
        1 for family in families if family["scope"] == "deliberately_excluded"
    )

    results = coverage.result_set_from_document(_passing_document(catalog))
    report = coverage.build_report(mutated, results, expected_commit="a" * 40)

    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["expanded_mechanics"]["status"] != "COMPLETE"
