"""Authored NOTRUN actual wait boundaries; synthetic effects grant no OS authority."""

from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_job_lifecycle as life
from scripts import production_gate_parallel_job_retirement as retirement
from scripts import production_gate_parallel_probe_launch as launch_module
from scripts import production_gate_parallel_probe_owner as owners
from scripts import production_gate_parallel_processes as processes
from scripts import production_gate_parallel_root_context as roots
from scripts.production_gate_parallel_measurement_budget import MeasurementBudget
from scripts.production_gate_parallel_probe_broker import RootProbeBroker
from scripts.production_gate_parallel_probe_descriptors import RetainedDescriptor
from scripts.production_gate_parallel_probe_launch import ProbeLaunch
from scripts.production_gate_parallel_probe_owner import ProbePeer
from scripts.production_gate_parallel_root_wait_partition import RootCustodyError
from tests import test_parallel_runtime_job_publication as job_controls
from tests.test_parallel_runtime_probe_owner import final_body, owner_case
from tests.test_parallel_runtime_probe_publication import publication_case


@pytest.fixture
def job_publication(tmp_path, monkeypatch):
    return job_controls.publication.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def isolated_roots(monkeypatch):
    monkeypatch.setattr(roots, "_ROOTS", [])


def linked_owner(tmp_path, monkeypatch, admitted):
    """Real owner/reconcile and pure launch membership, no allocate/process query."""
    owner, now = owner_case(tmp_path)
    context = roots.RootContext()
    context.claim(object(), lambda: True)
    scope = ProbeLaunch(
        owner.context, owner.budget, root_pin=owner.root_pin,
        control_python=owner.control_python, directory=tmp_path,
        command=["controlled-unused"], environment={}, cwd=tmp_path,
    )
    context.prepare_scope(scope)
    scope.process = SimpleNamespace(
        pid=200, returncode=None, stdout=None, stderr=None,
        poll=lambda: pytest.fail("competing holder Popen poll"),
        wait=lambda: pytest.fail("competing holder Popen wait"),
    )
    scope.spawn_attempted = scope.allocated = True
    context.partition.publish_original(scope, scope.process)
    scope.holder_pin, scope.owner = owner.holder_pin, owner
    scope.holder_pidfd_owner = "descriptor"
    owner.root_context, owner.wait_scope = context, scope
    owner.holder_pin.role = "holder"
    owner.holder_pin.bound_descriptor = lambda: 1001
    owner.holder_pin.exited = lambda: True
    context.partition.bind_holder(scope, owner.holder_pin)
    if admitted:
        guardian = RetainedDescriptor(
            1002, instance=(201, 3), parent=(200, 2), role="guardian", validated=True
        )
        payload = RetainedDescriptor(
            1003, instance=(202, 4), parent=(201, 3), role="payload", validated=True
        )
        for record in (guardian, payload):
            record.exited = lambda: True
        owner.custody.roles.update(holder=owner.holder_pin, guardian=guardian, payload=payload)
        owner.peers = [
            ProbePeer(SimpleNamespace(), record=owner.holder_pin,
                      final=final_body(owner, "holder", (200, 2), (201, 3), adopted=((202, 4),))),
            ProbePeer(SimpleNamespace(), record=guardian,
                      final=final_body(owner, "guardian", (201, 3), (202, 4))),
        ]
        scope.acknowledged = scope.setup_complete = True
    else:
        scope.refuse("controlled original pre-ACK refusal")
        assert scope._begin_unadmitted_abort()
    broker = RootProbeBroker(
        root_pin=owner.root_pin, control_python=owner.control_python,
        directory=tmp_path, resource_owner=SimpleNamespace(root_context=context),
    )
    broker.scopes.append(scope)
    return SimpleNamespace(owner=owner, scope=scope, context=context, now=now, broker=broker)


@pytest.mark.parametrize("admitted", [False, True])
@pytest.mark.parametrize("fault", ["none", "wait", "readback"])
def test_both_actual_holder_waits_publish_intent_and_never_repeat_unknown(
    tmp_path, monkeypatch, admitted, fault
):
    case = linked_owner(tmp_path, monkeypatch, admitted)
    original = OSError("exact original wait-after-effect failure")
    calls = []

    class Readback:
        @property
        def si_status(self):
            raise original

        si_code = owners.os.CLD_EXITED

    def wait(kind, fd, flags):
        record = case.context.partition.record(case.scope)
        assert record["state"] == case.owner.holder_wait_state == "attempting"
        assert kind == owners.os.P_PIDFD and fd == 1001
        assert flags == owners.os.WEXITED | owners.os.WNOHANG
        calls.append((kind, fd, flags))
        if fault == "wait":
            raise original  # controlled effect has occurred; no second wait allowed
        return Readback() if fault == "readback" else SimpleNamespace(
            si_code=owners.os.CLD_EXITED, si_status=0
        )

    monkeypatch.setattr(owners, "os", SimpleNamespace(
        P_PIDFD=3, WEXITED=4, WNOHANG=1, CLD_EXITED=1, waitid=wait,
    ))
    if fault == "none":
        assert case.owner.reconcile() and case.owner.reconcile()
        assert case.owner.holder_wait_status == 0 and len(calls) == 1
        assert case.context.partition.record(case.scope)["state"] == "consumed"
    else:
        with pytest.raises(OSError) as caught:
            case.owner.reconcile()
        assert caught.value is original and len(calls) == 1
        assert case.owner.holder_wait_state == "unknown"
        assert case.context.partition.record(case.scope)["state"] == "unknown"
        assert not case.owner.reconcile() and not case.broker.retire(case.scope)
        assert len(calls) == 1 and case.broker.pending()
        assert not case.context.children_quiescent()
        with pytest.raises(RootCustodyError, match="admission withheld"):
            case.context.require_admission()
    assert case.scope.process.returncode == (0 if fault == "none" else None)
    # A cached owned disposition changes the original object without another poll.
    originals = case.context.partition.unreaped_originals()
    if fault == "none":
        assert case.scope.process.pid not in originals
    else:
        assert case.scope.process.pid in originals


@pytest.mark.parametrize("admitted", [False, True])
def test_normal_no_status_can_reobserve_under_same_original_cap(tmp_path, monkeypatch, admitted):
    case = linked_owner(tmp_path, monkeypatch, admitted)
    calls = []
    results = [None, SimpleNamespace(si_code=1, si_status=0)]

    def wait(*args):
        calls.append(args)
        return results.pop(0)

    monkeypatch.setattr(owners, "os", SimpleNamespace(
        P_PIDFD=3, WEXITED=4, WNOHANG=1, CLD_EXITED=1, waitid=wait,
    ))
    bound = case.owner.budget.cleanup_bound()
    assert not case.owner.reconcile() and case.owner.holder_wait_state == "ready"
    assert case.owner.reconcile() and len(calls) == 2
    assert case.owner.budget.cleanup_bound() == bound == 120
    assert case.owner.budget.deadline == 100


@pytest.mark.parametrize("admitted", [False, True])
def test_consumed_status_survives_actual_fallible_budget_checkpoint(tmp_path, monkeypatch, admitted):
    case = linked_owner(tmp_path, monkeypatch, admitted)
    primary = RuntimeError("original post-wait clock failure")
    calls, reads = [], []

    def now():
        reads.append("now")
        if len(reads) == 1:
            raise primary
        return 10.0

    case.owner.budget = MeasurementBudget(100, 120, clock=now)
    monkeypatch.setattr(owners, "os", SimpleNamespace(
        P_PIDFD=3, WEXITED=4, WNOHANG=1, CLD_EXITED=1,
        waitid=lambda *args: (calls.append(args), SimpleNamespace(si_code=1, si_status=0))[1],
    ))
    with pytest.raises(RuntimeError) as caught:
        case.owner.reconcile()
    assert caught.value is primary and case.owner.holder_wait_status == 0
    assert case.owner.holder_wait_state == "consumed" and len(calls) == 1
    assert case.scope.process.returncode == 0
    assert case.scope.process.pid not in case.context.partition.unreaped_originals()
    assert case.owner.reconcile() and len(calls) == 1


def test_actual_probe_allocation_publishes_original_before_first_pin(tmp_path, monkeypatch):
    case = publication_case(tmp_path, monkeypatch)
    context = roots.RootContext()
    context.claim(object(), lambda: True)
    case.broker.root_context = context
    calls = []
    original = RuntimeError("first-pin boundary original")

    def first_pin(pid):
        scope = case.scope()
        record = context.partition.record(scope)
        assert scope in context.scopes and record["process"] is case.process is scope.process
        assert case.process.pid in context.partition.unreaped_originals()
        calls.append(pid)
        raise original

    monkeypatch.setattr(launch_module.os, "pidfd_open", first_pin)
    with pytest.raises(RuntimeError) as caught:
        case.begin()
    assert caught.value is original and calls == [case.process.pid]
    scope = case.scope()
    assert scope.owner is None and scope.holder_pin is None and scope.process is case.process
    assert not context.children_quiescent() and case.waits == case.descriptor_closes == []


def test_actual_job_publication_precedes_parent_pipe_finally_failure(monkeypatch, job_publication):
    job = job_publication
    actual = job_publication.current  # existing fixture retains the actual scheduler ledger
    original = RuntimeError("parent pipe close after normal Popen return")
    calls = []
    real = life.JobLaunch.close_parent_ends

    def closing(slot, os_module):
        if slot.process is not None:
            context = actual().root_context
            assert context.partition.record(slot)["process"] is slot.process
            assert slot.process.pid in context.partition.unreaped_originals()
            calls.append(slot.process)
            slot.pipe_states["status_write"] = "unknown"
            raise original
        return real(slot, os_module)

    monkeypatch.setattr(life.JobLaunch, "close_parent_ends", closing)
    with pytest.raises(RuntimeError) as caught:
        job.run()
    assert caught.value is original and calls == job.processes
    assert not job.polls and job.ledgers[0].is_retained()


def orphan_effects(monkeypatch, tree, children, *, fail_wait=False):
    """Real pin_orphans/drain/reap; only original OS/process queries replaced."""
    effects = SimpleNamespace(pins=[], waits=[], reads=[], stats=[], closes=[])
    root_pid = tree.root_pid

    def read(fd, limit):
        effects.reads.append(fd)
        raise BlockingIOError  # faithful actual GuardianStatus drain readiness

    def stat(pid):
        effects.stats.append(pid)
        if pid == root_pid:
            return tree.root_start, 70000, "Z"
        return (333 if pid == 90001 else 444), 70000, "Z"

    def wait(*args):
        effects.waits.append(args)
        if fail_wait:
            raise OSError("original adopted wait after-effect uncertainty")
        return SimpleNamespace(si_status=0, si_code=1)

    monkeypatch.setattr(processes, "children_of", lambda pid: list(children))
    monkeypatch.setattr(processes, "read_stat", stat)
    monkeypatch.setattr(processes, "read_session", lambda pid: root_pid)
    monkeypatch.setattr(processes, "_pidfd_open", lambda pid: (effects.pins.append(pid), 901)[1])
    monkeypatch.setattr(processes, "_exited", lambda fd: fd != 800)
    monkeypatch.setattr(processes, "os", SimpleNamespace(
        getpid=lambda: 70000, read=read, close=lambda fd: effects.closes.append(fd),
        waitid=wait, P_PIDFD=3, WEXITED=4, WNOHANG=1,
    ))
    return effects


@pytest.mark.parametrize("fail_wait", [False, True])
def test_actual_owned_orphan_wait_preserves_positive_and_suppressed_unknown(
    monkeypatch, job_publication, fail_wait
):
    def unread(*args):
        raise BlockingIOError

    monkeypatch.setattr(processes.os, "read", unread)
    job_publication.run("unproven")
    ledger = job_publication.ledgers[0]
    tree = ledger.slots["source-job"].owned
    effects = orphan_effects(monkeypatch, tree, [90001], fail_wait=fail_wait)
    tree.pin_orphans()
    assert effects.reads and effects.pins == [90001]
    assert tree.orphans_pinned == 1 and 90001 in tree._orphans
    assert not tree.status.error  # actual drain never treated blocking as notification failure
    tree._reap_orphans()
    assert effects.waits == [(3, 901, 5)]
    tree._reap_orphans()
    assert effects.waits == [(3, 901, 5)] and effects.closes == []
    state = ledger.root_context.partition.adopted[(id(tree), 90001, 333)]
    assert state == ("unknown" if fail_wait else "consumed")
    if fail_wait:
        assert not tree.contained and ledger.root_context.failure
        assert not ledger.root_context.children_quiescent()
    else:
        assert tree.contained and not ledger.root_context.failure


def test_foreign_unknown_freezes_new_pin_but_actual_existing_own_pin_still_reaps(
    tmp_path, monkeypatch, job_publication
):
    def unread(*args):
        raise BlockingIOError

    monkeypatch.setattr(processes.os, "read", unread)
    job_publication.run("unproven")
    ledger = job_publication.ledgers[0]
    context = ledger.root_context
    tree = ledger.slots["source-job"].owned
    scope = ProbeLaunch(
        SimpleNamespace(), MeasurementBudget(100, 120, clock=lambda: 10),
        root_pin=SimpleNamespace(), control_python=tmp_path, directory=tmp_path,
        command=["unused"], environment={}, cwd=tmp_path,
    )
    context.prepare_scope(scope)
    scope.spawn_attempted = True
    scope.process = SimpleNamespace(pid=201, returncode=None)
    context.partition.publish_original(scope, scope.process)
    scope.holder_pin = SimpleNamespace(
        validated=True, closed=False, instance=(201, 2), descriptor=1001,
    )
    context.partition.bind_holder(scope, scope.holder_pin)
    # Existing pin came from an already verified owned parent; no new pidfd.
    tree._entries[90001] = (902, 333)
    context.partition.note_owned_pin(tree, 90001, 902, 333)
    effects = orphan_effects(monkeypatch, tree, [201, 90001, 90002])
    tree.pin_orphans()
    tree._reap_orphans()
    assert effects.pins == [] and 201 not in effects.stats
    assert effects.waits == [(3, 902, 5)]
    assert tree.unattributed == [90002] and context.failure
    assert context.partition.adopted[(id(tree), 90001, 333)] == "consumed"
    assert not context.children_quiescent() and not scope.released


def test_partial_and_completed_roots_use_actual_original_returncode_only(job_publication):
    job_publication.run("sibling_partial", serial=False)
    ledger = job_publication.ledgers[0]
    completed, partial = job_publication.processes
    callbacks = [excluded for root, tracked, excluded in job_publication.exclusion_contexts
                 if root == completed.pid and completed.pid in tracked]
    assert callbacks and completed.returncode == 0 and partial.returncode is None
    assert all(completed.pid not in excluded and partial.pid in excluded for excluded in callbacks)
    assert completed.pid not in ledger.root_context.partition.unreaped_originals()
    assert partial.pid in ledger.root_context.partition.unreaped_originals()


def test_actual_b0_final_wait_fallback_never_observes_original_again(monkeypatch, job_publication):
    original = RuntimeError("original final poll after-effect escape")
    calls, escaped = [], []
    factory = processes.subprocess.Popen
    real_final = retirement.final_poll

    def popen(*args, **kwargs):
        process = factory(*args, **kwargs)

        def poll():
            final = process.slot.final_wait_attempted
            calls.append(final)
            if final:
                raise original
            return None  # alive until the actual stop/sweep final observation

        process.poll = poll
        return process

    def final(item):
        try:
            return real_final(item)
        except RuntimeError as error:
            escaped.append(error)
            raise

    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    monkeypatch.setattr(retirement, "final_poll", final)
    report = job_publication.run("interrupt")
    ledger = job_publication.ledgers[0]
    slot = ledger.slots["source-job"]
    assert escaped == [original] and calls.count(True) == 1
    assert slot.final_wait_attempted and not slot.final_wait_complete
    assert "original final poll after-effect escape" in ledger.cleanup[slot.job_id]["stop_error"]
    before = list(calls)
    assert retirement.poll_item(slot.item) is None and retirement.final_poll(slot.item) is None
    owner_service = slot.root_context.run_record(ledger)["service"]
    owner_service.deadlines.pulse()
    owner_service.pulse()
    record = {}
    assert retirement.fallback_record(slot.item, record)
    assert record["process_tree"]["proof"] is False and calls == before
    assert slot.root_context.partition.record(slot)["state"] == "unknown"
    assert report["overall"] == "FAIL" and not report["cleanup_complete"]
    assert ledger.is_retained()


@pytest.mark.parametrize("publication_failure", [False, True])
def test_actual_broker_retires_only_after_exact_wait_eof_and_checked_transfer(
    tmp_path, monkeypatch, publication_failure
):
    case = publication_case(tmp_path, monkeypatch)
    context = roots.RootContext()
    context.claim(object(), lambda: True)
    case.broker.root_context = context
    case.begin()
    scope = case.scope()
    case.without_allocation_callback()
    scope.refuse("controlled pre-ACK abort")
    case.exited = True
    assert scope._begin_unadmitted_abort()
    assert not case.broker.retire(scope) and len(case.waits) == 1
    assert context.scopes == [scope] and not context.children_quiescent()
    case.eof = True
    case.broker.service()
    assert all(scope.output_eof.values())
    actual = context.settle_scope
    original = RuntimeError("scope terminal transfer after-effect failure")
    snapshots = []

    def publish(original_scope, proof):
        assert original_scope is scope and case.broker.scopes == [scope]
        assert scope.released and scope.owner.closed and isinstance(proof, tuple)
        snapshots.append(proof)
        actual(original_scope, proof)
        if publication_failure:
            raise original

    monkeypatch.setattr(context, "settle_scope", publish)
    if publication_failure:
        with pytest.raises(RuntimeError) as caught:
            case.broker.retire(scope)
        assert caught.value is original and context.failure
        assert case.broker.scopes == [scope] and not context.children_quiescent()
        assert scope.terminal_publication_state == "unknown"
    else:
        assert case.broker.retire(scope) and context.children_quiescent()
        assert not case.broker.scopes and scope.terminal_publication_state == "published"
    counts = (list(case.descriptor_closes), case.parent.closes, case.child.closes)
    assert not case.broker.retire(scope)
    assert (case.descriptor_closes, case.parent.closes, case.child.closes) == counts
    assert len(case.waits) == len(snapshots) == 1 and context.scope_proofs == snapshots


@pytest.mark.parametrize("admitted", [False, True])
@pytest.mark.parametrize("changed", ["owner", "pin", "process", "closed"])
def test_actual_holder_wait_rejects_rival_owner_or_changed_original_before_effect(
    tmp_path, monkeypatch, admitted, changed
):
    case = linked_owner(tmp_path, monkeypatch, admitted)
    calls = []
    if changed == "owner":
        case.scope.owner = object()
    elif changed == "pin":
        case.owner.holder_pin = RetainedDescriptor(
            1001, instance=(200, 2), parent=(100, 1), role="holder", validated=True
        )
        case.owner.holder_pin.exited = lambda: True
    elif changed == "process":
        case.scope.process = SimpleNamespace(pid=200, returncode=None)
    else:
        case.owner.holder_pin.closed = True
    monkeypatch.setattr(owners, "os", SimpleNamespace(
        P_PIDFD=3, WEXITED=4, WNOHANG=1, CLD_EXITED=1,
        waitid=lambda *args: calls.append(args),
    ))
    with pytest.raises(RootCustodyError, match="not exclusive"):
        case.owner.reconcile()
    assert calls == [] and case.context.failure
    assert case.owner.holder_wait_state == "unknown"
    assert not case.owner.reconcile() and calls == []
