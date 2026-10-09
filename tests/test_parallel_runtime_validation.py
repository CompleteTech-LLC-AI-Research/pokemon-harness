"""Authored controls: validators are total over decoded JSON (#107.3, finding 9)."""

import copy
import json
import math

import pytest

from scripts.production_gate_parallel_artifacts import (
    read_terminal_artifact,
    validate_terminal_artifact,
)
from scripts.production_gate_parallel_plan import PlanError, load_plan, validate_plan_body
from scripts.production_gate_parallel_processes import read_guardian_receipt
from scripts.production_gate_parallel_validation import (
    MAX_JSON_CHARS,
    guarded,
    in_set,
    loads_strict,
    parse_utc,
    timestamp_problems,
)
from tests._parallel_runtime_report_support import good_artifact, make_plan


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", '{"a": 1, "a": 2}'])
def test_strict_json_rejects_non_finite_constants_and_duplicate_keys(text):
    with pytest.raises(ValueError):
        loads_strict(text)


def test_set_membership_never_raises_for_unhashable_decoded_values():
    allowed = frozenset({"PASS"})
    assert in_set("PASS", allowed) is True
    assert in_set(["PASS"], allowed) is False
    assert in_set({"x": 1}, allowed) is False
    assert in_set(None, allowed) is False


def test_timestamps_must_be_aware_ordered_and_inside_the_parent_window():
    first = parse_utc("2026-10-09T00:00:10+00:00")
    last = parse_utc("2026-10-09T00:00:20+00:00")
    assert (
        timestamp_problems("2026-10-09T00:00:10+00:00", "2026-10-09T00:00:12+00:00", (first, last))
        == []
    )
    assert timestamp_problems("2026-10-09T00:00:10", "2026-10-09T00:00:12+00:00")
    assert timestamp_problems("2026-10-09T00:00:12+00:00", "2026-10-09T00:00:10+00:00")
    assert timestamp_problems(
        "2026-10-09T00:00:00+00:00", "2026-10-09T00:00:12+00:00", (first, last)
    )
    assert timestamp_problems("nonsense", 5)


def test_guarded_turns_an_exception_into_a_failed_closed_problem():
    def explode():
        raise TypeError("unhashable")

    assert "failed closed" in guarded("x", explode)[0]


def test_a_container_valued_tier_status_is_a_recorded_problem_not_an_exception():
    plan = make_plan()
    artifact = good_artifact(plan, "source-job")
    artifact["tiers"][0]["status"] = ["PASS"]
    problems = validate_terminal_artifact(artifact, plan, "source-job", child_exit_status=0)
    assert any("invalid status" in item for item in problems)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tiers", "x"),
        ("rows", 5),
        ("evidence", 3),
        ("identity_after", []),
        ("runtime", 7),
        ("gate_problems", {}),
    ],
)
def test_structurally_wrong_valid_json_never_aborts_validation(key, value):
    plan = make_plan()
    artifact = copy.deepcopy(good_artifact(plan, "source-job"))
    artifact[key] = value
    assert validate_terminal_artifact(artifact, plan, "source-job", child_exit_status=0)


@pytest.mark.parametrize("bad", [math.inf, math.nan, 0, -1, True])
def test_plan_deadlines_must_be_finite_positive_numbers(bad):
    with pytest.raises(PlanError):
        make_plan(deadline_seconds=bad)


DEEP = "[" * 100000 + "]" * 100000


def test_runaway_nesting_is_a_contained_value_error_not_a_crash(tmp_path):
    with pytest.raises(ValueError, match="recursion|nesting"):
        loads_strict(DEEP)
    with pytest.raises(ValueError, match="size bound"):
        loads_strict(" " * (MAX_JSON_CHARS + 1))
    with pytest.raises(PlanError):
        load_plan(DEEP)
    root = tmp_path / "job"
    (root / "out").mkdir(parents=True)
    (root / "out" / "terminal.json").write_text(DEEP, encoding="utf-8")
    artifact, problems = read_terminal_artifact(root)
    assert artifact is None and problems == ["terminal artifact is malformed or truncated"]
    receipt = tmp_path / "guardian.json"
    receipt.write_text(DEEP, encoding="utf-8")
    assert read_guardian_receipt(receipt)[1]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(tiers=[["unit"], {"x": 1}]),
        lambda body: body.update(games=[{"a": 1}]),
        lambda body: body.update(required_nodeids={"unit": [["a"]]}),
        lambda body: body.update(jobs=[["x"], 5, None]),
        lambda body: body["deadlines"].update(tier_seconds={"a": 1}),
        lambda body: body.update(input_digests={"a": ["b"]}),
        lambda body: body.update(build_identity={"source": ["x"]}),
    ],
)
def test_hostile_containers_in_a_decoded_plan_are_problems_never_exceptions(mutate):
    body = json.loads(make_plan().text)
    mutate(body)
    problems = validate_plan_body(body)
    assert problems and all(isinstance(item, str) for item in problems)
