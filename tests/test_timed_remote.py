"""PKTH v1 / timed wire v3 remote contracts: transport and prelude half (#162).

Split from ``tests/test_timed_remote.py`` for #162 with no behavior change.
Shared constants, the ``remote`` fixture, and every helper live in
``tests._timed_remote_support``; the test functions and their parametrization
below are copied verbatim.
"""

import importlib
import queue
import socket
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from tests._timed_remote_support import (
    BOUND,
    PRELUDE,
    ROM,
    SIDE,
    TIMING,
    Job,
    endpoints,
    epoch,
    hello,
    record,
    recv_exact,
    sockets,
    start,
)


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
