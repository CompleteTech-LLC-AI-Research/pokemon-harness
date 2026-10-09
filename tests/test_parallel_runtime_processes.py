"""Authored controls: owned process trees with a real detached hierarchy (#107.4, finding 4).

The worker below mimics the gate: it starts children with ``start_new_session`` so they
are outside the worker's process group, exactly like preflight/pytest/matrix children.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time

from scripts.production_gate_parallel_processes import (
    JOB_TOKEN_ENV,
    GuardianStatus,
    OwnedTree,
    cleanup_proven,
    interrupt_on_signals,
    launch_guardian,
    read_guardian_receipt,
    read_stat,
)

WORKER = r"""
import os, socket, subprocess, sys, time
mark, mode = sys.argv[1], sys.argv[2]
code = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
    "open(sys.argv[1], 'w').write(str(s.getsockname()[1]))\n"
    "time.sleep(120)\n"
)
child = subprocess.Popen([sys.executable, "-c", code, mark + ".port"], start_new_session=True)
open(mark, "w").write(str(child.pid))
time.sleep(1.5 if mode == "exit" else 120)
"""


def wait_for(path, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def alive(pid):
    stat = read_stat(pid)
    return stat is not None and stat[2] != "Z"


def start(tmp_path, mode, token="tok-1"):
    mark = tmp_path / "grandchild.pid"
    env = {**os.environ, JOB_TOKEN_ENV: token}
    worker = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(mark), mode], env=env, start_new_session=True
    )
    return worker, mark


def sentinel():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )


def test_a_detached_grandchild_is_tracked_terminated_and_an_unrelated_process_survives(tmp_path):
    other = sentinel()
    worker, mark = start(tmp_path, "hold")
    try:
        grandchild = int(wait_for(mark))
        wait_for(tmp_path / "grandchild.pid.port")
        tree = OwnedTree(worker.pid, "tok-1")
        tree.observe(force=True)
        assert grandchild in tree.tracked and os.getpgid(grandchild) != worker.pid
        receipt = tree.terminate(grace_seconds=2.0)
        worker.wait(timeout=10)
        assert receipt["proof"] is True and receipt["remaining"] == []
        assert not alive(grandchild)
        assert other.poll() is None and alive(other.pid)
    finally:
        other.kill()
        other.wait()
        worker.kill()
        worker.wait()


def test_a_listener_socket_of_the_tree_is_seen_and_gone_after_cleanup(tmp_path):
    worker, _mark = start(tmp_path, "hold")
    try:
        port = int(wait_for(tmp_path / "grandchild.pid.port"))
        tree = OwnedTree(worker.pid, "tok-2")
        tree.observe(force=True)
        assert port in tree.ports and port in tree.current_ports()
        receipt = tree.terminate(grace_seconds=2.0)
        assert (
            port in receipt["ports_seen"] and receipt["surviving_ports"] == [] and receipt["proof"]
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    finally:
        worker.kill()
        worker.wait()


def test_descendants_that_outlive_a_normally_exiting_worker_are_found_and_removed(tmp_path):
    worker, mark = start(tmp_path, "exit")
    try:
        grandchild = int(wait_for(mark))
        tree = OwnedTree(worker.pid, "tok-3")
        tree.observe(force=True)
        worker.wait(timeout=10)
        assert alive(grandchild) and grandchild in tree.straggler_check()
        receipt = tree.terminate(grace_seconds=2.0)
        assert grandchild in receipt["terminated"] and receipt["proof"] and not alive(grandchild)
    finally:
        worker.kill()
        worker.wait()


def kill_all(*processes):
    for process in processes:
        process.kill()
        process.wait()


def test_a_pid_whose_start_time_does_not_match_is_never_adopted_or_signalled():
    owner, victim = sentinel(), sentinel()
    try:
        tree = OwnedTree(owner.pid, "tok-4")
        started = read_stat(victim.pid)[0]
        assert tree._adopt(victim.pid, started + 1) is False
        assert victim.pid not in tree.tracked
        receipt = tree.terminate(grace_seconds=0.5)
        assert owner.wait(timeout=10) is not None and receipt["proof"] is True
        assert victim.poll() is None, "a process that was not adopted must never be signalled"
    finally:
        kill_all(owner, victim)


def test_carrying_the_job_token_is_not_ownership_without_descending_from_the_root():
    environment = {**os.environ, JOB_TOKEN_ENV: "tok-5"}
    impostor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env=environment,
        start_new_session=True,
    )
    owner = sentinel()
    try:
        tree = OwnedTree(owner.pid, "tok-5")
        tree.terminate(grace_seconds=0.5)
        assert owner.wait(timeout=10) is not None
        assert impostor.poll() is None and alive(impostor.pid)
    finally:
        kill_all(owner, impostor)


def test_cleanup_is_never_claimed_proven_when_the_root_cannot_be_pinned():
    tree = OwnedTree(2**22 + 12345, "tok-6")  # no such process
    receipt = tree.terminate(grace_seconds=0.1)
    assert tree.contained is False and receipt["proof"] is False and receipt["terminated"] == []


def run_guardian(tmp_path, mode):
    """Run a real guardian around the detaching worker; returns (process, secret, receipt, mark)."""

    receipt = tmp_path / "guardian.json"
    mark = tmp_path / "grandchild.pid"
    process, secret = launch_guardian(
        receipt, 1.0, [sys.executable, "-c", WORKER, str(mark), mode], start_new_session=True
    )
    return process, secret, receipt, mark


def test_the_guardian_adopts_and_removes_an_orphan_created_at_the_moment_the_worker_exits(tmp_path):
    other = sentinel()
    guardian, secret, receipt, mark = run_guardian(tmp_path, "exit")
    try:
        grandchild = int(wait_for(mark))
        guardian.wait(timeout=30)
        body, problems = read_guardian_receipt(receipt, secret, guardian.pid)
        assert body["contained"] is True and body["proof"] is True and body["remaining"] == []
        assert grandchild in body["adopted"]
        assert any("outlived the worker" in item for item in problems)
        assert not alive(grandchild), "the orphan was not removed by the guardian"
        assert other.poll() is None and alive(other.pid), "an unrelated process was touched"
    finally:
        kill_all(other)
        guardian.kill()
        guardian.wait()


def test_a_stop_signal_makes_the_guardian_contain_the_whole_detached_subtree(tmp_path):
    guardian, secret, receipt, mark = run_guardian(tmp_path, "hold")
    try:
        grandchild = int(wait_for(mark))
        wait_for(tmp_path / "grandchild.pid.port")
        guardian.send_signal(signal.SIGTERM)
        guardian.wait(timeout=30)
        body, _problems = read_guardian_receipt(receipt, secret, guardian.pid)
        assert body["stopped_by_signal"] == signal.SIGTERM and body["proof"] is True
        assert grandchild in body["adopted"] and not alive(grandchild)
        assert body["worker_pgid"] == body["worker_pid"] != body["guardian_pgid"]
    finally:
        guardian.kill()
        guardian.wait()


def test_a_stop_signal_before_the_worker_starts_leaves_a_proven_never_started_receipt(tmp_path):
    receipt = tmp_path / "guardian.json"
    process, secret = launch_guardian(receipt, 1.0, [sys.executable, "-c", "pass"])
    try:
        wait_for(receipt)  # readiness receipt: handlers are installed from here on
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
        body, problems = read_guardian_receipt(receipt, secret, process.pid)
        assert body["phase"] in ("ready", "final")
        if body["phase"] == "final":
            assert body["proof"] is True and problems in (
                [],
                ["guardian receipt names no worker pid"],
            )
    finally:
        process.kill()
        process.wait()


def test_a_worker_that_cannot_start_is_reported_by_the_guardian_not_lost(tmp_path):
    receipt = tmp_path / "guardian.json"
    process, secret = launch_guardian(receipt, 1.0, ["/nonexistent/python"])
    assert process.wait(timeout=30) == 127
    body, problems = read_guardian_receipt(receipt, secret, process.pid)
    assert body["start_error"] == "FileNotFoundError"
    assert problems == ["could not start: FileNotFoundError"]


def test_a_forged_or_foreign_receipt_is_never_authenticated(tmp_path):
    guardian, secret, receipt, mark = run_guardian(tmp_path, "exit")
    try:
        wait_for(mark)
        guardian.wait(timeout=30)
        genuine, _ = read_guardian_receipt(receipt, secret, guardian.pid)
        assert genuine is not None
        assert read_guardian_receipt(receipt, b"x" * 32, guardian.pid)[0] is None
        assert read_guardian_receipt(receipt, secret, guardian.pid + 1)[0] is None
        assert read_guardian_receipt(receipt, secret, guardian.pid, 1)[0] is None
        forged = json.loads(receipt.read_text(encoding="utf-8"))
        forged["proof"], forged["adopted"] = True, []
        receipt.write_text(json.dumps(forged), encoding="utf-8")
        assert read_guardian_receipt(receipt, secret, guardian.pid)[0] is None
    finally:
        guardian.kill()
        guardian.wait()


class FakeStatus:
    def __init__(self, absent):
        self.absent = absent

    def absent_proof(self):
        return self.absent


def test_budget_may_only_be_released_with_both_the_parent_proof_and_the_guardian_proof():
    parent_ok = {"proof": True, "adopted_total": 0}
    final_ok = {"phase": "final", "contained": True, "proof": True}
    absent, present = FakeStatus(True), FakeStatus(False)
    assert cleanup_proven(parent_ok, final_ok, present) is True
    assert cleanup_proven({**parent_ok, "proof": False}, final_ok, absent) is False
    assert cleanup_proven(parent_ok, {**final_ok, "proof": False}, absent) is False
    assert cleanup_proven(parent_ok, {**final_ok, "contained": False}, absent) is False
    assert cleanup_proven(parent_ok, {"phase": "ready"}, present) is False
    assert cleanup_proven(parent_ok, None, present) is False, (
        "an unauthenticated receipt proves nothing"
    )
    assert cleanup_proven(parent_ok, None, None) is False
    assert cleanup_proven(parent_ok, None, absent) is True, (
        "affirmative absence from the status pipe"
    )


def test_the_status_pipe_is_an_independent_launch_record_not_a_file(tmp_path):
    read_end, write_end = os.pipe()
    view = GuardianStatus(read_end)
    try:
        assert view.absent_proof() is False, "no EOF yet: the guardian may still be alive"
        os.write(write_end, b"R")
        os.close(write_end)
        assert view.absent_proof() is True and view.ready and not view.spawned
    finally:
        view.close()
    read_end, write_end = os.pipe()
    view = GuardianStatus(read_end)
    try:
        os.write(write_end, b"RS")
        os.close(write_end)
        assert view.absent_proof() is False, "spawn intent was signalled: a worker may exist"
        assert view.spawned
    finally:
        view.close()


def test_a_real_guardian_reports_ready_and_spawn_intent_over_its_status_pipe(tmp_path):
    receipt = tmp_path / "guardian.json"
    process, secret = launch_guardian(receipt, 1.0, [sys.executable, "-c", "pass"])
    try:
        process.wait(timeout=30)
        view = process.guardian_status
        view.drain()
        assert view.ready and view.spawned and view.eof and view.absent_proof() is False
        assert read_guardian_receipt(receipt, secret, process.pid)[0]["phase"] == "final"
    finally:
        process.guardian_status.close()


def test_orchestrator_signals_become_the_bounded_cancel_interrupt():
    import threading

    event = threading.Event()
    before = signal.getsignal(signal.SIGHUP)
    with interrupt_on_signals(event):
        os.kill(os.getpid(), signal.SIGHUP)
        assert event.wait(timeout=5)
    assert signal.getsignal(signal.SIGHUP) == before, "the previous handler must be restored"


def test_a_missing_or_unproven_guardian_receipt_is_a_failed_closed_problem(tmp_path):
    assert read_guardian_receipt(tmp_path / "absent.json")[1]
    (tmp_path / "bad.json").write_text('{"contained": false}', encoding="utf-8")
    assert read_guardian_receipt(tmp_path / "bad.json")[1]
