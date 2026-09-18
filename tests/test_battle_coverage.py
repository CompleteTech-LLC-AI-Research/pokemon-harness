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


def _selectors(catalog: dict) -> list[str]:
    return [case["selector"] for case in coverage.one_turn_pairing_cases(catalog)]


def _passing_collections(nodeids: list[str]) -> list[dict]:
    return [{"name": "python-module", "status": "PASS", "nodeids": list(nodeids)}]


def _case_input_hashes(catalog: dict, case: dict) -> dict:
    """Return observed per-endpoint ROM/symbol/fixture hashes for one case."""
    hashes: dict[str, str] = {}
    for role, scenario in coverage._case_endpoint_scenarios(catalog, case).items():
        hashes[f"{role}_rom_sha1"] = scenario["game"]["rom_sha1"]
        hashes[f"{role}_sym_sha1"] = scenario["game"]["sym_sha1"]
        hashes[f"{role}_fixture_sha1"] = scenario["fixture"]["sha1"]
    return hashes


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
            "collections": _passing_collections(_selectors(catalog)),
        }
        for mode in runtimes
    ]
    records = [
        {
            "nodeid": case["selector"],
            "runtime": mode,
            "status": status,
            "input_hashes": _case_input_hashes(catalog, case),
        }
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
    # Every admitted manifest row is declared, including the captured boundary
    # rows; the one-turn pairing dimension still resolves its canonical
    # settled-battle scenario per version.
    assert len(scenarios) == len(_manifest()["fixtures"]) == 28
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
    tested = [family["effect_id"] for family in families if family["scope"] == "tested"]
    assert tested == [0]
    for family in families[1:]:
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
            if family["effect_id"] == 0:
                assert family["scope"] == "tested"
            else:
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
    assert families[0]["scope"] == "tested"
    assert families[0]["evidence"] == ["mechanics_effect_0_no_additional_effect"]
    for effect_id in (6, 44):
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


def _split_partial_document(catalog: dict, *, partial: bool, with_identity: bool) -> dict:
    cases = coverage.one_turn_pairing_cases(catalog)
    half = len(cases) // 2
    subsets = (cases[:half], cases[half:])
    revision = _pinned_revision(catalog)
    blocks: list[dict] = []
    for mode in ("source", "cython"):
        for index, subset in enumerate(subsets):
            block = {
                "mode": mode,
                "partial": partial,
                "runtime": {
                    "pyboy_mode": mode,
                    "pyboy_version": "2.7.0",
                    "pyboy_revision": revision,
                },
                "records": [
                    {"nodeid": case["selector"], "runtime": mode, "status": "passed"}
                    for case in subset
                ],
            }
            if with_identity:
                block["run_id"] = f"partial-run-{index}"
                block["commit"] = ("a" if index == 0 else "b") * 40
            blocks.append(block)
    return {"runtimes": blocks}


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


def _mechanics_case(catalog: dict) -> dict:
    return next(
        case
        for case in coverage.required_cases(catalog)
        if case["case_id"] == "mechanics_effect_0_no_additional_effect"
    )


def _mechanics_document(
    catalog: dict,
    *,
    runtimes: tuple[str, ...] = ("source", "cython"),
    status: str = "passed",
    effect: int | None = 0,
    hashes: dict | None = None,
) -> dict:
    case = _mechanics_case(catalog)
    selector = case["selector"]
    revision = _pinned_revision(catalog)
    blocks = [
        {
            "mode": mode,
            "runtime": {
                "pyboy_mode": mode,
                "pyboy_version": "2.7.0",
                "pyboy_revision": revision,
            },
            "collections": _passing_collections([selector]),
        }
        for mode in runtimes
    ]
    records = []
    for mode in runtimes:
        record = {
            "nodeid": selector,
            "runtime": mode,
            "status": status,
            "commit": "a" * 40,
            "input_hashes": dict(
                hashes if hashes is not None else _case_input_hashes(catalog, case)
            ),
        }
        if effect is not None:
            record["effect_id"] = effect
        records.append(record)
    return {
        "run_id": "synthetic-mechanics-test",
        "runtimes": blocks,
        "records": records,
    }


def _mechanics_status(catalog: dict, report: dict, runtime: str) -> dict:
    case = _mechanics_case(catalog)
    return next(
        item
        for item in report["cases"]
        if item["case_id"] == case["case_id"] and item["runtime"] == runtime
    )


def _dimension(catalog: dict, dimension_id: str) -> dict:
    return next(
        item for item in catalog["coverage"]["dimensions"] if item["dimension_id"] == dimension_id
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
    assert "test_battle_coverage.py" in _tier_config.UNIT_MODULES
