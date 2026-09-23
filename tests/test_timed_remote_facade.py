"""Timed-wire remote facade doubles and authored-owner integration (#162).

Split from ``tests/test_timed_remote.py`` for #162 with no behavior change.
Shared constants, the ``remote`` fixture, and every helper live in
``tests._timed_remote_support``; the ``SessionSpy`` double, the facade tests,
and the authored-owner integration tests below are copied verbatim.
"""

import hashlib
import importlib
import importlib.machinery
import importlib.util
import socket
import sys
import threading
import time
from itertools import repeat
from pathlib import Path

import pytest

from tests._timed_remote_support import (
    BOUND,
    TIMING,
    Job,
    endpoints,
    sockets,
    start,
)


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
    completed = threading.Barrier(2)
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
                # A peer's finished flag can arrive during our last control call.
                # Both owners must leave those calls before either endpoint closes.
                completed.wait(timeout=max(0, deadline - time.monotonic()))
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
@pytest.mark.parametrize("long_parent_path", [False, True])
def test_named_unix_stream_rejected_before_prelude(
    remote, tmp_path, monkeypatch, abstract, accepted_side, long_parent_path
):
    if long_parent_path:
        tmp_path = tmp_path / ("nested-" + "x" * 110)
        tmp_path.mkdir()
    # AF_UNIX limits the encoded socket address, not its resolved directory.
    # Keep the real named-socket case valid even when pytest's temporary root
    # exceeds that limit. Both endpoints use the same scoped working directory;
    # monkeypatch restores it even when the rejection assertion fails.
    monkeypatch.chdir(tmp_path)
    address = "remote.sock"
    if abstract:
        address = "\0pkth-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:24]
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
