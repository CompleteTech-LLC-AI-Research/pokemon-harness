"""Authored controls: owned process trees with a real detached hierarchy (#107.4, finding 4).

The worker below mimics the gate: it starts children with ``start_new_session`` so they
are outside the worker's process group, exactly like preflight/pytest/matrix children.
"""

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.production_gate_parallel_processes import (
    JOB_TOKEN_ENV,
    GuardianStatus,
    OrchestratorAdoption,
    OwnedTree,
    children_of,
    cleanup_proven,
    interrupt_on_signals,
    launch_guardian,
    read_guardian_receipt,
    read_session,
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


def test_children_of_lists_only_the_named_process_children_and_nothing_else():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert child.pid in children_of(os.getpid())
        assert child.pid not in children_of(child.pid)
        assert children_of(2**22 + 12345) == []
        assert read_session(child.pid) == read_session(os.getpid())
    finally:
        child.kill()
        child.wait()


def test_orchestrator_adoption_sets_reads_back_and_restores_the_subreaper_flag():
    with OrchestratorAdoption() as adoption:
        assert adoption.active and adoption.verify()
        previous = adoption._previous
    assert adoption.verify() is bool(previous)
    with OrchestratorAdoption(enabled=False) as disabled:
        assert not disabled.active and not disabled.verify()


class _Reported:
    """What a guardian reports over its status pipe about the worker it spawned."""

    def __init__(self, pid):
        self.worker = (pid, read_stat(pid)[0])

    def drain(self):
        return None


def test_an_orphan_reparented_to_the_orchestrator_is_pinned_terminated_and_proven(tmp_path):
    script = (
        "import os, subprocess, sys\n"
        "w = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True)\n"
        "print(w.pid, flush=True)\n"
        "os._exit(0)\n"
    )
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(
        0.05
    )  # start ticks have a 10 ms granularity: the sentinel must start strictly earlier
    with OrchestratorAdoption() as adoption:
        guardian = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, JOB_TOKEN_ENV: "tok-adopt"},
        )
        orphan = int(guardian.stdout.readline())
        end = time.monotonic() + 10
        while time.monotonic() < end and (read_stat(guardian.pid) or (0, 0, "?"))[2] != "Z":
            time.sleep(0.02)
        tree = OwnedTree(guardian.pid, "tok-adopt", adoption=adoption, status=_Reported(orphan))
        try:
            receipt = tree.terminate(grace_seconds=1.0)
        finally:
            guardian.wait()
            sentinel_alive = sentinel.poll() is None
            sentinel.kill()
            sentinel.wait()
    assert not alive(orphan), "the re-parented orphan survived the sweep"
    assert receipt["adoption"]["proof"] is True and receipt["adoption"]["orphans_pinned"] == 1
    assert receipt["proof"] is True and sentinel_alive, "an unrelated child must not be signalled"


def test_cleanup_is_unproven_when_adoption_was_not_active_at_launch(tmp_path):
    receipt = {"proof": True, "adoption": {"proof": False}}
    assert cleanup_proven(receipt, None, None) is False
    receipt["adoption"]["proof"] = True
    assert cleanup_proven(receipt, None, None) is True


def test_an_unattributed_child_with_the_job_token_is_never_signalled_and_blocks_the_proof():
    with OrchestratorAdoption() as adoption:
        guardian = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
        time.sleep(0.05)  # the outsider must start strictly after the root (10 ms tick granularity)
        outsider = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env={**os.environ, JOB_TOKEN_ENV: "tok-outsider"},
        )
        end = time.monotonic() + 10
        while time.monotonic() < end and (read_stat(guardian.pid) or (0, 0, "?"))[2] != "Z":
            time.sleep(0.02)
        tree = OwnedTree(guardian.pid, "tok-outsider", adoption=adoption)
        try:
            receipt = tree.terminate(grace_seconds=0.3)
            untouched = outsider.poll() is None
        finally:
            guardian.wait()
            outsider.kill()
            outsider.wait()
    assert untouched, "a child that is not kernel-attributed to the job must never be signalled"
    assert receipt["adoption"]["unattributed"] == [outsider.pid]
    assert receipt["adoption"]["proof"] is False and cleanup_proven(receipt, None, None) is False


MARK_WORKER = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('spawned')"


def run_marking_guardian(tmp_path, cgroup):
    receipt, mark = tmp_path / "guardian.json", tmp_path / "spawned.txt"
    process, secret = launch_guardian(
        receipt,
        1.0,
        [sys.executable, "-c", MARK_WORKER, str(mark)],
        cgroup=cgroup,
        start_new_session=True,
    )
    process.wait(timeout=30)
    return process, secret, receipt, mark


def test_required_ownership_refuses_to_spawn_the_worker_when_the_cgroup_join_fails(tmp_path):
    process, secret, receipt, mark = run_marking_guardian(
        tmp_path, "!" + str(tmp_path / "no-cgroup")
    )
    assert process.returncode == 70 and not mark.exists(), "the worker must never exist"
    body, problems = read_guardian_receipt(receipt, secret, process.pid)
    assert body["never_started"] is True and body["contained"] is False and body["proof"] is False
    assert "cgroup join failed" in body["reason"], "the original reason is recorded"
    assert any("cgroup join failed" in item for item in problems)
    assert process.guardian_status.absent_proof(), (
        "EOF without spawn intent proves no worker existed"
    )


def test_required_ownership_without_any_cgroup_path_also_refuses(tmp_path):
    process, secret, receipt, mark = run_marking_guardian(tmp_path, "!")
    assert process.returncode == 70 and not mark.exists()
    body, _problems = read_guardian_receipt(receipt, secret, process.pid)
    assert body["never_started"] is True and "no cgroup was supplied" in body["reason"]


def test_without_the_required_marker_a_failed_join_is_recorded_and_the_worker_still_runs(tmp_path):
    process, secret, receipt, mark = run_marking_guardian(tmp_path, str(tmp_path / "no-cgroup"))
    assert mark.read_text() == "spawned"
    body, _problems = read_guardian_receipt(receipt, secret, process.pid)
    assert body["contained"] is True and body["cgroup_error"], "unqualified: recorded, never hidden"


def _reuse_scenario(monkeypatch, *, dies, child_start=5000):
    """Fake the kernel facts: a pinned parent 4101 and a child 4102 whose parent field names 4101.

    ``dies`` picks WHEN the pinned parent instance exits (and its number would be reused): None
    never, "after-snapshot", "during-read" (while its children are listed), "before-child-read"
    (just before the child is looked at) or "during-pin" (while the child's pidfd is read back).
    A child with another ``child_start`` and the same parent number is a replacement's child.
    """

    from scripts import production_gate_parallel_processes as proc

    parent, child = 4101, 4102
    fds, state = {}, {"exited": False, "snapshots": 0, "child_reads": 0, "child_stats": 0}

    def fake_open(pid):
        fds[pid] = os.open(os.devnull, os.O_RDONLY)
        return fds[pid]

    def fake_exited(fd):
        if fd != fds.get(parent):
            return False
        state["snapshots"] += 1
        if dies == "after-snapshot" and state["snapshots"] > 1:
            state["exited"] = True
        return state["exited"]

    def fake_children(pid):
        state["child_reads"] += 1
        if dies == "during-read":
            state["exited"] = (
                True  # dies while its list is read; the number now belongs to a stranger
            )
        return [child] if pid == parent else []

    def fake_stat(pid):
        if pid == parent:
            return (1000, 1, "S")
        if pid != child:
            return None
        state["child_stats"] += 1
        if dies == "before-child-read" and state["child_stats"] == 1:
            state["exited"] = True
        if dies == "during-pin" and state["child_stats"] == 2:
            state["exited"] = True
        return (child_start, parent, "S")  # the replacement's child names the same parent number

    monkeypatch.setattr(proc, "_pidfd_open", fake_open)
    monkeypatch.setattr(proc, "_exited", fake_exited)
    monkeypatch.setattr(proc, "read_stat", fake_stat)
    monkeypatch.setattr(proc, "children_of", fake_children)
    monkeypatch.setattr(proc, "socket_ports", lambda pids: {})
    tree = proc.OwnedTree(parent, "tok", observe_seconds=0.0)
    tree.observe(force=True)
    return tree, state, child, fds


def _close_all(fds):
    for fd in fds.values():
        try:
            os.close(fd)
        except OSError:
            pass


def test_a_parent_number_reused_between_snapshot_and_traversal_never_donates_children(monkeypatch):
    """The held pidfd must show the SAME parent instance across the list, each child read and pin."""

    during, state, child, fds = _reuse_scenario(monkeypatch, dies="during-read")
    assert child not in during.tracked and during.adopted_total == 0
    assert state["child_reads"] == 1, "the list was read once and then discarded as untrustworthy"
    _close_all(fds)
    after, state, child, fds = _reuse_scenario(monkeypatch, dies="after-snapshot")
    assert child not in after.tracked and after.adopted_total == 0
    assert state["child_reads"] == 0, "a parent already gone is never asked for its children"
    _close_all(fds)
    alive, _state, child, fds = _reuse_scenario(monkeypatch, dies=None)
    assert child in alive.tracked and alive.adopted_total == 1, "the control adopts when valid"
    _close_all(fds)


def test_a_parent_that_dies_before_or_during_the_pin_never_lets_a_stranger_be_adopted(monkeypatch):
    """A replacement's child has the same parent number and a DIFFERENT start; its stat and pidfd
    read-back look consistent, so only the parent instance check around them can refuse it."""

    before, _state, child, fds = _reuse_scenario(
        monkeypatch, dies="before-child-read", child_start=7777
    )
    assert child not in before.tracked and before.adopted_total == 0
    assert child not in fds, "no pidfd may even be opened once the parent instance is gone"
    _close_all(fds)
    during, _state, child, fds = _reuse_scenario(monkeypatch, dies="during-pin", child_start=7777)
    assert child not in during.tracked and during.adopted_total == 0, "nothing is committed"
    with pytest.raises(OSError):
        os.fstat(fds[child])  # the provisional descriptor was closed, not kept
    _close_all(fds)
    stranger_ok, _state, child, fds = _reuse_scenario(monkeypatch, dies=None, child_start=7777)
    assert child in stranger_ok.tracked, "with a valid parent the same facts are an ordinary child"
    _close_all(fds)


def test_cleanup_signals_only_pinned_instances_and_never_a_replacement_by_number(monkeypatch):
    """Non-interference is independent of collection: a signal goes through the held pidfd only."""

    from scripts import production_gate_parallel_processes as proc

    sent = []

    def forbid(*_a, **_k):
        raise AssertionError("a process was signalled by its number")

    monkeypatch.setattr(proc.os, "kill", forbid)
    monkeypatch.setattr(proc.os, "killpg", forbid)
    monkeypatch.setattr(
        proc.signal, "pidfd_send_signal", lambda fd, signum: sent.append((fd, signum))
    )
    tree = object.__new__(
        proc.OwnedTree
    )  # no constructor: nothing was pinned or read from the kernel
    tree._entries = {777: (31, 1234)}
    assert tree._signal({777}, signal.SIGTERM) == [777] and sent == [(31, signal.SIGTERM)]

    def gone(fd, signum):
        raise ProcessLookupError

    monkeypatch.setattr(proc.signal, "pidfd_send_signal", gone)
    assert tree._signal({777}, signal.SIGKILL) == [], "an exited instance is not signalled again"


def _proc_standin(monkeypatch, *, listing, reads, present=()):
    """Stand in for /proc/4242 only: the thread list (or the error listing it raises), the text or
    error each thread's ``children`` read gives, and which /proc entries exist (others are real)."""

    real_listdir, real_read, real_exists = os.listdir, Path.read_text, Path.exists

    def listdir(path):
        if str(path) != "/proc/4242/task":
            return real_listdir(path)
        if isinstance(listing, OSError):
            raise listing
        return list(listing)

    def read_text(self, *args, **kwargs):
        parts = str(self).split("/")
        if str(self).startswith("/proc/4242/task/") and parts[-1] == "children":
            outcome = reads[parts[4]]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return real_read(self, *args, **kwargs)

    def exists(self, *args, **kwargs):
        if str(self).startswith("/proc/4242"):
            return str(self) in present
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "exists", exists)


GONE = FileNotFoundError(errno.ENOENT, "No such file or directory")
VANISHED = ProcessLookupError(errno.ESRCH, "No such process")
DENIED = PermissionError(errno.EACCES, "Permission denied")
TASK, OTHER_TASK, PROCESS = "/proc/4242/task/4242", "/proc/4242/task/4243", "/proc/4242"
LISTING_CASES = [
    # GONE contributes nothing: the process has no /proc entry, or a thread vanished mid-read
    pytest.param(GONE, {}, (), [], id="gone-process"),
    pytest.param(VANISHED, {}, (), [], id="gone-process-esrch"),
    pytest.param(["4242"], {"4242": ""}, (TASK,), [], id="live-process-without-children"),
    pytest.param(
        ["4242", "4243"], {"4242": "11 12\n", "4243": GONE}, (TASK,), [11, 12], id="thread-gone"
    ),
    pytest.param(
        ["4242", "4243"],
        {"4242": "11 12\n", "4243": VANISHED},
        (TASK, OTHER_TASK),
        [11, 12],
        id="thread-gone-esrch",
    ),
    # UNREADABLE is unproven (None), never an empty or a partial list
    pytest.param(DENIED, {}, (PROCESS,), None, id="listing-denied"),
    pytest.param(OSError(errno.EIO, "I/O error"), {}, (PROCESS,), None, id="listing-io-error"),
    pytest.param(GONE, {}, (PROCESS,), None, id="task-directory-missing-for-a-live-process"),
    pytest.param(
        ["4242", "4243"],
        {"4242": "11 12\n", "4243": DENIED},
        (TASK, OTHER_TASK),
        None,
        id="one-thread-denied",
    ),
    pytest.param(
        ["4242"], {"4242": OSError(errno.EMFILE, "Too many open files")}, (TASK,), None, id="emfile"
    ),
    pytest.param(["4242"], {"4242": GONE}, (TASK,), None, id="live-thread-without-children-file"),
    pytest.param(["4242"], {"4242": "11 x2\n"}, (TASK,), None, id="not-a-pid-list"),
    pytest.param(
        ["4242"],
        {"4242": UnicodeDecodeError("ascii", b"\xff", 0, 1, "ordinal not in range")},
        (TASK,),
        None,
        id="not-ascii",
    ),
]


@pytest.mark.parametrize(("listing", "reads", "present", "expected"), LISTING_CASES)
def test_children_of_separates_a_gone_process_or_thread_from_an_unreadable_list(
    monkeypatch, listing, reads, present, expected
):
    _proc_standin(monkeypatch, listing=listing, reads=reads, present=present)
    assert children_of(4242) == expected


def _listing_scenario(monkeypatch, *, dies_while_listing):
    """A pinned live parent 4101 whose children cannot be read (children_of gives None).

    With ``dies_while_listing`` the parent exits during that failed read, so the failure is
    explained by its death (gone) rather than by an unreadable list of a live process.
    """

    from scripts import production_gate_parallel_processes as proc

    parent = 4101
    fds, state = {}, {"exited": False, "strangers": 0, "signals": []}

    def fake_open(pid):
        fds[pid] = os.open(os.devnull, os.O_RDONLY)
        return fds[pid]

    def fake_children(_pid):
        state["exited"] = dies_while_listing
        return None

    def fake_stat(pid):
        state["strangers"] += pid != parent
        return (1000, 1, "S") if pid == parent else None

    monkeypatch.setattr(proc, "_pidfd_open", fake_open)
    monkeypatch.setattr(proc, "_exited", lambda fd: fd == fds.get(parent) and state["exited"])
    monkeypatch.setattr(proc, "read_stat", fake_stat)
    monkeypatch.setattr(proc, "children_of", fake_children)
    monkeypatch.setattr(proc, "socket_ports", lambda pids: {})
    monkeypatch.setattr(
        proc.signal, "pidfd_send_signal", lambda fd, signum: state["signals"].append(signum)
    )
    tree = proc.OwnedTree(parent, "tok", observe_seconds=0.0)
    tree.observe(force=True)
    return tree, state, fds


def test_a_live_pinned_parent_whose_children_cannot_be_read_is_containment_unproven(monkeypatch):
    tree, state, fds = _listing_scenario(monkeypatch, dies_while_listing=False)
    try:
        assert tree.contained is False and "cannot be read" in tree.reason
        assert tree.tracked == {4101: 1000} and set(fds) == {4101}, "nothing foreign was pinned"
        assert state["strangers"] == 0 and state["signals"] == [], "no foreign walk, no signal"
    finally:
        _close_all(fds)


def test_a_parent_that_exits_while_its_children_cannot_be_read_is_gone_not_unreadable(monkeypatch):
    tree, state, fds = _listing_scenario(monkeypatch, dies_while_listing=True)
    try:
        assert tree.contained is True and tree.reason == "", "its death explains the failed read"
        assert state["strangers"] == 0 and state["signals"] == []
    finally:
        _close_all(fds)


def test_an_orchestrator_that_cannot_read_its_own_children_claims_no_adoption_containment(
    monkeypatch,
):
    class Adopting:
        enabled = True

        def verify(self):
            return True

    tree, state, fds = _listing_scenario(monkeypatch, dies_while_listing=True)
    try:
        assert tree.contained is True
        tree.adoption = Adopting()
        tree.pin_orphans()
        assert tree.contained is False and "cannot be read" in tree.reason
        assert tree.orphans_pinned == 0 and state["signals"] == []
    finally:
        _close_all(fds)
