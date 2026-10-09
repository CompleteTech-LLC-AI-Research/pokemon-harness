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
            found[node.name] = hashlib.sha256(ast.get_source_segment(text, node).encode()).hexdigest()
    assert found == {STRICT: STRICT_SHA, "_alive": ALIVE_SHA}


def _receipt(tmp_path, **changes):
    body = {
        "phase": "final", "contained": True, "proof": True, "reason": "", "subreaper_start": True,
        "subreaper_end": True, "guardian": {"pid": 5000, "start": 111}, "guardian_status": -9,
        "forks": [[5000, 111]], "adopted": [], "killed": [], "reaped": [], "reaped_unheld": [],
        "unknown": [], "remaining": [], "incomplete": False, "echild": True, "never_started": False,
        "guardian_killed": False, "elapsed": 0.2, "holder_pid": PID, "holder_start": START,
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
        {"phase": "spawned"}, {"echild": False}, {"unknown": [[7, 1]]}, {"incomplete": True},
        {"remaining": [[7, 1]]}, {"subreaper_end": False}, {"subreaper_start": None},
        {"contained": False}, {"proof": False}, {"guardian_status": None},
        {"holder_pid": PID + 1}, {"holder_start": START + 1},
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


def test_an_adopted_instance_that_is_not_proven_absent_withholds_the_proof(tmp_path, monkeypatch):
    ref = _receipt(tmp_path, adopted=[[777, 5]])
    for answer in (False, None):
        monkeypatch.setattr(holder_module, "instance_absent", lambda pid, start, a=answer: a)
        assert ref.verdict(PID, START)["proof"] is False
    monkeypatch.setattr(holder_module, "instance_absent", lambda pid, start: True)
    assert ref.verdict(PID, START)["proof"] is True


class _Status:
    def __init__(self, holder):
        self.holder = holder


def _owned(root_gone=True):
    return {"proof": True, "root": {"pid": PID, "start": START}, "adoption": {"root_gone": root_gone}}


def test_cleanup_proven_in_holder_mode_requires_the_holder_final_and_the_gone_root(tmp_path):
    ref = _receipt(tmp_path)
    lost = {"phase": "ready"}  # a killed guardian never wrote a final receipt
    assert proc.cleanup_proven(_owned(), lost, _Status(ref)) is True  # cleanup proven, job still fails
    assert proc.cleanup_proven(_owned(), None, _Status(ref)) is True
    assert proc.cleanup_proven(_owned(root_gone=False), lost, _Status(ref)) is False
    bad = _receipt(tmp_path, echild=False)
    final_guardian = {"phase": "final", "contained": True, "proof": True}
    assert proc.cleanup_proven(_owned(), final_guardian, _Status(bad)) is False  # H's final always gates
    assert proc.cleanup_proven(_owned(), {"phase": "final", "contained": True, "proof": False}, _Status(ref)) is False


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
    monkeypatch.setattr(proc.os, "pidfd_open", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
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


HOLDER_SCENARIOS = '''
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
'''


def _run(tmp_path, monkeypatch, scenario, **kwargs):
    anchor = 'if name == "arbiter_loss":'
    assert gate.FAKE_WORKER.count(anchor) == 1
    monkeypatch.setattr(gate, "FAKE_WORKER", gate.FAKE_WORKER.replace(anchor, HOLDER_SCENARIOS + anchor))
    budget = PairBudget(2)
    plan, root, report = gate.run(
        tmp_path, {"source": scenario, "cython": "slow_pass:30"}, budget=budget, **kwargs
    )
    return plan, root, report, budget


def test_a_killed_guardian_is_swept_by_the_holder_yet_stays_a_failure_that_cancels_siblings(tmp_path):
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
        assert "the job's guardian was lost" in report["cancellation"] and report["overall"] == "FAIL"
        process_tree = report["cleanup"][pid_file.parent.name]["process_tree"]
        assert process_tree["adoption"]["root_gone"] is True and process_tree["adoption"]["unattributed"] == []
        assert budget.reserved_total == 0 and sentinel.poll() is None
    finally:
        stop_owned_child(sentinel)


def test_a_killed_holder_leaves_the_cleanup_unproven_and_keeps_failure_and_cancellation(
    tmp_path, monkeypatch
):
    """M3: a missing holder terminal ALWAYS withholds the proof."""

    _, _, report, budget = _run(tmp_path, monkeypatch, "kill_holder")
    assert gate.states(report)["source"] == "failed" and report["overall"] == "FAIL"
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
