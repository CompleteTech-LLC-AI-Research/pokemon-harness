"""Authored controls: rows from the producer's actual terminal records (#107.3, finding 1)."""

import copy

import pytest

from scripts.production_gate_parallel_plan import PlanError
from scripts.production_gate_parallel_records import (
    build_rows,
    case_identity,
    diagnostics_for_tier,
    row_id,
    row_problems,
)
from tests._parallel_runtime_report_support import gate_tiers, make_plan

MATRIX_ID = "tests/test_matrix.py::test_trade[red-listen-blue-connect]"


def test_case_identity_keeps_every_game_and_normalises_the_real_role_tokens():
    games, roles = ("red", "blue"), ("listener", "connector")
    assert case_identity(MATRIX_ID, games, roles) == (["red", "blue"], ["listener", "connector"])
    reversed_id = "tests/test_matrix.py::test_trade[blue-connect-red-listen]"
    assert case_identity(reversed_id, games, roles) == (["blue", "red"], ["connector", "listener"])
    assert case_identity("tests/x.py::plain", games, roles) == ([], [])


def test_rows_come_from_actual_records_case_results_and_per_iteration_timing():
    records = [
        {
            "iteration": it,
            "nodeid": MATRIX_ID,
            "outcome": "passed",
            "when": "call",
            "was_xfail": False,
        }
        for it in (1, 2)
    ]
    tiers = [
        {"name": "unit", "status": "PASS", "test_records": records[:1]},
        {"name": "timing", "status": "PASS", "test_records": records},
        {
            "name": "trade",
            "status": "PASS",
            "case_results": [{"nodeid": "m::t", "status": "PASS", "returncode": 0}],
        },
    ]
    rows = build_rows("source", tiers, ("red", "blue"), ("listener", "connector"))
    assert [row["source"] for row in rows] == ["pytest_records"] * 3 + ["case_results"]
    assert [row["repetition"] for row in rows] == [0, 1, 2, 0]
    assert (
        rows[1]["id"] == row_id("source", "timing", MATRIX_ID, 1) == f"source/timing/{MATRIX_ID}@1"
    )
    assert rows[0]["games"] == ["red", "blue"] and rows[0]["roles"] == ["listener", "connector"]
    assert "mode" not in rows[0] and "game" not in rows[0], "no duplicated mode, no single game"


def test_an_expected_failure_or_skip_record_is_never_a_passed_row():
    base = {"iteration": 1, "nodeid": "a::t", "when": "call"}
    tier = {
        "name": "unit",
        "test_records": [
            {**base, "outcome": "skipped", "was_xfail": True},
            {**base, "nodeid": "a::u", "outcome": "passed", "was_xfail": True},
            {**base, "nodeid": "a::v", "outcome": "skipped", "was_xfail": False},
        ],
    }
    assert [r["outcome"] for r in build_rows("source", [tier], (), ())] == [
        "xfailed",
        "xpassed",
        "skipped",
    ]


def good(plan):
    job = plan.job("source-job")
    raw = gate_tiers(job, plan.as_dict())
    return build_rows("source", raw, plan.as_dict()["games"], plan.as_dict()["roles"]), raw, job


def test_complete_rows_validate_and_each_corruption_is_refused():
    plan = make_plan()
    rows, raw, job = good(plan)
    body = plan.as_dict()
    assert row_problems(rows, job, body, raw) == []
    substituted = copy.deepcopy(rows)
    substituted[0]["case"] = "tests/test_a.py::test_other"
    substituted[0]["id"] = row_id("source", "unit", "tests/test_a.py::test_other", 0)
    duplicated = copy.deepcopy(rows)
    duplicated[-1] = copy.deepcopy(duplicated[-2])
    failed = copy.deepcopy(rows)
    failed[1]["outcome"] = "failed"
    wrong_runtime = copy.deepcopy(rows)
    wrong_runtime[0]["runtime"] = "cython"
    short_timing = [r for r in copy.deepcopy(rows) if r["repetition"] != 5]
    extra = [*rows[:2], {**rows[0], "id": "source/unit/x", "case": "x"}, *rows[2:]]
    for label, candidate, needle in (
        ("substituted at unchanged counts", substituted, "missing required"),
        ("duplicated repetition", duplicated, "duplicate"),
        ("non-passed outcome", failed, "outcome failed"),
        ("wrong runtime", wrong_runtime, "different runtime"),
        ("missing fifth repetition", short_timing, "timing repetitions"),
        ("row beyond the tier total", extra, "rows but the tier reports"),
        ("counts only", [], "non-empty list"),
    ):
        problems = row_problems(candidate, job, body, raw)
        assert any(needle in item for item in problems), (label, problems)


def test_a_role_reversal_in_a_row_is_detected_against_the_case_identity():
    plan = make_plan(games=("red", "blue"))
    job = {**plan.job("source-job"), "tiers": ["unit"], "timing_rep_ids": []}
    tier = {
        "name": "unit",
        "status": "PASS",
        "counts": {"total": 1},
        "test_records": [
            {
                "iteration": 1,
                "nodeid": MATRIX_ID,
                "outcome": "passed",
                "when": "call",
                "was_xfail": False,
            }
        ],
    }
    rows = build_rows("source", [tier], ("red", "blue"), ("listener", "connector"))
    rows[0]["roles"] = ["connector", "listener"]
    problems = row_problems(rows, job, {**plan.as_dict(), "required_nodeids": {}}, [tier])
    assert any("ordered roles" in item for item in problems)


def test_timing_repetitions_exist_only_when_timing_is_selected():
    unit_only = make_plan(tiers=("unit",)).as_dict()
    assert all(job["timing_rep_ids"] == [] for job in unit_only["jobs"])
    with_timing = make_plan(tiers=("unit", "timing")).as_dict()
    assert with_timing["jobs"][0]["timing_rep_ids"] == [f"timing-rep-{n}" for n in range(1, 6)]


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
