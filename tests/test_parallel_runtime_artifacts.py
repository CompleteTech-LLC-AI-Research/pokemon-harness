"""Authored controls for strict terminal-artifact validation and aggregation (#107.3, #107.6).

Fabricated artifacts exercise the validator's refusals.  They are authored fixtures,
never gameplay or performance evidence, and a PASS here proves only that the validator
accepts a fully consistent artifact and rejects each listed corruption.
"""

import copy
import hashlib
import json
import os

import pytest

from scripts.production_gate_parallel_artifacts import (
    aggregate_outcomes,
    collect_partial_evidence,
    outcome_record,
    parse_partial_progress,
    read_terminal_artifact,
    validate_terminal_artifact,
    verify_gate_report,
)
from scripts.production_gate_parallel_plan import PlanError
from tests._parallel_runtime_report_support import counts, good_artifact, make_plan, report_bytes


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


def write_report_for(tmp_path, plan, job_id, blob=None):
    root = tmp_path / job_id
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "gate-report.json").write_bytes(
        blob if blob is not None else report_bytes(plan, job_id)
    )
    return root


def test_the_parent_re_derives_tiers_rows_and_verdict_from_the_report_bytes(tmp_path):
    plan = make_plan()
    artifact = good_artifact(plan, "source-job")
    root = write_report_for(tmp_path, plan, "source-job")
    problems, report = verify_gate_report(root, artifact, plan.as_dict(), plan.job("source-job"))
    assert problems == [] and report["overall"] == "PASS" and report["capacity"] == {"status": "ok"}


def test_tampered_missing_or_divergent_reports_are_refused(tmp_path):
    plan = make_plan()
    body, job = plan.as_dict(), plan.job("source-job")
    artifact = good_artifact(plan, "source-job")
    genuine = report_bytes(plan, "source-job")
    # bytes that differ from the referenced digest
    root = write_report_for(tmp_path / "a", plan, "source-job", genuine + b" ")
    assert any("bytes differ" in p for p in verify_gate_report(root, artifact, body, job)[0])
    # no report at all
    assert any(
        "missing" in p for p in verify_gate_report(tmp_path / "none", artifact, body, job)[0]
    )
    # a report that disagrees with the artifact, even with a consistent digest
    report = json.loads(genuine)
    report["tiers"][0]["status"] = "FAIL"
    blob = json.dumps(report, sort_keys=True).encode()
    liar = copy.deepcopy(artifact)
    liar["evidence"] = {
        "gate_report_bytes": len(blob),
        "gate_report_sha256": hashlib.sha256(blob).hexdigest(),
    }
    root = write_report_for(tmp_path / "b", plan, "source-job", blob)
    assert any("tiers differ" in p for p in verify_gate_report(root, liar, body, job)[0])


def test_a_producer_that_did_not_propagate_terminal_records_cannot_pass(tmp_path):
    plan = make_plan()
    body, job = plan.as_dict(), plan.job("source-job")
    report = json.loads(report_bytes(plan, "source-job"))
    for tier in report["tiers"]:
        tier.pop("test_records")
    blob = json.dumps(report, sort_keys=True).encode()
    artifact = good_artifact(plan, "source-job")
    artifact["evidence"] = {
        "gate_report_bytes": len(blob),
        "gate_report_sha256": hashlib.sha256(blob).hexdigest(),
    }
    root = write_report_for(tmp_path, plan, "source-job", blob)
    assert any("did not propagate" in p for p in verify_gate_report(root, artifact, body, job)[0])


def test_the_collected_selection_must_equal_the_frozen_plan_selection(tmp_path):
    plan = make_plan()
    body, job = plan.as_dict(), plan.job("source-job")
    report = json.loads(report_bytes(plan, "source-job"))
    report["tiers"][0]["selected_nodeids"] = [
        *report["tiers"][0]["selected_nodeids"],
        "tests/test_a.py::test_extra",
    ]
    blob = json.dumps(report, sort_keys=True).encode()
    artifact = good_artifact(plan, "source-job")
    artifact["evidence"] = {
        "gate_report_bytes": len(blob),
        "gate_report_sha256": hashlib.sha256(blob).hexdigest(),
    }
    root = write_report_for(tmp_path, plan, "source-job", blob)
    problems = verify_gate_report(root, artifact, body, job)[0]
    assert any("exact collected selection" in p or "frozen plan selection" in p for p in problems)


def test_partial_evidence_of_an_unfinished_job_is_digested_not_deleted(tmp_path):
    tree = {name: tmp_path / name for name in ("out", "evidence", "raw", "tmp")}
    for path in tree.values():
        path.mkdir()
    (tree["raw"] / "progress.json").write_text('{"done": 3}', encoding="utf-8")
    (tree["tmp"] / "pytest.log").write_text("partial", encoding="utf-8")
    found = collect_partial_evidence(tree)
    paths = {entry["path"]: entry for entry in found["files"]}
    assert set(paths) == {"raw/progress.json", "tmp/pytest.log"} and not found["truncated"]
    assert paths["raw/progress.json"]["sha256"] == hashlib.sha256(b'{"done": 3}').hexdigest()
    assert (tree["raw"] / "progress.json").exists() and (tree["tmp"] / "pytest.log").exists()


def _progress_tree(tmp_path, report_text=None):
    root = tmp_path / "job"
    (root / "evidence").mkdir(parents=True)
    (root / "child.stdout").write_bytes(b"x" * 5000 + b"last line")
    if report_text is not None:
        (root / "evidence" / "gate-report.json").write_text(report_text, encoding="utf-8")
    return {"root": root, "evidence": root / "evidence"}


def test_partial_progress_is_parsed_into_structured_counts_and_never_a_terminal_verdict(tmp_path):
    import json

    report = {
        "tiers": [
            {
                "name": "unit",
                "status": "PASS",
                "counts": {"passed": 3, "failed": 0, "total": 3, "bogus": 9},
                "test_records": [{"iteration": 1}, {"iteration": 2}],
                "errors": ["boom"],
            },
            {"name": "timing", "status": "NOT-A-STATUS", "counts": "garbage"},
            "not-an-object",
        ]
    }
    progress = parse_partial_progress(_progress_tree(tmp_path, json.dumps(report)))
    assert progress["terminal"] is False and progress["parse_error"] == ""
    first, second = progress["tiers"]
    assert (
        first["counts"] == {"passed": 3, "failed": 0, "total": 3} and first["last_iteration"] == 2
    )
    assert first["claimed_status"] == "PASS" and first["errors"] == ["boom"]
    assert second["claimed_status"] == "UNKNOWN" and second["counts"] == {}
    assert progress["output_tail"].endswith("last line") and len(progress["output_tail"]) <= 2000


def test_unparseable_partial_progress_is_an_explicit_error_not_an_exception(tmp_path):
    progress = parse_partial_progress(_progress_tree(tmp_path, '{"tiers": [NaN'))
    assert progress["terminal"] is False and progress["parse_error"] and progress["tiers"] == []
    assert parse_partial_progress(_progress_tree(tmp_path / "other"))["source"] is None


def _report_with(tmp_path, mutate):
    plan = make_plan()
    report = json.loads(report_bytes(plan, "source-job"))
    mutate(report)
    blob = json.dumps(report, sort_keys=True).encode()
    artifact = good_artifact(plan, "source-job")
    artifact["evidence"] = {
        "gate_report_bytes": len(blob),
        "gate_report_sha256": hashlib.sha256(blob).hexdigest(),
    }
    root = write_report_for(tmp_path, plan, "source-job", blob)
    return root, artifact, plan.as_dict(), plan.job("source-job")


MALFORMED = {
    "selection_is_an_int": lambda r: r["tiers"][0].update(selected_nodeids=5),
    "selection_holds_a_list": lambda r: r["tiers"][0].update(selected_nodeids=[["x"]]),
    "iteration_is_unhashable": lambda r: r["tiers"][0].update(
        test_records=[{"iteration": [1], "nodeid": "a"}]
    ),
    "nodeid_is_unhashable": lambda r: r["tiers"][0].update(
        test_records=[{"iteration": 1, "nodeid": ["a"]}]
    ),
    "record_is_not_an_object": lambda r: r["tiers"][0].update(test_records=["not-an-object"]),
    "name_is_a_list": lambda r: r["tiers"][0].update(name=["unit"]),
    "counts_is_a_string": lambda r: r["tiers"][0].update(counts="garbage"),
    "tiers_is_a_number": lambda r: r.update(tiers=7),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_nested_report_values_are_a_terminal_rejection_never_an_exception(tmp_path, case):
    root, artifact, body, job = _report_with(tmp_path, MALFORMED[case])
    problems, _report = verify_gate_report(root, artifact, body, job)
    assert problems, f"{case} must be rejected with a problem, not accepted and not raised"


def test_the_worker_keeps_the_earlier_failures_when_nested_values_are_malformed():
    from scripts.production_gate_parallel_worker import build_artifact

    plan = make_plan()
    artifact = build_artifact(
        plan_body=plan.as_dict(),
        plan_digest=plan.digest,
        job=plan.job("source-job"),
        pid=1,
        started="2026-10-09T00:00:00+00:00",
        exit_status=1,
        report={
            "overall": "PASS",
            "tiers": [
                {
                    "name": "unit",
                    "status": "FAIL",
                    "counts": "garbage",
                    "selected_nodeids": 5,
                    "test_records": [{"iteration": [1]}],
                },
                "not-an-object",
            ],
            "gate_problems": ["an earlier failure"],
        },
        problem="",
    )
    assert artifact["overall"] == "FAIL", "malformed values never become PASS"
    assert "an earlier failure" in artifact["gate_problems"]
    assert artifact["tiers"][0]["status"] == "FAIL", "the earlier failure status is retained"


def test_a_non_list_tiers_value_in_the_worker_is_recorded_and_fails():
    from scripts.production_gate_parallel_worker import build_artifact

    plan = make_plan()
    artifact = build_artifact(
        plan_body=plan.as_dict(),
        plan_digest=plan.digest,
        job=plan.job("source-job"),
        pid=1,
        started="2026-10-09T00:00:00+00:00",
        exit_status=0,
        report={"overall": "PASS", "tiers": 7},
        problem="",
    )
    assert artifact["overall"] == "FAIL" and any(
        "not a list" in p for p in artifact["gate_problems"]
    )


def test_one_jobs_failing_aggregation_becomes_a_failed_outcome_and_aborts_nothing():
    from scripts.production_gate_parallel_artifacts import total_outcome

    plan = make_plan()
    outcome = total_outcome(
        plan,
        "source-job",
        lambda: {}["missing"],
        exit_status=0,
        launched_utc="2026-10-09T00:00:00+00:00",
        finished_utc="2026-10-09T00:00:01+00:00",
        token="tok",
    )
    assert outcome["state"] == "failed" and outcome["child_exit_status"] == 0
    assert any("failed closed" in p for p in outcome["artifact_problems"])
    assert total_outcome(
        plan,
        "source-job",
        lambda: {"state": "passed"},
        exit_status=0,
        launched_utc=None,
        finished_utc=None,
        token="t",
    ) == {"state": "passed"}
