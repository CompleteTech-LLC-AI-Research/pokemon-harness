"""Controlled actual B0 callgraphs. All cases NOTRUN; no kernel qualification."""

import __future__

import ast
import signal
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel as scheduler
from scripts import production_gate_parallel_arbiter as arbiter
from scripts import production_gate_parallel_job_lifecycle as life
from scripts import production_gate_parallel_job_retirement as retirement
from scripts import production_gate_parallel_ownership as ownership
from scripts import production_gate_parallel_processes as proc
from scripts import production_gate_parallel_safety as safety
from scripts.production_gate_parallel_service import OwnerService
from scripts.production_gate_parallel_stopping import DeadlineWatch, prepare_stops, stop_process
from tests import test_parallel_runtime_job_publication as publication_controls
from tests._parallel_runtime_report_support import make_plan
from tests.test_parallel_runtime_owner_service import run_final_boundary

publication = publication_controls.publication


@pytest.fixture
def controlled(monkeypatch, tmp_path):
    """Real slots/tree/status/sweep/service, with OS observations controlled."""
    c = SimpleNamespace(now=1.0, closed=[], effects=[], jobs=[], pending={}, cleanup={}, polls=[])
    c.plan = make_plan(modes=("source", "cython"))
    c.notice = retirement.HubLossNotice()
    c.hub = arbiter.ArbiterHub(2, on_loss=c.notice.notify, clock=lambda: c.now)
    c.budget = SimpleNamespace(mark_lost=lambda reason: c.effects.append(("loss", reason)))

    def close(fd):
        c.closed.append(fd)
        if c.close_hook is not None:
            c.close_hook(fd)

    c.close_hook = None
    fake_os = SimpleNamespace(
        close=close,
        set_blocking=lambda *a: None,
        read=lambda *a: b"",
        getpid=lambda: 12345,
        waitid=lambda *a: c.effects.append(("waitid", a)),
        P_PIDFD=3,
        WEXITED=4,
        WNOHANG=1,
    )
    monkeypatch.setattr(proc, "os", fake_os)
    monkeypatch.setattr(retirement, "os", fake_os)
    monkeypatch.setattr(proc, "read_stat", lambda pid: (1000 + pid, 12345, "R"))
    monkeypatch.setattr(proc, "_pidfd_open", lambda pid: 800 + pid)
    monkeypatch.setattr(proc, "_exited", lambda fd: c.states[fd - 800].dead)
    monkeypatch.setattr(proc, "socket_ports", lambda pids: {})
    monkeypatch.setattr(proc, "children_of", lambda pid: [])
    monkeypatch.setattr(
        proc.signal, "pidfd_send_signal", lambda fd, sig: c.effects.append(("signal", fd, sig))
    )
    c.states = {}

    def sleep(seconds):
        c.now += max(seconds, 0.001)

    c.watch = DeadlineWatch(
        c.jobs,
        c.hub,
        0.25,
        lambda item: False,
        grace_seconds=0.01,
        clock=lambda: c.now,
        sleeper=sleep,
        problems=[],
    )
    c.service = OwnerService(
        c.jobs,
        c.hub,
        c.budget,
        SimpleNamespace(check=lambda: None),
        c.watch,
        c.plan,
        None,
        cleanup_seconds=0.25,
        clock=lambda: c.now,
        loss_notice=c.notice,
    )
    c.watch.owner_loss_delivery = lambda: c.service.consume_hub_loss(deadlines_current=True)

    def make(job_id="source-job", pid=10, dead=True, holder=None):
        state = SimpleNamespace(dead=dead, mode="normal")
        c.states[pid] = state

        def poll():
            c.polls.append((pid, state.dead))
            if state.mode == "error":
                raise OSError("original poll uncertain")
            if state.mode == "interrupt":
                raise KeyboardInterrupt("original poll interrupted")
            if state.mode == "reaped_interrupt":
                process.returncode = 0
                raise KeyboardInterrupt("original wait mutated before result commit")
            return 0 if state.dead else None

        view = proc.GuardianStatus.prepare(holder=holder)
        view.configure(900 + pid)
        view.ready = True
        process = SimpleNamespace(pid=pid, returncode=None, poll=poll, guardian_status=view)
        slot = life.JobLaunch(job_id, process=process, status=view, spawn_attempted=True)
        tree = proc.OwnedTree.prepare(pid, "token", status=view, clock=lambda: c.now)
        slot.owned = tree
        tree.initialize_original(process, slot)
        root = tmp_path / job_id
        root.mkdir()
        (root / "out").mkdir()
        item = life._Running(
            job_id,
            "selected",
            process,
            1.0,
            2.0,
            {"root": root, "out": root / "out"},
            "token",
            tree,
            datetime(2026, 10, 15, tzinfo=UTC),
            root / "receipt",
            b"key",
            cleanup_deadline=2.25,
        )
        slot.item, slot.setup_complete, slot.cleanup_cap = item, True, 2.25
        item.launch = slot
        tree._custody_slot = view._custody_slot = slot
        c.jobs.append(item)
        return item

    c.make = make
    c.reader = lambda *a: (
        {"phase": "final", "contained": True, "proof": True, "worker_status": 0, "worker_pid": 20},
        [],
    )
    return c


def actual_functions(c):
    """Compile actual nested caller definitions; effects/evidence remain controlled."""
    source = Path(scheduler.__file__).read_text()
    outer = next(n for n in ast.parse(source).body if getattr(n, "name", "") == "_run_plan")
    nodes = [
        n
        for n in outer.body
        if isinstance(n, ast.FunctionDef) and n.name in ("finish", "harvest", "stop", "cancel")
    ]
    namespace = {
        "retirement": retirement,
        "safety": safety,
        "running": c.jobs,
        "outcomes": c.pending,
        "cleanup": c.cleanup,
        "body": c.plan.as_dict(),
        "plan": c.plan,
        "hub": c.hub,
        "budget": c.budget,
        "owner_service": c.service,
        "deadlines": c.watch,
        "clock": lambda: c.now,
        "wall_clock": lambda: datetime(2026, 10, 15, tzinfo=UTC),
        "sleeper": c.watch.sleep,
        "grace_seconds": 0.01,
        "read_guardian_receipt": c.reader,
        "cleanup_proven": proc.cleanup_proven,
        "record_cgroup_receipt": ownership.record_cgroup_receipt,
        "holder_loss": ownership.holder_loss,
        "latch_holder_loss": ownership.latch_holder_loss,
        "receipt_loss": ownership.receipt_loss,
        "prepare_stops": prepare_stops,
        "stop_process": stop_process,
        "_close_handles": scheduler._close_handles,
        "total_outcome": scheduler.total_outcome,
        "outcome_record": scheduler.outcome_record,
        "_finish_job": lambda *a, **k: {
            "state": "passed",
            "artifact_problems": [],
            "capacity": None,
        },
        "child_capacity_loss": lambda cap: None,
        "close_handoff": lambda job: None,
        "capacity_policy_text": None,
        "measure": None,
        "require_kernel_ownership": False,
        "ports_seen": {},
        "out_manifests": {},
        "_manifest": scheduler._manifest,
        "may_cancel_siblings": lambda *a: False,
        "progress": lambda: None,
        "cancel_reason": "",
        "recover": lambda item: (None, None),
    }

    def settle(job, state, reason, status, **kwargs):
        c.pending[job] = {"state": state, "reason": reason, "child_exit_status": status}

    namespace["settle"] = settle

    class Bindings(ast.NodeTransformer):
        def visit_Nonlocal(self, node):
            return ast.copy_location(ast.Global(names=node.names), node)

    nodes = [Bindings().visit(node) for node in nodes]
    exec(  # noqa: S102 - actual source AST, effects replaced at OS/evidence boundaries
        compile(
            ast.Module(body=nodes, type_ignores=[]),
            "actual-b0-callers",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    return SimpleNamespace(
        **{name: namespace[name] for name in ("finish", "harvest", "stop", "cancel")}
    )


@pytest.mark.parametrize("negative", ["raw", "holder", "reader", "authority"])
def test_actual_finish_negative_custody_survives_running_removal(controlled, monkeypatch, negative):
    c = controlled
    holder = None
    if negative == "holder":
        holder = SimpleNamespace(
            verdict=lambda *a: {"proof": False}, read_receipts=lambda *a, **k: (None, [])
        )
    item = c.make(holder=holder)
    if negative == "raw":
        monkeypatch.setattr(proc, "socket_ports", lambda pids: {10: {1234}})
    if negative == "reader":
        c.reader = lambda *a: (_ for _ in ()).throw(OSError("receipt refused"))
    callers = actual_functions(c)
    if negative == "authority":
        callers.finish.__globals__["cleanup_proven"] = lambda *a: (_ for _ in ()).throw(
            ValueError("strict authority refused")
        )
    callers.harvest()
    slot = item.launch
    assert item not in c.jobs and c.pending[item.job_id]["state"] == "failed"
    assert slot.owned._entries == {10: (810, 1010)}
    assert slot.status.fd == 910 and slot.status.close_state == "open"
    assert not c.closed and not slot.release_eligible(True)
    assert c.cleanup[item.job_id]["process_tree"]["proof"] is False


@pytest.mark.parametrize("method", ["finish", "stop"])
@pytest.mark.parametrize("failed_outcome", [False, True])
def test_actual_positive_callers_close_after_strict_authority_once(
    controlled, method, failed_outcome
):
    c = controlled
    item = c.make()
    callers = actual_functions(c)
    trace = []
    real = proc.cleanup_proven

    def authority(receipt, guardian, view):
        assert item.owned._entries and view.fd == 910
        trace.append("authority")
        return real(receipt, guardian, view)

    callers.stop.__globals__["cleanup_proven"] = authority

    def finish_evidence(*args, **kwargs):
        assert item.owned._entries and item.process.guardian_status.fd == 910
        trace.append("final_measurement")
        return {
            "state": "failed" if failed_outcome else "passed",
            "artifact_problems": ["original failure"] if failed_outcome else [],
            "capacity": None,
        }

    def last_status(view, receipt, guardian):
        assert item.owned._entries and view.fd == 910
        trace.append("last_status")
        return ownership.receipt_loss(view, receipt, guardian)

    callers.finish.__globals__["_finish_job"] = finish_evidence
    callers.finish.__globals__["receipt_loss"] = last_status
    c.close_hook = lambda fd: trace.append(("close", fd, item in c.jobs))
    callers.harvest() if method == "finish" else callers.stop(item)
    assert trace[0] == "authority"
    close_events = [event for event in trace if isinstance(event, tuple)]
    assert c.closed == [810, 910] and all(not event[2] for event in close_events)
    if method == "finish":
        assert (
            trace.index("authority") < trace.index("final_measurement") < trace.index("last_status")
        )
        assert trace.index("last_status") < trace.index(close_events[0])
    assert item.launch.custody_state == "disposed" and item.launch.release_eligible(True)
    if method == "finish":
        assert c.pending[item.job_id]["state"] == ("failed" if failed_outcome else "passed")
    if method == "finish" and failed_outcome:
        assert c.pending[item.job_id]["state"] == "failed"
        assert "original failure" in c.pending[item.job_id]["artifact_problems"]
    before = list(c.closed)
    assert retirement.fallback_record(item, c.cleanup[item.job_id])
    with pytest.raises(retirement.CustodyError):
        item.process.guardian_status.close()
    assert c.closed == before


def exercise_stale_paths(c, item):
    """Actual service/notification/fallback paths with deliberately stale membership."""
    c.jobs.insert(0, item)
    c.service.pulse()
    c.watch.pulse()
    c.watch.sleep(0)
    prepare_stops(
        [item], c.hub, 2.25, grace_seconds=0.01, clock=lambda: c.now, sleeper=c.watch.sleep
    )
    ownership.signal_guardians([item], c.watch.problems)
    with pytest.raises(retirement.CustodyError):
        stop_process(
            item,
            item.process.guardian_status,
            2.25,
            grace_seconds=0.01,
            clock=lambda: c.now,
            sleeper=c.watch.sleep,
        )
    assert safety.stop_safely(
        item, actual_functions(c).stop, c.cleanup, clock=lambda: c.now, sleeper=c.watch.sleep
    )
    actual_functions(c).harvest()
    assert item not in c.jobs


@pytest.mark.parametrize("target", ["pin", "status"])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_ambiguous_actual_close_has_no_active_use_on_cancel_and_fallback(controlled, target, error):
    c = controlled
    item = c.make()
    sibling = c.make("cython-job", 11, dead=False)
    target_fd = 810 if target == "pin" else 910

    def hook(fd):
        if fd == target_fd:
            raise error("close mutated then escaped")

    c.close_hook = hook
    callers = actual_functions(c)
    with pytest.raises(
        error if target == "pin" or error is KeyboardInterrupt else retirement.CustodyError
    ):
        callers.finish(item, 0, allow_cancel=False)
    # Pin OSError is propagated as the original OSError; status suppresses it.
    assert item.launch.custody_use != "active" and item not in c.jobs
    before_closed, before_polls = list(c.closed), len(c.polls)
    exercise_stale_paths(c, item)
    assert c.closed == before_closed and all(pid != 10 for pid, _ in c.polls[before_polls:])
    assert sibling.launch.custody_state == "holding"
    assert not item.launch.release_eligible(True)
    transaction = item.launch.custody_dispositions[10 if target == "pin" else "status"]
    assert transaction["state"] == "unknown"
    if target == "status" and error is KeyboardInterrupt:
        assert item.process.guardian_status.fd == 910
        assert item.process.guardian_status.close_state == "closing"
    c.close_hook = None
    callers.cancel("external interrupt")
    assert c.closed.count(target_fd) == 1


@pytest.mark.parametrize("prefix", ["remove", "detach"])
def test_interrupted_barrier_prefix_blocks_stale_services_without_close(
    controlled, monkeypatch, prefix
):
    c = controlled
    item = c.make()
    real_remove = retirement.remove_active

    def remove(it, running):
        if it is item:
            assert item.launch.custody_use == "quiescing"
            if prefix == "detach":
                real_remove(it, running)
            raise KeyboardInterrupt("barrier prefix interrupted")
        real_remove(it, running)

    monkeypatch.setattr(retirement, "remove_active", remove)
    with pytest.raises(KeyboardInterrupt):
        actual_functions(c).finish(item, 0, allow_cancel=False)
    assert item.launch.custody_entries == {10: (810, 1010)} and not c.closed
    monkeypatch.setattr(retirement, "remove_active", real_remove)
    exercise_stale_paths(c, item)
    assert not c.closed and not item.launch.release_eligible(True)


@pytest.mark.parametrize("target", ["pin", "status", "status_body"])
def test_successful_close_before_commit_is_quarantined_not_repeated(controlled, target):
    c = controlled
    item = c.make()

    class InterruptCommit(dict):
        def __setitem__(self, key, value):
            if key == "state" and value == "closed":
                raise KeyboardInterrupt("success before transaction commit")
            super().__setitem__(key, value)

    def hook(fd):
        if fd == (810 if target == "pin" else 910):
            key = 10 if target == "pin" else "status"
            # Preserve the real retained transaction, changing only its commit effect.
            transaction = item.launch.custody_dispositions[key]
            item.launch.custody_dispositions[key] = InterruptCommit(transaction)

    # For pin's already captured dict inject at its state commit through the snapshot map.
    c.close_hook = hook
    # Status transaction is fetched after its real close; pin commit needs a trace
    # interruption immediately after the real controlled close returns instead.
    if target in ("pin", "status_body"):
        import sys

        def trace(frame, event, arg):
            if event == "line" and (
                (
                    target == "pin"
                    and frame.f_code is retirement._close_pins.__code__
                    and c.closed == [810]
                    and frame.f_locals["transaction"]["state"] == "closing"
                )
                or (
                    target == "status_body"
                    and frame.f_code is proc.GuardianStatus.close.__code__
                    and c.closed == [810, 910]
                    and item.process.guardian_status.close_state == "closing"
                )
            ):
                sys.settrace(None)
                raise KeyboardInterrupt("pin success before commit")
            return trace

        previous_trace = sys.gettrace()
        sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt):
            actual_functions(c).finish(item, 0, allow_cancel=False)
    finally:
        if target in ("pin", "status_body"):
            sys.settrace(previous_trace)
    before = list(c.closed)
    exercise_stale_paths(c, item)
    assert c.closed == before and not item.launch.release_eligible(True)


@pytest.mark.parametrize("state", ["none", "error", "interrupt", "reaped_interrupt"])
def test_final_wait_uncertainty_blocks_actual_fallback_watch_and_service(
    controlled, monkeypatch, state
):
    c = controlled
    item = c.make(dead=False)
    callers = actual_functions(c)
    before_final = []
    real_authority = proc.cleanup_proven

    def authority(receipt, guardian, view):
        proven = real_authority(receipt, guardian, view)
        before_final.append(len(c.polls))
        c.states[10].mode = "normal" if state == "none" else state
        c.states[10].dead = state != "none"
        return proven

    def children(pid):
        c.states[pid].dead = True
        return []

    monkeypatch.setattr(proc, "children_of", children)
    callers.stop.__globals__["cleanup_proven"] = authority
    real_poll = item.process.poll
    if state in ("interrupt", "reaped_interrupt"):
        with pytest.raises(KeyboardInterrupt):
            safety.stop_safely(
                item, callers.stop, c.cleanup, clock=lambda: c.now, sleeper=c.watch.sleep
            )
    else:
        safety.stop_safely(
            item, callers.stop, c.cleanup, clock=lambda: c.now, sleeper=c.watch.sleep
        )
    attempted = len(c.polls)
    assert attempted == before_final[0] + 1 and item.launch.final_wait_attempted
    assert not item.launch.final_wait_complete
    if state == "reaped_interrupt":
        assert item.process.returncode == 0
    c.states[10].dead, c.states[10].mode = True, "normal"
    # If a fallback ever calls the original again it could now falsely upgrade status.
    assert item.process.poll is real_poll
    exercise_stale_paths(c, item)
    c.service.consume_hub_loss()
    with pytest.raises(retirement.CustodyError):
        retirement.sweep_owned(
            item, grace_seconds=0.01, deadline=2.25, clock=lambda: c.now, sleeper=c.watch.sleep
        )
    assert len(c.polls) == attempted and retirement.final_poll(item) is None
    assert not c.closed and not item.launch.release_eligible(True)


def test_root_exit_during_actual_sweep_moves_same_final_original_wait(controlled, monkeypatch):
    c = controlled
    item = c.make(dead=False)

    def children(pid):
        c.states[pid].dead = True
        return []

    monkeypatch.setattr(proc, "children_of", children)
    callers = actual_functions(c)
    callers.cancel("controlled cancellation")
    slot = item.launch
    assert c.polls[-1] == (10, True) and any(not dead for _, dead in c.polls[:-1])
    assert slot.final_wait_complete and slot.final_wait_status == 0
    assert slot.custody_state == "disposed" and c.closed == [810, 910]
    assert c.pending[item.job_id]["child_exit_status"] == 0
    count = len(c.polls)
    c.watch.pulse()
    c.service.pulse()
    assert len(c.polls) == count and item.cleanup_deadline <= 2.25


@pytest.mark.parametrize(
    "method",
    [
        "observe",
        "pin_orphans",
        "_adopt",
        "_alive_set",
        "_parent_instance_valid",
        "_reap_orphans",
        "_signal",
        "signal_root",
        "terminate",
        "close",
        "drain",
        "read_receipts",
        "absent_proof",
        "status_close",
        "cgroup",
    ],
)
def test_actual_resource_fences_precede_every_quarantined_effect(controlled, method):
    c = controlled
    item = c.make()
    retirement.enter_finish(item, 0, c.jobs, c.pending)
    retirement.dispose(item, c.jobs, {"proof": True}, True)
    before = list(c.closed), list(c.polls), list(c.effects)
    tree, view = item.owned, item.process.guardian_status
    calls = {
        "observe": lambda: tree.observe(force=True),
        "pin_orphans": tree.pin_orphans,
        "_adopt": lambda: tree._adopt(55, 1),
        "_alive_set": tree._alive_set,
        "_parent_instance_valid": lambda: tree._parent_instance_valid(10),
        "_reap_orphans": tree._reap_orphans,
        "_signal": lambda: tree._signal({10}, signal.SIGTERM),
        "signal_root": lambda: tree.signal_root(signal.SIGTERM),
        "terminate": lambda: tree.terminate(grace_seconds=0.01, deadline=2.25),
        "close": tree.close,
        "drain": view.drain,
        "read_receipts": lambda: view.read_receipts(item.receipt, b"key", 10, 1010),
        "absent_proof": view.absent_proof,
        "status_close": view.close,
        "cgroup": lambda: ownership.observe_cgroup_membership(tree),
    }
    with pytest.raises(retirement.CustodyError):
        calls[method]()
    assert (c.closed, c.polls, c.effects) == before


def test_foreign_active_slot_cannot_lend_status_or_tree_authority(controlled):
    c = controlled
    first, second = c.make(), c.make("cython-job", 11)
    first.process.guardian_status._custody_slot = second.launch
    with pytest.raises(retirement.CustodyError):
        first.process.guardian_status.drain()
    first.owned._custody_slot = second.launch
    with pytest.raises(retirement.CustodyError):
        first.owned.observe()
    assert not c.closed and not c.polls


@pytest.mark.parametrize("failure", ["before", "record", "disposed"])
def test_actual_finish_fallback_preserves_negative_or_completed_facts(controlled, failure):
    c = controlled
    item = c.make()
    callers = actual_functions(c)
    if failure == "disposed":
        callers.harvest()
    elif failure == "record":
        c.cleanup[item.job_id] = {"process_tree": {"proof": True}}
    safety.fail_finish(
        scheduler.outcome_record,
        c.plan,
        item,
        0,
        ValueError("primary"),
        c.pending,
        c.cleanup,
        c.jobs,
        deadline=2.25,
        clock=lambda: c.now,
        sleeper=c.watch.sleep,
    )
    assert c.pending[item.job_id]["state"] == "failed"
    assert "primary" in c.pending[item.job_id]["reason"]
    assert c.cleanup[item.job_id]["process_tree"]["proof"] is (failure == "disposed")
    assert len(c.closed) == (2 if failure == "disposed" else 0)


def capture_installed_hub(publication, monkeypatch):
    hubs = []
    original = arbiter.ArbiterHub.__init__

    def init(hub, *args, **kwargs):
        original(hub, *args, **kwargs)
        hubs.append(hub)

    monkeypatch.setattr(arbiter.ArbiterHub, "__init__", init)
    publication.run()
    assert len(hubs) == 1
    hub = hubs[0]
    assert hub.on_loss.__func__ is retirement.HubLossNotice.notify
    assert isinstance(hub.on_loss.__self__, retirement.HubLossNotice)
    return hub


@pytest.mark.parametrize("trigger", ["lose", "violation", "dispatch", "disconnect"])
def test_actual_installed_callback_and_real_hub_triggers_have_no_job_capability(
    publication, monkeypatch, trigger
):
    p = publication
    hub = capture_installed_hub(p, monkeypatch)
    # Actual callback is captured from construction, not replaced with a test no-op.
    before = list(p.closed), list(p.polls), list(p.pins)
    hub.register("late", 1)
    job = hub._jobs["late"]
    if trigger == "lose":
        hub.lose("first loss")
    elif trigger == "violation":
        for _ in range(arbiter.VIOLATION_LIMIT):
            hub._violation(job)
    elif trigger == "dispatch":
        hub._dispatch(
            job,
            {
                "op": "event",
                "kind": "capacity_loss",
                "job": "late",
                "reason": "physical capacity lost",
            },
            123,
        )
    else:
        job.active.add("pair")
        hub._stopping.clear()

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def settimeout(self, value):
                pass

            def recv(self, limit):
                return next(messages)

        messages = iter([b"request", b""])
        monkeypatch.setattr(hub, "_peer", lambda conn: (123, 456))
        monkeypatch.setattr(arbiter.os, "getuid", lambda: 456)
        monkeypatch.setattr(hub, "_handle", lambda *a: (job, 1))
        hub._serve(Connection())  # real finally branch; no listener/thread/peer syscall
    assert hub.closed_reason and hub.on_loss.__self__.event.is_set()
    first = hub.closed_reason
    hub.lose("second loss")
    assert hub.closed_reason == first
    assert (p.closed, p.polls, p.pins) == before
    with hub._lock:
        assert hub._reserve(job, "new", hub.closed_reason)[0] == "closed"


def test_delayed_real_callback_after_commit_has_finite_owner_delivery(controlled):
    c = controlled
    first = c.make(dead=False)
    second = c.make("cython-job", 11, dead=False)
    installed = c.hub.on_loss
    delayed = []
    c.hub.on_loss = delayed.append  # delay delivery, preserve actual Hub.lose commit
    c.hub.lose("first committed reason")
    assert not c.notice.event.is_set()
    c.watch.pulse()  # actual tail consumer, no recursive DeadlineWatch.pulse
    assert c.service.hub_loss_reason == c.hub.closed_reason == "first committed reason"
    assert c.service.cancel_reason and first.stop_requested and second.stop_requested
    assert c.service._hub_notification_attempted and not c.service._delivering_hub_loss
    assert all(item.cleanup_deadline <= 1.25 for item in c.jobs)
    signals = [e for e in c.effects if e[0] == "signal"]
    assert {e[1] for e in signals} == {810, 811}
    installed(delayed[0])
    c.service.pulse()
    assert len([e for e in c.effects if e[0] == "signal"]) == len(signals)
    assert not c.service._active and c.notice.event.is_set()


@pytest.mark.parametrize("boundary", ["before", "barrier", "pin", "status", "after"])
def test_real_hub_loss_interleavings_leave_retiring_capabilities_unused(
    controlled, monkeypatch, boundary
):
    c = controlled
    item = c.make()
    sibling = c.make("cython-job", 11, dead=False)
    calls = actual_functions(c)
    real_remove = retirement.remove_active

    def loss():
        c.hub.lose("first during retirement")

    if boundary == "before":
        loss()
    elif boundary == "barrier":

        def remove(it, running):
            if it is item:
                assert item.launch.custody_use == "quiescing"
                loss()
            real_remove(it, running)

        monkeypatch.setattr(retirement, "remove_active", remove)
    elif boundary in ("pin", "status"):
        c.close_hook = lambda fd: loss() if fd == (810 if boundary == "pin" else 910) else None
    else:
        original_consume = c.service.consume_hub_loss

        def consume(**kwargs):
            if item.launch.custody_state == "disposed":
                loss()
            original_consume(**kwargs)

        monkeypatch.setattr(c.service, "consume_hub_loss", consume)
    calls.finish(item, 0, allow_cancel=False)
    assert c.pending[item.job_id]["state"] == "failed"
    assert item.launch.custody_state == "disposed" and c.closed == [810, 910]
    assert sibling.stop_requested and item not in c.jobs
    assert c.service.hub_loss_reason == "first during retirement"
    assert c.hub.on_loss.__self__.event.is_set()
    assert (
        all(event[1] != 810 for event in c.effects if event[0] == "signal") or boundary == "before"
    )


def test_report_seal_catches_committed_reason_even_empty_and_delayed(controlled):
    c = controlled
    c.hub.on_loss = lambda reason: None  # controlled delay only, actual lose still commits
    c.hub.lose("loss before seal")
    report = {"overall": "PASS", "cancellation": "earlier interrupt", "orchestration_problems": []}
    retirement.seal_report(report, c.hub)
    assert report["overall"] == "FAIL" and report["cancellation"] == "earlier interrupt"
    assert "loss before seal" in report["orchestration_problems"][0]
    assert not c.jobs and not c.closed and not c.polls
    c.notice.notify(c.hub.closed_reason)
    assert c.notice.event.is_set()


def test_new_post_seal_loss_is_explicit_b2_gap_not_retroactive_report_mutation(controlled):
    c = controlled
    report = {"overall": "PASS", "cancellation": "", "orchestration_problems": []}
    retirement.seal_report(report, c.hub)
    c.hub.lose("new after seal")
    assert c.notice.event.is_set() and report["overall"] == "PASS"  # B2 remains NOTADMITTED
    assert not c.closed and not c.polls


@pytest.mark.parametrize("loss", ["interrupt", "capacity", "policy", "lease"])
@pytest.mark.parametrize("next_batch", [False, True])
def test_actual_final_measurement_preserves_cancel_latch_and_no_next_batch(
    tmp_path, monkeypatch, loss, next_batch
):
    result = run_final_boundary(
        tmp_path,
        monkeypatch,
        loss=loss,
        modes=("source", "cython") if next_batch else ("source",),
        serial=True,
    )
    assert result.report["overall"] == "FAIL"
    assert result.report["jobs"][0]["state"] == "failed"
    assert result.launched == result.admitted == ["source-job"]
    if next_batch:
        assert result.report["jobs"][1]["state"] == "not_started"


def test_actual_scheduler_negative_cleanup_blocks_capacity_and_next_batch(publication):
    p = publication
    report = p.run("unproven")
    slot = p.ledgers[0].slots["source-job"]
    assert slot.owned._entries and slot.status.fd >= 0
    assert slot.custody_state == "holding" and not slot.release_eligible(True)
    assert p.budget.reserved_total == 1
    assert report["jobs"][1]["state"] == "not_started" and report["overall"] == "FAIL"


def test_legacy_unmanaged_sweep_still_owns_its_old_close_contract(controlled):
    c = controlled
    item = c.make()
    del item.owned._custody_slot
    del item.process.guardian_status._custody_slot
    del item.launch
    receipt = retirement.sweep_owned(
        item, grace_seconds=0.01, deadline=2.25, clock=lambda: c.now, sleeper=c.watch.sleep
    )
    assert receipt["proof"] is True and c.closed == [810]
    item.process.guardian_status.close()
    assert c.closed == [810, 910]


@pytest.mark.parametrize("failure", ["raw", "reader", "sweep", "strict"])
def test_actual_managed_stop_and_bounded_fallback_keep_original_custody(
    controlled, monkeypatch, failure
):
    c = controlled
    item = c.make()
    if failure == "raw":
        monkeypatch.setattr(proc, "socket_ports", lambda pids: {10: {1234}})
    elif failure == "reader":
        c.reader = lambda *a: (_ for _ in ()).throw(OSError("stop receipt error"))
    elif failure == "sweep":
        monkeypatch.setattr(
            proc,
            "socket_ports",
            lambda pids: (_ for _ in ()).throw(OSError("sweep observation error")),
        )
    callers = actual_functions(c)
    if failure == "strict":
        callers.stop.__globals__["cleanup_proven"] = lambda *a: (_ for _ in ()).throw(
            ValueError("stop strict proof error")
        )
    safety.stop_safely(item, callers.stop, c.cleanup, clock=lambda: c.now, sleeper=c.watch.sleep)
    assert item.owned._entries == {10: (810, 1010)}
    assert item.process.guardian_status.fd == 910
    assert not c.closed and not item.launch.release_eligible(True)
    assert c.cleanup[item.job_id]["process_tree"]["proof"] is False
    assert item.cleanup_deadline <= 2.25


def test_interruption_after_actual_detachment_before_first_close_stays_barred(controlled):
    import sys

    c = controlled
    item = c.make()
    previous = sys.gettrace()

    def trace(frame, event, arg):
        if event == "call" and frame.f_code is retirement._close_pins.__code__:
            assert item not in c.jobs and not item.owned._entries
            assert item.launch.custody_entries == {10: (810, 1010)}
            sys.settrace(None)
            raise KeyboardInterrupt("detached before first close")
        return trace

    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt):
            actual_functions(c).finish(item, 0, allow_cancel=False)
    finally:
        sys.settrace(previous)
    exercise_stale_paths(c, item)
    assert not c.closed and not item.launch.release_eligible(True)


def test_uninitialized_original_and_partial_transfer_receive_no_disposal_permit(controlled):
    c = controlled
    item = c.make()
    item.launch.initialized = False
    item.launch.pin_state, item.launch.raw_pidfd = "transferring", 810
    assert not retirement.enter_finish(item, 0, c.jobs, c.pending)
    retirement.dispose(item, c.jobs, {"proof": True}, True)
    assert not c.closed and not c.polls and item.launch.raw_pidfd == 810
    assert item.launch.custody_state == "holding" and not item.launch.release_eligible(True)


@pytest.mark.parametrize("boundary", ["barrier", "close"])
def test_actual_installed_callback_overlaps_real_scheduler_disposition(
    publication, monkeypatch, boundary
):
    p, hubs, observed = publication, [], []
    original_init = arbiter.ArbiterHub.__init__
    original_remove = retirement.remove_active
    original_close = retirement.os.close

    def init(hub, *args, **kwargs):
        original_init(hub, *args, **kwargs)
        hubs.append(hub)

    def lose():
        before = list(p.closed), list(p.polls), list(p.pins)
        hubs[0].lose("installed callback during disposal")
        observed.append(before == (p.closed, p.polls, p.pins))

    def remove(item, running):
        if boundary == "barrier" and item.launch.custody_use == "quiescing":
            lose()
        original_remove(item, running)

    def close(fd):
        original_close(fd)
        if boundary == "close" and fd == 800:
            lose()

    monkeypatch.setattr(arbiter.ArbiterHub, "__init__", init)
    monkeypatch.setattr(retirement, "remove_active", remove)
    monkeypatch.setattr(retirement.os, "close", close)
    report = p.run(serial=False)
    assert observed and all(observed)
    assert hubs[0].on_loss.__func__ is retirement.HubLossNotice.notify
    assert hubs[0].on_loss.__self__.event.is_set()
    assert report["overall"] == "FAIL" and report["cancellation"]
    assert all(slot.custody_state == "disposed" for slot in p.ledgers[0].slots.values())


@pytest.mark.parametrize("escape", [OSError, KeyboardInterrupt])
def test_disposition_failure_keeps_original_outcome_evidence(controlled, escape):
    c = controlled
    item = c.make()
    callers = actual_functions(c)
    original_artifact = {"overall": "FAIL", "primary": "worker failure"}
    callers.finish.__globals__["_finish_job"] = lambda *a, **k: {
        "state": "failed",
        "artifact_problems": ["original failure"],
        "reason": "original worker failure",
        "artifact": original_artifact,
        "capacity": None,
    }
    c.close_hook = lambda fd: (_ for _ in ()).throw(escape("close escaped")) if fd == 810 else None
    if escape is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            callers.harvest()
        retirement.restore_pending_outcomes(
            SimpleNamespace(slots={item.job_id: item.launch}), c.pending
        )
    else:
        callers.harvest()  # actual safety.fail_finish must use the retained pending row
    outcome = c.pending[item.job_id]
    assert outcome["state"] == "failed" and outcome["artifact"] is original_artifact
    assert outcome["reason"].startswith("original worker failure")
    assert "original failure" in outcome["artifact_problems"]
    assert item.launch.custody_eligibility["receipt"]["proof"] is True
    assert c.closed == [810] and not item.launch.release_eligible(True)


def test_actual_return_seals_loss_after_completed_report_and_finalize(publication, monkeypatch):
    p, hubs, finalized, delayed = publication, [], [], []
    real_hub, real_finalize = arbiter.ArbiterHub, life.JobLaunchLedger.finalize
    real_report = scheduler.completed_report

    def hub(*args, **kwargs):
        instance = real_hub(*args, **kwargs)
        hubs.append((instance, instance.on_loss))
        return instance

    def finalize(ledger, *args, **kwargs):
        result = real_finalize(ledger, *args, **kwargs)
        finalized.append(result)
        return result

    def completed(*args, **kwargs):
        report = real_report(*args, **kwargs)
        assert finalized == [True] and not p.ledgers[0].retained
        assert all(slot.custody_state == "disposed" for slot in p.ledgers[0].slots.values())
        instance, installed = hubs[0]
        assert installed.__func__ is retirement.HubLossNotice.notify
        assert not installed.__self__.event.is_set() and not instance.closed_reason
        monkeypatch.setattr(retirement, "require_active", lambda *a: pytest.fail("active"))
        instance.on_loss = delayed.append
        before.extend((list(p.closed), list(p.polls), list(p.pins)))
        instance.lose("first loss after report construction")
        instance.lose("later reason must not replace first")
        assert not installed.__self__.event.is_set()
        return report

    before = []
    monkeypatch.setattr(scheduler, "ArbiterHub", hub)
    monkeypatch.setattr(life.JobLaunchLedger, "finalize", finalize)
    monkeypatch.setattr(scheduler, "completed_report", completed)
    report = p.run()
    instance, installed = hubs[0]
    assert report["overall"] == "FAIL"
    assert report["cancellation"] == "live arbiter closed admission: " + delayed[0]
    assert delayed == [instance.closed_reason] == ["first loss after report construction"]
    assert "live arbiter closed admission: " + delayed[0] in report["orchestration_problems"]
    saved = repr(report)
    installed(delayed[0])
    assert installed.__self__.event.is_set() and repr(report) == saved
    assert (p.closed, p.polls, p.pins) == tuple(before)
