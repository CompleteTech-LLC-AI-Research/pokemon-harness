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
import time

import pytest

from scripts.production_gate_parallel import (
    CapacityError,
    PairBudget,
    run_parallel_plan,
)
from tests._parallel_runtime_report_support import make_plan

FAKE_WORKER = r"""
import json, os, sys, time
plan_path, job_id, job_root, scenario = sys.argv[1:5]
name, _, argument = scenario.partition(":")
plan = json.load(open(plan_path))
job = next(item for item in plan["jobs"] if item["job_id"] == job_id)
mode = job["mode"]
started = time.time()
if name == "kill_guardian":
    import signal as _signal
    os.kill(os.getppid(), _signal.SIGKILL)
    time.sleep(0.3)
if name == "hang":
    time.sleep(120)
if name == "slow_pass":
    time.sleep(float(argument))
import datetime, hashlib
def counts(total):
    return {"passed": total, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0,
            "errors": 0, "total": total}
def row(tier, case, repetition):
    suffix = "@%d" % repetition if repetition else ""
    return {"id": mode + "/" + tier + "/" + case + suffix, "runtime": mode, "tier": tier,
            "case": case, "games": [], "roles": [], "repetition": repetition,
            "outcome": "passed", "source": "pytest_records"}
report_tiers, rows = [], []
for tier_name in job["tiers"]:
    cases = plan["required_nodeids"].get(tier_name) or ["tests/test_%s.py::test_case" % tier_name]
    repeats = len(job["timing_rep_ids"]) if tier_name == "timing" else 1
    records = [{"iteration": it, "nodeid": c, "outcome": "passed", "when": "call", "was_xfail": False}
               for it in range(1, repeats + 1) for c in cases]
    report_tiers.append({"name": tier_name, "status": "PASS", "counts": counts(len(cases) * repeats),
                         "returncodes": [0] * repeats, "selected_nodeids": list(cases),
                         "test_records": records})
    rows += [row(tier_name, r["nodeid"], r["iteration"] if tier_name == "timing" else 0) for r in records]
tiers = [{k: t[k] for k in ("name", "status", "counts", "returncodes")} for t in report_tiers]
report = {"overall": "PASS", "runtime": {"pyboy_mode": mode}, "tiers": report_tiers,
          "gate_problems": [], "capacity": {"status": "ok"}}
now = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
begun = now()
identity = {"commit": plan["commit"], "clean_head": True,
            "build_identity": plan["build_identity"][mode],
            "input_digests": plan["input_digests"], "asset_digest": plan["asset_digest"]}
artifact = {
    "schema_version": 2, "run_id": plan["run_id"], "plan_sha256": plan["plan_sha256"],
    "job_id": job_id, "mode": mode, "commit": identity["commit"], "clean_head": True,
    "build_identity": identity["build_identity"], "input_digests": identity["input_digests"],
    "asset_digest": identity["asset_digest"], "identity_after": dict(identity),
    "launch_token": os.environ.get("POKERED_PARALLEL_JOB_TOKEN", ""),
    "evidence": {"gate_report_bytes": 0, "gate_report_sha256": "0" * 64},
    "pid": os.getpid(), "child_exit_status": 0, "started_utc": begun, "finished_utc": begun,
    "fresh_run": True, "diagnostic_reuse": False,
    "runtime": {"pyboy_mode": mode}, "tiers": tiers, "rows": rows, "gate_problems": [],
    "cancellation": "", "overall": "PASS",
}
exit_code = 0
if name == "fail":
    report_tiers[0]["status"] = "FAIL"
    report["overall"] = "FAIL"
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
info = {"pid": os.getpid(), "pgid": root_pgid, "ppid": os.getppid(), "sid": os.getsid(0), "started": started,
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
    blob = json.dumps(report, sort_keys=True).encode()
    os.makedirs(os.path.join(job_root, "evidence"), exist_ok=True)
    open(os.path.join(job_root, "evidence", "gate-report.json"), "wb").write(blob)
    artifact["evidence"] = {"gate_report_bytes": len(blob), "gate_report_sha256": hashlib.sha256(blob).hexdigest()}
    artifact["finished_utc"] = now()
    os.makedirs(os.path.join(job_root, "out"), exist_ok=True)
    open(os.path.join(job_root, "out", "terminal.json"), "w").write(json.dumps(artifact))
info["finished"] = time.time()
open(os.path.join(job_root, "child.env.json"), "w").write(json.dumps(info))
sys.exit(exit_code)
"""


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
        guardian = json.loads((job_root / "guardian.json").read_text(encoding="utf-8"))
        assert guardian["phase"] == "final" and guardian["proof"] is True and guardian["mac"]
        assert guardian["worker_pid"] == info["pid"] and info["ppid"] == guardian["guardian_pid"]
        assert guardian["worker_pgid"] == info["pgid"] == info["sid"], (
            "worker is not its own session"
        )
        assert (
            guardian["guardian_pgid"] == guardian["guardian_pid"] != info["pgid"] != os.getpgid(0)
        )
        assert guardian["guardian_pgid"] != os.getpgid(0), "the guardian shares the caller's group"
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
    readings = iter([8, 8])  # the two admissions see full capacity; every later poll sees the loss
    _, _, report = run(
        tmp_path,
        {"source": "hang", "cython": "hang"},
        budget=budget,
        capacity_probe=lambda: next(readings, probe_value),
    )
    assert states(report) == {"source": "interrupted", "cython": "interrupted"}
    assert "capacity loss" in report["cancellation"]
    assert budget.lost_reason
    assert report["overall"] == "FAIL"
    assert budget.reserved_total == 0


@pytest.mark.parametrize("probe_value", [1, 0, None])
def test_insufficient_or_unknown_initial_capacity_refuses_dispatch_before_any_process(
    tmp_path, probe_value
):
    budget = PairBudget(2)
    plan, root, report = run(
        tmp_path,
        {"source": "pass", "cython": "pass"},
        budget=budget,
        capacity_probe=lambda: probe_value,
    )
    assert states(report) == {"source": "not_started", "cython": "not_started"}
    assert any("refused before dispatch" in job["reason"] for job in report["jobs"])
    assert report["overall"] == "FAIL" and budget.reserved_total == 0
    assert not (root / plan.run_id / "source-job").exists()


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


def job_states(report):
    return {job["job_id"]: job["state"] for job in report["jobs"]}


def test_simultaneous_failures_are_both_harvested_before_fail_fast_cancels_anything(tmp_path):
    plan = make_plan(fail_fast=True)
    waits = []

    def late_sleeper(seconds):  # first poll happens only after both children have finished
        waits.append(seconds)
        time.sleep(1.5 if len(waits) == 1 else seconds)

    _, _, report = run(
        tmp_path, {"source": "fail", "cython": "fail"}, plan=plan, sleeper=late_sleeper
    )
    assert states(report) == {"source": "failed", "cython": "failed"}
    assert all(job["artifact"] is not None for job in report["jobs"]), "a failure was discarded"
    assert all(job["child_exit_status"] == 1 for job in report["jobs"])
    assert "fail_fast" in report["cancellation"] and report["overall"] == "FAIL"


def test_both_smokes_finish_before_selected_jobs_and_a_failed_smoke_never_cancels_its_sibling(
    tmp_path,
):
    plan = make_plan(fail_fast=True, early_smoke=True)
    _, root, report = run(tmp_path, {"source": "fail", "cython": "slow_pass:0.6"}, plan=plan)
    assert job_states(report) == {
        "source-smoke-job": "failed",
        "cython-smoke-job": "passed",
        "source-job": "not_started",
        "cython-job": "not_started",
    }
    assert "smoke barrier" in report["cancellation"] and report["overall"] == "FAIL"
    assert not (root / plan.run_id / "source-job").exists()
    smoke = next(job for job in report["jobs"] if job["job_id"] == "cython-smoke-job")
    assert [tier["name"] for tier in smoke["artifact"]["tiers"]] == ["smoke"]


def test_with_the_early_smoke_all_four_jobs_run_in_phase_order_and_are_retained(tmp_path):
    plan = make_plan(fail_fast=False, early_smoke=True)
    _, _, report = run(tmp_path, {"source": "pass", "cython": "pass"}, plan=plan)
    assert job_states(report) == {
        "source-smoke-job": "passed",
        "cython-smoke-job": "passed",
        "source-job": "passed",
        "cython-job": "passed",
    }
    assert [job["job_id"] for job in report["jobs"]] == [
        "source-smoke-job",
        "cython-smoke-job",
        "source-job",
        "cython-job",
    ]


def test_a_guardian_killed_from_outside_keeps_the_reservation_and_fails_the_run(tmp_path):
    budget = PairBudget(2)
    _, _, report = run(tmp_path, {"source": "kill_guardian", "cython": "pass"}, budget=budget)
    assert states(report) == {"source": "failed", "cython": "passed"}
    assert budget.reserved_total == 1, "a reservation was released without a proven cleanup"
    assert any("cleanup unproven" in item for item in report["orchestration_problems"])
    assert report["overall"] == "FAIL"


def test_failed_and_interrupted_jobs_keep_their_temp_trees_and_recover_partial_evidence(tmp_path):
    readings = iter([8, 8])  # admissions see full capacity; the first poll sees the loss
    plan, root, report = run(
        tmp_path,
        {"source": "hang", "cython": "fail"},
        capacity_probe=lambda: next(readings, 0),
    )
    for job in report["jobs"]:
        tree = root / plan.run_id / f"{job['mode']}-job"
        assert (tree / "tmp").exists(), "evidence in a job that did not pass must be kept"
        if job["state"] == "interrupted":
            evidence = job["partial_evidence"]
            assert evidence is not None and isinstance(evidence["files"], list)
            assert job["launched_utc"] and job["finished_utc"], "parent-observed times are retained"
    assert report["cleanup"]["source-job"]["tmp_removed"] is False
