"""Synthetic controls for cancellation custody, allocation refusal, and tightened cleanup bounds.

All controls remain NOTRUN; these do not prove live signal or process retirement.
"""

import contextlib
import signal
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_cli as cli
from scripts import production_gate_parallel_probe_session as sessions
from scripts.production_gate_parallel_probe_broker import RootProbeBroker
from scripts.production_gate_parallel_probe_owner import ProbePeer
from tests.test_parallel_runtime_probe_launch import launch_case
from tests.test_parallel_runtime_probe_owner import final_body, owner_case
from tests.test_parallel_runtime_probe_protocol import signed_bytes


@pytest.mark.parametrize("earlier_failure", [False, True])
def test_cli_keeps_custody_after_cleanup_interrupt_and_refusal(monkeypatch, earlier_failure):
    calls = []
    original = RuntimeError("original measurement failed")
    interrupted = KeyboardInterrupt("controlled cleanup interrupt")

    class Session:
        closed = False
        interrupt = SimpleNamespace(set=lambda: calls.append("cancel"))

        @contextlib.contextmanager
        def cancellation_signals(self):
            calls.append("guard entered")
            yield
            calls.append("guard exited")

        def __enter__(self):
            return self

        def pending(self):
            return "retired" not in calls

        def continue_custody(self):
            calls.append("pulse")
            count = calls.count("pulse")
            if count == 1:
                raise interrupted
            if count == 2:
                raise RuntimeError("controlled service refusal")
            calls.append("retired")

        def close(self):
            assert "retired" in calls
            calls.append("closed")
            self.closed = True

    def run(**kwargs):
        if earlier_failure:
            raise original
        return 0

    monkeypatch.setattr(cli, "ProbeSession", Session)
    monkeypatch.setattr(cli, "_run_parallel_cli_owned", run)
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=lambda seconds: None))
    expected = original if earlier_failure else interrupted
    with pytest.raises(type(expected)) as caught:
        cli.run_parallel_cli()
    assert caught.value is expected
    assert calls.count("pulse") == 3 and calls[-1] == "closed"
    assert calls.count("cancel") == 2


def test_local_guard_latches_sigint_through_cleanup_and_restores(monkeypatch):
    handlers, restored = {}, []

    def install(number, handler):
        old = handlers.get(number, f"original-{number}")
        handlers[number] = handler
        restored.append((number, handler))
        return old

    monkeypatch.setattr(sessions.signal, "signal", install)
    session = sessions.ProbeSession()
    with session.cancellation_signals():
        handlers[signal.SIGINT](signal.SIGINT, None)
        assert session.interrupt.is_set()
        session.interrupt.clear()
        handlers[signal.SIGINT](signal.SIGINT, None)
        assert session.interrupt.is_set()
    assert all(handlers[number] == f"original-{number}" for number in handlers)
    assert len(restored) == 6


def test_never_attempted_allocation_retires_failed_receipt_without_finals(tmp_path):
    scope = launch_case(tmp_path)
    scope.allocated = True  # controlled refusal before Popen boundary
    scope.refuse("controlled pre-spawn refusal")
    broker = RootProbeBroker(
        root_pin=scope.root_pin,
        control_python=scope.control_python,
        directory=tmp_path,
        resource_owner=object(),
    )
    broker.scopes.append(scope)
    assert broker.retire(scope) and not broker.pending()
    receipt = broker.receipts[0]
    assert receipt["never_spawned"] and not receipt["spawn_attempted"]
    assert receipt["holder_wait_status"] is None and receipt["finals"] == {}
    assert receipt["physical_cleanup_verified_at"] is None and receipt["failures"]


@pytest.mark.parametrize("process", [None, SimpleNamespace(pid=201)])
def test_uncertain_or_returned_popen_is_never_zero_child(tmp_path, process):
    scope = launch_case(tmp_path)
    scope.allocated, scope.spawn_attempted, scope.process = True, True, process
    assert scope.owner is None and not scope.never_spawned()
    assert not scope.reconcile() and not scope.release_confirmed_resources()


def test_partial_zero_child_release_retries_only_remaining_owned_resources(tmp_path):
    scope = launch_case(tmp_path)
    scope.allocated, scope.directory_created = True, True
    scope.directory.mkdir()
    info = scope.directory.lstat()
    scope.directory_binding = (info.st_dev, info.st_ino)
    blocker = scope.directory / "unexpected"
    blocker.write_text("controlled obstruction")
    calls = []
    scope.parent = SimpleNamespace(close=lambda: calls.append("closed"))
    assert not scope.release_confirmed_resources()
    assert calls == ["closed"] and scope.parent is None and scope.directory_created
    blocker.unlink()
    assert scope.release_confirmed_resources()
    assert calls == ["closed"] and not scope.directory_created
    assert scope.failures and scope.process is None and not scope.spawn_attempted


@pytest.mark.parametrize("queued_before_tighten", [False, True])
def test_original_bound_final_survives_tightening_with_failed_disposition(
    tmp_path, queued_before_tighten
):
    owner, now = owner_case(tmp_path)
    current = [120.0]
    owner.budget = replace(owner.budget, cleanup_limit=lambda: current[0])
    owner.custody.roles["guardian"] = SimpleNamespace(instance=(201, 3))
    holder = SimpleNamespace(instance=(200, 2), role="holder", sequence=2)
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    peer = ProbePeer(receiver, record=holder)
    body = final_body(owner, "holder", holder.instance, (201, 3))
    try:
        if queued_before_tighten:
            sender.send(signed_bytes(body, owner.context))
        current[0] = 110.0
        now[0] = 111.0
        if not queued_before_tighten:
            # Producer still has original context; no tighter stop delivered.
            sender.send(signed_bytes(body, owner.context))
        owner._read(peer)
        assert peer.final == body and holder.sequence == body["sequence"]
        assert owner.custody.failures and not owner.operation_succeeded()
    finally:
        receiver.close()
        sender.close()


def test_valid_physical_final_stays_retained_when_current_bound_query_refuses(tmp_path):
    owner, _ = owner_case(tmp_path)

    def unknown_bound():
        raise RuntimeError("controlled current cleanup bound unavailable")

    owner.budget = replace(owner.budget, cleanup_limit=unknown_bound)
    owner.custody.roles["guardian"] = SimpleNamespace(instance=(201, 3))
    holder = SimpleNamespace(instance=(200, 2), role="holder", sequence=2)
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    peer = ProbePeer(receiver, record=holder)
    body = final_body(owner, "holder", holder.instance, (201, 3))
    try:
        sender.send(signed_bytes(body, owner.context))
        with pytest.raises(RuntimeError, match="current cleanup bound unavailable"):
            owner._read(peer)
        assert peer.final == body and holder.sequence == body["sequence"]
        # Outer owner.service records the refusal; no result is established here.
        assert owner.physical_cleanup_verified_at is None
    finally:
        receiver.close()
        sender.close()
