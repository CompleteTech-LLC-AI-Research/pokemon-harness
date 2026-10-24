"""Authored NOTRUN controls for actual retained broker/launch publication boundaries.

Process creation, pidfds, socket operations, waits, signals and descriptor closes
are controlled doubles. Temporary paths exercise ownership linkage only; these
controls do not establish kernel behavior or admit runtime execution.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_probe_broker as broker_module
from scripts import production_gate_parallel_probe_launch as launch_module
from scripts import production_gate_parallel_probe_owner as owner_module
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError
from tests.test_parallel_runtime_probe_launch import launch_case


class Channel:
    def __init__(self, descriptor):
        self.descriptor, self.closes, self.sent = descriptor, 0, []
        self.blocking = None

    def fileno(self):
        return self.descriptor

    def setblocking(self, flag):
        self.blocking = flag

    def close(self):
        self.closes += 1

    def send(self, raw):
        self.sent.append(raw)
        return len(raw)

    def recvmsg(self, *args):
        raise BlockingIOError


def publication_case(tmp_path, monkeypatch, fault=""):
    """Use the actual broker.begin -> scope.allocate -> owner path, never Popen."""

    seed = launch_case(tmp_path)
    parent, child = Channel(401), Channel(402)
    stdout, stderr = Channel(403), Channel(404)
    process = SimpleNamespace(
        pid=201,
        returncode=None,
        stdout=stdout,
        stderr=stderr,
        poll=lambda: pytest.fail("competing probe Popen.poll"),
        wait=lambda: pytest.fail("competing probe Popen.wait"),
    )
    case = SimpleNamespace(
        raw_fd=550001,
        parent=parent,
        child=child,
        process=process,
        listeners=[],
        pidfd_calls=[],
        descriptor_closes=[],
        waits=[],
        observations=[],
        eof=False,
        exited=False,
    )
    broker = broker_module.RootProbeBroker(
        root_pin=seed.root_pin,
        control_python=seed.control_python,
        directory=tmp_path,
        resource_owner=object(),
    )
    case.broker = broker

    def scope():
        assert len(broker.scopes) == 1  # actual caller registered before allocation
        return broker.scopes[0]

    case.scope = scope

    def callback():
        value = scope()
        if value.owner is not None:
            assert value.owner.custody.records == [] and value.owner.peers == []
            assert value.owner.stop_state.started is None
            assert value.owner.holder_wait_status is None
            assert value.owner.physical_cleanup_verified_at is None
            assert value.raw_holder_pidfd is None
            assert value.holder_pidfd_owner == "descriptor"
            assert not value.setup_complete
            case.observations.append(value.owner)
            if fault == "callback" and value.owner.listener is not None:
                raise IdentityDeadline("controlled owner callback refusal")

    # Later pulses/retirement do not use this allocation-only callback contract.
    budget = MeasurementBudget(100, 120, clock=lambda: 10.0, service=callback)

    def popen(*args, **kwargs):
        value = scope()
        assert value.spawn_attempted and value.process is None
        assert value.parent is parent and value.child is child
        assert value.directory_created and value.directory_binding is not None
        if fault == "popen":
            raise OSError("controlled uncertain Popen constructor")
        return process

    def pidfd_open(pid):
        assert scope().process is process and pid == process.pid
        case.pidfd_calls.append(pid)
        return case.raw_fd

    def fstat(descriptor):
        value = scope()
        assert value.process is process and descriptor == case.raw_fd
        assert value.owner is not None and value.owner.holder_pin is value.holder_pin
        assert value.raw_holder_pidfd is None and value.holder_pidfd_owner == "descriptor"
        if fault == "fstat":
            raise OSError("controlled first fstat failure")
        return SimpleNamespace(st_dev=11, st_ino=12)

    def validate(record, pid, active_budget):
        value = scope()
        assert value.owner is not None and value.owner.holder_pin is record
        assert record.binding == (11, 12) and not record.validated
        record.bound_descriptor = lambda: case.raw_fd
        record.exited = lambda: case.exited
        record.signal = lambda number: pytest.fail("unexpected synthetic signal")
        active_budget.checkpoint()  # real nested broker service sees complete owner
        if fault == "validate":
            raise ProbeProtocolError("controlled first pin validation failure")
        assert pid == process.pid
        return 2, seed.root_pin.instance[0]

    def read(descriptor, limit):
        assert descriptor in (stdout.descriptor, stderr.descriptor)
        if case.eof:
            return b""
        raise BlockingIOError

    class Listener(Channel):
        def setblocking(self, flag):
            assert scope().owner.listener is self  # retained before configuration
            if fault == "setblocking":
                raise OSError("controlled listener setblocking failure")
            super().setblocking(flag)

        def bind(self, path):
            assert scope().owner.listener is self
            assert Path(path) == scope().directory / "root.sock"
            if fault == "bind":
                raise OSError("controlled listener bind failure")
            Path(path).write_bytes(b"controlled endpoint, not a socket")

        def listen(self, count):
            assert scope().owner.endpoint_binding is not None
            if fault == "listen":
                raise OSError("controlled listener listen failure")

        def accept(self):
            raise BlockingIOError

    def listener(*args):
        value = Listener(405)
        case.listeners.append(value)
        return value

    def chmod(path, mode):
        assert scope().owner.endpoint_binding is not None
        if fault == "chmod":
            raise OSError("controlled endpoint chmod failure")

    def close(descriptor):
        assert descriptor == case.raw_fd  # NEVER pass synthetic fds to real os.close
        case.descriptor_closes.append(descriptor)

    def waitid(kind, descriptor, flags):
        assert kind == os.P_PIDFD and descriptor == case.raw_fd
        assert flags == os.WEXITED | os.WNOHANG
        case.waits.append((kind, descriptor, flags))
        return SimpleNamespace(si_code=os.CLD_EXITED, si_status=70)

    launch_os = SimpleNamespace(**vars(launch_module.os))
    launch_os.pidfd_open, launch_os.fstat, launch_os.read = pidfd_open, fstat, read
    launch_os.set_blocking = lambda descriptor, flag: None
    owner_os = SimpleNamespace(**vars(owner_module.os))
    owner_os.chmod, owner_os.close, owner_os.waitid = chmod, close, waitid
    launch_socket = SimpleNamespace(**vars(launch_module.socket))
    launch_socket.socketpair = lambda *args: (parent, child)
    owner_socket = SimpleNamespace(**vars(owner_module.socket))
    owner_socket.socket = listener
    monkeypatch.setattr(launch_module, "os", launch_os)
    monkeypatch.setattr(owner_module, "os", owner_os)
    monkeypatch.setattr(launch_module, "socket", launch_socket)
    monkeypatch.setattr(owner_module, "socket", owner_socket)
    monkeypatch.setattr(launch_module, "ctypes", SimpleNamespace(CDLL=lambda *a, **k: object()))
    monkeypatch.setattr(launch_module, "read_subreaper", lambda libc: 1)
    monkeypatch.setattr(launch_module, "validate_pidfd", validate)
    monkeypatch.setattr(
        launch_module, "subprocess", SimpleNamespace(Popen=popen, DEVNULL=-3, PIPE=-1)
    )
    case.begin = lambda: broker.begin(
        seed.command, environment=seed.environment, cwd=seed.cwd, budget=budget
    )
    case.without_allocation_callback = lambda: setattr(
        scope(), "budget", MeasurementBudget(100, 120, clock=lambda: 10.0)
    )
    return case


def assert_retained_refusal(case):
    scope = case.scope()
    assert case.broker.pending() and case.broker.scopes == [scope]
    assert scope.failures and not scope.released
    assert not scope.parent_ack_attempted and not scope.acknowledged
    assert case.parent.sent == [] and case.descriptor_closes == []
    assert not case.broker.retire(scope)
    with pytest.raises(ProbeProtocolError, match="cannot be retried"):
        scope.allocate()
    with pytest.raises(ProbeProtocolError, match="unresolved prior custody"):
        case.begin()


def test_raw_pidfd_wrapper_failure_keeps_actual_registered_scope(tmp_path, monkeypatch):
    case = publication_case(tmp_path, monkeypatch)

    def refuse_wrapper(descriptor):
        scope = case.scope()
        assert scope.process is case.process
        assert scope.raw_holder_pidfd == descriptor == case.raw_fd
        assert scope.holder_pidfd_owner == "raw" and scope.holder_pin is None
        raise ValueError("controlled descriptor wrapper failure")

    monkeypatch.setattr(launch_module, "RetainedDescriptor", refuse_wrapper)
    with pytest.raises(ValueError, match="wrapper failure"):
        case.begin()
    scope = case.scope()
    assert scope.raw_holder_pidfd == case.raw_fd and scope.holder_pin is None
    assert scope.holder_pidfd_owner == "raw" and scope.owner is None
    assert scope.process is case.process and not scope.never_spawned()
    assert_retained_refusal(case)
    case.without_allocation_callback()
    case.broker.service()  # unknown raw pin never becomes a stop/wait/close capability
    assert case.pidfd_calls == [201] and case.waits == []
    assert case.descriptor_closes == [] and not scope.release_confirmed_resources()
    assert scope.raw_holder_pidfd == case.raw_fd


@pytest.mark.parametrize("fault", ["fstat", "validate"])
def test_owner_is_complete_and_published_before_first_validation_fault(
    tmp_path, monkeypatch, fault
):
    case = publication_case(tmp_path, monkeypatch, fault)
    with pytest.raises((OSError, ProbeProtocolError), match="controlled first"):
        case.begin()
    scope = case.scope()
    assert scope.process is case.process and scope.raw_holder_pidfd is None
    assert scope.holder_pidfd_owner == "descriptor"
    assert scope.owner.holder_pin is scope.holder_pin and not scope.holder_pin.validated
    assert scope.owner.listener is None and scope.owner.custody.failures
    assert not scope._begin_unadmitted_abort()  # no validated original first pin
    assert_retained_refusal(case)
    assert not scope.release_confirmed_resources() and case.descriptor_closes == []


def test_pure_owner_preparation_failure_retains_callers_original_resources(tmp_path, monkeypatch):
    case = publication_case(tmp_path, monkeypatch)

    def refuse_prepare(cls, *args, **kwargs):
        scope = case.scope()
        assert scope.process is case.process and scope.holder_pin is kwargs["holder_pin"]
        assert scope.raw_holder_pidfd is None and scope.holder_pidfd_owner == "descriptor"
        assert scope.parent is case.parent and scope.child is case.child
        assert scope.directory_created and scope.directory_binding is not None
        raise ValueError("controlled pure owner preparation failure")

    monkeypatch.setattr(owner_module.RootProbeOwner, "prepare", classmethod(refuse_prepare))
    with pytest.raises(ValueError, match="pure owner preparation failure"):
        case.begin()
    scope = case.scope()
    assert scope.owner is None and scope.holder_pin is not None
    assert not scope.holder_pin.validated and case.child.closes == 0
    assert_retained_refusal(case)
    assert not scope.release_confirmed_resources() and case.descriptor_closes == []


@pytest.mark.parametrize("fault", ["setblocking", "bind", "chmod", "listen", "callback"])
def test_normal_listener_and_path_are_retained_before_fallible_configuration(
    tmp_path, monkeypatch, fault
):
    case = publication_case(tmp_path, monkeypatch, fault)
    with pytest.raises((OSError, IdentityDeadline), match="controlled"):
        case.begin()
    scope = case.scope()
    assert scope.owner.listener is case.listeners[0]
    assert scope.owner.endpoint == scope.directory / "root.sock"
    assert scope.holder_pin.validated and scope.owner.holder_pin is scope.holder_pin
    assert scope.raw_holder_pidfd is None and scope.holder_pidfd_owner == "descriptor"
    assert case.child.closes == 1 and scope.child is None
    assert not scope.setup_complete and scope.owner.custody.failures
    if fault in ("chmod", "listen", "callback"):
        info = scope.owner.endpoint.lstat()
        assert scope.owner.endpoint_binding == (info.st_dev, info.st_ino)
    else:
        assert scope.owner.endpoint_binding is None
    assert_retained_refusal(case)
    assert case.listeners[0].closes == 0
    assert case.waits == []  # failure alone is not exact physical wait proof


def test_pure_prepare_invokes_no_callback_process_query_socket_or_fd_mutation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("resource-free owner preparation performed an external operation")

    monkeypatch.setattr(owner_module, "os", SimpleNamespace(__getattr__=forbidden))
    monkeypatch.setattr(owner_module, "socket", SimpleNamespace(socket=forbidden))
    budget = SimpleNamespace(checkpoint=forbidden, now=forbidden, cleanup_bound=forbidden)
    context, root, pin = object(), object(), object()
    owner = owner_module.RootProbeOwner.prepare(
        context,
        budget,
        root_pin=root,
        holder_pin=pin,
        control_python=Path("/controlled/python"),
        endpoint=Path("/controlled/root.sock"),
    )
    assert owner.context is context and owner.budget is budget
    assert owner.root_pin is root and owner.holder_pin is pin
    assert owner.listener is None and owner.peers == [] and owner.custody.records == []
    assert owner.stop_state.started is None and owner.unadmitted_process is None
    assert owner.holder_wait_status is None and owner.physical_cleanup_verified_at is None


def test_completed_setup_transfers_exact_pin_once_and_preserves_existing_abort_proof(
    tmp_path, monkeypatch
):
    case = publication_case(tmp_path, monkeypatch)
    scope = case.begin()
    assert case.broker.scopes == [scope] and case.broker.pending()
    assert case.observations and all(owner is scope.owner for owner in case.observations)
    assert scope.setup_complete and scope.pending_initial is not None
    assert scope.raw_holder_pidfd is None and scope.holder_pidfd_owner == "descriptor"
    assert scope.holder_pin.descriptor == case.raw_fd and scope.holder_pin.validated
    assert scope.owner.holder_pin is scope.holder_pin and scope.owner.root_pin is scope.root_pin
    assert case.pidfd_calls == [201] and case.parent.sent == []
    assert case.child.closes == 1 and scope.child is None
    case.without_allocation_callback()
    scope.refuse("controlled postsetup refusal before parent ACK")
    case.exited = True
    assert scope._begin_unadmitted_abort()
    assert not case.broker.retire(scope)  # exact wait alone is not output EOF
    assert len(case.waits) == 1 and scope.process.returncode == 70
    assert case.descriptor_closes == [] and not scope.released
    case.eof = True
    case.broker.service()
    assert all(scope.output_eof.values())
    assert case.broker.retire(scope) and not case.broker.pending()
    assert case.descriptor_closes == [case.raw_fd] and len(case.waits) == 1
    assert scope.holder_pin.closed and scope.owner.closed and scope.released
    assert case.parent.closes == case.child.closes == case.listeners[0].closes == 1
    assert not scope.directory.exists()
    receipt = case.broker.receipts[0]
    assert receipt["unadmitted_abort"] and receipt["failures"] and receipt["custody_failures"]
    assert receipt["finals"] == {} and receipt["holder_wait_status"] == 70
    assert scope.release_confirmed_resources() and scope.owner.release_confirmed_resources()
    assert case.descriptor_closes == [case.raw_fd] and len(case.waits) == 1
    assert case.parent.closes == case.child.closes == case.listeners[0].closes == 1


@pytest.mark.parametrize("transfer_state", ["unopened", "raw", "descriptor"])
def test_unknown_raw_cannot_be_reclassified_as_positive_never_spawned(
    tmp_path, monkeypatch, transfer_state
):
    case = publication_case(tmp_path, monkeypatch)

    def refuse_wrapper(descriptor):
        raise ValueError("controlled raw retention")

    monkeypatch.setattr(launch_module, "RetainedDescriptor", refuse_wrapper)
    with pytest.raises(ValueError, match="raw retention"):
        case.begin()
    scope = case.scope()
    # Even contradictory phase absence cannot erase an actually returned raw fd.
    scope.spawn_attempted, scope.process = False, None
    scope.holder_pidfd_owner = transfer_state  # include interrupted transfer windows
    assert not scope.never_spawned() and not case.broker.retire(scope)
    assert scope.raw_holder_pidfd == case.raw_fd and case.descriptor_closes == []
    assert not scope.release_confirmed_resources() and case.broker.pending()


def test_uncertain_popen_failure_remains_retained_without_a_first_pin(tmp_path, monkeypatch):
    case = publication_case(tmp_path, monkeypatch, "popen")
    with pytest.raises(OSError, match="uncertain Popen"):
        case.begin()
    scope = case.scope()
    assert scope.spawn_attempted and scope.process is None
    assert scope.raw_holder_pidfd is None and scope.holder_pidfd_owner == "unopened"
    assert scope.owner is None and scope.holder_pin is None and not scope.never_spawned()
    assert_retained_refusal(case)
    assert case.pidfd_calls == [] and case.waits == []
