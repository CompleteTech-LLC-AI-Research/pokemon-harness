"""Authored controls only: NOTRUN until full CODE/EVI admits execution."""

import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel as scheduler
from scripts import production_gate_parallel_job_lifecycle as job_lifecycle
from scripts import production_gate_parallel_job_retirement as retirement
from scripts import production_gate_parallel_processes as proc
from scripts.production_gate_parallel_capacity import LiveCapacity, PairBudget
from scripts.production_gate_parallel_identity import expected_identity
from scripts.production_gate_parallel_measurement_budget import (
    IdentityCancelled,
    IdentityDeadline,
    MeasurementBudget,
)
from scripts.production_gate_parallel_processes import JOB_TOKEN_ENV
from scripts.production_gate_parallel_service import OwnerService
from scripts.production_gate_parallel_stopping import DeadlineWatch
from tests._parallel_runtime_report_support import good_artifact, make_plan, report_bytes


def fixture():
    now = [10.0]
    events = []
    jobs = []
    hub = SimpleNamespace(
        closed_reason="",
        check_leases=lambda: events.append("leases"),
        expect_drop=lambda job: events.append(("drop", job)),
    )

    def lose(reason):
        hub.closed_reason = hub.closed_reason or reason

    hub.lose = lose
    budget = SimpleNamespace(mark_lost=lambda reason: events.append(("lost", reason)))
    for name, deadline in (("a", 11.0), ("b", 15.0)):
        owned = SimpleNamespace(
            contained=True,
            root_start=1,
            reason="",
            observe=lambda: events.append("observe"),
            signal_root=lambda sig: events.append(("signal", sig)),
        )
        jobs.append(
            SimpleNamespace(
                job_id=name,
                phase="selected",
                deadline=deadline,
                cleanup_deadline=None,
                stop_requested=False,
                owned=owned,
                process=SimpleNamespace(
                    pid=100,
                    poll=lambda: None,
                    guardian_status=SimpleNamespace(drain=lambda: None, ready=True, eof=False),
                ),
            )
        )
    deadlines = DeadlineWatch(
        jobs,
        hub,
        4.0,
        lambda item: False,
        grace_seconds=0.1,
        clock=lambda: now[0],
        sleeper=lambda _: pytest.fail("pulse slept"),
        problems=[],
    )
    live = SimpleNamespace(check=lambda: None)
    interrupt = SimpleNamespace(is_set=lambda: False)
    service = OwnerService(
        jobs,
        hub,
        budget,
        live,
        deadlines,
        SimpleNamespace(job=lambda _: {}),
        interrupt,
        cleanup_seconds=4.0,
        clock=lambda: now[0],
    )
    return service, jobs, now, events


def test_callback_services_sibling_timeout_without_harvest_or_removal():
    service, jobs, now, events = fixture()
    now[0] = 12.0
    callback = service.pulse
    callback()
    assert len(jobs) == 2 and jobs[0].stop_requested
    assert jobs[0].cleanup_deadline == 15.0
    assert not jobs[1].stop_requested
    assert service.deadlines.expired == {"a"}
    assert any(isinstance(event, tuple) and event[0] == "signal" for event in events)


def test_callback_cancellation_tightens_existing_bound_and_never_resets_it():
    service, jobs, now, _ = fixture()
    jobs[1].cleanup_deadline = 12.5
    service.interrupt.is_set = lambda: True
    service.pulse()
    assert all(item.cleanup_deadline == 12.5 and item.stop_requested for item in jobs)
    now[0] = 99.0
    service.pulse()
    assert all(item.cleanup_deadline == 12.5 for item in jobs)
    assert service.cancel_reason == "external interrupt"


def test_recursive_entry_raises_and_still_notifies_all_siblings():
    service, jobs, _, events = fixture()
    jobs[0].owned.observe = service.pulse
    with pytest.raises(RuntimeError, match="recursive owner service"):
        service.pulse()
    assert not service._active
    assert all(item.stop_requested for item in jobs)
    assert "observe" in events  # b was observed despite a's recursive failure
    assert sum(isinstance(e, tuple) and e[0] == "signal" for e in events) == 2


@pytest.mark.parametrize("failure", ["lease", "capacity", "observe"])
def test_failures_propagate_after_sibling_notification(failure):
    service, jobs, _, events = fixture()

    def refuse():
        raise OSError("injected")

    if failure == "lease":
        service.hub.check_leases = refuse
    elif failure == "capacity":
        service.live.check = refuse
    else:
        jobs[0].owned.observe = refuse
    with pytest.raises(OSError, match="injected"):
        service.pulse()
    assert all(item.stop_requested for item in jobs)
    assert sum(isinstance(e, tuple) and e[0] == "signal" for e in events) == 2
    assert not service._active


def test_policy_loss_closes_admission_and_requests_stop(monkeypatch, tmp_path):
    service, jobs, _, _ = fixture()
    service.plan.job = lambda _: {"policy_sha256": "frozen"}
    for item in jobs:
        item.tree = {"root": tmp_path}
    monkeypatch.setattr(
        "scripts.production_gate_parallel_service.policy_file_problem",
        lambda *_: "changed",
    )
    service.pulse()
    assert "capacity policy changed" in service.hub.closed_reason
    assert all(item.stop_requested for item in jobs)


def test_containment_loss_and_fail_fast_timeout_latch_all_siblings():
    service, jobs, now, _ = fixture()
    jobs[0].owned.contained = False
    jobs[0].owned.reason = "lost instance"
    service.pulse()
    assert "lost instance" in service.hub.closed_reason
    assert all(item.stop_requested for item in jobs)
    other, siblings, later, _ = fixture()
    other.deadlines.may_cancel = lambda _: True
    later[0] = 12.0
    other.pulse()
    assert "timed out" in other.cancel_reason
    assert all(item.stop_requested and item.cleanup_deadline == 15.0 for item in siblings)
    assert now[0] == 10.0


def test_finish_checkpoint_skip_does_not_observe_retired_owner():
    service, jobs, now, events = fixture()
    jobs[0].owned.observe = lambda: pytest.fail("retired owner observed")
    now[0] = 16.0
    service.pulse(skip=jobs[0])
    assert "observe" in events
    assert jobs[1].job_id in service.deadlines.expired


def test_interrupt_after_original_timeout_cannot_grant_fresh_cleanup_allowance():
    service, jobs, now, _ = fixture()
    now[0] = 12.0
    service.interrupt.is_set = lambda: True
    service.pulse()
    assert "a" in service.deadlines.expired
    assert all(item.cleanup_deadline == 15.0 for item in jobs)


def test_capacity_loss_latches_budget_before_stopping_siblings():
    service, jobs, _, events = fixture()
    service.live.check = lambda: "reservation disappeared"
    service.pulse()
    assert ("lost", "reservation disappeared") in events
    assert "capacity loss" in service.hub.closed_reason
    assert all(item.stop_requested for item in jobs)


def test_terminal_holder_loss_is_latched_without_finishing_job(monkeypatch):
    service, jobs, _, _ = fixture()
    jobs[0].process.poll = lambda: 0
    monkeypatch.setattr(
        "scripts.production_gate_parallel_service.holder_loss", lambda *_: "holder lost"
    )
    service.pulse()
    assert len(jobs) == 2
    assert "holder lost" in service.hub.closed_reason
    assert jobs[1].stop_requested


def test_closed_lease_stops_all_jobs_without_checking_new_capacity():
    service, jobs, _, _ = fixture()
    service.hub.check_leases = lambda: service.hub.lose("lease expired")
    service.live.check = lambda: pytest.fail("closed admission rechecked capacity")
    service.pulse()
    assert "lease expired" in service.cancel_reason
    assert all(item.stop_requested for item in jobs)


def run_final_boundary(
    tmp_path,
    monkeypatch,
    *,
    loss="",
    modes=("source",),
    serial=False,
    early_smoke=False,
    target="source-job",
    after_publication=False,
    expired=False,
):
    """Actual scheduler/finish/budget/report; only process/lease ownership is simulated.

    Artifacts, referenced reports, pinned policy bytes and final identity checks are
    real Python/file paths. No synthetic owner proof is a kernel-cleanup assertion.
    """
    monkeypatch.setattr(job_lifecycle, "_RETAINED_RUNS", [])
    wall = datetime(2026, 10, 15, tzinfo=UTC)
    now, triggered, events = [10.0], [False], []
    policy = json.dumps({"max_concurrent_pairs": 2})
    plan = make_plan(modes=modes, early_smoke=early_smoke, capacity_policy_text=policy)
    budget = PairBudget(2)
    live = LiveCapacity(budget, probe=lambda: 0 if triggered[0] and loss == "capacity" else 2)
    admitted, launched, measured, bounds = [], [], [], []
    services = {}
    real_admit = live.admit

    def admit(job_id, pairs):
        admitted.append(job_id)
        real_admit(job_id, pairs)

    monkeypatch.setattr(live, "admit", admit)

    class Hub:
        closed_reason = ""
        startup_disposed = True

        def __init__(self, *args, **kwargs):
            self._lock = threading.Lock()
            self.on_loss = kwargs["on_loss"]

        def start(self):
            pass

        def stop(self):
            pass

        def register(self, *args):
            return {}

        def unregister(self, *args, **kwargs):
            return {"hello": True, "unreleased": []}

        def expect_drop(self, job_id):
            events.append(("drop", job_id))

        def lose(self, reason):
            with self._lock:
                first = not self.closed_reason
                self.closed_reason = self.closed_reason or reason
            if first:
                self.on_loss(self.closed_reason)

        def check_leases(self):
            if triggered[0] and loss == "lease":
                self.lose("injected lease loss")

    class Handoffs:
        startup_disposed = True

        def __init__(self, *, job_ids):
            self.job_ids = tuple(job_ids)
            expected = tuple(job["job_id"] for job in plan.as_dict()["jobs"])
            assert self.job_ids == expected
            assert len(self.job_ids) == len(expected) == len(set(self.job_ids))
            self.loss_sink = None
            self.loss_bound = False

        def bind_loss(self, sink):
            assert not self.loss_bound and callable(sink)
            assert isinstance(sink.__self__, Hub) and sink.__func__ is Hub.lose
            self.loss_sink, self.loss_bound = sink, True

        def service(self):
            assert self.loss_bound

        def open(self, *args, **kwargs):
            return None

        def close(self, *args):
            return None

        def close_all(self):
            pass

    class Tree:
        @classmethod
        def prepare(cls, pid, token, **kwargs):
            return cls(pid, token, **kwargs)

        def initialize_original(self, process, launch):
            launch.initialized = True
            launch.pin_state = "tree"  # synthetic fixture, never a kernel-proof assertion

        def __init__(self, pid, token, **kwargs):
            self.root_start, self.contained, self.reason = 1, True, ""
            self.ports, self.tracked, self.cgroup = set(), {}, None
            self.root_pid, self.status = pid, kwargs["status"]
            self._entries = {pid: (4000 + pid, 1)}
            self.retired = False

        def terminate(self, **kwargs):
            bounds.append(kwargs["deadline"])
            self.retired = not kwargs.get("retain_pins", False)
            return {"proof": True, "terminated": False}

        def observe(self):
            assert not self.retired, "final checkpoint observed retired descriptors"

        def signal_root(self, sig):
            events.append(("signal", sig))

    def launch(receipt_path, grace, argv, **kwargs):
        job_id = argv[1]
        launched.append(job_id)
        root = receipt_path.parent
        pid = 200 + len(launched)
        artifact = good_artifact(
            plan,
            job_id,
            pid=pid,
            token=kwargs["env"][JOB_TOKEN_ENV],
            started=wall.isoformat(),
            finished=wall.isoformat(),
        )
        (root / "out" / "terminal.json").write_text(json.dumps(artifact))
        (root / "evidence" / "gate-report.json").write_bytes(report_bytes(plan, job_id))
        guardian = {"phase": "final", "contained": True, "worker_pid": pid, "worker_status": 0}
        view = proc.GuardianStatus.prepare()
        view.fd, view.close_state, view.ready = 9000 + pid, "open", True
        view.read_receipts = lambda *a, **k: (guardian, [])
        launch = kwargs["launch"]
        launch.spawn_attempted = True
        launch.process = SimpleNamespace(
            pid=100 + len(launched), poll=lambda: 0, guardian_status=view, returncode=None
        )
        launch.status = view
        return launch.process, b""

    def measure(job_id, *, checkpoint, budget):
        measured.append(job_id)
        services[job_id] = budget.service
        if job_id == target and loss and not after_publication:
            triggered[0] = True
            if expired:
                now[0] = budget.deadline + 1.0
            if loss == "policy":
                path = tmp_path / "runs" / plan.run_id / job_id / "capacity-policy.json"
                path.write_text(path.read_text() + " ")
        try:
            checkpoint()  # actual MeasurementBudget checkpoint services the actual scheduler pulse
        except IdentityCancelled:
            assert budget.cleanup_open(), "success cancellation must not abandon physical cleanup"
            events.append(("cancelled_measurement", job_id))
            raise
        events.append(("measurement_returned", job_id))
        return expected_identity(plan.as_dict(), plan.job(job_id)["mode"]), []

    real_manifest = scheduler._manifest

    def manifest(directory):
        job_id = directory.parent.name
        if after_publication and job_id == target and not triggered[0]:
            triggered[0] = True
            services[job_id]()  # raw owner pulse after row publication; no success checkpoint
            events.append(("post_publication_latch", job_id))
        return real_manifest(directory)

    monkeypatch.setattr(scheduler, "_manifest", manifest)

    def read(fd, limit):
        events.append(("status_read", fd, limit))
        raise BlockingIOError  # Controlled nonblocking pipe has no additional bytes yet.

    controlled_os = SimpleNamespace(close=lambda fd: events.append(("close", fd)), read=read)
    monkeypatch.setattr(proc, "os", controlled_os)
    monkeypatch.setattr(retirement, "os", controlled_os)
    monkeypatch.setattr(scheduler, "ArbiterHub", Hub)
    monkeypatch.setattr(scheduler, "HandoffRegistry", Handoffs)
    monkeypatch.setattr(scheduler, "OwnedTree", Tree)
    monkeypatch.setattr(scheduler, "launch_holder", launch)
    monkeypatch.setattr(scheduler, "cleanup_proven", lambda *args: True)
    monkeypatch.setattr(
        scheduler,
        "kernel_ownership",
        lambda required: {
            "admitted": True,
            "required": False,
            "qualified": False,
        },
    )
    (tmp_path / "runs").mkdir()
    (tmp_path / "snapshot").mkdir()
    report = scheduler._run_plan(
        plan,
        adoption=SimpleNamespace(enabled=True),
        use_cgroup=False,
        require_kernel_ownership=False,
        run_root=tmp_path / "runs",
        project_root=tmp_path / "snapshot",
        budget=budget,
        capacity=live,
        worker_argv_factory=lambda plan, job_id, tree, root: ["simulated", job_id],
        base_env={},
        capacity_probe=None,
        interrupt=SimpleNamespace(is_set=lambda: triggered[0] and loss == "interrupt"),
        serial=serial,
        measure=measure,
        capacity_policy_text=policy,
        poll_seconds=0.01,
        grace_seconds=0.01,
        clock=lambda: now[0],
        sleeper=lambda _: pytest.fail("completed simulated jobs must not sleep"),
        wall_clock=lambda: wall,
    )
    assert all(bound == 10.0 + plan.as_dict()["deadlines"]["cleanup_seconds"] for bound in bounds)
    if loss in ("capacity", "policy", "lease"):
        assert any(event[0] == "status_read" for event in events)
    assert not any("stop notification" in problem for problem in report["orchestration_problems"])
    return SimpleNamespace(
        report=report,
        admitted=admitted,
        launched=launched,
        measured=measured,
        events=events,
        budget=budget,
    )


@pytest.mark.parametrize("loss", ["interrupt", "capacity", "policy", "lease"])
def test_last_job_cancellation_crosses_actual_finish_budget_and_empty_poll_exit(
    tmp_path, monkeypatch, loss
):
    result = run_final_boundary(tmp_path, monkeypatch, loss=loss)
    report = result.report
    assert result.measured == result.launched == result.admitted == ["source-job"]
    assert ("cancelled_measurement", "source-job") in result.events
    assert ("measurement_returned", "source-job") not in result.events
    assert report["jobs"][0]["state"] == "failed"
    assert report["jobs"][0]["child_exit_status"] == 0
    assert report["jobs"][0]["artifact"]["overall"] == "PASS"  # retained child claim only
    assert "identity measurement cancelled:" in report["jobs"][0]["reason"]
    assert report["cancellation"] and report["overall"] == "FAIL"
    assert report["orchestration_problems"] == [
        f"owner service cancelled: {report['cancellation']}"
    ]
    assert report["cleanup_complete"] is True  # simulated proofs, not physical-retirement evidence


@pytest.mark.parametrize("loss", ["interrupt", "capacity", "policy", "lease"])
@pytest.mark.parametrize("boundary", ["serial_batch", "next_phase"])
def test_latched_final_measurement_cannot_admit_or_launch_the_next_batch(
    tmp_path, monkeypatch, loss, boundary
):
    phase = boundary == "next_phase"
    first = "source-smoke-job" if phase else "source-job"
    result = run_final_boundary(
        tmp_path,
        monkeypatch,
        loss=loss,
        target=first,
        early_smoke=phase,
        modes=("source",) if phase else ("source", "cython"),
        serial=True,
    )
    assert result.launched == result.admitted == result.measured == [first]
    assert result.report["overall"] == "FAIL" and result.report["cancellation"]
    assert [job["state"] for job in result.report["jobs"]] == ["failed", "not_started"]
    assert "not started:" in result.report["jobs"][1]["reason"]


def test_previously_finalized_pass_is_retained_when_last_sibling_measurement_cancels(
    tmp_path, monkeypatch
):
    result = run_final_boundary(
        tmp_path,
        monkeypatch,
        loss="interrupt",
        modes=("source", "cython"),
        target="cython-job",
    )
    assert [job["state"] for job in result.report["jobs"]] == ["passed", "failed"]
    assert result.report["jobs"][0]["artifact_problems"] == []
    assert (
        result.report["overall"] == "FAIL" and result.report["cancellation"] == "external interrupt"
    )


def test_same_actual_finish_budget_path_passes_without_cancellation(tmp_path, monkeypatch):
    result = run_final_boundary(tmp_path, monkeypatch)
    assert result.report["overall"] == "PASS", result.report
    assert result.report["cancellation"] == ""
    assert result.report["jobs"][0]["state"] == "passed"
    assert ("measurement_returned", "source-job") in result.events


def test_cancellation_refuses_success_but_preserves_cleanup_and_original_deadline_identity():
    calls, reason = [], ["external interrupt"]
    budget = MeasurementBudget(
        5.0,
        8.0,
        clock=lambda: 2.0,
        service=lambda: calls.append("service"),
        cancel_reason=lambda: reason[0],
    )
    for operation in (
        budget,
        budget.tighten(deadline=4.0, cleanup_deadline=7.0),
        budget.with_checkpoint(lambda: calls.append("nested")),
    ):
        with pytest.raises(IdentityCancelled, match="external interrupt"):
            operation.checkpoint()
        assert operation.cleanup_open()
    expired = MeasurementBudget(1.0, 8.0, clock=lambda: 2.0, cancel_reason=lambda: reason[0])
    with pytest.raises(IdentityDeadline, match="exceeded its frozen deadline") as caught:
        expired.checkpoint()
    assert type(caught.value) is IdentityDeadline
    assert expired.cleanup_open() and calls


@pytest.mark.parametrize("next_batch", [False, True])
def test_post_publication_latch_preserves_completed_row_but_forces_aggregate_failure(
    tmp_path, monkeypatch, next_batch
):
    result = run_final_boundary(
        tmp_path,
        monkeypatch,
        loss="interrupt",
        after_publication=True,
        modes=("source", "cython") if next_batch else ("source",),
        serial=True,
    )
    assert result.report["jobs"][0]["state"] == "passed"
    assert result.report["jobs"][0]["artifact_problems"] == []
    assert result.report["overall"] == "FAIL"
    assert result.report["cancellation"] == "external interrupt"
    assert result.report["orchestration_problems"] == [
        "owner service cancelled: external interrupt"
    ]
    assert result.admitted == result.launched == ["source-job"]
    if next_batch:
        assert result.report["jobs"][1]["state"] == "not_started"


def test_actual_finish_retains_timeout_identity_when_interrupt_coincides_with_expiry(
    tmp_path, monkeypatch
):
    result = run_final_boundary(tmp_path, monkeypatch, loss="interrupt", expired=True)
    job = result.report["jobs"][0]
    assert job["state"] == "timeout" and job["child_exit_status"] == 0
    assert "identity measurement exceeded its frozen deadline" in job["artifact_problems"]
    assert not any("identity measurement cancelled:" in p for p in job["artifact_problems"])
    assert result.report["cancellation"] == "external interrupt"
    assert result.report["overall"] == "FAIL"
