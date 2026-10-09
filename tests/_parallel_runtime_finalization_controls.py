"""Actual finalizer/worker producer with controlled time; no subprocess or kernel execution."""

import __future__

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from scripts import production_gate_parallel_finishing as finishing
from scripts import production_gate_parallel_worker as worker
from scripts.production_gate_parallel_artifacts import TERMINAL_ARTIFACT_NAME
from scripts.production_gate_parallel_identity import expected_identity
from tests._parallel_runtime_holder_kernel import Clock
from tests._parallel_runtime_report_support import gate_tiers, make_plan


def finished_deadline_case(tmp_path, monkeypatch, point):
    """Exercise the actual finalizer; unrelated content validators are controlled."""

    plan, clock = make_plan(deadline_seconds=1.0), Clock()
    launch = datetime(2026, 10, 9, tzinfo=UTC)
    terminal = launch + timedelta(seconds=2.0 if point == "late_artifact" else 0.2)
    artifact = {"finished_utc": terminal.isoformat()}
    monkeypatch.setattr(finishing, "read_terminal_artifact", lambda root, **kwargs: (artifact, []))
    monkeypatch.setattr(finishing, "verify_gate_report", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(finishing, "validate_terminal_artifact", lambda *args, **kwargs: [])
    clock.now = 1.0 if point == "boundary" else 0.5

    def measure(job, *, checkpoint):
        if point == "blocking_measure":
            clock.now += 2.0
        return dict(clean_head=True, **expected_identity(plan.as_dict(), "source")), []

    running = SimpleNamespace(
        job_id="source-job",
        tree={"root": tmp_path},
        token="controlled-token",
        process=SimpleNamespace(pid=42),
        launched_wall=launch,
        started=0.0,
        deadline=1.0,
    )
    result = finishing.finish_job(
        plan,
        running,
        0,
        clock.monotonic,
        lambda: launch + timedelta(seconds=clock.now),
        measure,
    )
    assert result["child_exit_status"] == 0
    assert result["state"] == ("passed" if point == "within" else "timeout")
    if point != "within":
        assert any("frozen deadline" in problem for problem in result["artifact_problems"])
    return result


def worker_deadline_case(tmp_path, monkeypatch, point):
    """Run real main()/artifact publication with a gate double that never starts a process."""

    plan, clock = make_plan(deadline_seconds=1.0), Clock()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.text)
    for name in ("evidence", "out"):
        (tmp_path / name).mkdir()
    calls, measurements = [], []
    monkeypatch.setattr(worker, "runtime_pin_problem", lambda *args: "")
    monkeypatch.setattr(
        worker, "time", SimpleNamespace(monotonic=clock.monotonic, time=lambda: 1000.0)
    )
    monkeypatch.setenv("POKERED_PARALLEL_DEADLINE_EPOCH", "1001.0")
    if point.startswith("invalid_"):
        monkeypatch.setenv("POKERED_PARALLEL_DEADLINE_EPOCH", point.removeprefix("invalid_"))

    def measure(**kwargs):
        measurements.append(clock.now)
        if (point == "before" and len(measurements) == 1) or (
            point == "after" and len(measurements) == 2
        ):
            clock.now += 1.1
        return dict(clean_head=True, **expected_identity(plan.as_dict(), "source")), []

    def run(command, **kwargs):
        calls.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0)

    job = plan.job("source-job")
    report = {"overall": "PASS", "tiers": gate_tiers(job, plan.as_dict()), "gate_problems": []}
    monkeypatch.setattr(worker, "measure_identity", measure)
    monkeypatch.setattr(worker.subprocess, "run", run)

    def load_report(root, **kwargs):
        if point == "report":
            clock.now += 1.1
        return report, "", {}

    monkeypatch.setattr(worker, "_load_report", load_report)
    monkeypatch.setattr(worker, "_fresh", lambda *args: True)
    if point == "build":
        original_build = worker.build_artifact

        def delayed_build(**kwargs):
            artifact = original_build(**kwargs)
            clock.now += 1.1
            return artifact

        monkeypatch.setattr(worker, "build_artifact", delayed_build)
    status = worker.main(
        [
            "--plan",
            str(plan_path),
            "--job-id",
            "source-job",
            "--job-root",
            str(tmp_path),
            "--project-root",
            str(tmp_path),
            "--rom-root",
            str(tmp_path),
            "--fixture-root",
            str(tmp_path),
        ]
    )
    if point.startswith("invalid_"):
        assert status == 3 and calls == measurements == []
        assert not (tmp_path / "out" / TERMINAL_ARTIFACT_NAME).exists()
        return None
    artifact = json.loads((tmp_path / "out" / TERMINAL_ARTIFACT_NAME).read_text())
    expected = 0 if point == "within" else worker.TIMEOUT_EXIT_STATUS
    assert status == artifact["child_exit_status"] == expected
    assert artifact["overall"] == ("PASS" if point == "within" else "FAIL")
    assert len(measurements) == (1 if point in ("before", "report") else 2)
    assert len(calls) == (0 if point == "before" else 1)
    assert all(0 < allowance <= 1.0 for allowance in calls)
    if point != "within":
        assert any("deadline" in problem for problem in artifact["gate_problems"])
    if point == "after":
        assert any("gate exit status 0" in problem for problem in artifact["gate_problems"])
    return artifact


def overdue_sibling_harvest_case(tmp_path, monkeypatch):
    """Actual harvest and finalizer; receipt/tree cleanup is controlled, never proved here."""

    plan, clock = make_plan(deadline_seconds=1.0), Clock()
    launch = datetime(2026, 10, 9, tzinfo=UTC)
    artifact = {"finished_utc": (launch + timedelta(seconds=0.2)).isoformat()}
    monkeypatch.setattr(finishing, "read_terminal_artifact", lambda root, **kwargs: (artifact, []))
    monkeypatch.setattr(finishing, "verify_gate_report", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(finishing, "validate_terminal_artifact", lambda *args, **kwargs: [])
    clock.now = 0.5
    running, outcomes = [], {}
    for job, mode in (("source-job", "source"), ("cython-job", "cython")):
        running.append(
            SimpleNamespace(
                job_id=job,
                mode=mode,
                tree={"root": tmp_path},
                token="controlled-token",
                process=SimpleNamespace(
                    pid=42, poll=lambda m=mode: 0 if m == "source" or clock.now >= 1.5 else None
                ),
                launched_wall=launch,
                started=0.0,
                deadline=1.0,
                stop_requested=False,
            )
        )

    def measure(job, *, checkpoint):
        mode = plan.job(job)["mode"]
        if mode == "source":
            clock.now += 2.0  # B crosses its frozen deadline and exits during A's blocking call.
        return dict(clean_head=True, **expected_identity(plan.as_dict(), mode)), []

    def finish(item, status, *, allow_cancel):
        outcomes[item.job_id] = finishing.finish_job(
            plan,
            item,
            status,
            clock.monotonic,
            lambda: launch + timedelta(seconds=clock.now),
            measure,
        )
        running.remove(item)

    source = Path(__file__).resolve().parents[1] / "scripts/production_gate_parallel.py"
    outer = next(
        n for n in ast.parse(source.read_text()).body if getattr(n, "name", "") == "_run_plan"
    )
    harvest = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == "harvest")
    namespace = {"running": running, "finish": finish}
    exec(  # noqa: S102 - actual AST with controlled finalizer dependencies; no process operations
        compile(
            ast.Module(body=[harvest], type_ignores=[]),
            "actual-harvest",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    namespace["harvest"](allow_cancel=False)
    assert running == []
    assert outcomes["cython-job"]["state"] == "timeout"
    assert all(row["state"] != "passed" for row in outcomes.values())
    assert clock.now == 2.5
    return outcomes
