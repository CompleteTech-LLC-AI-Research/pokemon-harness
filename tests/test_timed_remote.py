"""PKTH v1 / timed wire v3 remote contracts and authored owner integration.

Real local sockets and real channels validate the prelude and transport. Any
SessionSpy below is only a facade delegation double, never owner/CPU proof.
Source/native synthetic retirement and IRQ settlement belong to the owner gate.
"""

import ast
import hashlib
import importlib
import importlib.machinery
import importlib.util
import queue
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from itertools import repeat
from pathlib import Path
from types import ModuleType

import pytest

PRELUDE = struct.Struct(">4sBBBB16sI")
HEADER = struct.Struct(">4sBBH16sQ")
DOMAIN = b"pokered-timed-epoch-v3\0"
ROM = {"red": 1, "blue": 2, "yellow": 3}
SIDE = {"listener": 1, "connector": 2}
BOUND = 3.0
TIMING = {"rearm_budget": 32, "rearm_instruction_cap": 16, "max_edge_lateness": 32}


@pytest.fixture(scope="module")
def remote():
    """Load relative leaf modules without importing the runtime-facing package."""
    name = "_timed_remote_contract"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/pokered_harness/link")]
    sys.modules[name] = package
    try:
        yield importlib.import_module(f"{name}.timed_remote")
    finally:
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                del sys.modules[key]


class Job:
    """Bounded worker with exceptions returned to the asserting thread."""

    def __init__(self, call):
        self.results = queue.Queue()

        def run():
            try:
                self.results.put((True, call()))
            except BaseException as exc:  # noqa: BLE001 - propagate all worker failures
                self.results.put((False, exc))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(BOUND + 1)
        assert not self.thread.is_alive(), "remote worker exceeded outer bound"
        ok, result = self.results.get_nowait()
        if not ok:
            raise result
        return result


def record(rom_name="blue", side_name="connector", nonce=bytes(16), **changes):
    fields = {
        "magic": b"PKTH",
        "prelude": 1,
        "wire": 3,
        "rom": ROM[rom_name],
        "side": SIDE[side_name],
        "nonce": nonce,
        "caps": 1,
    }
    fields.update(changes)
    return PRELUDE.pack(*fields.values())


def epoch(listener, connector):
    return hashlib.sha256(DOMAIN + listener + connector).digest()[:16]


def hello(session_epoch):
    return HEADER.pack(b"PKTW", 3, 1, 4, session_epoch, 1) + struct.pack(">I", 1)


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        assert part, f"unexpected EOF after {len(data)}/{size} bytes"
        data.extend(part)
    return bytes(data)


@contextmanager
def sockets(kind="socketpair"):
    if kind == "socketpair":
        left, right = socket.socketpair()
    else:
        family = socket.AF_INET6 if kind == "tcp6" else socket.AF_INET
        host = "::1" if kind == "tcp6" else "127.0.0.1"
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.settimeout(BOUND)
            listener.bind((host, 0))
            listener.listen(1)
            left = socket.socket(family, socket.SOCK_STREAM)
            left.settimeout(BOUND)
            left.connect(listener.getsockname())
            right, _ = listener.accept()
    left.settimeout(BOUND)
    right.settimeout(BOUND)
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def start(remote, sock, *, side="listener", rom="red", deadline=None, **options):
    return remote.TimedRemoteEndpoint.from_connected_socket(
        sock,
        side=side,
        rom_version=rom,
        deadline=time.monotonic() + BOUND if deadline is None else deadline,
        **(TIMING | options),
    )


@contextmanager
def endpoints(remote, kind="socketpair", listener_rom="red", connector_rom="blue"):
    with sockets(kind) as (left, right):
        deadline = time.monotonic() + BOUND
        worker = Job(lambda: start(remote, left, rom=listener_rom, deadline=deadline))
        a = b = None
        try:
            b = start(remote, right, side="connector", rom=connector_rom, deadline=deadline)
            a = worker.result()
            yield a, b
        finally:
            if a is not None:
                a.close()
            if b is not None:
                b.close()
            left.close()
            right.close()
            worker.thread.join(BOUND + 1)
            assert not worker.thread.is_alive()


@pytest.mark.parametrize("kind", ["socketpair", "tcp", "tcp6"])
@pytest.mark.parametrize("listener_rom", ROM)
@pytest.mark.parametrize("connector_rom", ROM)
def test_real_pair_role_ordered_epoch_and_rom_clock_selection(
    remote,
    kind,
    listener_rom,
    connector_rom,
):
    with endpoints(remote, kind, listener_rom, connector_rom) as (a, b):
        assert a.peer_rom_version == connector_rom
        assert b.peer_rom_version == listener_rom
        assert a.is_internal_clock is (ROM[listener_rom] <= ROM[connector_rom])
        assert b.is_internal_clock is (not a.is_internal_clock)
        assert a.channel.revision == b.channel.revision == 3
        assert a.epoch == b.epoch == epoch(a.metadata.local_prelude, b.metadata.local_prelude)
        assert a.metadata.peer_prelude == b.metadata.local_prelude
        assert b.metadata.peer_prelude == a.metadata.local_prelude
        assert a.metadata.side == b.metadata.peer_side == "listener"
        assert b.metadata.side == a.metadata.peer_side == "connector"
        assert a.metadata.local_rom_version == listener_rom
        assert b.metadata.local_rom_version == connector_rom
        for endpoint in (a, b):
            assert len(endpoint.metadata.local_prelude) == PRELUDE.size == 28
            assert endpoint.metadata.epoch == endpoint.channel.epoch
            with pytest.raises((FrozenInstanceError, AttributeError)):
                endpoint.metadata.peer_rom_version = "yellow"
            with pytest.raises(AttributeError):
                endpoint.epoch = bytes(16)


@pytest.mark.parametrize("side", SIDE)
@pytest.mark.parametrize("fragmented", [False, True])
@pytest.mark.parametrize("kind", ["socketpair", "tcp"])
def test_exact_prelude_zero_nonce_and_coalesced_hello_not_overread(
    remote, monkeypatch, side, fragmented, kind
):
    calls = []

    def nonce(size):
        calls.append(size)
        return bytes(16)

    monkeypatch.setattr(remote.secrets, "token_bytes", nonce)
    peer_side = "connector" if side == "listener" else "listener"
    with sockets(kind) as (local, peer):
        worker = Job(lambda: start(remote, local, side=side))
        endpoint = None
        try:
            local_record = recv_exact(peer, 28)  # Both sides send before receive.
            assert local_record == record(rom_name="red", side_name=side)
            peer_record = record(side_name=peer_side)
            listener, connector = (
                (local_record, peer_record) if side == "listener" else (peer_record, local_record)
            )
            expected_epoch = epoch(listener, connector)
            payload = peer_record + hello(expected_epoch)
            if fragmented:
                for value in payload[:27]:
                    peer.sendall(bytes([value]))
                peer.sendall(payload[27:])  # Last prelude byte adjacent to HELLO.
            else:
                peer.sendall(payload)
            assert recv_exact(peer, 36) == hello(expected_epoch)
            endpoint = worker.result()
            assert endpoint.epoch == expected_epoch
            assert endpoint.metadata.peer_prelude == peer_record
            assert calls == [16]
        finally:
            if endpoint is not None:
                endpoint.close()
            local.close()
            peer.close()
            worker.thread.join(BOUND + 1)
            assert not worker.thread.is_alive()


def test_every_factory_requests_a_fresh_nonce(remote, monkeypatch):
    calls = []

    def nonce(size):
        calls.append(size)
        return bytes([len(calls)]) * size

    monkeypatch.setattr(remote.secrets, "token_bytes", nonce)
    records = []
    for _ in range(2):
        with endpoints(remote) as (a, b):
            records.extend((a.metadata.local_prelude, b.metadata.local_prelude))
    assert calls == [16] * 4
    assert {PRELUDE.unpack(value)[5] for value in records} == {bytes([n]) * 16 for n in range(1, 5)}


@pytest.mark.parametrize(
    "changes",
    [
        {"magic": b"NOPE"},
        {"prelude": 0},
        {"prelude": 2},
        {"wire": 2},
        {"wire": 4},
        {"caps": 0},
        {"caps": 2},
        {"caps": 3},
        {"caps": 0xFFFFFFFF},
        {"rom": 0},
        {"rom": 4},
        {"rom": 255},
        {"side": 0},
        {"side": 1},
        {"side": 3},
    ],
)
def test_rejected_peer_prelude_closes_owned_socket_without_fallback(remote, changes):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    with sockets() as (local, peer):
        worker = Job(lambda: start(remote, local))
        recv_exact(peer, 28)
        peer.sendall(record(**changes))
        with pytest.raises((wire.ProtocolError, ValueError)):
            worker.result()
        assert local.fileno() == -1
        assert peer.recv(1) == b""  # No fallback HELLO or legacy negotiation.


@pytest.mark.parametrize("cut", [0, 1, 4, 8, 24, 27])
def test_partial_prelude_eof_closes_socket(remote, cut):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    with sockets() as (local, peer):
        worker = Job(lambda: start(remote, local))
        recv_exact(peer, 28)
        peer.sendall(record()[:cut])
        peer.shutdown(socket.SHUT_WR)
        with pytest.raises((wire.ChannelClosed, wire.ProtocolError, EOFError, ConnectionError)):
            worker.result()
        assert local.fileno() == -1


@pytest.mark.parametrize("phase", ["prelude", "hello"])
@pytest.mark.parametrize("action", ["timeout", "cancel"])
@pytest.mark.parametrize("kind", ["socketpair", "tcp"])
def test_prelude_and_hello_waits_share_deadline_and_cancel_cleanup(remote, phase, action, kind):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    cancel = threading.Event()
    with sockets(kind) as (local, peer):
        deadline = time.monotonic() + (0.3 if action == "timeout" else BOUND)
        worker = Job(lambda: start(remote, local, deadline=deadline, cancel_event=cancel))
        recv_exact(peer, 28)
        if phase == "hello":
            peer.sendall(record())
            recv_exact(peer, 36)
        else:
            peer.sendall(record()[:27])
        if action == "cancel":
            cancel.set()
        expected = wire.Cancelled if action == "cancel" else wire.DeadlineExceeded
        with pytest.raises(expected):
            worker.result()
        assert local.fileno() == -1
        if action == "timeout":
            assert time.monotonic() < deadline + 0.5


@pytest.mark.parametrize("action", ["expired", "cancelled"])
def test_factory_pre_admission_failure_sends_nothing_and_closes(remote, action):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    cancel = threading.Event()
    if action == "cancelled":
        cancel.set()
    with sockets() as (local, peer):
        deadline = time.monotonic() - 1 if action == "expired" else time.monotonic() + BOUND
        with pytest.raises(wire.DeadlineExceeded if action == "expired" else wire.Cancelled):
            start(remote, local, deadline=deadline, cancel_event=cancel)
        assert local.fileno() == -1
        assert peer.recv(1) == b""


@pytest.mark.parametrize("method", ["connect", "listen"])
@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.0.2.1", "::ffff:192.0.2.1", "localhost", "::1%lo"]
)
def test_factory_rejects_non_loopback_before_socket_creation(remote, monkeypatch, method, host):
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe address reached socket creation")

    monkeypatch.setattr(remote.socket, "socket", forbidden)
    factory = getattr(remote.TimedRemoteEndpoint, method)
    kwargs = dict(rom_version="red", deadline=time.monotonic() + BOUND, **TIMING)
    with pytest.raises(ValueError):
        if method == "connect":
            factory(host, 12345, **kwargs)
        else:
            factory(12345, host=host, **kwargs)


def test_real_channel_handshake_is_already_complete_and_repeat_is_idempotent(remote):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    with endpoints(remote) as (a, b):
        deadline = time.monotonic() + BOUND
        a.channel.handshake(deadline=deadline)
        b.channel.handshake(deadline=deadline)
        for marker in (0, 255, 98, 98):
            a.channel.send(wire.Sync(marker), deadline=deadline)
            assert b.channel.receive(deadline=deadline) == wire.Sync(marker)
        assert a.channel.poll() is None
        assert b.channel.poll() is None


def test_remote_close_is_idempotent_closes_channel_and_wakes_peer(remote):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    with endpoints(remote) as (a, b):
        worker = Job(lambda: b.channel.receive(deadline=time.monotonic() + BOUND))
        a.close()
        a.close()
        assert a.channel.closed
        with pytest.raises(wire.ChannelClosed):
            worker.result()


@pytest.mark.parametrize("endpoint", ["local", "peer"])
@pytest.mark.parametrize("address", ["192.0.2.1", "::ffff:192.0.2.1", "::"])
def test_connected_socket_checks_both_addresses_before_sending(remote, endpoint, address):
    class AddressView:
        """Address-only security double around an actual connected local stream."""

        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        proto = socket.IPPROTO_TCP

        def __init__(self, sock):
            self.sock = sock

        def getsockname(self):
            return (address if endpoint == "local" else "127.0.0.1", 1234)

        def getpeername(self):
            return (address if endpoint == "peer" else "127.0.0.1", 5678)

        def __getattr__(self, name):
            return getattr(self.sock, name)

    with sockets() as (local, peer):
        with pytest.raises(ValueError):
            start(remote, AddressView(local))
        assert local.fileno() == -1
        assert peer.recv(1) == b""


def test_datagram_local_socket_rejected_and_closed(remote):
    local, peer = socket.socketpair(type=socket.SOCK_DGRAM)
    try:
        with pytest.raises(ValueError):
            start(remote, local)
        assert local.fileno() == -1
    finally:
        local.close()
        peer.close()


@pytest.mark.parametrize(
    "options",
    [
        {"side": "other"},
        {"side": True},
        {"rom": "green"},
        {"rom": 1},
        {"deadline": True},
        {"deadline": float("nan")},
        {"deadline": float("inf")},
    ],
)
def test_local_validation_takes_ownership_and_emits_nothing(remote, options):
    with sockets() as (local, peer):
        with pytest.raises((TypeError, ValueError)):
            start(remote, local, **options)
        assert local.fileno() == -1
        assert peer.recv(1) == b""


def test_connect_listen_share_exact_factory_deadline_through_hello(remote, monkeypatch):
    addresses = queue.Queue()
    created = []
    original_socket = socket.socket
    original_ready = remote._ready
    original_handshake = remote.TimedWireChannel.handshake
    readiness_deadlines = []
    handshake_deadlines = []
    with original_socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    class ObservedSocket(original_socket):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def listen(self, backlog):
            result = super().listen(backlog)
            addresses.put(self.getsockname())
            return result

    def ready(sock, **kwargs):
        readiness_deadlines.append(kwargs["deadline"])
        return original_ready(sock, **kwargs)

    def handshake(channel, **kwargs):
        handshake_deadlines.append(kwargs["deadline"])
        return original_handshake(channel, **kwargs)

    monkeypatch.setattr(remote.socket, "socket", ObservedSocket)
    monkeypatch.setattr(remote, "_ready", ready)
    monkeypatch.setattr(remote.TimedWireChannel, "handshake", handshake)
    deadline = time.monotonic() + BOUND
    worker = Job(
        lambda: remote.TimedRemoteEndpoint.listen(
            port,
            rom_version="yellow",
            deadline=deadline,
            **TIMING,
        )
    )
    a = b = None
    try:
        host, port = addresses.get(timeout=BOUND)
        b = remote.TimedRemoteEndpoint.connect(
            host,
            port,
            rom_version="red",
            deadline=deadline,
            **TIMING,
        )
        a = worker.result()
        assert a.is_internal_clock is False  # Transport listener is lower-ranked ROM.
        assert b.is_internal_clock is True
        assert a.epoch == b.epoch
        assert readiness_deadlines and set(readiness_deadlines) == {deadline}
        assert handshake_deadlines == [deadline, deadline]
        assert created[0].fileno() == -1  # Listener is closed before returning facade.
    finally:
        for endpoint in (a, b):
            if endpoint is not None:
                endpoint.close()
        for sock in created:
            sock.close()
        worker.thread.join(BOUND + 1)
        assert not worker.thread.is_alive()
    assert all(sock.fileno() == -1 for sock in created)


@pytest.mark.parametrize("action", ["timeout", "cancel"])
def test_accept_wait_is_bounded_and_closes_listener(remote, monkeypatch, action):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    listening = threading.Event()
    created = []
    original_socket = socket.socket
    with original_socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    class ObservedSocket(original_socket):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def listen(self, backlog):
            super().listen(backlog)
            listening.set()

    monkeypatch.setattr(remote.socket, "socket", ObservedSocket)
    cancel = threading.Event()
    deadline = time.monotonic() + (0.2 if action == "timeout" else BOUND)
    worker = Job(
        lambda: remote.TimedRemoteEndpoint.listen(
            port,
            rom_version="red",
            deadline=deadline,
            cancel_event=cancel,
            **TIMING,
        )
    )
    try:
        assert listening.wait(BOUND)
        if action == "cancel":
            cancel.set()
        with pytest.raises(wire.Cancelled if action == "cancel" else wire.DeadlineExceeded):
            worker.result()
        assert created and all(sock.fileno() == -1 for sock in created)
    finally:
        cancel.set()
        for sock in created:
            sock.close()
        worker.thread.join(BOUND + 1)
        assert not worker.thread.is_alive()


class SessionSpy:
    """Delegation-only double: no CPU, serial, IRQ, or settlement claims."""

    def __init__(self, channel, **options):
        self.channel = channel
        self.options = options
        self.calls = []
        self.states = repeat({"quiescent": True})
        self.acks = iter([True])
        self.markers = iter([False, True, False])
        self.attach_error = None

    def attach(self, pyboy, **kwargs):
        self.calls.append(("attach", pyboy, kwargs))
        if self.attach_error is not None:
            raise self.attach_error

    def announce_sync(self, marker, **kwargs):
        self.calls.append(("announce", marker, kwargs))

    def poll_peer_sync(self, marker):
        self.calls.append(("poll", marker))
        return next(self.markers)

    def start_fence(self, **kwargs):
        self.calls.append(("fence", kwargs))
        return 1

    def service_controls(self, **kwargs):
        self.calls.append(("service", kwargs))
        return {"delegation_double": True}

    def controls_snapshot(self):
        self.calls.append(("controls_snapshot",))
        return next(self.states)

    def poll_fence(self, fence):
        self.calls.append(("ack", fence))
        return next(self.acks)

    def snapshot(self):
        return {"delegation_double": True}

    def tick(self, count, **kwargs):
        self.calls.append(("tick", count, kwargs))
        return "tick-result"

    def cancel(self):
        self.calls.append(("cancel",))
        self.channel.close()

    def close(self):
        self.calls.append(("close",))
        self.channel.close()


def test_facade_delegates_attach_metadata_deadline_controls_snapshot_and_tick(remote, monkeypatch):
    monkeypatch.setattr(remote, "TimedLinkSession", SessionSpy)
    with endpoints(remote, listener_rom="yellow", connector_rom="red") as (a, b):
        assert a.session.calls == b.session.calls == []  # Factories return unattached.
        deadline = time.monotonic() + BOUND
        game = object()
        a.attach(game, deadline=deadline)
        assert a.session.calls == [
            (
                "attach",
                game,
                {
                    "deadline": deadline,
                    "startup_internal_clock": False,
                },
            )
        ]
        assert all(a.session.options[key] == value for key, value in TIMING.items())
        a.announce_sync(98, deadline=deadline)
        assert a.session.calls[-1] == ("announce", 98, {"deadline": deadline})
        assert [a.poll_peer_sync(98, deadline=deadline) for _ in range(3)] == [False, True, False]
        assert (
            a.session.calls[-6:]
            == [
                ("service", {"deadline": deadline}),
                ("poll", 98),
            ]
            * 3
        )
        assert a.snapshot() == {"delegation_double": True}
        assert a.tick(2, render=False, sound=False) == "tick-result"
        assert a.session.calls[-1] == ("tick", 2, {"render": False, "sound": False})
        with pytest.raises(RuntimeError, match="attaching thread"):
            Job(a.snapshot).result()


@pytest.mark.parametrize(
    "states,acks",
    [
        ([False, True], [True]),  # ACK is consumed once; local delivery still pending.
        ([True, True], [False, True]),  # Local idle is insufficient without ACK.
        ([False, False, True], [False, True]),
    ],
)
def test_facade_fence_requires_ack_and_local_quiescence_without_cpu_reentry(
    remote,
    monkeypatch,
    states,
    acks,
):
    monkeypatch.setattr(remote, "TimedLinkSession", SessionSpy)
    with endpoints(remote) as (a, _):
        a.session.states = iter({"quiescent": value} for value in states)
        a.session.acks = iter(acks)
        deadline = time.monotonic() + BOUND
        assert a.wait_for_wire_idle(deadline=deadline) == {"quiescent": True}
        calls = a.session.calls
        assert calls[0] == ("fence", {"deadline": deadline})
        assert sum(call[0] == "fence" for call in calls) == 1
        assert sum(call[0] == "service" for call in calls) == len(states)
        assert sum(call[0] == "ack" for call in calls) == len(acks)
        assert sum(call[0] == "controls_snapshot" for call in calls) == len(states)
        assert calls[-1] == ("controls_snapshot",)
        assert all(call[1] == {"deadline": deadline} for call in calls if call[0] == "service")
        assert not any(call[0] in ("tick", "attach") for call in calls)


def test_facade_attach_failure_preserves_exception_and_closes_channel(remote, monkeypatch):
    monkeypatch.setattr(remote, "TimedLinkSession", SessionSpy)
    with endpoints(remote) as (a, _):
        error = RuntimeError("owner bootstrap failed after mutation")
        a.session.attach_error = error
        with pytest.raises(RuntimeError) as caught:
            a.attach(object(), deadline=time.monotonic() + BOUND)
        assert caught.value is error
        assert a.session.calls[-1] == ("close",)
        assert a.channel.closed


def test_facade_cancel_delegates_and_prevents_new_deadline_operations(remote, monkeypatch):
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    monkeypatch.setattr(remote, "TimedLinkSession", SessionSpy)
    with endpoints(remote) as (a, _):
        a.cancel()
        assert a.session.calls == [("cancel",)]
        for call in (
            lambda: a.attach(object(), deadline=time.monotonic() + BOUND),
            lambda: a.announce_sync(1, deadline=time.monotonic() + BOUND),
            lambda: a.wait_for_wire_idle(deadline=time.monotonic() + BOUND),
            lambda: a.tick(),
        ):
            with pytest.raises(wire.Cancelled):
                call()
        assert not any(
            call[0] in ("attach", "announce", "fence", "tick") for call in a.session.calls
        )


def test_facade_default_poll_is_bounded_and_services_before_consuming(remote, monkeypatch):
    monkeypatch.setattr(remote, "TimedLinkSession", SessionSpy)
    with endpoints(remote) as (a, _):
        before = time.monotonic()
        assert a.poll_peer_sync(9) is False
        after = time.monotonic()
        service, poll = a.session.calls
        assert service[0] == "service"
        assert before + 1.0 <= service[1]["deadline"] <= after + 1.0
        assert poll == ("poll", 9)


@pytest.fixture
def authored_game(remote, monkeypatch):
    # Reuse the authored cartridge helper without binding other tests to our
    # isolated wire module or requiring installation of this uncommitted tree.
    spec = importlib.util.spec_from_file_location(
        "_remote_authored_helper",
        Path(__file__).with_name("test_timed_link_session.py"),
    )
    helper = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as context:
        context.setitem(
            sys.modules,
            "pokered_harness.link.timed_wire",
            importlib.import_module(remote.__package__ + ".timed_wire"),
        )
        spec.loader.exec_module(helper)
    return helper.authored_game


@pytest.mark.parametrize("kind", ["socketpair", "tcp"])
def test_authored_runtime_two_owner_factory_attach_passive_sync_and_bilateral_fence(
    remote,
    tmp_path,
    record_property,
    authored_game,
    kind,
):
    """Real owners bootstrap and fence without passive CPU execution."""
    from pyboy import pyboy as public
    from pyboy.core import cpu, mb, serial

    paths = [str(module.__file__) for module in (public, cpu, mb, serial)]
    native = [
        any(path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)
        for path in paths
    ]
    assert all(native) or all(path.endswith(".py") for path in paths), paths
    record_property("runtime", "native" if all(native) else "source")
    record_property("runtime_modules", repr(paths))
    ready = threading.Barrier(2, timeout=BOUND)
    finished = [threading.Event(), threading.Event()]
    deadline = time.monotonic() + BOUND

    def owner(sock, index):
        side = "listener" if index == 0 else "connector"
        rom = "yellow" if index == 0 else "red"
        endpoint = None
        with authored_game(tmp_path, name=f"remote-owner-{index}") as game:
            try:
                before_attach = (
                    game.mb.cpu.cycles,
                    game.mb.cpu.retired_instructions,
                    game.register_file.PC,
                )
                endpoint = start(remote, sock, side=side, rom=rom, deadline=deadline)
                endpoint.attach(game, deadline=deadline)
                assert (
                    game.mb.cpu.cycles,
                    game.mb.cpu.retired_instructions,
                    game.register_file.PC,
                ) == before_attach
                assert game.mb.serial.SB == (1 if endpoint.is_internal_clock else 2)
                assert game.mb.serial.SC & 0x81 == (0x81 if endpoint.is_internal_clock else 0x80)
                baseline = (
                    game.mb.cpu.cycles,
                    game.mb.cpu.retired_instructions,
                    game.register_file.PC,
                    game.mb.serial.SB,
                    game.mb.serial.SC,
                )
                ready.wait()
                endpoint.announce_sync(98, deadline=deadline)
                while not endpoint.poll_peer_sync(98, deadline=deadline):
                    assert time.monotonic() < deadline
                    threading.Event().wait(0.001)
                assert endpoint.poll_peer_sync(98, deadline=deadline) is False
                state = endpoint.wait_for_wire_idle(deadline=deadline)
                assert state["quiescent"] is True
                finished[index].set()
                while not finished[1 - index].is_set():
                    endpoint.service_controls(deadline=deadline)
                    assert time.monotonic() < deadline
                    threading.Event().wait(0.001)
                assert (
                    game.mb.cpu.cycles,
                    game.mb.cpu.retired_instructions,
                    game.register_file.PC,
                    game.mb.serial.SB,
                    game.mb.serial.SC,
                ) == baseline
                assert endpoint.snapshot() is not None
                return endpoint.epoch
            finally:
                if endpoint is not None:
                    endpoint.close()

    with sockets(kind) as (left, right):
        workers = [Job(lambda: owner(left, 0)), Job(lambda: owner(right, 1))]
        try:
            assert workers[0].result() == workers[1].result()
        finally:
            left.close()
            right.close()
            for worker in workers:
                worker.thread.join(BOUND + 1)
                assert not worker.thread.is_alive()


def test_facade_fence_preserves_primary_failure_when_cancel_cleanup_raises(remote, monkeypatch):
    monkeypatch.setattr(remote, "TimedLinkSession", SessionSpy)
    with endpoints(remote) as (a, _):
        primary = RuntimeError("primary fence service failure")

        def service(**kwargs):
            raise primary

        def cancel():
            raise RuntimeError("secondary cancel cleanup failure")

        monkeypatch.setattr(a.session, "service_controls", service)
        monkeypatch.setattr(a.session, "cancel", cancel)
        with pytest.raises(RuntimeError) as caught:
            a.wait_for_wire_idle(deadline=time.monotonic() + BOUND)
        assert caught.value is primary


@pytest.mark.parametrize("method", ["listen", "connect"])
@pytest.mark.parametrize("port", [0, -1, 65536, True, "1234"])
def test_invalid_ports_rejected_before_socket_creation(remote, monkeypatch, method, port):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid port reached socket creation")

    monkeypatch.setattr(remote.socket, "socket", forbidden)
    options = dict(rom_version="red", deadline=time.monotonic() + BOUND, **TIMING)
    with pytest.raises(ValueError):
        if method == "connect":
            remote.TimedRemoteEndpoint.connect("127.0.0.1", port, **options)
        else:
            remote.TimedRemoteEndpoint.listen(port, **options)


@pytest.mark.parametrize("abstract", [False, True])
@pytest.mark.parametrize("accepted_side", [False, True])
def test_named_unix_stream_rejected_before_prelude(remote, tmp_path, abstract, accepted_side):
    address = str(tmp_path / "remote.sock")
    if abstract:
        address = "\0pkth-" + hashlib.sha256(address.encode()).hexdigest()[:24]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(address)
        listener.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(address)
            accepted, _ = listener.accept()
            with accepted:
                local, peer = (accepted, client) if accepted_side else (client, accepted)
                peer.settimeout(BOUND)
                with pytest.raises(ValueError):
                    start(remote, local)
                assert local.fileno() == -1
                assert peer.recv(1) == b""


@pytest.mark.parametrize("operation", ["tick", "fence"])
def test_external_cancel_survives_factory_into_real_endpoint_wait(
    remote,
    tmp_path,
    authored_game,
    monkeypatch,
    operation,
):
    """Factory event reaches a real TCP session after negotiation and attachment."""
    wire = importlib.import_module(remote.__package__ + ".timed_wire")
    external = threading.Event()
    entered = threading.Event()
    with sockets("tcp") as (left, right):
        peer_job = Job(lambda: start(remote, right, side="connector"))
        endpoint = start(remote, left, cancel_event=external, quantum_cycles=24)
        peer = peer_job.result()
        try:
            with authored_game(tmp_path, name="remote-cancel") as game:
                endpoint.attach(game, deadline=time.monotonic() + BOUND)
                if operation == "tick":
                    receive = endpoint.channel.receive

                    def observe_receive(**options):
                        assert not external.is_set()
                        entered.set()
                        return receive(**options)

                    monkeypatch.setattr(endpoint.channel, "receive", observe_receive)
                else:
                    poll = endpoint.session.poll_fence

                    def observe_fence(token):
                        result = poll(token)
                        assert result is False  # Peer has no owner servicing the prefix.
                        entered.set()
                        return result

                    monkeypatch.setattr(endpoint.session, "poll_fence", observe_fence)

                def cancel_wait():
                    assert entered.wait(BOUND)
                    external.set()

                canceller = Job(cancel_wait)
                try:
                    with pytest.raises(wire.Cancelled):
                        if operation == "tick":
                            endpoint.tick(render=False, sound=False)
                        else:
                            endpoint.wait_for_wire_idle(deadline=time.monotonic() + BOUND)
                    canceller.result()
                    assert endpoint.channel.closed
                    if operation == "fence":
                        assert game.mb.cpu.retired_instructions == 0
                finally:
                    external.set()
                    entered.set()
                    endpoint.close()
                    canceller.thread.join(BOUND + 1)
        finally:
            endpoint.close()
            peer.close()


@pytest.mark.parametrize("duck", [False, True])
@pytest.mark.parametrize("method", ["connected", "listen", "connect"])
def test_invalid_cancellation_event_rejected_at_admission(remote, monkeypatch, duck, method):
    class DuckEvent:
        def is_set(self):
            return False

    invalid = DuckEvent() if duck else object()
    with sockets() as (local, peer):

        def forbidden(*args, **kwargs):
            pytest.fail("invalid cancellation event reached socket creation")

        monkeypatch.setattr(remote.socket, "socket", forbidden)
        options = dict(
            rom_version="red", deadline=time.monotonic() + BOUND, cancel_event=invalid, **TIMING
        )
        with pytest.raises(TypeError):
            if method == "connected":
                start(remote, local, cancel_event=invalid)
            elif method == "connect":
                remote.TimedRemoteEndpoint.connect("127.0.0.1", 12345, **options)
            else:
                remote.TimedRemoteEndpoint.listen(12345, **options)
        if method == "connected":
            assert local.fileno() == -1
            assert peer.recv(1) == b""


def test_factory_forwards_original_cancel_event_to_session(remote, monkeypatch):
    monkeypatch.setattr(remote, "TimedLinkSession", SessionSpy)
    external = threading.Event()
    with sockets() as (left, right):
        worker = Job(lambda: start(remote, right, side="connector"))
        endpoint = start(remote, left, cancel_event=external)
        peer = worker.result()
        try:
            assert endpoint.session.options["cancel_event"] is external
        finally:
            endpoint.close()
            peer.close()


# The real-ROM TCP peer driver deliberately has no import-time PyBoy
# dependency. Parse it rather than booting a ROM so a newly added phase helper
# cannot quietly start a fresh fixed-duration timeout after the process-wide
# deadline is nearly spent.
_TCP_TRADE_PEER = Path(__file__).with_name("_tcp_trade_peer.py")
_TCP_PHASE_CALLS = {
    "cooperative_sync",
    "passive_sync",
    "peer_shutdown_sync",
    "_finish_link_menu_phase",
    "wait_for_link_menu_selection_exchange",
    "wait_for_menu_ready",
    "move_menu_to_item",
    "wait_for_wire_idle",
    "_hold_at_sync_boundary",
}
_TCP_TIMEOUT_HELPERS = {
    "cooperative_sync",
    "passive_sync",
    "peer_shutdown_sync",
    "wait_for_link_menu_selection_exchange",
    "wait_for_menu_ready",
    "move_menu_to_item",
}


def _tcp_trade_peer_driver() -> ast.FunctionDef:
    tree = ast.parse(_TCP_TRADE_PEER.read_text(encoding="utf-8"), filename=str(_TCP_TRADE_PEER))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_peer"
    )


def _tcp_call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _tcp_uses_global_deadline(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id in {"deadline", "remaining", "bounded_timeout"}
        for child in ast.walk(node)
    )


def _tcp_timeout_keyword(call: ast.Call) -> ast.keyword | None:
    return next((keyword for keyword in call.keywords if keyword.arg == "timeout"), None)


def _tcp_direct_phase_calls(driver: ast.FunctionDef) -> list[ast.Call]:
    """Return phase calls in the drive body, excluding nested helper bodies."""

    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is driver:
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if _tcp_call_name(node.func) in _TCP_PHASE_CALLS:
                calls.append(node)
            self.generic_visit(node)

    Visitor().visit(driver)
    return calls


def test_tcp_trade_peer_phase_timeouts_are_globally_bounded():
    """Every wait-capable phase call derives its timeout from ``deadline``."""
    violations = []
    for call in _tcp_direct_phase_calls(_tcp_trade_peer_driver()):
        timeout = _tcp_timeout_keyword(call)
        if timeout is None:
            violations.append(f"line {call.lineno}: {_tcp_call_name(call.func)} has no timeout")
        elif not _tcp_uses_global_deadline(timeout.value):
            violations.append(f"line {call.lineno}: {_tcp_call_name(call.func)}")

    assert not violations, "unbounded TCP phase timeouts: " + ", ".join(violations)


def test_tcp_trade_peer_timeout_helpers_clamp_inner_deadlines():
    """Timeout-accepting phase helpers cannot create independent deadlines."""
    helpers = {
        node.name: node
        for node in ast.walk(_tcp_trade_peer_driver())
        if isinstance(node, ast.FunctionDef) and node.name in _TCP_TIMEOUT_HELPERS
    }
    assert helpers.keys() == _TCP_TIMEOUT_HELPERS

    violations = []
    for name, helper in helpers.items():
        if not _tcp_uses_global_deadline(helper):
            violations.append(name)
            continue
        deadline_assignments = [
            node
            for node in ast.walk(helper)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and "deadline" in target.id
                for target in node.targets
            )
        ]
        if any(
            not _tcp_uses_global_deadline(assignment.value)
            for assignment in deadline_assignments
        ):
            violations.append(name)

    assert not violations, "helpers create an independent timeout: " + ", ".join(violations)
