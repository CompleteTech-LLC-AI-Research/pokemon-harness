"""Actual CLI/session publication controls, authored NOTRUN; all OS effects controlled."""

from contextlib import nullcontext
from dataclasses import replace as dataclass_replace
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_cli as cli
from scripts import production_gate_parallel_probe_session as module
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from scripts.production_gate_parallel_probe_broker import RootProbeBroker
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError
from tests._tier_parallel_runtime_modules import PARALLEL_RUNTIME_MODULES


class CustodyEscape(BaseException):
    """Test-only finite exit from permanent refusal; never product retirement proof."""


@pytest.fixture
def publication(monkeypatch):
    state = SimpleNamespace(
        point="",
        calls=[],
        now=10.0,
        sleeps=0,
        sessions=[],
        wrappers=[],
        primary=RuntimeError("controlled original setup failure"),
        removed=False,
        descriptor_closed=False,
        validates=0,
        restores=0,
        factories=0,
        replacements=0,
        attach_refusals=0,
        unwired_cancel_observed=False,
        owned_calls=0,
    )
    monkeypatch.setattr(module, "_LIVE_SESSIONS", [])
    original_budget = MeasurementBudget(100.0, 120.0, clock=lambda: state.now)

    def budget_factory():
        state.factories += 1
        return original_budget

    def refuse_attach():
        before = (list(state.calls), state.factories, state.replacements)
        with pytest.raises(ProbeProtocolError, match="unavailable"):
            state.session.attach(original_budget)
        assert (state.calls, state.factories, state.replacements) == before
        state.attach_refusals += 1

    class Adoption:
        def __init__(self):
            self.active = False
            self.restoration_pending = False

        def __enter__(self):
            assert module._LIVE_SESSIONS == [state.session]
            assert state.session.enter_attempted
            self.restoration_pending = True
            if state.point == "adoption":
                raise state.primary
            self.active = True
            return self

        def verify(self):
            return self.active

        def __exit__(self, *_args):
            assert state.session.disposition_started
            refuse_attach()
            state.calls.append("restore")
            state.restores += 1
            if state.point == "restore" and state.restores == 1:
                raise RuntimeError("controlled restore refusal")
            if state.point == "restore_pending" and state.restores == 1:
                return  # models checked restoration failure suppressed by the owner
            self.restoration_pending = False
            self.active = False

    def session_factory():
        session = real_session_type()
        state.sessions.append(session)
        state.session = session
        session.cancellation_signals = lambda: nullcontext()
        return session

    def pin(pid):
        state.calls.append("pidfd")
        assert pid == 7300 and module._LIVE_SESSIONS == [state.session]
        assert state.session.root_pin_state == "allocating"
        if state.point == "pidfd":
            raise state.primary
        return 7100

    class Descriptor:
        def __init__(self, raw):
            assert raw == state.session.raw_root_pidfd == 7100
            assert state.session.root_pin_state == "wrapping"
            if state.point == "wrapper":
                raise state.primary
            self.descriptor = raw
            self.binding = self.instance = None
            self.validated = self.closed = False
            state.wrappers.append(self)

        def bound_descriptor(self):
            assert self.validated and self.binding == (1, 2)
            return self.descriptor

    def fstat(fd):
        assert fd == 7100 and state.session.root_pin is state.wrappers[0]
        assert state.session.raw_root_pidfd is None
        if state.point == "fstat":
            raise state.primary
        return SimpleNamespace(st_dev=1, st_ino=2)

    def validate(record, pid, budget):
        state.validates += 1
        session = state.session
        assert record is session.root_pin and pid == 7300
        assert session.root_pin_state == "validating" and budget is original_budget
        assert session.directory_state == "none" and session.raw_directory is None
        assert not session.setup_complete and session.setup_error is None
        with pytest.raises(ProbeProtocolError, match="unavailable"):
            session.attach(original_budget)
        budget.checkpoint()
        if state.point == "validate_interrupt":
            raise KeyboardInterrupt("controlled validation interruption")
        if state.point == "validate":
            raise state.primary
        if state.point == "validate_final":
            state.now = 100.0
            budget.checkpoint()  # actual original-clock final validation checkpoint refusal
        if state.point == "cancel_setup":
            session.interrupt.set()
            budget.checkpoint()  # current preflight service is noop, NOT session.checkpoint
            state.unwired_cancel_observed = True
        budget.checkpoint()
        return 890, 1

    class Path:
        def __init__(self, value):
            self.value = value

        def lstat(self):
            assert self.value == "/controlled/root-exact"
            if state.point == "lstat":
                raise state.primary
            return SimpleNamespace(st_dev=3, st_ino=4)

        def resolve(self, *, strict):
            assert strict and self.value == "/usr/bin/python3"
            if state.point == "resolve":
                raise state.primary
            return self

        def rmdir(self):
            assert state.session.directory_state == "removing"
            state.calls.append("rmdir")
            state.removed = True
            if state.point == "rmdir_interrupt":
                raise KeyboardInterrupt("controlled interrupted rmdir")
            if state.point == "rmdir":
                raise RuntimeError("controlled rmdir mutated then failed")

    def path_factory(value):
        if value == "/controlled/root-exact":
            assert state.session.raw_directory == value
            if state.point == "path":
                raise state.primary
        return Path(value)

    def mkdtemp(*, prefix):
        state.calls.append("mkdtemp")
        assert prefix == "pokered-probe-" and state.session.directory_state == "allocating"
        if state.point == "mkdtemp":
            raise state.primary  # no pathname returned; no absent-resource proof
        return "/controlled/root-exact"

    def close(fd):
        assert fd == 7100 and state.session.root_pin_state == "closing"
        state.calls.append("close")
        state.descriptor_closed = True
        if state.point == "close_interrupt":
            raise KeyboardInterrupt("controlled interrupted close")
        if state.point == "close":
            raise RuntimeError("controlled close mutated then failed")

    def replace(budget, **kwargs):
        state.replacements += 1
        if budget is not original_budget:
            return dataclass_replace(budget, **kwargs)
        assert budget.deadline == 100.0
        assert budget.cleanup_bound() == 120.0
        assert state.session.broker.resource_owner is state.session
        if state.point == "replace":
            raise state.primary
        return dataclass_replace(budget, **kwargs)

    def owned(*, probe_session, **_kwargs):
        state.owned_calls += 1
        assert probe_session is state.session and probe_session.setup_complete
        cleanup = [85.0]
        job_budget = MeasurementBudget(
            80.0, 90.0, clock=lambda: state.now, cleanup_limit=lambda: cleanup[0]
        )
        attached = probe_session.attach(job_budget)
        assert attached.deadline == 80.0 and attached.cleanup_bound() == 85.0
        cleanup[0] = 90.0
        assert attached.cleanup_bound() == job_budget.cleanup_bound() == 85.0
        assert attached.probe_runner.__self__ is probe_session.broker
        assert attached.clock is job_budget.clock
        state.attached = attached
        if state.point == "coherence":
            for owner, name, changed in (
                (probe_session, "root_pin_state", "unknown"),
                (probe_session, "raw_root_pidfd", 7200),
                (probe_session.root_pin, "validated", False),
                (probe_session.root_pin, "closed", True),
                (probe_session, "directory_state", "removed"),
                (probe_session, "directory_binding", None),
                (probe_session.broker, "root_pin", object()),
                (probe_session.broker, "directory", object()),
            ):
                original = getattr(owner, name)
                setattr(owner, name, changed)
                try:
                    refuse_attach()
                finally:
                    setattr(owner, name, original)
        if state.point in ("restore", "restore_pending"):
            raise state.primary
        if state.point == "child":
            broker = probe_session.broker
            scope = SimpleNamespace(refuse=lambda _reason: None)
            broker.scopes.append(scope)
            assert probe_session.attach(job_budget).deadline == 80.0
            assert not probe_session.disposition_started
            with pytest.raises(ProbeProtocolError, match="retain inputs and adoption"):
                probe_session.close()
            assert not state.restores and not state.removed and not state.descriptor_closed

            def retire(actual):
                assert actual is scope and broker.pending()
                state.calls.append("child-retire")
                broker.scopes.remove(actual)
                return True  # synthetic physical/disposition oracle; not kernel proof

            broker.service = lambda: None
            broker.retire = retire
        return 17

    def sleep(_seconds):
        if state.point in ("rmdir", "close", "rmdir_interrupt", "close_interrupt"):
            refuse_attach()  # actual CLI retry interval, after ambiguous mutation
        if state.point in ("restore", "restore_pending"):
            assert state.session.adoption.restoration_pending
            refuse_attach()  # actual CLI retry interval, before same-owner restoration
        state.sleeps += 1
        if state.sleeps >= 3:
            raise CustodyEscape("bounded test escape; custody not transferred")

    real_session_type = module.ProbeSession
    monkeypatch.setattr(module, "OrchestratorAdoption", Adoption)
    monkeypatch.setattr(module, "pre_dispatch_budget", budget_factory)
    monkeypatch.setattr(module, "RetainedDescriptor", Descriptor)
    monkeypatch.setattr(module, "validate_pidfd", validate)
    monkeypatch.setattr(module, "Path", path_factory)
    monkeypatch.setattr(module, "tempfile", SimpleNamespace(mkdtemp=mkdtemp))
    monkeypatch.setattr(module, "replace", replace)
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            getpid=lambda: 7300,
            pidfd_open=pin,
            fstat=fstat,
            close=close,
            access=lambda _path, _mode: state.point == "writable",
            W_OK=2,
        ),
    )
    monkeypatch.setattr(cli, "ProbeSession", session_factory)
    monkeypatch.setattr(cli, "_run_parallel_cli_owned", owned)
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=sleep))
    state.original_budget = original_budget
    state.real_session_type = real_session_type
    state.run = lambda point="": (setattr(state, "point", point), cli.run_parallel_cli())[1]
    return state


@pytest.mark.parametrize(
    "point", ["pidfd", "wrapper", "fstat", "validate", "validate_final", "validate_interrupt"]
)
def test_actual_cli_retains_pin_uncertainty_and_first_failure_without_retry(publication, point):
    p = publication
    with pytest.raises(CustodyEscape) as escaped:
        p.run(point)
    session = p.session
    assert module._LIVE_SESSIONS == [session] and not session.closed
    assert p.calls.count("pidfd") == 1 and not p.descriptor_closed and not p.restores
    assert session.setup_error is not None and session.setup_failure
    if point == "validate_final":
        assert isinstance(session.setup_error, IdentityDeadline)
    elif point == "validate_interrupt":
        assert isinstance(session.setup_error, KeyboardInterrupt)
    else:
        assert session.setup_error is p.primary
    assert escaped.value.__context__ is not None  # outward escape may replace primary in finally
    assert not session.setup_complete and p.owned_calls == 0 and p.factories == 1
    if point == "wrapper":
        assert session.raw_root_pidfd == 7100 and session.root_pin is None
    elif point == "pidfd":
        assert session.raw_root_pidfd is None and session.root_pin_state == "allocating"
    else:
        assert session.root_pin is p.wrappers[0] and session.root_pin_state == "validating"
    before = list(p.calls)
    first = session.setup_error
    with pytest.raises(ProbeProtocolError, match="re-entered"):
        session.__enter__()
    assert session.setup_error is first
    replacement = p.real_session_type()
    with pytest.raises(ProbeProtocolError, match="previous root probe owner"):
        replacement.__enter__()
    replacement.close()
    assert module._LIVE_SESSIONS == [session] and p.calls == before
    assert p.factories == 2  # new object's construction is not recovery of original bounds


@pytest.mark.parametrize("point", ["mkdtemp", "path", "lstat"])
def test_actual_cli_keeps_returned_path_before_conversion_and_never_rebinds(publication, point):
    p = publication
    with pytest.raises(CustodyEscape):
        p.run(point)
    session = p.session
    assert session.setup_error is p.primary and not session.setup_complete
    assert session.raw_directory == (None if point == "mkdtemp" else "/controlled/root-exact")
    assert session.directory_binding is None and not p.removed
    assert not p.descriptor_closed and not p.restores and p.owned_calls == 0
    assert p.calls.count("mkdtemp") == p.calls.count("pidfd") == 1
    assert module._LIVE_SESSIONS == [session] and p.factories == 1


@pytest.mark.parametrize("point", ["resolve", "writable", "replace"])
def test_late_setup_failure_disposes_valid_resources_in_actual_cli_order(publication, point):
    p = publication
    with pytest.raises((RuntimeError, ProbeProtocolError)) as caught:
        p.run(point)
    session = p.session
    assert caught.value is session.setup_error
    if point != "writable":
        assert caught.value is p.primary
    assert not session.setup_complete and p.owned_calls == 0
    assert p.calls[-3:] == ["rmdir", "close", "restore"]
    assert session.root_pin_state == "closed" and session.directory_state == "removed"
    assert session.closed and not module._LIVE_SESSIONS
    assert p.factories == 1 and p.original_budget.deadline == 100.0
    assert p.original_budget.cleanup_bound() == 120.0
    if point == "replace":
        assert session.budget is p.original_budget and isinstance(session.broker, RootProbeBroker)
    session.close()
    assert p.calls[-3:] == ["rmdir", "close", "restore"] and p.restores == 1


def test_positive_actual_cli_transfer_attach_and_idempotent_close(publication):
    p = publication
    assert p.run() == 17
    session = p.session
    assert session.setup_complete and not session.setup_failure and session.setup_error is None
    assert session.raw_root_pidfd is None and session.root_pin.closed
    assert session.raw_directory == "/controlled/root-exact" and session.directory is None
    assert session.closed and p.restores == 1 and p.factories == 1 and p.owned_calls == 1
    assert p.calls[-3:] == ["rmdir", "close", "restore"]
    before = list(p.calls)
    session.close()
    with pytest.raises(ProbeProtocolError, match="re-entered"):
        session.__enter__()
    with pytest.raises(ProbeProtocolError, match="unavailable"):
        session.attach(p.original_budget)
    assert p.calls == before


@pytest.mark.parametrize("point", ["rmdir", "close", "rmdir_interrupt", "close_interrupt"])
def test_ambiguous_disposition_is_attempted_once_across_actual_cli_retries(publication, point):
    p = publication
    with pytest.raises(CustodyEscape):
        p.run(point)
    session = p.session
    assert session.setup_complete and not session.setup_failure
    operation = point.split("_", 1)[0]
    assert p.calls.count(operation) == 1 and not p.restores and p.sleeps == 3
    assert module._LIVE_SESSIONS == [session] and not session.closed
    assert session.disposition_started and p.attach_refusals == 3
    if operation == "rmdir":
        assert session.directory_state == "unknown" and not p.descriptor_closed
    else:
        assert session.root_pin_state == "unknown" and session.directory_state == "removed"
    with pytest.raises(ProbeProtocolError, match="ownership remains unresolved"):
        session.close()
    assert p.calls.count(operation) == 1


@pytest.mark.parametrize("point", ["restore", "restore_pending"])
def test_same_owner_restoration_retry_preserves_actual_original_exception(publication, point):
    p = publication
    with pytest.raises(RuntimeError) as caught:
        p.run(point)
    session = p.session
    assert caught.value is p.primary and session.closed and not module._LIVE_SESSIONS
    assert p.restores == 2 and not session.adoption.restoration_pending
    assert session.disposition_started and p.attach_refusals == 3
    assert p.calls.count("rmdir") == p.calls.count("close") == 1
    assert p.calls[-2:] == ["restore", "restore"]
    assert p.primary.__notes__  # actual CLI records refusal then eventual checked retirement


def test_no_resource_adoption_failure_can_restore_without_fabricated_allocation(publication):
    p = publication
    with pytest.raises(RuntimeError) as caught:
        p.run("adoption")
    assert caught.value is p.primary and p.session.setup_error is p.primary
    assert p.session.closed and p.calls == ["restore"] and not module._LIVE_SESSIONS
    assert p.session.root_pin_state == p.session.directory_state == "none"


def test_actual_broker_child_pending_is_serviced_before_root_disposition(publication):
    p = publication
    assert p.run("child") == 17
    assert isinstance(p.session.broker, RootProbeBroker) and not p.session.broker.pending()
    assert p.calls[-4:] == ["child-retire", "rmdir", "close", "restore"]
    assert p.session.closed and not module._LIVE_SESSIONS


def test_setup_interrupt_is_observed_but_unwired_checkpoint_does_not_claim_cancellation(
    publication,
):
    p = publication
    with pytest.raises(IdentityDeadline, match="CLI cancelled"):
        p.run("cancel_setup")
    assert p.unwired_cancel_observed and p.session.setup_complete and p.owned_calls == 1
    assert p.session.closed and p.factories == 1
    # Earlier setup cancellation is NOTPROVEN; existing wiring begins at final replace.


def test_registry_appends_the_actual_new_control_module():
    assert "test_parallel_runtime_session_publication.py" in PARALLEL_RUNTIME_MODULES


def test_current_resource_incoherence_refuses_attach_in_actual_cli(publication):
    p = publication
    assert p.run("coherence") == 17
    assert p.attach_refusals == 9  # eight injected inconsistencies plus final restoration
    assert p.session.closed and p.factories == 1
