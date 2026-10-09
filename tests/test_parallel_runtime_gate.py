"""Authored controls driving REAL child subprocesses through the concurrent orchestrator (#107).

Each scenario starts actual owned child processes that write terminal artifacts (or fail to),
and the assertions read the raw terminal outcome the orchestrator observed.  The children are
authored deterministic workers: they never run a gate, an emulator or a ROM, so nothing here
is gameplay, real-ROM or performance evidence and none of it closes the full dual-runtime run.
"""

import json
import os
import sys
import threading

import pytest

from scripts.production_gate_parallel import (
    CapacityError,
    PairBudget,
    capacity_loss_probe,
    run_parallel_plan,
)
from scripts.production_gate_parallel_plan import build_parallel_plan

REQUIRED = {"unit": ["tests/test_a.py::test_one", "tests/test_a.py::test_two"], "timing": ["t::x"]}

FAKE_WORKER = r"""
import json, os, sys, time
plan_path, job_id, job_root, scenario = sys.argv[1:5]
name, _, argument = scenario.partition(":")
plan = json.load(open(plan_path))
job = next(item for item in plan["jobs"] if item["job_id"] == job_id)
mode = job["mode"]
started = time.time()
if name == "hang":
    time.sleep(120)
if name == "slow_pass":
    time.sleep(float(argument))
def counts(total):
    return {"passed": total, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0,
            "errors": 0, "total": total}
rows = [mode + "/" + t + "/" + c for t, cases in plan["required_nodeids"].items() for c in cases]
rows += [mode + "/timing/" + rep for rep in job["timing_rep_ids"]]
tiers = [
    {"name": "unit", "status": "PASS", "counts": counts(2), "returncodes": [0]},
    {"name": "timing", "status": "PASS", "counts": counts(5), "returncodes": [0] * 5},
]
artifact = {
    "schema_version": 1, "run_id": plan["run_id"], "plan_sha256": plan["plan_sha256"],
    "job_id": job_id, "mode": mode, "commit": plan["commit"],
    "build_identity": plan["build_identity"], "input_digests": plan["input_digests"],
    "asset_digest": plan["asset_digest"], "pid": os.getpid(), "child_exit_status": 0,
    "started_utc": "t0", "finished_utc": "t1", "fresh_run": True, "diagnostic_reuse": False,
    "runtime": {"pyboy_mode": mode}, "tiers": tiers, "rows": rows, "gate_problems": [],
    "cancellation": "", "overall": "PASS",
}
exit_code = 0
if name == "fail":
    tiers[0]["status"] = "FAIL"
    artifact.update(overall="FAIL", gate_problems=["unit failed"], child_exit_status=1)
    exit_code = 1
elif name == "wrong_run":
    artifact["run_id"] = "9" * 32
elif name == "wrong_head":
    artifact["commit"] = "f" * 40
elif name == "native_fallback":
    artifact["runtime"] = {"pyboy_mode": "source"}
elif name == "duplicate_tier":
    tiers.append(dict(tiers[0]))
elif name == "missing_rep":
    tiers[1]["returncodes"] = [0] * 4
elif name == "old_reuse":
    artifact.update(fresh_run=False, diagnostic_reuse=True)
root_pgid = os.getpgid(0)
info = {"pid": os.getpid(), "pgid": root_pgid, "started": started,
        "TMPDIR": os.environ.get("TMPDIR"), "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
        "HOME": os.environ.get("HOME"), "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
        "SECRET": os.environ.get("POKERED_TEST_SECRET")}
if name == "mutate_other":
    other = os.path.join(os.path.dirname(job_root), "cython-job", "out", "terminal.json")
    for _ in range(100):
        if os.path.exists(other):
            break
        time.sleep(0.05)
    time.sleep(0.5)
    with open(other, "ab") as handle:
        handle.write(b" ")
if name == "malformed":
    os.makedirs(os.path.join(job_root, "out"), exist_ok=True)
    open(os.path.join(job_root, "out", "terminal.json"), "w").write('{"run_id": ')
elif name != "lost":
    os.makedirs(os.path.join(job_root, "out"), exist_ok=True)
    open(os.path.join(job_root, "out", "terminal.json"), "w").write(json.dumps(artifact))
info["finished"] = time.time()
open(os.path.join(job_root, "child.env.json"), "w").write(json.dumps(info))
sys.exit(exit_code)
"""


def make_plan(**overrides):
    arguments = {
        "commit": "a" * 40,
        "build_identity": "build-1",
        "input_digests": {"VERSIONS.md": "b" * 64},
        "asset_digest": "c" * 64,
        "games": ("red",),
        "roles": ("listener", "connector"),
        "modes": ("source", "cython"),
        "tiers": ("unit", "timing"),
        "required_nodeids_by_tier": REQUIRED,
        "repetitions": 5,
        "deadline_seconds": 30.0,
        "total_pairs": 2,
        "policy_digest": "d" * 64,
        "early_smoke": False,
        "fail_fast": False,
    }
    arguments.update(overrides)
    return build_parallel_plan(**arguments)


def run(tmp_path, scenarios, plan=None, budget=None, **kwargs):
    plan = plan or make_plan()
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    run_root = tmp_path / "runs"
    run_root.mkdir(exist_ok=True)

    def factory(selected_plan, job_id, tree, project_root):
        mode = selected_plan.job(job_id)["mode"]
        executable = "/nonexistent/python" if scenarios[mode] == "nostart" else sys.executable
        plan_file = str(tree["root"].parent / "plan.json")
        return [executable, str(script), plan_file, job_id, str(tree["root"]), scenarios[mode]]

    environment = {"PATH": os.environ.get("PATH", ""), "POKERED_TEST_SECRET": "leak"}
    kwargs.setdefault("grace_seconds", 0.5)
    kwargs.setdefault("poll_seconds", 0.02)
    report = run_parallel_plan(
        plan,
        run_root=run_root,
        project_root=tmp_path,
        budget=budget or PairBudget(plan.as_dict()["capacity"]["total_pairs"]),
        worker_argv_factory=factory,
        base_env=environment,
        **kwargs,
    )
    return plan, run_root, report


def states(report):
    return {job["mode"]: job["state"] for job in report["jobs"]}


def child_info(run_root, plan, mode):
    path = run_root / plan.run_id / f"{mode}-job" / "child.env.json"
    return json.loads(path.read_text(encoding="utf-8"))


def group_gone(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    return False


def test_both_runtimes_pass_concurrently_in_isolated_trees_with_terminal_artifacts(tmp_path):
    plan, root, report = run(tmp_path, {"source": "slow_pass:0.6", "cython": "slow_pass:0.6"})
    assert report["overall"] == "PASS", report["problems"]
    assert states(report) == {"source": "passed", "cython": "passed"}
    infos = {mode: child_info(root, plan, mode) for mode in ("source", "cython")}
    overlap = min(i["finished"] for i in infos.values()) - max(i["started"] for i in infos.values())
    assert overlap > 0.2, "the two runtime children did not run concurrently"
    assert infos["source"]["TMPDIR"] != infos["cython"]["TMPDIR"]
    assert infos["source"]["XDG_CACHE_HOME"] != infos["cython"]["XDG_CACHE_HOME"]
    for mode, info in infos.items():
        job_root = root / plan.run_id / f"{mode}-job"
        assert info["TMPDIR"] == str(job_root / "tmp")
        assert info["HOME"] == str(job_root)
        assert info["OMP_NUM_THREADS"] == "1"
        assert info["SECRET"] is None, "an unlisted parent variable leaked into the child"
        assert info["pgid"] == info["pid"] != os.getpgid(0), "child is not its own process group"
        assert group_gone(info["pgid"])
        assert report["cleanup"][f"{mode}-job"]["tmp_removed"] is True
        assert not (job_root / "tmp").exists()
    assert report["sequential_comparison"] == {"status": "NOT_RUN"}
    assert any("real-ROM" in item for item in report["limitations"])
    declaration = report["coverage_declaration"]
    assert any("#107.5" in item and "NOT done" in item for item in declaration["exclusions"])
    assert any("opt-in" in item for item in declaration["implemented"])


@pytest.mark.parametrize("failing", ["source", "cython"])
def test_an_ordinary_failure_lets_the_other_runtime_finish_when_fail_fast_is_false(
    tmp_path, failing
):
    other = "cython" if failing == "source" else "source"
    _, _, report = run(tmp_path, {failing: "fail", other: "slow_pass:0.8"})
    assert states(report) == {failing: "failed", other: "passed"}
    assert report["overall"] == "FAIL"
    failed = next(job for job in report["jobs"] if job["mode"] == failing)
    assert failed["child_exit_status"] == 1
    assert any("child exit status 1" in item for item in failed["artifact_problems"])
    assert failed["artifact"]["gate_problems"] == ["unit failed"]
    assert report["cancellation"] == ""


@pytest.mark.parametrize("failing", ["source", "cython"])
def test_fail_fast_true_cancels_the_other_runtime_with_bounded_owned_cleanup(tmp_path, failing):
    other = "cython" if failing == "source" else "source"
    plan = make_plan(fail_fast=True)
    _, root, report = run(tmp_path, {failing: "fail", other: "hang"}, plan=plan)
    assert states(report) == {failing: "failed", other: "interrupted"}
    assert report["overall"] == "FAIL"
    assert "fail_fast" in report["cancellation"]
    cancelled = next(job for job in report["jobs"] if job["mode"] == other)
    assert cancelled["child_exit_status"] is not None
    assert cancelled["child_exit_status"] != 0
    assert report["elapsed_seconds"] < 20
    assert not (root / plan.run_id / f"{other}-job" / "tmp").exists()


def test_a_runtime_that_never_starts_is_a_retained_failure_not_a_pass(tmp_path):
    _, _, report = run(tmp_path, {"source": "nostart", "cython": "slow_pass:0.3"})
    assert states(report) == {"source": "failed", "cython": "passed"}
    assert report["overall"] == "FAIL"
    source = next(job for job in report["jobs"] if job["mode"] == "source")
    assert "could not start" in source["reason"]


def test_fail_fast_true_marks_the_other_runtime_unstarted_or_interrupted_when_one_cannot_start(
    tmp_path,
):
    plan = make_plan(fail_fast=True)
    _, _, report = run(tmp_path, {"source": "nostart", "cython": "hang"}, plan=plan)
    assert states(report)["source"] == "failed"
    assert states(report)["cython"] in {"not_started", "interrupted"}
    assert report["overall"] == "FAIL"


@pytest.mark.parametrize(
    ("scenario", "needle"),
    [
        ("lost", "missing"),
        ("malformed", "malformed or truncated"),
        ("wrong_run", "run_id"),
        ("wrong_head", "commit"),
        ("duplicate_tier", "duplicate tier"),
        ("missing_rep", "timing repetitions"),
        ("old_reuse", "diagnostic reuse"),
    ],
)
def test_a_child_that_exits_zero_cannot_pass_with_a_bad_terminal_artifact(
    tmp_path, scenario, needle
):
    _, _, report = run(tmp_path, {"source": scenario, "cython": "pass"})
    assert report["overall"] == "FAIL"
    assert states(report) == {"source": "failed", "cython": "passed"}
    source = next(job for job in report["jobs"] if job["mode"] == "source")
    assert source["child_exit_status"] == 0
    assert any(needle in item for item in source["artifact_problems"]), source["artifact_problems"]


def test_a_native_job_reporting_the_source_runtime_is_rejected_as_a_fallback(tmp_path):
    _, _, report = run(tmp_path, {"source": "pass", "cython": "native_fallback"})
    cython = next(job for job in report["jobs"] if job["mode"] == "cython")
    assert report["overall"] == "FAIL"
    assert cython["state"] == "failed"
    assert any("source fallback in native" in item for item in cython["artifact_problems"])


def test_a_runtime_past_its_deadline_is_terminated_and_reported_as_a_timeout(tmp_path):
    plan = make_plan(deadline_seconds=1.0)
    _, root, report = run(tmp_path, {"source": "hang", "cython": "pass"}, plan=plan)
    assert states(report) == {"source": "timeout", "cython": "passed"}
    assert report["overall"] == "FAIL"
    assert report["elapsed_seconds"] < 15
    assert not (root / plan.run_id / "source-job" / "tmp").exists()


def test_an_external_interrupt_cancels_every_owned_child_without_touching_other_processes(
    tmp_path,
):
    interrupt = threading.Event()
    timer = threading.Timer(0.8, interrupt.set)
    timer.start()
    try:
        _, _, report = run(tmp_path, {"source": "hang", "cython": "hang"}, interrupt=interrupt)
    finally:
        timer.cancel()
    assert states(report) == {"source": "interrupted", "cython": "interrupted"}
    assert report["cancellation"] == "external interrupt"
    assert report["overall"] == "FAIL"
    assert all(job["child_exit_status"] not in (0, None) for job in report["jobs"])
    assert report["elapsed_seconds"] < 15


@pytest.mark.parametrize("probe_value", [1, 0, None, -1, "unknown"])
def test_capacity_loss_or_an_unknown_quota_cancels_running_jobs_and_marks_the_budget_lost(
    tmp_path, probe_value
):
    budget = PairBudget(2)
    _, _, report = run(
        tmp_path,
        {"source": "hang", "cython": "hang"},
        budget=budget,
        capacity_probe=lambda: probe_value,
    )
    assert states(report) == {"source": "interrupted", "cython": "interrupted"}
    assert "capacity loss" in report["cancellation"]
    assert budget.lost_reason
    assert report["overall"] == "FAIL"
    assert budget.reserved_total == 0


def test_parallel_dispatch_is_refused_before_any_process_when_the_budget_disagrees(tmp_path):
    plan = make_plan(total_pairs=2)
    _, root, report = run(
        tmp_path, {"source": "pass", "cython": "pass"}, plan=plan, budget=PairBudget(4)
    )
    assert report["overall"] == "FAIL"
    assert states(report) == {"source": "not_started", "cython": "not_started"}
    assert any("differs from the frozen plan" in item for item in report["orchestration_problems"])
    assert not (root / plan.run_id / "source-job").exists()


def test_a_job_that_mutates_another_jobs_output_after_it_finished_fails_the_aggregate(tmp_path):
    _, _, report = run(tmp_path, {"source": "mutate_other", "cython": "pass"})
    assert states(report) == {"source": "passed", "cython": "passed"}
    assert report["overall"] == "FAIL"
    assert any("cross-job mutation" in item for item in report["orchestration_problems"])


def test_the_single_global_budget_rejects_unknown_zero_duplicate_and_excess_reservations():
    for bad in (None, 0, -1, True, "2"):
        with pytest.raises(CapacityError):
            PairBudget(bad)
    budget = PairBudget(3)
    assert budget.reserve("a", 2) == 2
    with pytest.raises(CapacityError, match="duplicate"):
        budget.reserve("a", 1)
    with pytest.raises(CapacityError, match="exceeds"):
        budget.reserve("b", 2)
    with pytest.raises(CapacityError):
        budget.reserve("b", 0)
    assert budget.reserve("b", 1) == 1
    assert budget.reserved_total == 3
    budget.release("a")
    assert budget.reserved_total == 1
    budget.mark_lost("probe reported fewer pairs")
    with pytest.raises(CapacityError, match="capacity lost"):
        budget.reserve("c", 1)


def test_the_capacity_probe_reports_loss_only_when_the_baseline_floor_is_breached():
    readings = iter([6, 6, 4, 3])
    probe = capacity_loss_probe(4, measure=lambda: next(readings))  # baseline 6 -> floor 4
    assert [probe(), probe(), probe()] == [4, 4, 3]


def test_the_capacity_probe_floor_never_exceeds_the_reserved_pairs_and_unknown_quota_is_loss():
    readings = iter([2, 2, 1])
    probe = capacity_loss_probe(4, measure=lambda: next(readings))  # baseline 2 -> floor 2
    assert [probe(), probe()] == [4, 1]

    def unreadable():
        raise OSError("cgroup unreadable")

    assert capacity_loss_probe(4, measure=unreadable)() is None
