"""Authored controls: a job-scoped port reservation held until the listener binds (#107)."""

import errno
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from pokered_harness.link import network_listener_handoff as handoff
from scripts.production_gate_parallel_ports import HANDOFF_ENV, HandoffRegistry, ListenerBroker
from tests import _parallel_runtime_port_support as ports

CHILD = "from tests._parallel_runtime_port_support import reserve_port\nprint(reserve_port())\n"


@pytest.fixture
def job_range(tmp_path, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        base = probe.getsockname()[1] + 1
    monkeypatch.setenv(ports.PORT_BASE_ENV, str(base))
    monkeypatch.setenv(ports.PORT_COUNT_ENV, "8")
    monkeypatch.setenv(ports.PORT_LOCK_ENV, str(tmp_path / "locks"))
    monkeypatch.setattr(ports, "_HELD", {})
    return base


def test_outside_a_parallel_job_nothing_is_reserved(monkeypatch):
    for name in (ports.PORT_BASE_ENV, ports.PORT_COUNT_ENV, ports.PORT_LOCK_ENV):
        monkeypatch.delenv(name, raising=False)
    assert ports.reserve_port() is None


def test_ports_come_from_the_job_range_and_are_never_handed_out_twice(job_range):
    first, second = ports.reserve_port(), ports.reserve_port()
    assert first != second
    assert all(job_range <= port < job_range + 8 for port in (first, second))


def test_the_reservation_is_held_across_processes_until_the_holder_ends(job_range, tmp_path):
    held = {ports.reserve_port(), ports.reserve_port()}
    env = {**os.environ}
    other = int(
        subprocess.run(
            [sys.executable, "-c", CHILD], env=env, capture_output=True, text=True, check=True
        ).stdout
    )
    assert other not in held and job_range <= other < job_range + 8


def test_a_port_with_a_live_listener_is_skipped(job_range):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", job_range))
        except OSError:
            pytest.skip("range start is not bindable on this host")
        listener.listen(1)
        assert ports.reserve_port() != job_range


def test_release_makes_the_lowest_port_available_again(job_range):
    first = ports.reserve_port()
    ports.release_port(first)
    assert ports.reserve_port() == first


def test_exhausting_the_range_fails_closed(job_range, monkeypatch):
    monkeypatch.setenv(ports.PORT_COUNT_ENV, "1")
    ports.reserve_port()
    with pytest.raises(RuntimeError, match="no loopback port"):
        ports.reserve_port()


@pytest.fixture
def owner(monkeypatch):
    broker = ListenerBroker().start()
    monkeypatch.setenv(HANDOFF_ENV, broker.name)
    yield broker
    broker.close()


def _wait_state(broker, port, wanted, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if broker.state(port) == wanted:
            return True
        time.sleep(0.02)
    return False


def _rival_bind(port, *, reuse_port):
    rival = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        rival.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if reuse_port:
            rival.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        rival.bind(("127.0.0.1", port))
    except OSError as exc:
        return exc.errno
    finally:
        rival.close()
    return None


def test_a_competing_binder_cannot_take_a_leased_port_before_or_after_the_listener_adopts_it(owner):
    port = handoff.lease_port()
    assert _rival_bind(port, reuse_port=False) == errno.EADDRINUSE
    assert _rival_bind(port, reuse_port=True) == errno.EADDRINUSE
    adopted = handoff.take_listener("127.0.0.1", port)
    assert adopted is not None and adopted.getsockname() == ("127.0.0.1", port)
    try:
        assert _wait_state(owner, port, "acknowledged"), "the owner never saw the acknowledgment"
        assert _rival_bind(port, reuse_port=False) == errno.EADDRINUSE
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        server, _ = adopted.accept()
        client.sendall(b"x")
        assert server.recv(1) == b"x"
        client.close()
        server.close()
    finally:
        adopted.close()


def test_a_connector_that_arrives_before_the_listener_adopts_is_queued_not_refused(owner):
    port = handoff.lease_port()
    early = socket.create_connection(("127.0.0.1", port), timeout=5)
    adopted = handoff.take_listener("127.0.0.1", port)
    try:
        server, _ = adopted.accept()
        early.sendall(b"y")
        assert server.recv(1) == b"y"
        server.close()
    finally:
        early.close()
        adopted.close()


def test_a_port_the_owner_never_leased_is_left_to_the_normal_bind(owner):
    assert handoff.take_listener("127.0.0.1", 1) is None


def test_without_an_owner_nothing_changes(monkeypatch):
    for name in (HANDOFF_ENV, ports.PORT_BASE_ENV, ports.PORT_COUNT_ENV, ports.PORT_LOCK_ENV):
        monkeypatch.delenv(name, raising=False)
    assert handoff.take_listener("127.0.0.1", 40000) is None
    assert handoff.lease_port() is None
    assert ports.leased_port() is None
    assert ports.handoff_environment() == {}


def test_a_parallel_job_without_an_owner_refuses_a_bare_port_number(job_range, monkeypatch):
    monkeypatch.delenv(HANDOFF_ENV, raising=False)
    with pytest.raises(ports.PortLeaseError):
        ports.leased_port()


def test_a_parallel_job_with_an_owner_gets_a_held_listening_socket(owner, job_range):
    port = ports.leased_port()
    assert isinstance(port, int) and _rival_bind(port, reuse_port=False) == errno.EADDRINUSE
    assert ports.handoff_environment() == {HANDOFF_ENV: owner.name}


def test_a_negative_acknowledgment_keeps_the_socket_with_the_owner(owner):
    port = handoff.lease_port()
    with handoff._connect(owner.name) as conn:
        conn.send(json.dumps({"op": "take", "host": "127.0.0.1", "port": port}).encode())
        _data, descriptors, _flags, _address = socket.recv_fds(conn, 4096, 1)
        assert len(descriptors) == 1
        os.close(descriptors[0])
        conn.send(b'{"op":"nack"}')
    assert _wait_state(owner, port, "leased")
    assert _rival_bind(port, reuse_port=False) == errno.EADDRINUSE
    assert handoff.take_listener("127.0.0.1", port) is not None


def test_a_handed_over_socket_is_refused_unless_it_is_a_listening_loopback_socket_on_the_port():
    with socket.socket() as plain:
        plain.bind(("127.0.0.1", 0))
        with pytest.raises(handoff.HandoffError, match="not listening"):
            handoff.validate_listener(plain, "127.0.0.1", plain.getsockname()[1])
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        port = listening.getsockname()[1]
        handoff.validate_listener(listening, "127.0.0.1", port)
        with pytest.raises(handoff.HandoffError, match="different port"):
            handoff.validate_listener(listening, "127.0.0.1", port + 1)
        with pytest.raises(handoff.HandoffError, match="loopback host"):
            handoff.validate_listener(listening, "::1", port)
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram,
        pytest.raises(handoff.HandoffError, match="TCP stream"),
    ):
        handoff.validate_listener(datagram, "127.0.0.1", 1)


def test_the_registry_ledger_reports_unconsumed_and_unacknowledged_leases(monkeypatch):
    registry = HandoffRegistry()
    name = registry.open("job", 0, 0)
    monkeypatch.setenv(HANDOFF_ENV, name)
    untouched, taken = handoff.lease_port(), handoff.lease_port()
    listener = handoff.take_listener("127.0.0.1", taken)
    try:
        ledger = registry.close("job")
    finally:
        listener.close()
    assert untouched in ledger["unconsumed"] and taken not in ledger["unconsumed"]
    assert registry.close("job") is None


def test_the_broker_leases_only_inside_its_range():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        base = probe.getsockname()[1] + 1
    broker = ListenerBroker(base, 2).start()
    try:
        assert {broker.lease(), broker.lease()} == {base, base + 1}
        with pytest.raises(OSError, match="no loopback port"):
            broker.lease()
    finally:
        broker.close()
