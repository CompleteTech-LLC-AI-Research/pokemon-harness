"""Authored controls: structured rows from a real-shaped gate report (#107.3, finding 1)."""

import copy

import pytest

from scripts.production_gate_parallel_plan import PlanError
from scripts.production_gate_parallel_records import (
    build_rows,
    case_identity,
    diagnostics_for_tier,
    row_problems,
)
from tests._parallel_runtime_report_support import gate_tiers, make_plan

MATRIX_ID = "tests/test_matrix.py::test_trade[red-listener-connector]"


def test_case_identity_derives_game_and_ordered_roles_from_the_parameter_id():
    assert case_identity(MATRIX_ID, ("red", "blue"), ("listener", "connector")) == (
        "red",
        ["listener", "connector"],
    )
    reversed_id = "tests/test_matrix.py::test_trade[red-connector-listener]"
    assert case_identity(reversed_id, ("red",), ("listener", "connector"))[1] == [
        "connector",
        "listener",
    ]
    assert case_identity("tests/x.py::plain", ("red",), ("listener",)) == ("", [])


def test_rows_come_from_case_results_selected_ids_and_return_codes():
    tiers = [
        {
            "name": "trade",
            "status": "PASS",
            "case_results": [{"nodeid": MATRIX_ID, "status": "PASS", "returncode": 0}],
        },
        {
            "name": "unit",
            "status": "PASS",
            "selected_nodeids": ["a::one", "a::two"],
            "failure_details": [],
        },
        {"name": "timing", "status": "PASS", "returncodes": [0, 0, 1]},
    ]
    rows = build_rows("source", tiers, ("red",), ("listener", "connector"))
    assert [row["source"] for row in rows] == [
        "case_results",
        "selected_nodeids+tier_status",
        "selected_nodeids+tier_status",
        "returncodes",
        "returncodes",
        "returncodes",
    ]
    assert rows[0]["game"] == "red" and rows[0]["roles"] == ["listener", "connector"]
    assert [row["outcome"] for row in rows[-3:]] == ["passed", "passed", "failed"]
    assert rows[0]["id"] == f"source/trade/{MATRIX_ID}"


def test_failure_details_mark_a_selected_case_failed_even_in_a_pass_shaped_tier():
    tier = {
        "name": "unit",
        "status": "FAIL",
        "selected_nodeids": ["a::one", "a::two"],
        "failure_details": [{"nodeid": "a::two"}],
    }
    outcomes = {row["case"]: row["outcome"] for row in build_rows("source", [tier], (), ())}
    assert outcomes == {"a::one": "unrun", "a::two": "failed"}


def good_rows(plan):
    job = plan.job("source-job")
    raw = gate_tiers(job)
    return build_rows("source", raw, plan.as_dict()["games"], plan.as_dict()["roles"]), raw, job


def test_complete_rows_validate_and_each_corruption_is_refused():
    plan = make_plan()
    rows, raw, job = good_rows(plan)
    body = plan.as_dict()
    assert row_problems(rows, job, body, raw) == []
    substituted = copy.deepcopy(rows)
    substituted[0]["case"] = "tests/test_a.py::test_other"
    substituted[0]["id"] = "source/unit/tests/test_a.py::test_other"
    duplicated = copy.deepcopy(rows)
    duplicated[-1] = copy.deepcopy(duplicated[-2])
    failed = copy.deepcopy(rows)
    failed[1]["outcome"] = "failed"
    wrong_runtime = copy.deepcopy(rows)
    wrong_runtime[0]["runtime"] = "cython"
    extra = [*rows, {**rows[0], "id": "source/unit/x", "case": "x"}]
    for label, candidate, needle in (
        ("substituted at unchanged counts", substituted, "missing required"),
        ("duplicated repetition", duplicated, "duplicate"),
        ("non-passed outcome", failed, "outcome failed"),
        ("wrong runtime", wrong_runtime, "different runtime"),
        ("row beyond the tier total", extra, "rows but the tier reports"),
        ("counts only", [], "non-empty list"),
    ):
        problems = row_problems(candidate, job, body, raw)
        assert any(needle in item for item in problems), (label, problems)


def test_a_role_reversal_in_a_row_is_detected_against_the_case_identity():
    plan = make_plan(games=("red",))
    job = plan.job("source-job")
    tier = {
        "name": "unit",
        "status": "PASS",
        "counts": {"total": 1},
        "returncodes": [0],
        "selected_nodeids": [MATRIX_ID],
    }
    rows = build_rows("source", [tier], ("red",), ("listener", "connector"))
    rows[0]["roles"] = ["connector", "listener"]
    problems = row_problems(
        rows, {**job, "tiers": ["unit"], "timing_rep_ids": []}, plan.as_dict(), [tier]
    )
    assert any("ordered roles" in item for item in problems)


def test_diagnostics_are_kept_verbatim():
    tier = {
        "name": "unit",
        "failure_details": [{"nodeid": "a"}],
        "iteration_failures": ["x"],
        "output_tail": "tail",
    }
    assert diagnostics_for_tier(tier) == {
        "failure_details": [{"nodeid": "a"}],
        "iteration_failures": ["x"],
        "output_tail": "tail",
    }


def test_the_plan_refuses_to_waive_row_identity():
    with pytest.raises(PlanError, match="require_row_ids"):
        make_plan(require_row_ids=False)
