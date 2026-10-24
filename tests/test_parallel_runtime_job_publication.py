"""Controlled actual scheduler/launch publication boundaries. All cases NOTRUN.

Synthetic descriptors and processes never gain kernel authority. These controls
exercise custody/release decisions, not installed process or orphan containment.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel as scheduler
from scripts import production_gate_parallel_arbiter as arbiter
from scripts import production_gate_parallel_cgroup as cgroups
from scripts import production_gate_parallel_holder as holder
from scripts import production_gate_parallel_job_lifecycle as life
from scripts import production_gate_parallel_job_retirement as retirement
from scripts import production_gate_parallel_ports as ports
from scripts import production_gate_parallel_processes as proc
from scripts import production_gate_parallel_service as service
from scripts.production_gate_parallel_capacity import LiveCapacity, PairBudget
from tests._parallel_runtime_report_support import make_plan
from tests._tier_parallel_runtime_modules import PARALLEL_RUNTIME_MODULES


@pytest.fixture
def publication(tmp_path, monkeypatch):
    """Actual facade, registry, helpers, tree/status setup and startup owners.

    Only process/kernel/thread effects and final job evidence are controlled. Real
    temporary paths exercise positive creation/disposition; no real child is made.
    """
    monkeypatch.setattr(life, "_RETAINED_RUNS", [])
    state = SimpleNamespace(
        point="",
        now=10.0,
        events=[],
        processes=[],
        sockets=[],
        threads=[],
        closed=[],
        pipe_calls=0,
        ledgers=[],
        cleanup_bounds=[],
        original_caps=[],
        report=None,
        exited=0,
        polls=[],
        pins=[],
        exclusions=[],
        exclusion_contexts=[],
        cgroup_path=None,
    )
    policy = '{"max_concurrent_pairs":2}'
    plan = make_plan(capacity_policy_text=policy)
    budget = PairBudget(2)
    live = LiveCapacity(
        budget,
        probe=lambda: 0 if state.point == "capacity" and initialized() else 2,
    )
    wall = datetime(2026, 10, 15, tzinfo=UTC)

    def current():
        ledger = life._RETAINED_RUNS[0]
        if ledger not in state.ledgers:
            state.ledgers.append(ledger)
        return ledger

    def initialized():
        return bool(state.ledgers and any(s.initialized for s in state.ledgers[0].slots.values()))

    class Adoption:
        def __init__(self, *, enabled):
            self.enabled = enabled
            self.restoration_pending = False

        def __enter__(self):
            assert current().adoption is self
            self.restoration_pending = self.enabled
            if state.point == "restore_suppressed":
                raise RuntimeError("original caller failure")
            return self

        def verify(self):
            if state.point == "adoption":
                raise RuntimeError("controlled adoption verification")
            return self.enabled

        def __exit__(self, *exc):
            state.exited += 1
            if state.point == "restore_suppressed":
                assert exc[1] is not None
                exc[1].add_note("controlled suppressed restoration failure")
                return
            if state.point == "restore":
                raise RuntimeError("controlled restoration failure")
            self.restoration_pending = False

    class FakeOS:
        @staticmethod
        def getpid():
            return 70000

        @staticmethod
        def urandom(size):
            return b"x" * size

        @staticmethod
        def pipe():
            state.pipe_calls += 1
            if state.point == "second_pipe" and state.pipe_calls == 2:
                raise OSError("controlled second pipe")
            return 100 + state.pipe_calls * 2, 101 + state.pipe_calls * 2

        @staticmethod
        def write(fd, data):
            if state.point == "write":
                raise OSError("controlled key write")
            return len(data)

        @staticmethod
        def read(fd, limit):
            return b""

        @staticmethod
        def close(fd):
            state.closed.append(fd)
            if (state.point == "close" and fd == 105) or (
                state.point == "status_close" and fd == 104
            ):
                raise OSError("controlled ambiguous close")

        @staticmethod
        def set_blocking(fd, blocking):
            slot = next(iter(current().slots.values()))
            assert slot.status is not None and slot.status.fd == -1
            assert slot.pipes["status_read"] == fd and slot.pipe_states["status_read"] == "raw"
            if state.point == "configure" or (
                state.point == "sibling_partial" and len(state.processes) == 2
            ):
                raise OSError("controlled status configure")

    class Process:
        def __init__(self, argv, **kwargs):
            slot = next(
                s for s in current().slots.values() if s.spawn_attempted and s.process is None
            )
            assert "launch" not in kwargs and slot.deadline is not None
            state.original_caps.append(slot.cleanup_cap)
            state.events.append(("popen", slot.job_id))
            if state.point == "popen":
                # Raising means no returned identity; it never proves no child.
                raise OSError("controlled no returned Popen")
            self.pid = 90000 + len(state.processes)
            self.slot = slot
            self.returncode = None
            state.processes.append(self)

        def poll(self):
            assert self.slot.initialized, "original child polled before first-pin completion"
            state.polls.append(self)
            self.returncode = 0
            return 0

    class Socket:
        family, type, proto = ports.socket.AF_UNIX, ports.socket.SOCK_STREAM, 0

        def __init__(self, kind):
            self.kind = kind
            if kind == "hub":
                self.owner = current().hub
            else:
                (self.owner,) = (
                    slot.broker
                    for slot in current().slots.values()
                    if slot.broker is not None and slot.broker._server is None
                )
            self.closes = 0
            state.sockets.append(self)

        def bind(self, address):
            owner = self.owner
            assert owner._server is self  # actual retained owner before configuration
            if state.point == self.kind + "_bind":
                raise OSError("controlled " + self.kind + " bind")

        def listen(self, limit):
            if state.point == self.kind + "_listen":
                raise OSError("controlled " + self.kind + " listen")

        def settimeout(self, timeout):
            if state.point == self.kind + "_timeout":
                raise OSError("controlled " + self.kind + " timeout")

        def close(self):
            self.closes += 1
            if state.point == self.kind + "_close":
                raise OSError("controlled socket disposition")

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.name, self.ident, self.alive = name, None, False
            self.target = target
            state.threads.append(self)

        def start(self):
            kind = "hub" if self.name == "arbiter-hub" else "handoff"
            if state.point == kind + "_thread":
                raise RuntimeError("controlled uncertain thread start")
            self.ident, self.alive = 1, True
            state.events.append(("thread_start_effect", self.name))
            if state.point == kind + "_thread_late":
                raise RuntimeError("controlled returned thread identity before start error")

        def join(self, *, timeout):
            state.events.append(("join", self.name, timeout))
            if state.point != "hub_alive":
                if self.name == "arbiter-hub":
                    assert state.ledgers[0].hub._server.closes == 0
                    self.target()  # actual stopping/accept-loop finally, before listener close
                elif self.name == "listener-broker":
                    owner = self.target.__self__
                    assert owner._thread is self
                    assert owner._server.closes == 0
                    self.target()  # same actual broker loop-finally before listener close
                self.alive = False

        def is_alive(self):
            return self.alive

    real_prepare = proc.OwnedTree.prepare.__func__
    real_initialize = proc.OwnedTree.initialize_original
    real_status_prepare = proc.GuardianStatus.prepare.__func__
    real_holder_ref = holder.HolderRef
    real_mkdir = Path.mkdir
    real_progress = scheduler.write_running_progress
    real_start = scheduler.start_allocation
    real_cgroup_create = cgroups.JobCgroup.create.__func__

    class RejectInsertion(dict):
        def __setitem__(self, key, value):
            if state.point == "insertion_after":
                super().__setitem__(key, value)
            raise RuntimeError("controlled first-pin insertion")

    def prepare(cls, *args, **kwargs):
        if state.point == "tree_prepare":
            raise RuntimeError("controlled tree preparation")
        owner = real_prepare(cls, *args, **kwargs)
        if state.point in ("insertion", "insertion_after"):
            owner._entries = RejectInsertion()
        return owner

    def initialize(owner, process, launch):
        assert current().slots[launch.job_id].owned is owner
        real_initialize(owner, process, launch)
        if state.point == "tighten":
            state.now = launch.deadline + 1000.0
        if state.point == "policy":
            path = launch.tree["root"] / "capacity-policy.json"
            path.write_text(path.read_text() + " ")

    def status_prepare(cls, **kwargs):
        if state.point == "status_prepare":
            raise RuntimeError("controlled pure status preparation")
        return real_status_prepare(cls, **kwargs)

    def ref(*args):
        assert (
            state.processes and next(iter(current().slots.values())).process is state.processes[0]
        )
        if state.point == "holder_ref":
            raise RuntimeError("controlled HolderRef preparation")
        reference = real_holder_ref(*args)
        # Controlled terminal producer for the real HolderRef reader. No adopted
        # instances exist here, so this fixture performs no absence/kernel query.
        terminal = {
            "phase": "final",
            "holder_pid": reference.pid,
            "holder_start": 123,
            "contained": True,
            "proof": True,
            "subreaper_start": True,
            "subreaper_end": True,
            "incomplete": False,
            "echild": True,
            "never_started": False,
            "guardian_killed": False,
            "guardian": {"pid": 91000, "start": 456},
            "guardian_status": 0,
            "forks": [[91000, 456]],
            "adopted": [],
            "killed": [],
            "reaped": [],
            "reaped_unheld": [],
            "unknown": [],
            "remaining": [],
        }
        encoded = json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()
        terminal["mac"] = hmac.new(reference.secret, encoded, hashlib.sha256).hexdigest()
        reference.path.write_text(json.dumps(terminal))
        return reference

    def stat(pid):
        assert pid in {p.pid for p in state.processes}
        if state.point == "stat":
            raise RuntimeError("controlled original stat")
        return None if state.point == "missing_stat" else (123, 1, "R")

    def pin(pid):
        assert pid in {p.pid for p in state.processes}
        assert all(p.pid != pid for p in state.polls)
        state.pins.append(pid)
        return None if state.point == "first_pin" else 800

    def terminate(owner, **kwargs):
        state.cleanup_bounds.append(kwargs["deadline"])
        excluded = owner._known_children()
        state.exclusions.append(excluded)
        state.exclusion_contexts.append((owner.root_pid, set(owner.tracked), excluded))
        if not kwargs.get("retain_pins", False):
            owner.close()
        return {"proof": state.point != "unproven", "terminated": [], "remaining": []}

    def observe(owner):
        if state.point == "role":
            owner.contained, owner.reason = False, "controlled role loss"

    def mkdir(path, *args, **kwargs):
        if state.point == "tree_path" and path.name == "evidence":
            raise OSError("controlled later mkdir")
        return real_mkdir(path, *args, **kwargs)

    def progress(*args):
        if state.point == "progress" and state.processes:
            raise RuntimeError("controlled progress failure")
        return real_progress(*args)

    def allocate(*args, **kwargs):
        if state.point == "append":

            class FailingAppend:
                def __iter__(self):
                    return iter(())

                def append(self, item):
                    raise RuntimeError("controlled running append")

            kwargs["running"] = FailingAppend()
        return real_start(*args, **kwargs)

    def running(*args, **kwargs):
        if state.point == "running":
            raise RuntimeError("controlled scheduling record")
        return life._Running(*args, **kwargs)

    def finish(plan, item, status, *args, **kwargs):
        return scheduler.outcome_record(
            plan,
            item.job_id,
            "failed",
            child_exit_status=status,
            artifact=None,
            artifact_problems=["controlled absent worker evidence"],
        )

    real_check = arbiter.ArbiterHub.check_leases

    def check(hub):
        if state.point == "lease" and initialized():
            hub.lose("controlled lease loss")
        real_check(hub)

    monkeypatch.setattr(scheduler, "OrchestratorAdoption", Adoption)
    monkeypatch.setattr(retirement, "os", FakeOS)
    monkeypatch.setattr(proc, "os", FakeOS)
    monkeypatch.setattr(holder, "os", FakeOS)
    monkeypatch.setattr(proc.subprocess, "Popen", Process)
    monkeypatch.setattr(holder, "read_stat", lambda pid: (456, 1, "R"))
    monkeypatch.setattr(proc, "read_stat", stat)
    monkeypatch.setattr(proc, "_pidfd_open", pin)
    monkeypatch.setattr(proc.OwnedTree, "prepare", classmethod(prepare))
    monkeypatch.setattr(proc.OwnedTree, "initialize_original", initialize)
    monkeypatch.setattr(proc.OwnedTree, "observe", observe)
    monkeypatch.setattr(proc.OwnedTree, "terminate", terminate)
    monkeypatch.setattr(proc.OwnedTree, "signal_root", lambda *args: None)
    monkeypatch.setattr(proc.GuardianStatus, "prepare", classmethod(status_prepare))
    monkeypatch.setattr(
        proc.GuardianStatus,
        "read_receipts",
        lambda *args, **kwargs: (
            {"phase": "final", "contained": True, "worker_status": 0, "worker_pid": 91000},
            [],
        ),
    )
    monkeypatch.setattr(holder, "HolderRef", ref)

    def socket_factory(kind):
        if state.point == kind + "_socket":
            raise OSError("controlled unreturned socket")
        return Socket(kind)

    monkeypatch.setattr(arbiter, "_SOCKET", lambda *args: socket_factory("hub"))
    monkeypatch.setattr(ports, "_SOCKET", lambda *args: socket_factory("handoff"))
    monkeypatch.setattr(arbiter.threading, "Thread", Thread)
    monkeypatch.setattr(arbiter.ArbiterHub, "check_leases", check)
    monkeypatch.setattr(scheduler, "holder_loss", lambda *args: None)
    monkeypatch.setattr(service, "holder_loss", lambda *args: None)
    monkeypatch.setattr(
        scheduler, "cleanup_proven", lambda receipt, *args: receipt["proof"] is True
    )
    monkeypatch.setattr(scheduler, "_finish_job", finish)
    monkeypatch.setattr(scheduler, "write_running_progress", progress)
    monkeypatch.setattr(scheduler, "_Running", running)
    monkeypatch.setattr(scheduler, "start_allocation", allocate)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(
        scheduler,
        "kernel_ownership",
        lambda required: {
            "admitted": True,
            "required": False,
            "qualified": False,
        },
    )
    run_root, project_root = tmp_path / "runs", tmp_path / "project"
    run_root.mkdir()
    project_root.mkdir()
    cg_root = tmp_path / "cgroup"
    cg_root.mkdir()
    (cg_root / "cgroup.procs").write_text("")

    def create(cls, name, **kwargs):
        return real_cgroup_create(
            cls, name, root=cg_root, own="/", required=("cgroup.procs",), **kwargs
        )

    monkeypatch.setattr(cgroups.JobCgroup, "create", classmethod(create))

    def run(point="", *, adopting=True, serial=True):
        state.point = point
        state.report = scheduler.run_parallel_plan(
            plan,
            run_root=run_root,
            project_root=project_root,
            budget=budget,
            worker_argv_factory=lambda *args: ["controlled-worker"],
            capacity=live,
            capacity_policy_text=policy,
            base_env={},
            adopt_orphans=adopting,
            use_cgroup=point == "cgroup",
            serial=serial,
            interrupt=SimpleNamespace(
                is_set=lambda: point in ("interrupt", "tighten") and initialized()
            ),
            clock=lambda: state.now,
            sleeper=lambda seconds: setattr(state, "now", state.now + seconds),
            wall_clock=lambda: wall,
            grace_seconds=0.01,
        )
        return state.report

    state.run, state.current, state.budget, state.plan = run, current, budget, plan
    return state


@pytest.mark.parametrize("adopting", [False, True])
@pytest.mark.parametrize(
    "point",
    [
        "configure",
        "status_prepare",
        "tree_prepare",
        "adoption",
        "stat",
        "missing_stat",
        "first_pin",
        "insertion",
        "insertion_after",
    ],
)
def test_actual_both_launchers_retain_original_before_setup_and_never_poll_uninitialized(
    publication, point, adopting
):
    p = publication
    if point in ("configure",):
        report = p.run(point, adopting=adopting)
        assert report["overall"] == "FAIL" and report["jobs"][0]["state"] == "failed"
    else:
        with pytest.raises(RuntimeError):
            p.run(point, adopting=adopting)
    ledger = p.ledgers[0]
    slot = ledger.slots["source-job"]
    assert slot.process is p.processes[0] and slot.spawn_attempted
    assert not slot.initialized and not slot.no_spawn_eligible
    assert p.polls == [] and p.budget.reserved_total == 1
    assert life._RETAINED_RUNS == [ledger] and p.exited == 0
    assert len(p.processes) == 1
    if point in ("insertion", "insertion_after", "missing_stat"):
        assert slot.raw_pidfd == 800 and 800 not in p.closed
    if point == "insertion_after":
        assert slot.owned._entries[slot.process.pid][0] == slot.raw_pidfd
        assert slot.pin_state == "transferring" and not slot.initialized
    if point == "first_pin":
        assert slot.raw_pidfd is None and slot.pin_state == "attempted"
    if point == "configure":
        assert slot.status.fd == -1 and slot.pipe_states["status_read"] == "raw"


def test_holder_ref_failure_uses_the_actual_holder_helper(publication):
    p = publication
    with pytest.raises(RuntimeError, match="HolderRef"):
        p.run("holder_ref")
    slot = p.ledgers[0].slots["source-job"]
    assert slot.process is p.processes[0] and slot.holder is None and slot.status is None
    assert slot.pipe_states["status_read"] == "raw" and not p.polls


@pytest.mark.parametrize("point", ["second_pipe", "write", "close", "popen"])
def test_pipe_publication_unknown_close_and_uncertain_popen(publication, point):
    p = publication
    report = p.run(point)
    slot = p.ledgers[0].slots["source-job"]
    assert report["overall"] == "FAIL"
    assert all(p.closed.count(fd) == 1 for fd in set(p.closed))
    if point in ("second_pipe", "write"):
        assert slot.no_spawn_eligible and slot.local_disposed
        assert not slot.spawn_attempted
    else:
        assert slot.spawn_attempted and not slot.no_spawn_eligible and not slot.local_disposed
        assert p.budget.reserved_total == 1 and len(p.processes) <= 1
        assert len(p.ledgers[0].attempted) == 1 and not p.polls
        assert (
            report["jobs"][0]["state"] == "failed" and report["jobs"][1]["state"] == "not_started"
        )
    if point == "close":
        assert slot.process is p.processes[0] and slot.pipe_states["status_write"] == "unknown"
    if point == "popen":
        assert slot.process is None and slot.raw_pidfd is None
        assert report["allocation_publication"]["returned"] == []
        assert p.pins == [] and [event for event in p.events if event[0] == "popen"] == [
            ("popen", "source-job")
        ]


@pytest.mark.parametrize("point", ["running", "append", "progress"])
def test_actual_caller_escape_keeps_original_and_bars_next_dispatch(publication, point):
    p = publication
    with pytest.raises(RuntimeError, match="controlled"):
        p.run(point)
    ledger = p.ledgers[0]
    slot = ledger.slots["source-job"]
    assert slot.process is p.processes[0] and slot.initialized
    assert slot.failure and not ledger.proof("source-job", True)
    assert life._RETAINED_RUNS == [ledger] and p.budget.reserved_total == 1
    assert len(p.processes) == 1 and p.exited == 0


def test_positive_exact_transfer_one_original_wait_owner_and_single_close(publication):
    p = publication
    report = p.run()
    ledger = p.ledgers[0]
    assert len(p.processes) == 2 and set(p.polls) == set(p.processes)
    assert all(
        s.item.process is s.process and s.item.owned is s.owned for s in ledger.slots.values()
    )
    assert all(s.pin_state == "tree" and s.raw_pidfd is None for s in ledger.slots.values())
    assert report["cleanup_complete"] is True and p.budget.reserved_total == 0
    assert not life._RETAINED_RUNS and not ledger.adoption.restoration_pending
    # Different launches use synthetic equal fd integers after checked closure.
    assert p.closed.count(800) == 2
    before = list(p.closed)
    assert ledger.finalize(ledger.cleanup)
    assert p.closed == before
    assert all(sock.closes == 1 for sock in p.sockets)


@pytest.mark.parametrize("point", ["tree_path", "cgroup"])
def test_actual_path_and_cgroup_allocations_retain_positive_creation_not_intent(publication, point):
    p = publication
    report = p.run(point)
    slot = p.ledgers[0].slots["source-job"]
    assert not slot.spawn_attempted
    assert not slot.local_disposed if point == "tree_path" else slot.local_disposed
    assert report["overall"] == "FAIL"
    if point == "tree_path":
        intent = next(path for path, status in slot.path_states.items() if status == "intent")
        assert intent.name == "evidence" and intent not in slot.created_paths
        assert p.budget.reserved_total == 1 and life._RETAINED_RUNS
    else:
        assert slot.cgroup_path is not None and slot.cgroup_state == "disposed"
        assert not slot.cgroup_path.exists()


@pytest.mark.parametrize("kind", ["hub", "handoff"])
@pytest.mark.parametrize("stage", ["bind", "listen", "timeout", "thread", "thread_late"])
def test_actual_startup_retains_owner_before_configuration_and_checks_quiescence(
    publication, kind, stage
):
    p = publication
    if stage.startswith("thread"):
        with pytest.raises(RuntimeError, match="controlled"):
            p.run(kind + "_" + stage)
    else:
        assert p.run(kind + "_" + stage)["overall"] == "FAIL"
    ledger = p.ledgers[0]
    owner = ledger.hub if kind == "hub" else ledger.slots["source-job"].broker
    assert owner._server in p.sockets
    assert not p.processes
    if kind == "hub":
        assert not ledger.slots  # real run-owned retention before any job exists
    if stage in ("thread", "thread_late"):
        assert not owner.startup_disposed and life._RETAINED_RUNS == [ledger]
        assert owner._thread_state == "uncertain" and p.exited == 0
        if kind == "hub":
            assert ledger.adoption.restoration_pending and owner._server.closes == 0
            assert not owner._thread_dispose_attempted
            assert not [e for e in p.events if e[:2] == ("join", "arbiter-hub")]
            effects = [e for e in p.events if e == ("thread_start_effect", "arbiter-hub")]
            assert len(effects) == (1 if stage == "thread_late" else 0)
        else:
            assert ledger.adoption.restoration_pending and owner._server.closes == 0
            assert not owner._thread_dispose_attempted
            assert not [e for e in p.events if e[:2] == ("join", "listener-broker")]
            effects = [e for e in p.events if e == ("thread_start_effect", "listener-broker")]
            assert len(effects) == (1 if stage == "thread_late" else 0)
    else:
        assert owner.startup_disposed
        assert owner._server.closes == 1


def test_join_return_without_thread_exit_does_not_retire_run(publication):
    p = publication
    report = p.run("hub_alive")
    ledger = p.ledgers[0]
    assert ledger.hub._thread.is_alive() and not ledger.hub.startup_disposed
    assert report["allocation_publication"]["pending"] and life._RETAINED_RUNS == [ledger]
    assert ledger.adoption.restoration_pending and p.exited == 0


@pytest.mark.parametrize("point", ["interrupt", "capacity", "lease", "role", "policy", "tighten"])
def test_original_caps_and_setup_cancellation_close_next_batch(publication, point):
    p = publication
    report = p.run(point)
    slot = p.ledgers[0].slots["source-job"]
    assert len(p.processes) == 1 and report["overall"] == "FAIL"
    assert report["jobs"][1]["state"] == "not_started"
    assert report["cancellation"]
    assert slot.cleanup_cap == p.original_caps[0]
    assert all(bound <= slot.cleanup_cap for bound in p.cleanup_bounds)
    if point == "tighten":
        assert p.now > slot.cleanup_cap and p.cleanup_bounds


def test_unproven_established_cleanup_is_not_repaired_by_strong_references(publication):
    p = publication
    report = p.run("unproven")
    ledger = p.ledgers[0]
    assert report["cleanup_complete"] is False and report["allocation_publication"]["pending"]
    slot = ledger.slots["source-job"]
    assert slot.owned._entries == {slot.process.pid: (800, 123)}
    assert slot.status.fd >= 0 and slot.status.close_state == "open"
    assert slot.custody_state == "holding" and not slot.release_eligible(True)
    assert p.budget.reserved_total == 1 and life._RETAINED_RUNS == [ledger]
    assert p.exited == 0  # no false local restoration on empty running


def test_no_spawn_eligibility_is_separate_from_completed_disposal_and_close_unknown_is_monotone(
    monkeypatch,
):
    slot = life.JobLaunch("unstarted")
    failures = []

    class UnknownClose:
        def close(self):
            failures.append("close")
            raise OSError("ambiguous close")

    slot.handles.append(UnknownClose())
    assert slot.no_spawn_eligible and not slot.local_disposed
    assert not slot.release_eligible(True)
    assert not slot.dispose_no_spawn(None, None)
    assert not slot.dispose_no_spawn(None, None)
    assert failures == ["close"] and slot.handle_states[id(slot.handles[0])] == "unknown"


def test_restoration_failure_retains_same_owner_even_when_attached_to_original_exception(
    publication,
):
    p = publication
    with pytest.raises(RuntimeError, match="original caller failure") as caught:
        p.run("restore_suppressed")
    ledger = p.ledgers[0]
    assert ledger.adoption.restoration_pending and life._RETAINED_RUNS == [ledger]
    assert caught.value.__notes__ == ["controlled suppressed restoration failure"]
    with pytest.raises(RuntimeError, match="unresolved"):
        p.run()
    assert life._RETAINED_RUNS == [ledger]


def test_actual_public_restoration_failure_and_pending_reentry(publication):
    p = publication
    report = p.run("restore")
    ledger = p.ledgers[0]
    assert ledger.adoption.restoration_pending and life._RETAINED_RUNS == [ledger]
    assert report["cleanup_complete"] is False
    with pytest.raises(RuntimeError, match="unresolved"):
        p.run()
    assert life._RETAINED_RUNS == [ledger]


def test_registered_tier_points_to_the_actual_authored_module():
    assert PARALLEL_RUNTIME_MODULES.count("test_parallel_runtime_job_publication.py") == 1
    assert Path(__file__).name == "test_parallel_runtime_job_publication.py"


def test_actual_run_plan_fdopen_uncertainty_is_retained_before_any_job(publication, monkeypatch):
    p = publication
    original_os = scheduler.os
    closed = []

    def wrap(*args, **kwargs):
        raise RuntimeError("controlled plan wrapper uncertainty")

    monkeypatch.setattr(
        scheduler,
        "os",
        SimpleNamespace(
            O_WRONLY=original_os.O_WRONLY,
            O_CREAT=original_os.O_CREAT,
            O_EXCL=original_os.O_EXCL,
            open=lambda *args: 7100,
            fdopen=wrap,
            close=lambda fd: closed.append(fd),
        ),
    )
    with pytest.raises(RuntimeError, match="plan wrapper uncertainty"):
        p.run()
    ledger = p.ledgers[0]
    assert ledger.plan_raw_fd == 7100 and ledger.plan_fd_state == "wrapping"
    assert closed == [] and not ledger.slots and not p.processes
    assert ledger.hub is not None and ledger.handoffs is not None and ledger.live is not None
    assert life._RETAINED_RUNS == [ledger] and ledger.adoption.restoration_pending


def test_startup_disposition_never_buys_another_join_allowance(publication):
    p = publication
    p.run("hub_alive")
    ledger = p.ledgers[0]
    before = [event for event in p.events if event[:2] == ("join", "arbiter-hub")]
    assert len(before) == 1
    assert not ledger.finalize(ledger.cleanup)
    assert [event for event in p.events if event[:2] == ("join", "arbiter-hub")] == before
    assert ledger.hub._thread_dispose_attempted and life._RETAINED_RUNS == [ledger]


def test_partial_original_child_is_excluded_by_actual_sibling_owner_callback(publication):
    p = publication
    report = p.run("sibling_partial", serial=False)
    ledger = p.ledgers[0]
    partial = ledger.slots["cython-job"]
    assert partial.process is p.processes[1] and not partial.initialized
    assert partial.process not in p.polls
    assert any(partial.process.pid in excluded for excluded in p.exclusions)
    assert partial.process.pid in ledger.wait_owned_children()
    completed = p.processes[0]
    assert completed.returncode == 0 and completed.pid not in ledger.wait_owned_children()
    # The actual callback is captured while the completed root is still tracked.
    # This catches both the unconditional running-root and tracked-root bypasses.
    captured = [
        excluded
        for root, tracked, excluded in p.exclusion_contexts
        if root == completed.pid and completed.pid in tracked
    ]
    assert captured
    assert all(completed.pid not in excluded for excluded in captured)
    assert all(partial.process.pid in excluded for excluded in captured)
    assert report["cleanup_complete"] is False and p.budget.reserved_total == 1


@pytest.mark.parametrize("kind", ["hub", "handoff"])
def test_unreturned_startup_socket_cannot_be_declared_disposed(publication, kind):
    p = publication
    report = p.run(kind + "_socket")
    ledger = p.ledgers[0]
    owner = ledger.hub if kind == "hub" else ledger.slots["source-job"].broker
    assert owner._server is None and owner._server_state == "allocating"
    assert not owner.startup_disposed and not p.processes
    assert report["allocation_publication"]["pending"] and p.exited == 0
    assert life._RETAINED_RUNS == [ledger]


def test_status_close_unknown_result_cannot_satisfy_actual_ledger_disposal(publication):
    p = publication
    report = p.run("status_close")
    ledger = p.ledgers[0]
    slot = ledger.slots["source-job"]
    assert slot.status.fd == -1 and slot.status.close_state == "unknown"
    assert not ledger.proof(slot.job_id, True) and report["cleanup_complete"] is False
    assert p.closed.count(104) == 1
    slot.status.close()
    assert p.closed.count(104) == 1 and life._RETAINED_RUNS == [ledger]


def test_partial_capacity_release_failure_refuses_next_batch_without_retry(
    publication, monkeypatch
):
    p = publication
    attempts = []
    original = LiveCapacity.release

    def release(live, job_id):
        attempts.append(job_id)
        original(live, job_id)
        raise OSError("controlled partial capacity release")

    monkeypatch.setattr(LiveCapacity, "release", release)
    report = p.run()
    ledger = p.ledgers[0]
    assert attempts == ["source-job"] and len(p.processes) == 1
    assert ledger.slots["source-job"].capacity_state == "unknown"
    assert report["jobs"][1]["state"] == "not_started" and report["cleanup_complete"] is False
    assert life._RETAINED_RUNS == [ledger] and ledger.adoption.restoration_pending
    # The implementation may already have released part of its authority; we do
    # not pretend a retained reference recharges a partially mutated PairBudget.
