"""Partial-run and mixed-identity merge rules for the coverage report (#143).

Split from ``tests/test_battle_coverage.py`` for #143 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Synthetic report documents only; never instantiates PyBoy
or loads a ROM/state.
"""

import copy

import pytest

from scripts import coverage_report as coverage
from tests._battle_coverage_support import (
    _case_report,
    _passing_document,
    _split_partial_document,
)


def test_partial_block_runs_are_not_merged(catalog: dict) -> None:
    document = _split_partial_document(catalog, partial=True, with_identity=True)
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert any("partial" in problem for problem in report["problems"])


def test_mixed_block_run_identities_are_not_merged(catalog: dict) -> None:
    document = _split_partial_document(catalog, partial=False, with_identity=True)
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0


def test_mixed_record_commits_without_declared_commit_are_not_merged(catalog: dict) -> None:
    document = _passing_document(catalog)
    document.pop("commit")
    for index, record in enumerate(document["records"]):
        record["commit"] = ("a" if index % 2 == 0 else "b") * 40
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0


def test_alternating_record_run_ids_are_not_merged(catalog: dict) -> None:
    document = _passing_document(catalog)
    document.pop("run_id")
    for index, record in enumerate(document["records"]):
        record["run_id"] = f"run-{index % 2}"
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert any("run identities" in problem for problem in report["problems"])


def test_uniform_record_commit_contradicting_document_is_not_merged(catalog: dict) -> None:
    document = _passing_document(catalog)
    for record in document["records"]:
        record["commit"] = "b" * 40
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert any("commit identities" in problem for problem in report["problems"])


def test_top_level_partial_is_propagated_and_not_merged(catalog: dict) -> None:
    document = _passing_document(catalog)
    document["partial"] = True
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results, expected_commit="a" * 40)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert report["run"]["partial"] is True
    assert any("partial" in problem for problem in report["problems"])


def test_missing_commit_provenance_fails_closed_without_declared_commit(catalog: dict) -> None:
    document = _passing_document(catalog)
    document.pop("commit")
    results = coverage.result_set_from_document(document)

    report = coverage.build_report(catalog, results)
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
    assert report["run"]["commit_source"] is None
    assert any("commit provenance" in problem for problem in report["problems"])
    sample = _case_report(report, coverage.one_turn_pairing_cases(catalog)[0]["selector"], "source")
    assert sample["status"] == "unidentified"


def test_forbidden_outcomes_cannot_be_authorized_by_policy(catalog: dict) -> None:
    mutated = copy.deepcopy(catalog)
    mutated["coverage"]["evidence_policy"]["accepted_outcomes"] = [
        "skipped",
        "xfailed",
        "timed_out",
        "not_run",
    ]
    with pytest.raises(coverage.CoverageError):
        coverage.validate_catalog(mutated)

    results = coverage.result_set_from_document(_passing_document(catalog, status="skipped"))
    report = coverage.build_report(mutated, results)
    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["summary"]["tested"] == 0
