"""Authored controls for the per-job holder above the guardian (#107 ownership design v2).

AUTHORED, NOT RUN when committed.  The strict original orphan test stays in the unchanged
``tests/test_parallel_runtime_gate.py``; a later admitted selection MUST include it.  Real-process cases
reuse the gate module's helpers and only extend its fake worker with new scenarios (no edit to that file).
"""

import ast
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import pytest

from scripts import production_gate_parallel_holder as holder_module
from scripts import production_gate_parallel_processes as proc
from scripts.production_gate_parallel import PairBudget
from scripts.production_gate_parallel_holder import HolderRef
from tests import test_parallel_runtime_gate as gate
from tests._parallel_runtime_orphan_support import SENTINEL_ARGV, Survivor, stop_owned_child

GATE_FILE = Path(gate.__file__)
STRICT = "test_a_killed_guardian_is_contained_by_orchestrator_adoption_with_a_proven_cleanup"
STRICT_SHA = "b8095526e323e1085f767c757c4d82d1fc75f31dbba73bbf98cd9171cbfd3550"
ALIVE_SHA = "bc789b1dec554270d7bed56d8519f30f4fbb1ed0829df6ad09e3f80c7e19a7e0"
SECRET = b"k" * 32
PID, START = 4242, 987654


def test_the_strict_original_orphan_test_and_alive_are_ast_identical_to_the_pinned_source():
    """M1 guard: the strict test and its helper are not edited, weakened or replaced."""

    text = GATE_FILE.read_text(encoding="utf-8")
    found = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name in (STRICT, "_alive"):
            found[node.name] = hashlib.sha256(
                ast.get_source_segment(text, node).encode()
            ).hexdigest()
    assert found == {STRICT: STRICT_SHA, "_alive": ALIVE_SHA}


def _receipt(tmp_path, **changes):
    body = {
        "phase": "final",
        "contained": True,
        "proof": True,
        "reason": "",
        "subreaper_start": True,
        "subreaper_end": True,
        "guardian": {"pid": 5000, "start": 111},
        "guardian_status": -9,
        "forks": [[5000, 111]],
        "adopted": [],
        "killed": [],
        "reaped": [],
        "reaped_unheld": [],
        "unknown": [],
        "remaining": [],
        "incomplete": False,
        "echild": True,
        "never_started": False,
        "guardian_killed": False,
        "elapsed": 0.2,
        "holder_pid": PID,
        "holder_start": START,
    }
    body.update(changes)
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["mac"] = hmac.new(SECRET, text.encode(), hashlib.sha256).hexdigest()
    path = tmp_path / "holder.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return HolderRef(path, SECRET, PID)


def test_an_authentic_final_completed_sweep_is_the_only_holder_proof(tmp_path):
    ref = _receipt(tmp_path)
    verdict = ref.verdict(PID, START)
    assert verdict["proof"] is True and verdict["guardian"] == (5000, 111)


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "spawned"},
        {"echild": False},
        {"unknown": [[7, 1]]},
        {"incomplete": True},
        {"remaining": [[7, 1]]},
        {"subreaper_end": False},
        {"subreaper_start": None},
        {"contained": False},
        {"proof": False},
        {"guardian_status": None},
        {"holder_pid": PID + 1},
        {"holder_start": START + 1},
    ],
    ids=lambda changes: next(iter(changes)) + "=" + str(next(iter(changes.values()))),
)
def test_a_non_final_incomplete_or_foreign_holder_receipt_never_proves(tmp_path, changes):
    assert _receipt(tmp_path, **changes).verdict(PID, START)["proof"] is False


def test_a_missing_or_forged_holder_receipt_always_withholds_the_proof(tmp_path):
    assert HolderRef(tmp_path / "absent.json", SECRET, PID).verdict(PID, START)["proof"] is False
    ref = _receipt(tmp_path)
    forged = json.loads(ref.path.read_text())
    forged["echild"] = True
    forged["mac"] = "0" * 64
    ref.path.write_text(json.dumps(forged), encoding="utf-8")
    assert ref.verdict(PID, START)["proof"] is False
    other = HolderRef(ref.path, b"x" * 32, PID)  # wrong key
    assert other.verdict(PID, START)["authentic"] is False


@pytest.mark.parametrize(
    "mac",
    [None, 7, [], {}, "", "0" * 63, "0" * 65, "A" * 64, "g" * 64, "\u00e9" * 64],
)
def test_malformed_mac_is_unproven_without_raising(tmp_path, mac):
    ref = _receipt(tmp_path)
    body = json.loads(ref.path.read_text())
    body["mac"] = mac
    ref.path.write_text(json.dumps(body), encoding="utf-8")
    verdict = ref.verdict(PID, START)
    assert verdict["present"] is True
    assert verdict["authentic"] is False
    assert verdict["proof"] is False
    assert verdict["problems"] == ["holder receipt is not authenticated by the producer's key"]


def test_an_adopted_instance_that_is_not_proven_absent_withholds_the_proof(tmp_path, monkeypatch):
    ref = _receipt(tmp_path, adopted=[[777, 5]], reaped=[[777, 5]])
    for answer in (False, None):
        monkeypatch.setattr(holder_module, "instance_absent", lambda pid, start, a=answer: a)
        assert ref.verdict(PID, START)["proof"] is False
    monkeypatch.setattr(holder_module, "instance_absent", lambda pid, start: True)
    assert ref.verdict(PID, START)["proof"] is True


class _Status:
    def __init__(self, holder):
        self.holder = holder


def _owned(root_gone=True):
    return {
        "proof": True,
        "root": {"pid": PID, "start": START},
        "adoption": {"root_gone": root_gone},
    }


def test_cleanup_proven_in_holder_mode_requires_the_holder_final_and_the_gone_root(tmp_path):
    ref = _receipt(tmp_path)
    lost = {"phase": "ready"}  # a killed guardian never wrote a final receipt
    assert (
        proc.cleanup_proven(_owned(), lost, _Status(ref)) is True
    )  # cleanup proven, job still fails
    assert proc.cleanup_proven(_owned(), None, _Status(ref)) is True
    assert proc.cleanup_proven(_owned(root_gone=False), lost, _Status(ref)) is False
    bad = _receipt(tmp_path, echild=False)
    final_guardian = {"phase": "final", "contained": True, "proof": True}
    assert (
        proc.cleanup_proven(_owned(), final_guardian, _Status(bad)) is False
    )  # H's final always gates
    assert (
        proc.cleanup_proven(
            _owned(), {"phase": "final", "contained": True, "proof": False}, _Status(ref)
        )
        is False
    )


def test_the_status_pipe_records_the_holder_ready_byte_the_guardian_and_the_worker_identities():
    read_end, write_end = os.pipe()
    status = proc.GuardianStatus(read_end)
    try:
        os.write(write_end, b"HG12:34\nRSW56:78\n")
        status.drain()
        assert status.guardian == (12, 34) and status.worker == (56, 78)
        assert status.ready and status.spawned and status.holder is None
    finally:
        status.close()
        os.close(write_end)


def test_read_receipts_never_trusts_a_guardian_identity_nobody_recorded(tmp_path):
    ref = _receipt(tmp_path, guardian=None)
    read_end, write_end = os.pipe()
    status = proc.GuardianStatus(read_end, holder=ref)
    try:
        body, problems = ref.read_receipts(status, tmp_path / "guardian.json", SECRET, PID, START)
    finally:
        status.close()
        os.close(write_end)
    assert body is None and any("never recorded" in item for item in problems)


def test_proc_state_separates_an_unreadable_live_process_from_a_kernel_proven_exit(monkeypatch):
    monkeypatch.setattr(proc, "read_stat", lambda pid: None)
    monkeypatch.setattr(proc, "_exited", lambda fd: fd == 7)
    assert proc.proc_state(1, 7) == "exited" and proc.proc_state(1, 8) == "unknown"
    monkeypatch.setattr(
        proc.os, "pidfd_open", lambda pid: (_ for _ in ()).throw(ProcessLookupError())
    )
    assert proc.proc_state(1) == "exited"
    monkeypatch.setattr(proc.os, "pidfd_open", lambda pid: (_ for _ in ()).throw(PermissionError()))
    assert proc.proc_state(1) == "unknown"
    monkeypatch.setattr(proc, "read_stat", lambda pid: (1, 2, "S"))
    assert proc.proc_state(1, 8) == "alive"


def test_a_live_pinned_parent_with_an_unreadable_stat_is_unknown_not_gone(monkeypatch):
    tree = object.__new__(proc.OwnedTree)
    tree._entries, tree.contained, tree.unknown_live, tree.reason = {9: (5, 100)}, True, [], ""
    monkeypatch.setattr(proc, "_exited", lambda fd: False)
    monkeypatch.setattr(proc, "read_stat", lambda pid: None)
    assert tree._parent_instance_valid(9) is False
    assert tree.contained is False and tree.unknown_live == [9] and "unknown" in tree.reason


HOLDER_SCENARIOS = """
if name in ("kill_holder", "kill_both"):
    import signal as _signal, subprocess as _subprocess
    with open("/proc/%d/stat" % os.getppid()) as handle:
        holder = int(handle.read().rsplit(")", 1)[1].split()[1])
    guardian = os.getppid()
    if name == "kill_both":
        orphan = _subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                   start_new_session=True)
        open(os.path.join(job_root, "orphan.pid"), "w").write(str(orphan.pid))
    os.kill(holder, _signal.SIGKILL)
    if name == "kill_both":
        os.kill(guardian, _signal.SIGKILL)
    time.sleep(0.5)
    os._exit(0)
"""


def _run(tmp_path, monkeypatch, scenario, **kwargs):
    anchor = 'if name == "arbiter_loss":'
    assert gate.FAKE_WORKER.count(anchor) == 1
    monkeypatch.setattr(
        gate, "FAKE_WORKER", gate.FAKE_WORKER.replace(anchor, HOLDER_SCENARIOS + anchor)
    )
    budget = PairBudget(2)
    plan, root, report = gate.run(
        tmp_path, {"source": scenario, "cython": "slow_pass:30"}, budget=budget, **kwargs
    )
    return plan, root, report, budget


def test_a_killed_guardian_is_swept_by_the_holder_yet_stays_a_failure_that_cancels_siblings(
    tmp_path,
):
    """M2: cleanup proven by the holder, the guardian-loss failure and sibling cancellation preserved."""

    sentinel = __import__("subprocess").Popen(SENTINEL_ARGV)
    try:
        time.sleep(0.05)
        budget = PairBudget(2)
        plan, root, report = gate.run(
            tmp_path, {"source": "kill_guardian_orphan", "cython": "slow_pass:30"}, budget=budget
        )
        pid_file = next((root / plan.run_id).glob("*/orphan.pid"))
        assert not gate._alive(int(pid_file.read_text())), "the holder must have swept the orphan"
        assert gate.states(report) == {"source": "failed", "cython": "interrupted"}
        assert (
            "the job's guardian was lost" in report["cancellation"] and report["overall"] == "FAIL"
        )
        process_tree = report["cleanup"][pid_file.parent.name]["process_tree"]
        assert (
            process_tree["adoption"]["root_gone"] is True
            and process_tree["adoption"]["unattributed"] == []
        )
        assert budget.reserved_total == 0 and sentinel.poll() is None
    finally:
        stop_owned_child(sentinel)


def test_a_killed_holder_leaves_the_cleanup_unproven_and_keeps_failure_and_cancellation(
    tmp_path, monkeypatch
):
    """M3: a missing holder terminal ALWAYS withholds the proof."""

    _, _, report, budget = _run(tmp_path, monkeypatch, "kill_holder")
    assert gate.states(report) == {"source": "failed", "cython": "interrupted"}
    assert "the job's holder was lost" in report["cancellation"] and report["overall"] == "FAIL"
    assert any("cleanup unproven" in item for item in report["orchestration_problems"])
    assert budget.reserved_total == 1 and report["cleanup_complete"] is False


def test_a_job_whose_holder_and_guardian_both_died_stays_unproven_and_nothing_foreign_is_signalled(
    tmp_path, monkeypatch
):
    """M4: the residual that no user-space design closes; never a false proof, the survivor is the test's."""

    survivor = Survivor()
    with __import__("contextlib").ExitStack() as cleanup:
        cleanup.callback(survivor.remove)
        plan, root, report, budget = _run(tmp_path, monkeypatch, "kill_both")
        pid_file = next((root / plan.run_id).glob("*/orphan.pid"))
        survivor.capture(int(pid_file.read_text()))
        assert any("cleanup unproven" in item for item in report["orchestration_problems"])
        assert budget.reserved_total == 1 and report["overall"] == "FAIL"


@pytest.mark.parametrize(
    "field", ["forks", "adopted", "killed", "reaped", "reaped_unheld", "unknown", "remaining"]
)
def test_a_terminal_receipt_cannot_omit_any_instance_collection(tmp_path, field):
    ref = _receipt(tmp_path)
    body = json.loads(ref.path.read_text())
    del body[field]
    body.pop("mac")
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["mac"] = hmac.new(SECRET, text.encode(), hashlib.sha256).hexdigest()
    ref.path.write_text(json.dumps(body))
    assert ref.verdict(PID, START)["proof"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"adopted": [[777, None]]},
        {"adopted": [[777, True]]},
        {"adopted": [["777", 5]]},
        {"adopted": [[777, 5], [777, 5]], "reaped": [[777, 5]]},
        {"adopted": [[777, 5]], "reaped": []},
        {"killed": [[777, 5]]},
        {"forks": []},
        {"forks": [[5001, 111]]},
        {"guardian_status": True},
        {"reaped_unheld": [[777, None]]},
        {"reaped_unheld": [[777, 5]]},
        {"never_started": True},
    ],
)
def test_invalid_or_unreconciled_instance_accounting_is_unproven_without_raising(tmp_path, changes):
    assert _receipt(tmp_path, **changes).verdict(PID, START)["proof"] is False


def test_holder_loss_is_a_safety_loss_even_with_a_genuine_guardian_final(tmp_path):
    from scripts.production_gate_parallel_ownership import receipt_loss

    status = _Status(HolderRef(tmp_path / "missing.json", SECRET, PID))
    guardian = {"phase": "final", "contained": True, "proof": True}
    assert "holder" in receipt_loss(status, _owned(), guardian)
    assert receipt_loss(_Status(_receipt(tmp_path)), _owned(), guardian) is None


def test_holder_receipt_reader_injection_reaches_the_actual_guardian_reader(tmp_path):
    ref = _receipt(tmp_path)
    read_end, write_end = os.pipe()
    view = proc.GuardianStatus(read_end, holder=ref)
    calls = []

    def reader(*args):
        calls.append(args)
        raise RuntimeError("injected actual guardian reader failure")

    try:
        with pytest.raises(RuntimeError, match="injected actual"):
            view.read_receipts(tmp_path / "guardian.json", SECRET, PID, START, reader=reader)
        assert calls == [(tmp_path / "guardian.json", SECRET, 5000, 111)]
    finally:
        view.close()
        os.close(write_end)


def test_cgroup_membership_follows_only_the_pinned_guardian_not_the_holder():
    from types import SimpleNamespace

    from scripts.production_gate_parallel_ownership import observe_cgroup_membership

    calls = []
    tree = SimpleNamespace(
        root_pid=PID,
        root_start=START,
        holder_mode=True,
        cgroup_joined=False,
        status=SimpleNamespace(drain=lambda: None, guardian=(5000, 111)),
        cgroup=SimpleNamespace(contains=lambda pid: calls.append(pid) or True),
        _entries={PID: (7, START), 5000: (8, 111)},
        _parent_instance_valid=lambda pid: True,
    )
    observe_cgroup_membership(tree)
    assert tree.cgroup_joined is True and calls == [5000]
    tree.cgroup_joined = False
    tree._entries[5000] = (8, 112)  # recycled identity: the pipe alone never establishes ownership
    observe_cgroup_membership(tree)
    assert tree.cgroup_joined is False and calls == [5000]
    tree._entries[5000] = (8, 111)
    answers = iter([True, False])  # vanished or unreadable during membership observation
    tree._parent_instance_valid = lambda pid: next(answers)
    observe_cgroup_membership(tree)
    assert tree.cgroup_joined is False and calls == [5000, 5000]


def test_failed_child_listing_with_echild_and_empty_listing_without_echild_are_not_proofs():
    from tests._parallel_runtime_holder_kernel import Kernel

    failed = Kernel(listing="failed")
    result = failed.sweep()
    assert result["echild"] is True and result["incomplete"] is True and result["proof"] is False
    empty = Kernel([7001], listing="empty")
    result = empty.sweep()
    assert result["echild"] is False and result["proof"] is False and empty.signals == []


def test_unpinnable_and_unidentified_reaped_children_cannot_prove_instance_accounting():
    from tests._parallel_runtime_holder_kernel import Kernel

    refused = Kernel([7001], unpinnable=True)
    result = refused.sweep()
    assert result["unknown"] and result["proof"] is False and refused.signals == []
    unheld = Kernel([7001], listing="empty")
    unheld.children[7001] = False
    result = unheld.sweep()
    assert result["echild"] is True and result["reaped_unheld"] == [[7001, None]]
    assert result["proof"] is False


def test_the_actual_sweep_reaps_nested_adoptions_and_keeps_concurrent_holders_disjoint():
    from tests._parallel_runtime_holder_kernel import Kernel

    first, second = Kernel([7001], nested=True), Kernel([8001])
    left, right = first.sweep(), second.sweep()
    assert left["proof"] is True and right["proof"] is True
    assert {pid for pid, start in left["adopted"]} == {7001, 7002}
    assert {pid for pid, start in right["adopted"]} == {8001}
    assert left["adopted"] == left["reaped"] and right["adopted"] == right["reaped"]
    assert {pid for pid, signum in first.signals} == {7001, 7002}
    assert {pid for pid, signum in second.signals} == {8001}


@pytest.mark.parametrize("condition", ["owner_present", "pdeath", "subreaper"])
def test_actual_startup_never_reaches_fork_after_original_owner_or_setup_loss(tmp_path, condition):
    from tests._parallel_runtime_holder_kernel import startup

    with pytest.raises(SystemExit) as stopped:
        startup(tmp_path, **{condition: False})
    record = json.loads((tmp_path / "holder.json").read_text())
    assert stopped.value.code != 0 and record["never_started"] is True
    assert record["forks"] == [] and record["guardian"] is None
    if condition != "owner_present":
        assert record["proof"] is False and record["contained"] is False


def test_actual_startup_rechecks_owner_after_the_first_check_succeeded(tmp_path):
    from tests._parallel_runtime_holder_kernel import startup

    with pytest.raises(SystemExit):
        startup(tmp_path, owner_parents=(100, 1))
    record = json.loads((tmp_path / "holder.json").read_text())
    assert record["never_started"] is True
    assert record["forks"] == [] and record["guardian"] is None


def test_actual_producer_withholds_proof_for_a_live_child_with_unreadable_stat():
    from tests._parallel_runtime_holder_kernel import Kernel

    kernel = Kernel([7001], unreadable_stat=True)
    result = kernel.sweep()
    assert result["unknown"] and all(pid == 7001 for pid, start in result["unknown"])
    assert result["proof"] is False and result["echild"] is False
    assert result["adopted"] == [] and kernel.signals == []


@pytest.mark.parametrize("branch", ["_adopt", "observe", "pin_orphans"])
@pytest.mark.parametrize("state", ["unknown", "exited"])
def test_unreadable_child_is_not_equated_with_an_exit_in_actual_owned_tree_branches(
    monkeypatch, branch, state
):
    from types import SimpleNamespace

    closed = []
    monkeypatch.setattr(proc, "os", SimpleNamespace(getpid=lambda: 100, close=closed.append))
    monkeypatch.setattr(proc, "_pidfd_open", lambda pid: 11)
    monkeypatch.setattr(proc, "_exited", lambda descriptor: False)
    monkeypatch.setattr(proc, "proc_state", lambda *args: state)
    monkeypatch.setattr(proc, "children_of", lambda pid: [7001])
    monkeypatch.setattr(proc, "read_stat", lambda pid: (START, 100, "S") if pid == PID else None)
    monkeypatch.setattr(proc, "socket_ports", lambda pids: {})
    tree = object.__new__(proc.OwnedTree)
    tree._entries = {PID: (7, START)}
    tree.root_pid, tree.root_start = PID, START
    tree.contained, tree.unknown_live, tree.reason = True, [], ""
    tree.adopted_total, tree.ports = 0, set()
    tree._clock, tree._last, tree._observe_seconds = lambda: 1.0, 0.0, 0.01
    tree.cgroup, tree.cgroup_joined, tree.status = None, False, None
    tree.adoption = SimpleNamespace(verify=lambda: True)
    tree._known_children = lambda: set()
    if branch == "_adopt":
        assert tree._adopt(7001, 9) is False
        assert closed == [11]
    elif branch == "observe":
        tree.observe(force=True)
    else:
        tree.pin_orphans()
    assert tree.contained is (state == "exited")
    assert tree.unknown_live == ([7001] if state == "unknown" else [])
    assert tree.tracked == {PID: START}


def test_all_siblings_are_told_before_waiting_and_share_one_cleanup_deadline():
    from types import SimpleNamespace

    from scripts.production_gate_parallel_stopping import prepare_stops, stop_process
    from tests._parallel_runtime_holder_kernel import Clock

    clock, sent = Clock(), []
    roots = []
    for name in ("first", "second"):
        roots.append(
            SimpleNamespace(
                job_id=name,
                process=SimpleNamespace(
                    poll=lambda: None,
                    guardian_status=SimpleNamespace(drain=lambda: None, ready=True, eof=False),
                ),
                owned=SimpleNamespace(
                    holder_mode=True, signal_root=lambda sig, n=name: sent.append((n, clock.now))
                ),
                cleanup_deadline=None,
                stop_requested=False,
            )
        )
    hub = SimpleNamespace(expect_drop=lambda name: None)
    prepare_stops(roots, hub, 0.25, grace_seconds=5, clock=clock.monotonic, sleeper=clock.sleep)
    assert sent == [("first", 0), ("second", 0)]
    assert all(item.stop_requested and item.cleanup_deadline == 0.25 for item in roots)
    for item in roots:
        stop_process(
            item,
            item.process.guardian_status,
            item.cleanup_deadline,
            grace_seconds=5,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )
    assert clock.now == pytest.approx(0.25)  # the second job never receives a fresh wait allowance


def test_a_wedged_guardian_is_escalated_under_controlled_time_without_fabricating_exit():
    from tests._parallel_runtime_holder_kernel import wedged_guardian_wait

    state, calls, elapsed = wedged_guardian_wait()
    assert [number for number, when in calls] == [
        __import__("signal").SIGTERM,
        __import__("signal").SIGKILL,
    ]
    assert 2.3 < calls[1][1] < 2.4
    assert 5.3 < elapsed < 5.4
    assert state["guardian_killed"] is True and state["guardian_status"] is None


def test_cgroup_kill_wait_cannot_extend_an_already_shared_deadline(tmp_path):
    from scripts.production_gate_parallel_cgroup import JobCgroup
    from tests._parallel_runtime_holder_kernel import Clock

    (tmp_path / "cgroup.kill").write_text("")
    (tmp_path / "cgroup.events").write_text("populated 1\n")
    clock = Clock()
    group = JobCgroup(tmp_path)
    assert group.kill(clock=clock.monotonic, sleeper=clock.sleep, deadline=0.1) is False
    assert clock.now == pytest.approx(0.1)
    assert (tmp_path / "cgroup.kill").read_text() == "1"


NESTED_SOURCE = """
import array, ctypes, json, os, socket, subprocess, sys, time
root = sys.argv[1]
libc = ctypes.CDLL(None, use_errno=True)
flag = ctypes.c_int(0)
if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(37, ctypes.byref(flag), 0, 0, 0) != 0 or flag.value != 1:
    sys.exit(3)
child, handed_off, descriptors = None, False, []
def started(pid):
    with open("/proc/%d/stat" % pid) as handle:
        return int(handle.read().rsplit(")", 1)[1].split()[19])
try:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    for pid in (os.getpid(), child.pid):
        descriptors.append(os.pidfd_open(pid))
    body = {"token":sys.argv[3], "job_root":root,
            "nested":{"pid":os.getpid(), "start":started(os.getpid())},
            "child":{"pid":child.pid, "start":started(child.pid)}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(3.0)
        connection.connect(sys.argv[2])
        connection.sendmsg([json.dumps(body).encode()],
                           [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))])
        if connection.recv(32) != b"owned":
            raise RuntimeError("outer fixture ownership was not acknowledged")
        handed_off = True
    with open(os.path.join(root, "nested-child.json"), "w") as handle:
        json.dump(body["child"], handle)
    time.sleep(120)
finally:
    for descriptor in descriptors:
        os.close(descriptor)
    if not handed_off and child is not None:
        child.kill()
        child.wait(timeout=3.0)
"""


def test_a_real_nested_subreaper_is_swept_without_cross_job_or_foreign_signals(
    tmp_path, monkeypatch
):
    """M7 real-kernel counterpart to the deterministic producer attribution model."""

    import contextlib
    import subprocess

    from tests._parallel_runtime_nested_cleanup import NestedCleanup

    with contextlib.ExitStack() as cleanup:
        sentinel = subprocess.Popen(SENTINEL_ARGV)
        cleanup.callback(stop_owned_child, sentinel)
        outer = cleanup.enter_context(NestedCleanup(tmp_path, NESTED_SOURCE))
        nested_scenario = f"""
if name == "holder_peer_live":
    open(os.path.join(job_root, "peer.running.json"), "w").write(json.dumps(
        {{"worker_pid": os.getpid(), "guardian_pid": os.getppid()}}))
    time.sleep(30)
if name == "nested_subreaper_orphan":
    import signal as _signal, subprocess as _subprocess
    nested = _subprocess.Popen([sys.executable, "-c", {NESTED_SOURCE!r}, job_root,
                               {outer.endpoint!r}, {outer.token!r}], start_new_session=True)
    open(os.path.join(job_root, "orphan.pid"), "w").write(str(nested.pid))
    ready = os.path.join(job_root, "nested-child.json")
    peer = os.path.join(os.path.dirname(job_root), "cython-job", "peer.running.json")
    limit = time.monotonic() + 20.0
    while (not os.path.exists(ready) or not os.path.exists(peer)) and time.monotonic() < limit:
        time.sleep(0.02)
    if not os.path.exists(ready) or not os.path.exists(peer):
        os._exit(3)
    os.kill(os.getppid(), _signal.SIGKILL)
    time.sleep(0.3)
    os._exit(0)
"""
        anchor = 'if name == "arbiter_loss":'
        assert gate.FAKE_WORKER.count(anchor) == 1
        monkeypatch.setattr(
            gate, "FAKE_WORKER", gate.FAKE_WORKER.replace(anchor, nested_scenario + anchor)
        )
        budget = PairBudget(2)
        plan, root, report = gate.run(
            tmp_path,
            {"source": "nested_subreaper_orphan", "cython": "holder_peer_live"},
            budget=budget,
        )
        job = root / plan.run_id / "source-job"
        child = json.loads((job / "nested-child.json").read_text())
        assert outer.state("nested") == "gone"
        assert outer.state("child") == "gone"
        assert gate.states(report) == {"source": "failed", "cython": "interrupted"}
        assert report["cleanup"]["source-job"]["process_tree"]["proof"] is True
        holder = json.loads((job / "holder.json").read_text())
        assert holder["proof"] is True and holder["echild"] is True
        assert [child["pid"], child["start"]] in holder["adopted"]
        assert holder["adopted"] == holder["reaped"]
        assert budget.reserved_total == 0 and sentinel.poll() is None


def test_a_second_stop_request_cannot_extend_an_existing_cleanup_deadline():
    from types import SimpleNamespace

    from scripts.production_gate_parallel_stopping import prepare_stops
    from tests._parallel_runtime_holder_kernel import Clock

    clock = Clock()
    clock.now = 0.2
    item = SimpleNamespace(
        job_id="already-stopping",
        cleanup_deadline=0.25,
        stop_requested=True,
        process=SimpleNamespace(
            poll=lambda: None,
            guardian_status=SimpleNamespace(drain=lambda: None, ready=False, eof=False),
        ),
        owned=SimpleNamespace(signal_root=lambda sig: None),
    )
    prepare_stops(
        [item],
        SimpleNamespace(expect_drop=lambda job: None),
        10.0,
        grace_seconds=5,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert item.cleanup_deadline == 0.25 and clock.now == pytest.approx(0.25)


@pytest.mark.parametrize("point", ["terminate", "receipt"])
def test_holder_loss_is_latched_and_siblings_cancel_after_actual_finish_raises(point):
    from tests._parallel_runtime_finish_controls import finish_failure_case

    namespace, budget, hub, failed, sibling, sent, cancellations = finish_failure_case(point)
    assert budget.lost_reason and hub.closed_reason
    assert sibling.stop_requested is True and sibling.cleanup_deadline == failed.cleanup_deadline
    assert "sibling" in sent
    assert cancellations and cancellations[0][1] == ["sibling"]
    assert namespace["outcomes"]["failed"]["state"] == "failed"
    assert namespace["cleanup"]["failed"]["process_tree"]["proof"] is False
    assert namespace["running"] == []


def test_a_stop_reader_exception_cannot_prevent_other_owned_jobs_cleanup():
    from types import SimpleNamespace

    from scripts.production_gate_parallel_safety import stop_safely
    from tests._parallel_runtime_holder_kernel import Clock

    clock, cleanup, attempts = Clock(), {}, []
    clock.now = 0.2

    def stop(item):
        raise RuntimeError("controlled reader exception")

    def terminate(name, **kwargs):
        attempts.append((name, kwargs["deadline"]))
        if name == "first":
            raise RuntimeError("controlled owned-sweep exception")
        return {"proof": True, "remaining": []}

    for name in ("first", "second"):
        item = SimpleNamespace(
            job_id=name,
            cleanup_deadline=0.25,
            owned=SimpleNamespace(terminate=lambda n=name, **kw: terminate(n, **kw)),
        )
        assert stop_safely(item, stop, cleanup, clock=clock.monotonic, sleeper=clock.sleep)
    assert attempts == [("first", 0.25), ("second", 0.25)]
    assert all(record["process_tree"]["proof"] is False for record in cleanup.values())


@pytest.mark.parametrize("fail_fast", [False, True])
def test_timeout_stops_are_broadcast_before_any_cleanup_wait(fail_fast):
    from tests._parallel_runtime_deadline_controls import watched_jobs

    watch, clock, jobs, sent = watched_jobs((1.0, 1.0), fail_fast=fail_fast)
    clock.now = 1.0
    watch.pulse()
    assert sent == [("first", 1.0), ("second", 1.0)]
    assert watch.expired == {"first", "second"}
    assert all(item.cleanup_deadline == 1.25 for item in jobs)
    clock.now = 1.24
    watch.pulse()
    assert all(item.cleanup_deadline == 1.25 for item in jobs)


def test_a_sibling_deadline_is_enforced_during_another_jobs_cleanup_sleep():
    from tests._parallel_runtime_deadline_controls import watched_jobs

    watch, clock, jobs, sent = watched_jobs((1.0, 1.05))
    clock.now = 1.0
    watch.pulse()
    assert sent == [("first", 1.0)]
    clock.now = 1.049
    watch.sleep(0.02)  # a requested wait cannot cross the sibling's still-live deadline
    assert sent == [("first", 1.0), ("second", 1.05)]
    assert jobs[0].cleanup_deadline == 1.25
    assert jobs[1].cleanup_deadline == pytest.approx(1.3)


def test_fail_fast_timeout_stops_other_jobs_before_wait_and_does_not_call_them_timeouts():
    from tests._parallel_runtime_deadline_controls import watched_jobs

    watch, clock, jobs, sent = watched_jobs((1.0, 20.0), fail_fast=True)
    clock.now = 1.0
    watch.pulse()
    assert sent == [("first", 1.0), ("second", 1.0)]
    assert watch.expired == {"first"} and watch.fail_fast_reason
    assert all(item.cleanup_deadline == 1.25 for item in jobs)


def test_authenticated_final_cgroup_evidence_survives_guardian_exit_before_observation(tmp_path):
    from scripts.production_gate_parallel_ownership import record_cgroup_receipt
    from tests._parallel_runtime_cgroup_controls import final_cgroup_receipt

    tree, guardian, problems = final_cgroup_receipt(tmp_path)
    assert tree._entries == {} and not tree._parent_instance_valid(5000)
    assert record_cgroup_receipt(tree, guardian, problems) is True
    assert tree.cgroup_joined is True
    assert tree.cgroup_membership_evidence["guardian_start"] == 111


@pytest.mark.parametrize(
    "changes",
    [
        {"guardian_pid": 5001},
        {"guardian_start": 112},
        {"cgroup": "/different/group"},
        {"cgroup_error": "join failed"},
        {"cgroup_error": "omit"},
        {"phase": "ready"},
        {"contained": False},
        {"proof": False},
    ],
)
def test_cgroup_join_cannot_be_inferred_from_wrong_or_incomplete_evidence(tmp_path, changes):
    from scripts.production_gate_parallel_ownership import record_cgroup_receipt
    from tests._parallel_runtime_cgroup_controls import final_cgroup_receipt

    tree, guardian, problems = final_cgroup_receipt(tmp_path, **changes)
    assert record_cgroup_receipt(tree, guardian, problems) is False
    assert tree.cgroup_joined is False


def test_required_final_ownership_fails_report_and_lists_missing_started_jobs(monkeypatch):
    from types import SimpleNamespace

    from scripts import production_gate_parallel_reporting as reporting
    from scripts.production_gate_parallel_report import render_report_text

    monkeypatch.setattr(reporting, "aggregate_outcomes", lambda *a: {"overall": "PASS"})
    plan = SimpleNamespace(as_dict=lambda: {"fail_fast": False, "capacity": {}}, text="{}")
    cleanup = {
        "first": {"process_tree": {"adoption": {"cgroup": {"joined": True, "emptied": True}}}}
    }
    report = reporting.completed_report(
        plan,
        [],
        0.0,
        lambda: 1.0,
        "",
        cleanup,
        [],
        {"required": True, "qualified": True, "qualification": "initial probe passed"},
        {"first", "missing"},
        True,
    )
    assert report["overall"] == "FAIL"
    assert report["kernel_ownership"]["jobs"]["missing"] == {"joined": False, "emptied": False}
    text = render_report_text(report)
    assert "UNQUALIFIED" in text and "initial probe passed" not in text


@pytest.mark.parametrize("mac", ["\u00e9" * 64, [], {}, None, "A" * 64, "0" * 63])
def test_malformed_guardian_mac_returns_unproven_instead_of_raising(tmp_path, mac):
    path = tmp_path / "guardian.json"
    path.write_text(json.dumps({"phase": "final", "mac": mac}))
    body, problems = proc.read_guardian_receipt(path, SECRET, 5000, 111)
    assert body is None and problems == [
        "guardian receipt is not authenticated by the producer's key"
    ]


def test_nested_outer_cleanup_runs_on_an_assertion_failure_after_independent_handoff(tmp_path):
    """Real-kernel negative control, authored only: no product PASS from outer signals."""

    import subprocess
    import sys

    from tests._parallel_runtime_nested_cleanup import NestedCleanup

    job = tmp_path / "standalone-owned-fixture"
    job.mkdir()
    nested = None
    outer = NestedCleanup(tmp_path, NESTED_SOURCE)
    try:
        with pytest.raises(AssertionError, match="intentional assertion after ownership"), outer:
            nested = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    NESTED_SOURCE,
                    str(job),
                    outer.endpoint,
                    outer.token,
                ],
                start_new_session=True,
            )
            limit = time.monotonic() + 5.0
            while not outer.metadata and not outer.problem and time.monotonic() < limit:
                time.sleep(0.01)
            assert set(outer.fds) == {"nested", "child"}, outer.problem
            assert outer.state("nested") == outer.state("child") == "held"
            raise AssertionError("intentional assertion after ownership")
        receipt = json.loads((tmp_path / "outer-nested-cleanup.json").read_text())
        assert receipt["problems"] == []
        assert {item["name"] for item in receipt["actions"]} == {"nested", "child"}
        assert all(item["gone"] for item in receipt["actions"])
    finally:
        if nested is not None:
            try:
                nested.wait(
                    timeout=8.0
                )  # rejected handshakes kill/reap the direct child before exiting
            except subprocess.TimeoutExpired:
                stop_owned_child(nested)
                raise AssertionError("nested fixture did not respect its independent startup bound")
