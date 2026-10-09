"""Authored controls: owned process trees with a real detached hierarchy (#107.4, finding 4).

The worker below mimics the gate: it starts children with ``start_new_session`` so they
are outside the worker's process group, exactly like preflight/pytest/matrix children.
"""

import os
import socket
import subprocess
import sys
import time

from scripts.production_gate_parallel_processes import JOB_TOKEN_ENV, OwnedTree, read_stat

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
