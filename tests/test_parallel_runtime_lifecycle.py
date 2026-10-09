"""Authored lifecycle controls: owned process groups, private trees, worker and CLI wiring (#107.2, #107.4).

Real child processes are started only to prove bounded cleanup of owned groups.  Nothing here runs a
gate, emulator or ROM, so these controls are not gameplay or performance evidence.
"""

import json
import os
import subprocess
import sys

import pytest

from scripts import production_gate as gate
from scripts import production_gate_parallel_worker as worker
from scripts.production_gate_parallel import (
    default_worker_argv,
    job_environment,
    prepare_job_tree,
    terminate_owned_group,
)
from scripts.production_gate_parallel_plan import PlanError, canonical_json, load_plan
from tests.test_parallel_runtime_gate import make_plan, run, states


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"  # a zombie awaiting reaping is already dead


def test_terminating_an_owned_group_stops_a_grandchild_and_spares_an_unrelated_group():
    code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print(p.pid, flush=True); time.sleep(60)"
    )
    owned = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        grandchild = int(owned.stdout.readline())
        assert alive(grandchild)
        terminate_owned_group(owned, grace_seconds=1.0)
        assert owned.poll() is not None
        assert not alive(grandchild)
        assert unrelated.poll() is None, "an unrelated process group was signalled"
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait()
        owned.stdout.close()


def test_job_trees_are_private_new_and_refuse_symlinked_or_existing_roots(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    tree = prepare_job_tree(root, "1" * 32, "source-job")
    assert set(tree) == {"root", "out", "tmp", "cache", "build", "evidence", "raw", "ports"}
    assert all((path.stat().st_mode & 0o777) == 0o700 for path in tree.values())
    with pytest.raises(FileExistsError):
        prepare_job_tree(root, "1" * 32, "source-job")
    link = tmp_path / "link"
    os.symlink(root, link)
    with pytest.raises(OSError, match="real directory"):
        prepare_job_tree(link, "2" * 32, "source-job")
    with pytest.raises(OSError, match="real directory"):
        prepare_job_tree(tmp_path / "missing", "3" * 32, "source-job")


def test_the_job_environment_isolates_trees_and_drops_unlisted_variables(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    tree = prepare_job_tree(root, "4" * 32, "cython-job")
    env = job_environment(tree, {"PATH": "/bin", "TOKEN": "secret", "OMP_NUM_THREADS": "64"})
    assert "TOKEN" not in env
    assert env["PATH"] == "/bin"
    assert env["TMPDIR"] == str(tree["tmp"])
    assert env["XDG_CACHE_HOME"] == str(tree["cache"])
    assert env["POKERED_PARALLEL_BUILD_DIR"] == str(tree["build"])
    assert env["HOME"] == str(tree["root"])
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        assert env[name] == "1"


def test_the_default_worker_command_names_the_plan_job_interpreter_and_gate_arguments(tmp_path):
    plan = make_plan()
    root = tmp_path / "runs"
    root.mkdir()
    tree = prepare_job_tree(root, plan.run_id, "cython-job")
    factory = default_worker_argv(["--keep-going"], {"source": "/s/python", "cython": "/c/python"})
    argv = factory(plan, "cython-job", tree, tmp_path)
    assert argv[0] == "/c/python"
    assert argv[1:3] == ["-m", "scripts.production_gate_parallel_worker"]
    assert f"--plan={root / plan.run_id / 'plan.json'}" in argv
    assert "--job-id=cython-job" in argv
    assert f"--job-root={tree['root']}" in argv
    assert "--gate-arg=--keep-going" in argv


def test_a_second_run_cannot_reuse_a_plan_file_or_touch_files_outside_its_trees(tmp_path):
    plan = make_plan()
    (tmp_path / "runs").mkdir()
    keep = tmp_path / "runs" / "keep.txt"
    keep.write_text("owner data", encoding="utf-8")
    _, _, first = run(tmp_path, {"source": "pass", "cython": "pass"}, plan=plan)
    assert first["overall"] == "PASS"
    _, _, second = run(tmp_path, {"source": "pass", "cython": "pass"}, plan=plan)
    assert second["overall"] == "FAIL"
    assert states(second) == {"source": "not_started", "cython": "not_started"}
    assert any("refused before dispatch" in job["reason"] for job in second["jobs"])
    assert keep.read_text(encoding="utf-8") == "owner data"
    assert (tmp_path / "runs" / plan.run_id / "source-job" / "out" / "terminal.json").is_file()


def test_the_worker_command_pins_the_runtime_job_root_and_pair_share(tmp_path):
    command = worker.build_gate_command(
        python_executable="/py/python",
        project_root=tmp_path / "checkout",
        mode="cython",
        pairs=3,
        job_root=tmp_path / "job",
        gate_arguments=["--keep-going", "--repeat-timing=5"],
    )
    assert command[0] == "/py/python"
    assert command[1] == str(tmp_path / "checkout" / "scripts" / "production_gate.py")
    assert "--runtime-mode=cython" in command
    assert "--matrix-workers=3" in command
    assert f"--evidence-dir={tmp_path / 'job' / 'evidence'}" in command
    assert f"--raw-output-dir={tmp_path / 'job' / 'raw' / 'pytest'}" in command
    assert command[-2:] == ["--keep-going", "--repeat-timing=5"]
    assert not any("capacity-policy" in item for item in command), "no cloned capacity authority"


def test_the_worker_never_reports_a_pass_it_did_not_observe():
    plan = make_plan()
    job = plan.job("source-job")
    common = {
        "plan_body": plan.as_dict(),
        "plan_digest": plan.digest,
        "job": job,
        "pid": 7,
        "started": "t0",
        "exit_status": 0,
    }
    lost = worker.build_artifact(report=None, problem="gate report unreadable: OSError", **common)
    assert lost["overall"] == "FAIL"
    assert lost["tiers"] == []
    assert "gate report unreadable: OSError" in lost["gate_problems"]
    report = {
        "overall": "PASS",
        "runtime": {"pyboy_mode": "source"},
        "tiers": [{"name": "unit", "status": "PASS", "counts": {"total": 1}, "returncodes": [0]}],
    }
    observed = worker.build_artifact(report=report, problem="", **common)
    assert observed["overall"] == "PASS"
    assert observed["rows"] == [], "row ids are never synthesised"
    assert observed["tiers"][0]["counts"] == {"total": 1}
    contradicted = worker.build_artifact(report=report, problem="evidence mismatch", **common)
    assert contradicted["overall"] == "FAIL"


def test_the_worker_refuses_a_tampered_plan_and_an_unknown_job(tmp_path, capsys):
    plan = make_plan()
    good = tmp_path / "plan.json"
    good.write_text(plan.text, encoding="utf-8")
    tampered = tmp_path / "tampered.json"
    body = json.loads(plan.text)
    body["commit"] = "f" * 40
    tampered.write_text(canonical_json(body), encoding="utf-8")
    base = ["--job-root", str(tmp_path), "--project-root", str(tmp_path)]
    assert worker.main(["--plan", str(tampered), "--job-id", "source-job", *base]) == 3
    assert worker.main(["--plan", str(good), "--job-id", "ghost-job", *base]) == 3
    assert "worker refused" in capsys.readouterr().err
    with pytest.raises(PlanError):
        load_plan(tampered.read_text(encoding="utf-8"))


def test_the_sequential_gate_stays_the_default_and_the_parallel_flags_are_guarded(tmp_path):
    args = gate.build_parser().parse_args([])
    assert args.parallel_runtimes is False
    assert args.parallel_run_root is None
    assert args.runtime_mode == "source"
    with pytest.raises(SystemExit) as root_without_flag:
        gate.main(["--parallel-run-root", str(tmp_path)])
    assert root_without_flag.value.code == 2
    with pytest.raises(SystemExit) as with_evidence:
        gate.main(["--parallel-runtimes", "--evidence-dir", str(tmp_path / "evidence")])
    assert with_evidence.value.code == 2
