"""Authored NOTRUN actual shared-root controls. All launch/kernel effects controlled."""

import json
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel as scheduler
from scripts import production_gate_parallel_cli as cli
from scripts import production_gate_parallel_finishing as finishing
from scripts import production_gate_parallel_job_retirement as retirement
from scripts.production_gate_parallel_service import OwnerService
from scripts.production_gate_parallel_stopping import DeadlineWatch
from scripts.production_gate_parallel_identity import expected_identity
from scripts import production_gate_parallel_job_lifecycle as life
from scripts import production_gate_parallel_probe_launch as launches
from scripts import production_gate_parallel_probe_owner as owners
from scripts import production_gate_parallel_probe_session as sessions
from scripts import production_gate_parallel_root_context as roots
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError
from scripts.production_gate_parallel_root_wait_partition import RootCustodyError
from tests._tier_parallel_runtime_modules import PARALLEL_RUNTIME_MODULES
from tests._parallel_runtime_report_support import good_artifact, report_bytes
from tests import test_parallel_runtime_job_publication as job_controls
from tests.test_parallel_runtime_probe_publication import publication_case
from tests.test_parallel_runtime_session_publication import CustodyEscape
from tests import test_parallel_runtime_session_publication as session_controls


@pytest.fixture
def job_publication(tmp_path, monkeypatch):
    return job_controls.publication.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def session_publication(monkeypatch):
    return session_controls.publication.__wrapped__(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_roots(monkeypatch):
    monkeypatch.setattr(roots, "_ROOTS", [])


def lend_cli(monkeypatch, job, session, *, point=""):
    """Use the real CLI/session/dispatch wrapper; replace only the owned CLI body."""
    actual_run = scheduler.run_parallel_plan
    captured = []

    def borrowed(plan, **kwargs):
        kwargs["root_context"] = session.session.root_context
        captured.append(kwargs["root_context"])
        return actual_run(plan, **kwargs)

    def body(*, probe_session, **kwargs):
        assert probe_session is session.session and probe_session.setup_complete
        # The old CLI-only double omitted the scheduler's existing enabled flag.
        # Add it only in this new combined caller fixture, not the frozen tests.
        probe_session.adoption.enabled = True
        report = job.run(point)
        assert job.ledgers[0].adoption is probe_session.adoption
        assert captured == [probe_session.root_context]
        return 17 if report["cleanup_complete"] else 19

    monkeypatch.setattr(scheduler, "run_parallel_plan", borrowed)
    monkeypatch.setattr(cli, "_run_parallel_cli_owned", body)
    return captured


def test_actual_cli_scheduler_share_one_adoption_and_non_circular_close(
    monkeypatch, job_publication, session_publication
):
    job, session = job_publication, session_publication
    lend_cli(monkeypatch, job, session)
    assert session.run() == 17
    ledger = job.ledgers[0]
    context = session.session.root_context
    assert ledger.root_context is context and not ledger.owns_adoption
    assert context.run_record(ledger)["settled"] and context.run_record(ledger)["proof"]
    assert session.restores == 1 and job.exited == 0
    assert context.closed and session.session.closed and not session.session.pending()
    assert not life._RETAINED_RUNS and not roots._ROOTS
    assert session.calls[-3:] == ["rmdir", "close", "restore"]


def test_standalone_uses_adoption_only_and_preserves_original_public_facade(
    monkeypatch, job_publication
):
    job = job_publication
    monkeypatch.setattr(sessions.ProbeSession, "__enter__", lambda self: pytest.fail("new endpoint"))
    report = job.run()
    context = job.ledgers[0].root_context
    assert context.standalone and context.closed and context.probe_service is None
    assert context.adoption is job.ledgers[0].adoption and not context.scopes
    assert report["cleanup_complete"] and job.exited == 1
    assert not roots._ROOTS


def test_disabled_adoption_cannot_borrow_live_root(monkeypatch, job_publication):
    job = job_publication
    owner = roots.RootContext()
    owner.claim(object(), lambda: True)
    actual = scheduler.run_parallel_plan
    calls = []

    def borrowed(plan, **kwargs):
        calls.append("facade")
        return actual(plan, root_context=owner, **kwargs)

    monkeypatch.setattr(scheduler, "run_parallel_plan", borrowed)
    with pytest.raises(RuntimeError, match="disabled adoption cannot borrow"):
        job.run(adopting=False)
    assert calls == ["facade"] and owner.runs == []
    assert not job.processes and job.exited == 0


@pytest.mark.parametrize("point", ["configure", "first_pin", "insertion_after"])
def test_partial_originals_block_outer_close_without_probe_scope_or_wait_retry(
    monkeypatch, job_publication, session_publication, point
):
    job, session = job_publication, session_publication
    lend_cli(monkeypatch, job, session, point=point)
    # Actual CLI retains the owner indefinitely. Test-only BaseException exits
    # after three controlled retry intervals; it is never physical retirement.
    with pytest.raises(CustodyEscape):
        session.run()
    ledger = job.ledgers[0]
    root = session.session
    assert root.broker.scopes == [] and root.pending() and not root.children_quiescent()
    assert ledger.is_retained() and not ledger.root_context.run_record(ledger)["settled"]
    assert ledger.slots["source-job"].process is job.processes[0]
    assert not job.polls and not session.restores and not session.removed
    assert not session.descriptor_closed and not root.disposition_started
    with pytest.raises(RootCustodyError, match="admission withheld"):
        root.root_context.require_admission()


@pytest.mark.parametrize("after", [False, True])
def test_terminal_proof_failure_keeps_actual_ledger_and_outer_membership(
    monkeypatch, job_publication, after
):
    original = RuntimeError("terminal proof publication escaped")
    actual = roots.RootContext.settle_run
    calls = []

    def publish(context, ledger, proof):
        calls.append((ledger, proof))
        assert ledger.is_retained() and isinstance(proof, tuple)
        if after:
            actual(context, ledger, proof)
        raise original

    monkeypatch.setattr(roots.RootContext, "settle_run", publish)
    with pytest.raises(RuntimeError) as caught:
        job_publication.run()
    ledger = job_publication.ledgers[0]
    assert caught.value is original and len(calls) == 1
    assert ledger.terminal_state == "unknown" and ledger.is_retained()
    assert not ledger.root_context.children_quiescent() and job_publication.exited == 0
    assert not ledger.finalize(ledger.cleanup) and len(calls) == 1


def test_combined_actual_owner_callback_reentry_and_primary_still_service_probe(
    monkeypatch, job_publication
):
    job = job_publication
    job.run("unproven")
    ledger = job.ledgers[0]
    context = ledger.root_context
    service = context.run_record(ledger)["service"]
    assert service.running is service.deadlines.running
    actual = service.pulse
    events = []
    primary = KeyboardInterrupt("owner callback original")
    polls = list(job.polls)
    bounds = [slot.current_cleanup_cap for slot in ledger.slots.values()]

    def pulse(**kwargs):
        events.append("job")
        actual(**kwargs)
        raise primary

    def probes():
        events.append("probe")
        context.service()  # composed callback returns through the same guard

    monkeypatch.setattr(service, "pulse", pulse)
    context.probe_service = probes
    with pytest.raises(KeyboardInterrupt) as caught:
        context.service()
    assert caught.value is primary and events == ["job", "probe"]
    assert job.polls == polls and not context._servicing
    assert [slot.current_cleanup_cap for slot in ledger.slots.values()] == bounds


def test_actual_session_combined_guard_runs_before_nested_scope_use(monkeypatch):
    session = sessions.ProbeSession()
    context = session.root_context
    context.claim(object(), lambda: True)
    calls = []
    session.broker = SimpleNamespace(
        scopes=[SimpleNamespace(refuse=lambda reason: calls.append(reason))]
    )
    session.interrupt.set()
    context.probe_service = session.checkpoint
    # The composed session callback observes cancellation but cannot recursively
    # touch the scope or reenter the outer finite service pass.
    with pytest.raises(IdentityDeadline, match="measurement cancelled"):
        context.service()
    assert calls == [] and not context._servicing
    context.disposing = True
    with pytest.raises(RootCustodyError, match="barred"):
        session.service()
    assert calls == []


def test_job_pending_retains_actual_runtime_settlement(monkeypatch, job_publication, tmp_path):
    job_publication.run("configure")
    ledger = job_publication.ledgers[0]
    root = sessions.ProbeSession()
    root.root_context = ledger.root_context
    root.broker = SimpleNamespace(pending=lambda: False)
    effects = []
    monkeypatch.setattr(cli, "should_retain_runtime", lambda report: False)
    monkeypatch.setattr(cli, "thaw_and_remove", lambda path: effects.append(path))
    report = {}
    assert not cli.settle_runtime(report, "/snapshot", tmp_path, probe_session=root)
    assert report["snapshot_retained"] == "/snapshot" and effects == []
    with pytest.raises(ProbeProtocolError, match="retain inputs and adoption"):
        monkeypatch.setattr(sessions, "_LIVE_SESSIONS", [root])
        root.close()
    assert root.root_pin_state == root.directory_state == "none"


def test_original_job_budget_cancellation_and_tightening_keep_same_suppliers(job_publication):
    job_publication.run("tighten")
    ledger = job_publication.ledgers[0]
    context = ledger.root_context
    caps = tuple(slot.current_cleanup_cap for slot in ledger.slots.values())
    polls = list(job_publication.polls)
    context.service()
    assert tuple(slot.current_cleanup_cap for slot in ledger.slots.values()) == caps
    assert job_publication.polls == polls and not context.children_quiescent()
    assert context.run_record(ledger)["service"].cancel_reason


def test_root_control_discovery_is_append_only():
    assert "test_parallel_runtime_root_context.py" in PARALLEL_RUNTIME_MODULES
    assert "test_parallel_runtime_wait_partition.py" in PARALLEL_RUNTIME_MODULES


@pytest.mark.parametrize("refusal", ["", "cancel", "tighten"])
def test_actual_finish_keeps_same_skip_across_composed_session_checkpoint(
    monkeypatch, job_publication, session_publication, refusal
):
    job, session = job_publication, session_publication
    real_finish = finishing.finish_job  # never the fixture's scheduler replacement
    callbacks, measured, outcomes, artifact_reads, siblings = [], [], [], [], []
    validations = []
    original_validate = finishing.validate_terminal_artifact

    def validate(*args, **kwargs):
        problems = original_validate(*args, **kwargs)
        validations.append((args[2], kwargs["child_exit_status"], problems))
        assert problems == []
        return problems

    monkeypatch.setattr(finishing, "validate_terminal_artifact", validate)
    original_read = finishing.read_terminal_artifact

    def read(*args, **kwargs):
        artifact_reads.append(args[0])
        result = original_read(*args, **kwargs)
        assert result[0] is not None and result[1] == []
        return result

    monkeypatch.setattr(finishing, "read_terminal_artifact", read)

    def finish(plan, item, status, clock, wall_clock, ignored_measure, worker_pid, **kwargs):
        context = session.session.root_context
        owner = context.run_record(job.ledgers[0])["service"]
        assert context._finish_owner is item and status == 0
        if outcomes:
            return real_finish(plan, item, status, clock, wall_clock,
                               ignored_measure, worker_pid, **kwargs)
        original_cap, original_deadline = item.cleanup_deadline, item.deadline
        wall = item.launched_wall.isoformat()
        artifact = good_artifact(plan, item.job_id, pid=worker_pid, token=item.token,
                                 started=wall, finished=wall)
        (item.tree["root"] / "out" / "terminal.json").write_text(json.dumps(artifact))
        (item.tree["root"] / "evidence" / "gate-report.json").write_bytes(
            report_bytes(plan, item.job_id)
        )
        actual_pulse = owner.pulse

        def pulse(**pulse_kwargs):
            callbacks.append((context._finish_owner, pulse_kwargs.get("skip")))
            return actual_pulse(**pulse_kwargs)

        monkeypatch.setattr(owner, "pulse", pulse)
        for other in owner.running:
            if other is item:
                continue
            observe = other.owned.observe

            def observed(observe=observe, other=other):
                siblings.append(other.job_id)
                return observe()

            monkeypatch.setattr(other.owned, "observe", observed)

        def measure(job_id, *, checkpoint, budget):
            measured.append((job_id, budget))
            assert budget.deadline == original_deadline
            assert budget.cleanup_bound() <= original_cap
            assert budget.cancel_reason() == owner.cancel_reason
            # Compose the actual attached CLI session checkpoint with finishing's
            # original MeasurementBudget, rather than constructing a test budget.
            attached = session.session.attach(budget)
            assert attached.clock is budget.clock and attached.cancel_reason is budget.cancel_reason
            assert attached.deadline == budget.deadline
            assert attached.cleanup_bound() <= original_cap
            attached.checkpoint()
            if refusal == "cancel":
                owner.cancel_reason = "unique final measurement cancellation"
            elif refusal == "tighten":
                item.cleanup_deadline = clock()
            checkpoint()  # actual finishing checkpoint consumes original suppliers
            identity = expected_identity(plan.as_dict(), plan.job(job_id)["mode"])
            return dict(identity, clean_head=True), []

        result = real_finish(plan, item, status, clock, wall_clock, measure, worker_pid, **kwargs)
        outcomes.append(result)
        assert item.deadline == original_deadline and item.cleanup_deadline <= original_cap
        assert result["child_exit_status"] == status and result["artifact"] == artifact
        if refusal:
            assert result["state"] == "failed"
            reason = "cancelled" if refusal == "cancel" else "frozen deadline"
            assert any(reason in problem for problem in result["artifact_problems"])
        else:
            assert result["state"] == "passed" and not result["artifact_problems"]
        monkeypatch.setattr(owner, "pulse", actual_pulse)
        return result

    monkeypatch.setattr(scheduler, "_finish_job", finish)
    original_run = job.run
    job.run = lambda point="": original_run(point, serial=False)
    lend_cli(monkeypatch, job, session)
    assert session.run() in (17, 19)
    assert measured and artifact_reads and validations and outcomes and siblings
    assert all(status == 0 and problems == [] for _, status, problems in validations)
    assert callbacks and all(retiring is skip for retiring, skip in callbacks)
    assert session.session.root_context._finish_owner is None


def test_actual_no_return_popen_never_creates_identity_or_root_close_authority(job_publication):
    report = job_publication.run("popen")
    ledger = job_publication.ledgers[0]
    slot = ledger.slots["source-job"]
    record = ledger.root_context.partition.record(slot)
    assert slot.spawn_attempted and slot.process is None and record["process"] is None
    assert record["state"] == "prepared" and not slot.no_spawn_eligible
    assert not ledger.root_context.partition.unreaped_originals()
    assert ledger.is_retained() and not ledger.root_context.children_quiescent()
    assert report["overall"] == "FAIL" and not report["cleanup_complete"]
    assert job_publication.polls == [] and job_publication.exited == 0


def controlled_probe(tmp_path, monkeypatch):
    case = publication_case(tmp_path, monkeypatch)
    # Use one consistent root identity across both existing controlled OS models.
    case.broker.root_pin.instance = (70000, 1)
    monkeypatch.setattr(launches.os, "getpid", lambda: 70000)
    monkeypatch.setattr(owners.os, "getpid", lambda: 70000)
    return case


def test_actual_completed_jobs_cannot_restore_while_registered_probe_is_live(
    tmp_path, monkeypatch, job_publication
):
    case = controlled_probe(tmp_path, monkeypatch)
    actual = scheduler._finish_job
    started = []

    def finish(*args, **kwargs):
        if not started:
            context = job_publication.ledgers[0].root_context
            case.broker.root_context = context
            case.begin()
            started.append(case.scope())
        return actual(*args, **kwargs)

    monkeypatch.setattr(scheduler, "_finish_job", finish)
    report = job_publication.run()
    ledger = job_publication.ledgers[0]
    context = ledger.root_context
    assert all(slot.custody_state == "disposed" for slot in ledger.slots.values())
    assert context.run_record(ledger)["settled"] and context.scopes == started
    assert not context.children_quiescent() and not started[0].released
    assert job_publication.exited == 0 and not report["cleanup_complete"]
    assert case.waits == case.descriptor_closes == []
    assert ledger.terminal_state == "restoration_pending"
    scope = started[0]
    case.without_allocation_callback()
    scope.refuse("controlled later positive pre-ACK disposition")
    case.exited = case.eof = True
    assert scope._begin_unadmitted_abort()
    case.broker.service()
    assert case.broker.retire(scope)
    before = (list(job_publication.polls), list(job_publication.closed))
    monkeypatch.setattr(ledger, "dispose_unstarted", lambda: pytest.fail("repeated disposal"))
    monkeypatch.setattr(context, "settle_run", lambda *args: pytest.fail("second terminal publish"))
    assert ledger.finalize(ledger.cleanup)
    assert (job_publication.polls, job_publication.closed) == before
    assert context.closed and job_publication.exited == 1 and not ledger.is_retained()


def test_actual_probe_terminal_transfer_cannot_hide_partial_job_custody(
    tmp_path, monkeypatch, job_publication
):
    case = controlled_probe(tmp_path, monkeypatch)
    actual = scheduler.start_allocation
    started = []

    def start(*args, **kwargs):
        if not started:
            case.broker.root_context = kwargs["ledger"].root_context
            case.begin()
            started.append(case.scope())
        return actual(*args, **kwargs)

    monkeypatch.setattr(scheduler, "start_allocation", start)
    report = job_publication.run("configure")
    ledger = job_publication.ledgers[0]
    scope = started[0]
    case.without_allocation_callback()
    scope.refuse("controlled pre-ACK terminal probe")
    case.exited = case.eof = True
    assert scope._begin_unadmitted_abort()
    case.broker.service()
    assert case.broker.retire(scope)
    assert ledger.root_context.scopes == [] and ledger.root_context.scope_proofs
    assert not ledger.root_context.children_quiescent() and ledger.is_retained()
    assert not ledger.root_context.run_record(ledger)["settled"]
    assert job_publication.budget.reserved_total == 1 and job_publication.exited == 0
    assert not report["cleanup_complete"] and job_publication.polls == []
    assert len(case.waits) == 1 and case.process.returncode == 70


@pytest.mark.parametrize("suppressed", [False, True])
def test_actual_standalone_restoration_continues_without_second_publication_or_disposal(
    monkeypatch, job_publication, suppressed
):
    job = job_publication
    original = RuntimeError("original primary before restoration") if suppressed else None
    calls, publications, allowed = [], [], []
    actual_settle = roots.RootContext.settle_run
    actual_exit = scheduler.OrchestratorAdoption.__exit__
    actual_enter = scheduler.OrchestratorAdoption.__enter__

    def settle(context, ledger, proof):
        publications.append(proof)
        return actual_settle(context, ledger, proof)

    def restore(adoption, *exc):
        calls.append(exc[1])
        if not allowed:
            assert adoption.restoration_pending
            if suppressed:
                assert exc[1] is original
                original.add_note("unique suppressed restoration remains pending")
                return
            raise RuntimeError("unique transient restore failure")
        return actual_exit(adoption, *exc)

    monkeypatch.setattr(roots.RootContext, "settle_run", settle)
    monkeypatch.setattr(scheduler.OrchestratorAdoption, "__exit__", restore)
    if suppressed:
        def enter(adoption):
            actual_enter(adoption)
            raise original

        monkeypatch.setattr(scheduler.OrchestratorAdoption, "__enter__", enter)
        with pytest.raises(RuntimeError) as caught:
            job.run()
        assert caught.value is original
    else:
        job.run()
    ledger = job.ledgers[0]
    assert ledger.terminal_state == "restoration_pending" and ledger.is_retained()
    assert len(publications) == 1 and len(calls) >= 1
    pending_attempts = len(calls)
    allowed.append(True)
    before = (list(job.polls), list(job.closed), list(job.events))
    monkeypatch.setattr(ledger, "dispose_unstarted", lambda: pytest.fail("second disposition"))
    assert ledger.finalize(ledger.cleanup, original)
    assert calls == [original] * (pending_attempts + 1) and len(publications) == 1
    assert (job.polls, job.closed, job.events) == before
    assert ledger.terminal_state == "published" and not ledger.is_retained()
    assert ledger.root_context.closed and not roots._ROOTS
    if suppressed:
        assert original.__notes__ == ["unique suppressed restoration remains pending"] * pending_attempts


@pytest.mark.parametrize("first", ["service", "scope"])
def test_actual_session_continuation_attempts_later_scope_and_preserves_primary(monkeypatch, first):
    root = sessions.ProbeSession()
    root.root_context.claimed = True
    primary = KeyboardInterrupt("unique first custody continuation")
    secondary = RuntimeError("unique later custody continuation")
    effects = []
    scopes = [SimpleNamespace(refuse=lambda reason: effects.append("refuse-a")),
              SimpleNamespace(refuse=lambda reason: effects.append("refuse-b"))]

    def service():
        effects.append("service")
        if first == "service":
            raise primary

    def retire(scope):
        effects.append("retire-a" if scope is scopes[0] else "retire-b")
        if scope is scopes[0]:
            raise primary if first == "scope" else secondary
        scopes.remove(scope)  # independent positive reconciliation

    root.broker = SimpleNamespace(scopes=scopes, retire=retire)
    monkeypatch.setattr(root, "service", service)
    with pytest.raises(KeyboardInterrupt) as caught:
        root.continue_custody()
    assert caught.value is primary
    assert effects == ["service", "refuse-a", "retire-a", "refuse-b", "retire-b"]
    assert len(scopes) == 1
    if first == "service":
        assert any("unique later custody continuation" in note for note in primary.__notes__)


def two_job_service():
    """Controlled effect endpoints with real association, partition, watch and owner."""
    context = roots.RootContext()
    context.claimed = True
    counts = {"a": {"poll": 0, "observe": 0, "signal": 0},
              "b": {"poll": 0, "observe": 0, "signal": 0}}
    primary = RuntimeError("unique ordinary poll after-effect uncertainty")
    items = []
    for index, key in enumerate(("a", "b")):
        effects = counts[key]
        process = SimpleNamespace(pid=100 + index, returncode=None)
        view = SimpleNamespace(ready=True, eof=False, drain=lambda: None)
        tree = SimpleNamespace(root_pid=process.pid, root_start=1, status=view,
                               contained=True, reason="")
        tree.observe = lambda effects=effects: effects.__setitem__("observe", effects["observe"] + 1)
        tree.signal_root = lambda sig, effects=effects: effects.__setitem__("signal", effects["signal"] + 1)
        item = SimpleNamespace(job_id=key, process=process, owned=tree,
                               deadline=10.0, cleanup_deadline=15.0, stop_requested=False)
        slot = SimpleNamespace(item=item, process=process, owned=tree, status=view,
                               initialized=True, setup_complete=True, custody_use="active",
                               custody_state="holding", final_wait_attempted=False,
                               final_wait_complete=False, root_context=context)
        item.launch = tree._custody_slot = view._custody_slot = slot
        process.guardian_status = view
        context.partition.prepare(slot, "job")
        context.partition.publish_original(slot, process)

        def poll(key=key, effects=effects):
            effects["poll"] += 1
            if key == "a":
                raise primary
            return None

        process.poll = poll
        items.append(item)
    hub = SimpleNamespace(expect_drop=lambda job: None, check_leases=lambda: None,
                          closed_reason="", lose=lambda reason: None)
    watch = DeadlineWatch(items, hub, 5.0, lambda item: False,
                          grace_seconds=0.01, clock=lambda: 10.0,
                          sleeper=lambda seconds: None, problems=[])
    owner = OwnerService(items, hub, SimpleNamespace(mark_lost=lambda reason: None),
                         SimpleNamespace(check=lambda: ""), watch,
                         SimpleNamespace(job=lambda job: {}), None,
                         cleanup_seconds=5.0, clock=lambda: 10.0)
    return context, owner, items, counts, primary


@pytest.mark.parametrize("entry", ["owner", "deadline"])
def test_actual_two_job_ordinary_poll_unknown_keeps_primary_and_services_sibling(entry):
    context, owner, items, counts, primary = two_job_service()
    operation = owner.pulse if entry == "owner" else owner.deadlines.pulse
    with pytest.raises(RuntimeError) as caught:
        operation()
    assert caught.value is primary and counts["a"]["poll"] == 1
    assert not retirement.active_item(items[0]) and retirement.active_item(items[1])
    assert context.partition.record(items[0].launch)["state"] == "unknown"
    assert counts["b"]["poll"] > 0 and counts["b"]["signal"] == 1
    assert items[1].stop_requested and items[1].cleanup_deadline <= 15.0
    before = dict(counts["a"])
    owner.deadlines.pulse()
    owner.pulse()
    owner.deadlines.pulse()
    assert counts["a"] == before and counts["b"]["observe"] > 0
    assert counts["b"]["signal"] == 1 and items[1].cleanup_deadline <= 15.0
