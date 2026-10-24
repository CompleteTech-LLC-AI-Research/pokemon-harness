"""Synthetic controls for exact wait, output EOF, and release of unadmitted probe holders.

All controls remain NOTRUN; these do not prove kernel retirement.
"""

from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_probe_owner as owner_module
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError
from tests.test_parallel_runtime_probe_launch import launch_case
from tests.test_parallel_runtime_probe_owner import owner_case


def abort_case(tmp_path, monkeypatch):
    scope = launch_case(tmp_path)
    owner, now = owner_case(tmp_path)
    scope.root_pin, scope.holder_pin, scope.owner = owner.root_pin, owner.holder_pin, owner
    scope.process = SimpleNamespace(pid=200, returncode=None)
    scope.spawn_attempted = True
    scope.refuse("controlled parent config refusal")
    monkeypatch.setattr(owner.holder_pin, "bound_descriptor", lambda: 1001)
    monkeypatch.setattr(owner.holder_pin, "exited", lambda: True)
    return scope, owner, now


def test_abort_exact_wait_then_output_eof_without_fabricated_final(tmp_path, monkeypatch):
    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    waits = []

    def waitid(kind, descriptor, flags):
        waits.append((kind, descriptor, flags))
        return SimpleNamespace(si_code=owner_module.os.CLD_EXITED, si_status=70)

    monkeypatch.setattr(owner_module.os, "waitid", waitid)
    assert scope._begin_unadmitted_abort()
    assert not scope.reconcile()  # exact wait is saved but output is not drained
    assert scope.process.returncode == 70
    scope.output_eof = dict.fromkeys(scope.output, True)
    assert scope.reconcile()
    assert waits == [
        (owner_module.os.P_PIDFD, 1001, owner_module.os.WEXITED | owner_module.os.WNOHANG)
    ]
    assert owner.peers == [] and owner.custody.roles == {}
    assert scope.failures and owner.custody.failures
    assert not owner.operation_succeeded()


@pytest.mark.parametrize("fault", ["ack", "peer", "rights", "pin", "waiter", "process"])
def test_abort_unknown_custody_or_authority_stays_pending(tmp_path, monkeypatch, fault):
    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    if fault == "ack":
        scope.parent_ack_attempted = True
    elif fault == "peer":
        owner.peers.append(object())
    elif fault == "rights":
        owner.custody.records.append(object())
    elif fault == "pin":
        scope.holder_pin.validated = False
    elif fault == "waiter":
        scope.process.returncode = 1
    else:
        scope.process = None  # uncertain constructor never supplies process authority
    assert not scope._begin_unadmitted_abort()
    assert not scope.unadmitted_abort and owner.unadmitted_process is None


@pytest.mark.parametrize("outcome", ["short", "raise"])
def test_parent_ack_attempt_is_latched_before_uncertain_send(tmp_path, outcome):
    scope = launch_case(tmp_path)
    scope.pending_ack = b"controlled"

    def send(raw):
        assert scope.parent_ack_attempted
        if outcome == "raise":
            raise OSError("uncertain send")
        return len(raw) - 1

    scope.parent = SimpleNamespace(send=send)
    with pytest.raises((OSError, ProbeProtocolError)):
        scope._send_pending("pending_ack")
    assert scope.parent_ack_attempted and not scope.acknowledged
    assert scope.pending_ack == b"controlled"


def test_abort_normal_service_cannot_accept_or_ack(tmp_path, monkeypatch):
    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    calls = []
    assert scope._begin_unadmitted_abort()
    monkeypatch.setattr(owner_module, "advance_probe_stop", lambda value: calls.append(value))
    owner.listener = SimpleNamespace(accept=lambda: pytest.fail("abort accepted peer"))
    owner.service()
    assert calls == [owner]


def test_abort_wait_is_retained_when_clock_refuses(tmp_path, monkeypatch):
    scope, owner, now = abort_case(tmp_path, monkeypatch)
    calls = []
    assert scope._begin_unadmitted_abort()
    monkeypatch.setattr(
        owner_module.os,
        "waitid",
        lambda *args: (
            calls.append(args) or SimpleNamespace(si_code=owner_module.os.CLD_EXITED, si_status=1)
        ),
    )
    original = owner.budget
    owner.budget = SimpleNamespace(now=lambda: (_ for _ in ()).throw(ValueError("clock")))
    with pytest.raises(ValueError, match="clock"):
        owner.reconcile()
    assert owner.holder_wait_status == 1 and len(calls) == 1
    owner.budget = original
    now[0] = 121.0
    assert owner.reconcile() and len(calls) == 1
    assert owner.custody.failures and owner.physical_cleanup_verified_at == 121.0


@pytest.mark.parametrize("fault", ["accept", "stop_budget"])
def test_owner_only_refusal_selects_abort_and_eof_gates_failed_retirement(
    tmp_path, monkeypatch, fault
):
    from scripts.production_gate_parallel_probe_broker import RootProbeBroker

    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    scope.failures.clear()
    owner.custody.failures.clear()
    scope.process.stdout = scope.process.stderr = None
    closes, waits, descriptor_closes = [], [], []
    if fault == "accept":

        def refuse_accept():
            raise OSError("controlled accept failure before any peer")

        owner.listener = SimpleNamespace(accept=refuse_accept, close=lambda: closes.append(True))
        owner.endpoint.write_bytes(b"synthetic endpoint")
        info = owner.endpoint.lstat()
        owner.endpoint_binding = (info.st_dev, info.st_ino)
    else:
        original = owner.budget
        owner.budget = SimpleNamespace(now=lambda: (_ for _ in ()).throw(ValueError("clock")))
    owner.service()  # actual owner catch/stop-budget refusal, no launch refusal
    if fault == "stop_budget":
        owner.budget = original
    assert not scope.failures and owner.custody.failures and not owner.peers

    def waitid(*args):
        waits.append(args)
        return SimpleNamespace(si_code=owner_module.os.CLD_EXITED, si_status=70)

    def close(descriptor):
        assert descriptor == 1001  # no real close, including on an unexpected descriptor
        descriptor_closes.append(descriptor)

    # Replace this module's dependency, leaving the runner's shared os untouched.
    isolated_os = SimpleNamespace(**vars(owner_module.os))
    isolated_os.waitid, isolated_os.close = waitid, close
    monkeypatch.setattr(owner_module, "os", isolated_os)
    scope.pulse()
    assert scope.unadmitted_abort and not scope.parent_ack_attempted
    broker = RootProbeBroker(
        root_pin=scope.root_pin,
        control_python=scope.control_python,
        directory=tmp_path,
        resource_owner=object(),
    )
    broker.scopes.append(scope)
    assert not broker.retire(scope) and broker.pending() and len(waits) == 1
    assert waits == [
        (owner_module.os.P_PIDFD, 1001, owner_module.os.WEXITED | owner_module.os.WNOHANG)
    ]
    assert descriptor_closes == []
    scope.output_eof = dict.fromkeys(scope.output, True)
    assert broker.retire(scope) and not broker.pending() and len(waits) == 1
    receipt = broker.receipts[0]
    assert receipt["unadmitted_abort"] and receipt["custody_failures"]
    assert receipt["finals"] == {} and receipt["holder_wait_status"] == 70
    assert receipt["output_counts"] == {"stdout": 0, "stderr": 0}
    assert closes == ([True] if fault == "accept" else [])

    assert descriptor_closes == [1001] and owner.closed and scope.released
    if fault == "accept":
        assert not owner.endpoint.exists()
    assert not broker.retire(scope)  # already removed from registry
    assert scope.release_confirmed_resources() and owner.release_confirmed_resources()
    assert descriptor_closes == [1001]  # idempotent release never closes twice


def test_appended_peer_before_owner_error_still_excludes_abort(tmp_path, monkeypatch):
    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    scope.failures.clear()
    owner.custody.failures.clear()

    def refuse_configuration(flag):
        assert owner.peers  # source has already retained the actual accepted channel
        raise OSError("controlled accepted-channel configuration failure")

    channel = SimpleNamespace(setblocking=refuse_configuration)
    owner.listener = SimpleNamespace(accept=lambda: (channel, None))
    owner.service()
    assert not scope.failures and owner.custody.failures
    assert len(owner.peers) == 1 and owner.peers[0].channel is channel
    assert not scope._begin_unadmitted_abort()


@pytest.mark.parametrize("entry", ["service", "service_unadmitted_abort"])
@pytest.mark.parametrize("callback", ["clock", "cleanup_limit"])
def test_abort_reentrant_budget_callbacks_skip_both_public_routes(
    tmp_path, monkeypatch, entry, callback
):
    from scripts.production_gate_parallel_measurement_budget import MeasurementBudget

    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    assert scope._begin_unadmitted_abort()
    calls, callbacks = [], []
    original_advance = owner_module.advance_probe_stop

    def advance(value):
        assert value.servicing
        calls.append(value)
        original_advance(value)

    def reenter():
        assert owner.servicing
        callbacks.append(True)
        owner.service()
        owner.service_unadmitted_abort()
        return 10.0 if callback == "clock" else None

    owner.budget = MeasurementBudget(100, 120, **{callback: reenter})
    owner.listener = SimpleNamespace(accept=lambda: pytest.fail("nested accept"))
    monkeypatch.setattr(owner_module, "advance_probe_stop", advance)
    getattr(owner, entry)()
    assert calls == [owner] and callbacks == [True] and not owner.servicing


@pytest.mark.parametrize("entry", ["service", "service_unadmitted_abort"])
def test_abort_service_guard_restores_after_step_failure(tmp_path, monkeypatch, entry):
    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    assert scope._begin_unadmitted_abort()
    calls = []

    def refuse(value):
        assert value.servicing
        calls.append(value)
        value.service()
        value.service_unadmitted_abort()
        raise OSError("controlled abort step failure")

    monkeypatch.setattr(owner_module, "advance_probe_stop", refuse)
    if entry == "service":
        owner.service()
        assert any("controlled abort step failure" in reason for reason in owner.custody.failures)
    else:
        with pytest.raises(OSError, match="controlled abort step failure"):
            owner.service_unadmitted_abort()
    assert calls == [owner] and not owner.servicing


def test_direct_abort_service_skips_while_ordinary_service_owns_scope(tmp_path, monkeypatch):
    scope, owner, _ = abort_case(tmp_path, monkeypatch)
    scope.failures.clear()
    owner.custody.failures.clear()
    calls = []

    def accept():
        assert owner.servicing and owner.unadmitted_process is None
        owner.service_unadmitted_abort()  # must skip before checking abort admission
        calls.append(True)
        raise BlockingIOError

    owner.listener = SimpleNamespace(accept=accept)
    monkeypatch.setattr(owner_module, "advance_probe_stop", lambda value: None)
    owner.service()
    assert calls == [True] and not owner.servicing and not owner.custody.failures
