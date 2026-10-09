"""Authored controls for strict terminal-artifact validation and aggregation (#107.3, #107.6).

Fabricated artifacts exercise the validator's refusals.  They are authored fixtures,
never gameplay or performance evidence, and a PASS here proves only that the validator
accepts a fully consistent artifact and rejects each listed corruption.
"""

import copy
import json
import os

import pytest

from scripts.production_gate_parallel_artifacts import (
    aggregate_outcomes,
    outcome_record,
    read_terminal_artifact,
    validate_terminal_artifact,
)
from scripts.production_gate_parallel_plan import PlanError
from tests._parallel_runtime_report_support import counts, good_artifact, make_plan


def problems_for(plan, artifact, job_id="source-job", **kwargs):
    kwargs.setdefault("child_exit_status", 0)
    return validate_terminal_artifact(artifact, plan, job_id, **kwargs)


def test_a_fully_consistent_artifact_for_each_runtime_validates_cleanly():
    plan = make_plan()
    assert problems_for(plan, good_artifact(plan, "source-job"), child_pid=4242) == []
    assert problems_for(plan, good_artifact(plan, "cython-job"), "cython-job") == []


def set_key(key, value):
    def mutate(artifact):
        artifact[key] = value

    return mutate


def set_tier(index, **changes):
    def mutate(artifact):
        artifact["tiers"][index].update(changes)

    return mutate


@pytest.mark.parametrize(
    ("label", "mutate", "needle"),
    [
        ("stale run id", set_key("run_id", "9" * 32), "run_id"),
        ("wrong plan digest", set_key("plan_sha256", "9" * 64), "plan_sha256"),
        ("wrong head", set_key("commit", "f" * 40), "commit"),
        ("wrong build", set_key("build_identity", "other"), "build_identity"),
        ("changed input", set_key("input_digests", {"VERSIONS.md": "e" * 64}), "input_digests"),
        ("changed fixture assets", set_key("asset_digest", "e" * 64), "asset_digest"),
        ("wrong job", set_key("job_id", "cython-job"), "job or runtime"),
        ("wrong mode", set_key("mode", "cython"), "job or runtime"),
        ("missing runtime identity", set_key("runtime", {}), "source fallback"),
        ("diagnostic reuse", set_key("diagnostic_reuse", True), "diagnostic reuse"),
        ("not a fresh run", set_key("fresh_run", False), "diagnostic reuse"),
        ("gate problems", set_key("gate_problems", ["collection failed"]), "gate problems"),
        ("cancellation text", set_key("cancellation", "interrupt"), "cancelled"),
        ("overall fail", set_key("overall", "FAIL"), "overall"),
        ("wrong pid", set_key("pid", 1), "pid"),
        ("non-integer exit", set_key("child_exit_status", "0"), "exit status"),
        ("missing tier", lambda a: a["tiers"].pop(), "planned order"),
        ("tier out of order", lambda a: a["tiers"].reverse(), "planned order"),
        (
            "duplicate tier",
            lambda a: a["tiers"].append(copy.deepcopy(a["tiers"][0])),
            "duplicate tier",
        ),
        ("invalid status", set_tier(0, status="MAYBE"), "invalid status"),
        ("failed tier", set_tier(0, status="FAIL"), "status FAIL"),
        (
            "pass with a skip",
            set_tier(0, counts={**counts(2), "skipped": 1, "passed": 1}),
            "skipped",
        ),
        (
            "pass with xfail",
            set_tier(0, counts={**counts(2), "xfailed": 1, "passed": 1}),
            "xfailed",
        ),
        ("pass with error", set_tier(0, counts={**counts(2), "errors": 1, "passed": 1}), "errors"),
        (
            "pass with failure",
            set_tier(0, counts={**counts(2), "failed": 1, "passed": 1}),
            "failed",
        ),
        ("pass with zero total", set_tier(0, counts=counts(0)), "PASS with"),
        ("pass without counts", set_tier(0, counts=None), "integer counts"),
        ("timing with a failing repetition", set_tier(1, returncodes=[0, 0, 1, 0, 0]), "non-zero"),
        (
            "timing missing the fifth repetition",
            set_tier(1, returncodes=[0] * 4),
            "timing repetitions",
        ),
        ("missing returncodes", set_tier(0, returncodes=None), "returncodes"),
        ("missing required row", lambda a: a["rows"].pop(0), "missing required rows"),
        ("duplicate row", lambda a: a["rows"].append(a["rows"][0]), "duplicate row"),
        (
            "row beyond the tier total",
            lambda a: a["rows"].append(
                {**a["rows"][0], "id": "source/unit/tests/other.py::x", "case": "tests/other.py::x"}
            ),
            "rows but the tier reports",
        ),
        (
            "row from another runtime",
            lambda a: a["rows"].append({**a["rows"][0], "runtime": "cython"}),
            "different runtime",
        ),
        ("missing timing repetition row", lambda a: a["rows"].pop(), "timing repetition"),
        ("counts-only artifact", set_key("rows", []), "non-empty list"),
        ("string rows", set_key("rows", ["source/unit/x"]), "structured row record"),
        ("unknown key", set_key("extra", 1), "keys differ"),
        ("missing key", lambda a: a.pop("tiers"), "keys differ"),
    ],
)
def test_each_corruption_is_rejected_with_a_specific_reason(label, mutate, needle):
    plan = make_plan()
    artifact = good_artifact(plan, "source-job")
    mutate(artifact)
    found = problems_for(plan, artifact, child_pid=4242)
    assert found, f"{label}: a corrupted artifact must not validate"
    assert any(needle in item for item in found), f"{label}: expected {needle!r} in {found}"


def test_a_native_job_that_ran_the_source_runtime_is_rejected_as_a_source_fallback():
    plan = make_plan()
    artifact = good_artifact(plan, "cython-job")
    artifact["runtime"] = {"pyboy_mode": "source"}
    found = problems_for(plan, artifact, "cython-job")
    assert any("source fallback in native" in item for item in found)
    assert problems_for(plan, good_artifact(plan, "cython-job"), "cython-job") == []


def test_the_observed_child_exit_status_must_be_zero_and_agree_with_the_artifact():
    plan = make_plan()
    artifact = good_artifact(plan, "source-job")
    assert any("exit status" in p for p in problems_for(plan, artifact, child_exit_status=None))
    assert any("disagrees" in p for p in problems_for(plan, artifact, child_exit_status=3))
    artifact["child_exit_status"] = 3
    assert any(
        "child exit status 3" in p for p in problems_for(plan, artifact, child_exit_status=3)
    )


def test_totals_alone_cannot_authorise_a_pass_when_row_identity_is_required():
    plan = make_plan()
    artifact = good_artifact(plan, "source-job")
    artifact["rows"] = []
    found = problems_for(plan, artifact)
    assert any("non-empty list" in item for item in found)
    with pytest.raises(PlanError, match="require_row_ids"):
        make_plan(require_row_ids=False)


def write_artifact(root, body, name="terminal.json"):
    (root / "out").mkdir(parents=True, exist_ok=True)
    path = root / "out" / name
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


def test_the_reader_refuses_missing_malformed_truncated_and_non_object_artifacts(tmp_path):
    assert read_terminal_artifact(tmp_path)[0] is None
    assert read_terminal_artifact(tmp_path)[1] == ["terminal artifact is missing"]
    write_artifact(tmp_path, '{"run_id": "x"')
    assert read_terminal_artifact(tmp_path)[1] == ["terminal artifact is malformed or truncated"]
    write_artifact(tmp_path, "[1, 2]")
    assert read_terminal_artifact(tmp_path)[1] == ["terminal artifact is not a JSON object"]
    write_artifact(tmp_path, {"ok": True})
    assert read_terminal_artifact(tmp_path) == ({"ok": True}, [])


def test_the_reader_refuses_symlinked_and_escaping_artifact_paths(tmp_path):
    job = tmp_path / "job"
    outside = tmp_path / "outside.json"
    outside.write_text('{"ok": true}', encoding="utf-8")
    (job / "out").mkdir(parents=True)
    os.symlink(outside, job / "out" / "terminal.json")
    assert read_terminal_artifact(job) == (None, ["terminal artifact is a symlink"])
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "terminal.json").write_text('{"ok": true}', encoding="utf-8")
    os.symlink(other, job / "linked")
    body, found = read_terminal_artifact(job, "linked/terminal.json")
    assert body is None
    assert found == ["terminal artifact escapes the owned job root"]


def outcomes_for(plan, states):
    records = []
    for job_id, state in states.items():
        artifact = good_artifact(plan, job_id) if state == "passed" else None
        records.append(
            outcome_record(
                plan,
                job_id,
                state,
                child_exit_status=0 if state == "passed" else 1,
                artifact=artifact,
                artifact_problems=[] if state == "passed" else [f"{state} for the test"],
            )
        )
    return records


def test_aggregate_passes_only_when_every_planned_job_is_a_valid_pass():
    plan = make_plan()
    report = aggregate_outcomes(
        plan, outcomes_for(plan, {"source-job": "passed", "cython-job": "passed"})
    )
    assert report["overall"] == "PASS"
    assert report["problems"] == []
    assert [job["job_id"] for job in report["jobs"]] == ["source-job", "cython-job"]


@pytest.mark.parametrize(
    "failed_state", ["failed", "timeout", "interrupted", "not_started", "invalid_artifact"]
)
def test_a_successful_runtime_cannot_mask_the_other_runtimes_failure(failed_state):
    plan = make_plan()
    report = aggregate_outcomes(
        plan, outcomes_for(plan, {"source-job": "passed", "cython-job": failed_state})
    )
    assert report["overall"] == "FAIL"
    assert any(failed_state in item for item in report["problems"])
    assert {job["job_id"]: job["state"] for job in report["jobs"]}["source-job"] == "passed"


def test_aggregate_rejects_missing_duplicate_unknown_and_tampered_outcomes():
    plan = make_plan()
    both = outcomes_for(plan, {"source-job": "passed", "cython-job": "passed"})
    assert any("missing outcome" in p for p in aggregate_outcomes(plan, both[:1])["problems"])
    assert any(
        "duplicate outcome" in p for p in aggregate_outcomes(plan, [*both, both[0]])["problems"]
    )
    foreign = dict(both[0], job_id="third-job")
    assert any(
        "not in the plan" in p for p in aggregate_outcomes(plan, [*both, foreign])["problems"]
    )
    tampered = copy.deepcopy(both)
    tampered[1]["artifact"]["commit"] = "f" * 40
    report = aggregate_outcomes(plan, tampered)
    assert report["overall"] == "FAIL"
    assert any("re-validation failed" in p for p in report["problems"])
