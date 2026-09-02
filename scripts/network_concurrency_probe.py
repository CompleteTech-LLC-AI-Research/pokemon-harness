#!/usr/bin/env python3
"""Bounded localhost-only acceptance probe for the network concurrency lane.

The probe uses real TCP sockets for the public transport APIs and an AF_UNIX
socket pair only for the synthetic raw-constructor security check. It does not
need ROMs, never binds a non-loopback address, and exits nonzero on any failed
assertion. Every worker, join, and network wait has a finite deadline.
"""

from __future__ import annotations

import select
import socket
import struct
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager

from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_link import (
    OP_BYE,
    SerialLinkClosed,
    SerialLinkTimeout,
    TcpSerialLink,
)

LOOPBACK = "127.0.0.1"


class ProbeFailure(RuntimeError):
    """A failed acceptance assertion."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def wait_until(predicate, timeout_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


@contextmanager
def tcp_pair() -> Iterator[tuple[TcpSerialLink, TcpSerialLink]]:
    port = free_port()
    ready = threading.Event()
    holder: dict[str, TcpSerialLink] = {}
    listener_errors: list[BaseException] = []

    def listen() -> None:
        try:
            holder["server"] = TcpSerialLink.listen(
                port,
                "blue",
                host=LOOPBACK,
                accept_timeout_s=3.0,
                ready_event=ready,
            )
        except BaseException as exc:  # noqa: BLE001 - report setup failure
            listener_errors.append(exc)

    listener_thread = threading.Thread(target=listen, name="probe-listener", daemon=True)
    listener_thread.start()
    client: TcpSerialLink | None = None
    try:
        require(ready.wait(timeout=1.0), "loopback listener did not become ready")
        client = TcpSerialLink.connect(
            LOOPBACK,
            port,
            "yellow",
            timeout_s=2.0,
        )
        listener_thread.join(timeout=2.0)
        require(not listener_thread.is_alive(), "listener thread did not finish")
        require(not listener_errors, f"listener setup failed: {listener_errors}")
        require("server" in holder, "listener returned no server link")
        yield holder["server"], client
    finally:
        if client is not None:
            client.close()
        server = holder.get("server")
        if server is not None:
            server.close()
        listener_thread.join(timeout=2.0)
        require(not listener_thread.is_alive(), "listener thread leaked")


def read_frame(sock: socket.socket) -> bytes:
    header = bytearray()
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        require(bool(chunk), "peer closed while reading frame header")
        header.extend(chunk)
    (size,) = struct.unpack(">I", header)
    body = bytearray()
    while len(body) < size:
        chunk = sock.recv(size - len(body))
        require(bool(chunk), "peer closed while reading frame body")
        body.extend(chunk)
    return bytes(body)


def probe_concurrent_exchange_load() -> None:
    with tcp_pair() as (server, client):
        require(server.peer_rom_version == "yellow", "server HELLO mismatch")
        require(client.peer_rom_version == "blue", "client HELLO mismatch")
        count = 16
        start_barrier = threading.Barrier(count * 2 + 1)
        result_lock = threading.Lock()
        server_results: list[tuple[int, bytes]] = []
        client_results: list[tuple[int, bytes]] = []
        worker_errors: list[BaseException] = []

        def exchange(
            link: TcpSerialLink,
            marker: bytes,
            index: int,
            results: list[tuple[int, bytes]],
        ) -> None:
            kind = f"probe/load/{index}"
            payload = marker + bytes([index, index ^ 0xFF])
            try:
                start_barrier.wait(timeout=2.0)
                result = link.exchange(kind, payload, timeout_ms=3000)
            except BaseException as exc:  # noqa: BLE001 - report worker failures
                with result_lock:
                    worker_errors.append(exc)
            else:
                with result_lock:
                    results.append((index, result))

        workers = [
            threading.Thread(
                target=exchange,
                args=(server, b"S", index, server_results),
                name=f"probe-server-load-{index}",
                daemon=True,
            )
            for index in range(count)
        ] + [
            threading.Thread(
                target=exchange,
                args=(client, b"C", index, client_results),
                name=f"probe-client-load-{index}",
                daemon=True,
            )
            for index in range(count)
        ]
        for worker in workers:
            worker.start()
        try:
            start_barrier.wait(timeout=2.0)
        except threading.BrokenBarrierError as exc:
            raise ProbeFailure("concurrent load workers missed the start barrier") from exc
        deadline = time.monotonic() + 6.0
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        require(
            not any(worker.is_alive() for worker in workers),
            "concurrent load worker exceeded the bounded join deadline",
        )
        require(not worker_errors, f"concurrent load errors: {worker_errors!r}")

        require(
            sorted(server_results)
            == [(index, b"C" + bytes([index, index ^ 0xFF])) for index in range(count)],
            f"server received unexpected concurrent results: {server_results!r}",
        )
        require(
            sorted(client_results)
            == [(index, b"S" + bytes([index, index ^ 0xFF])) for index in range(count)],
            f"client received unexpected concurrent results: {client_results!r}",
        )
        require(server.connected and client.connected, "load closed a healthy link")
        require(server._inbound_frame_count == 0, "server retained inbound frames")
        require(client._inbound_frame_count == 0, "client retained inbound frames")


def probe_shutdown_overlap() -> None:
    with tcp_pair() as (server, client):
        exchange_result: list[BaseException] = []
        exchange_started = threading.Event()

        def blocked_exchange() -> None:
            exchange_started.set()
            try:
                client.exchange("probe/shutdown", b"pending", timeout_ms=5000)
            except BaseException as exc:  # noqa: BLE001 - assert terminal error
                exchange_result.append(exc)

        exchange_thread = threading.Thread(
            target=blocked_exchange, name="probe-blocked-exchange", daemon=True
        )
        exchange_thread.start()
        require(exchange_started.wait(timeout=1.0), "exchange worker did not start")
        require(
            wait_until(
                lambda: (
                    (queue := server._inbound.get("probe/shutdown")) is not None
                    and queue.qsize() == 1
                )
            ),
            "server did not observe the blocked exchange",
        )

        close_errors: list[BaseException] = []

        def close(link: TcpSerialLink) -> None:
            try:
                link.close()
            except BaseException as exc:  # noqa: BLE001 - retain teardown error
                close_errors.append(exc)

        server_close = threading.Thread(
            target=close, args=(server,), name="probe-server-close", daemon=True
        )
        client_close = threading.Thread(
            target=close, args=(client,), name="probe-client-close", daemon=True
        )
        server_close.start()
        client_close.start()
        server_close.join(timeout=2.0)
        client_close.join(timeout=2.0)
        exchange_thread.join(timeout=2.0)

        require(not server_close.is_alive(), "server close worker leaked")
        require(not client_close.is_alive(), "client close worker leaked")
        require(not exchange_thread.is_alive(), "blocked exchange did not wake")
        require(not close_errors, f"shutdown errors: {close_errors!r}")
        require(
            exchange_result and isinstance(exchange_result[0], SerialLinkClosed),
            f"unexpected blocked exchange result: {exchange_result!r}",
        )
        require(not server.connected and not client.connected, "links stayed connected")
        require(not server._reader.is_alive(), "server reader leaked")
        require(not client._reader.is_alive(), "client reader leaked")


def probe_receiver_start_race() -> None:
    backend, peer = NetworkBackend.pair()
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def start_receiver() -> None:
        try:
            barrier.wait(timeout=1.0)
            backend.start_receiver(local_core=None)
        except RuntimeError as exc:
            outcomes.append(type(exc).__name__)
        except BaseException as exc:  # noqa: BLE001 - surface race failures
            outcomes.append(f"unexpected:{type(exc).__name__}:{exc}")
        else:
            outcomes.append("started")

    workers = [
        threading.Thread(target=start_receiver, name=f"probe-receiver-{index}", daemon=True)
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=1.0)
    for worker in workers:
        worker.join(timeout=1.0)

    try:
        require(sorted(outcomes) == ["RuntimeError", "started"], f"race outcomes: {outcomes}")
        require(backend._reader is not None, "receiver race left no reader")
        require(backend._edge_worker is not None, "receiver race left no worker")
        require(backend.stop(timeout_s=1.0), "backend workers did not stop")
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def probe_receiver_stop_race() -> None:
    for index in range(8):
        backend, peer = NetworkBackend.pair()
        barrier = threading.Barrier(3)
        start_outcomes: list[str] = []
        stop_outcomes: list[object] = []

        def start_receiver(
            *,
            backend: NetworkBackend = backend,
            barrier: threading.Barrier = barrier,
            outcomes: list[str] = start_outcomes,
        ) -> None:
            try:
                barrier.wait(timeout=1.0)
                backend.start_receiver(local_core=None)
            except NetworkBackendError:
                outcomes.append("closed")
            except BaseException as exc:  # noqa: BLE001 - surface race failures
                outcomes.append(f"unexpected:{type(exc).__name__}:{exc}")
            else:
                outcomes.append("started")

        def stop_receiver(
            *,
            backend: NetworkBackend = backend,
            barrier: threading.Barrier = barrier,
            outcomes: list[object] = stop_outcomes,
        ) -> None:
            try:
                barrier.wait(timeout=1.0)
                outcomes.append(backend.stop(timeout_s=1.0))
            except BaseException as exc:  # noqa: BLE001 - surface race failures
                outcomes.append(exc)

        start_thread = threading.Thread(
            target=start_receiver,
            name=f"probe-start-stop-start-{index}",
            daemon=True,
        )
        stop_thread = threading.Thread(
            target=stop_receiver,
            name=f"probe-start-stop-stop-{index}",
            daemon=True,
        )
        start_thread.start()
        stop_thread.start()
        try:
            barrier.wait(timeout=1.0)
            start_thread.join(timeout=2.0)
            stop_thread.join(timeout=2.0)
            require(not start_thread.is_alive(), "start/stop start worker leaked")
            require(not stop_thread.is_alive(), "start/stop stop worker leaked")
            require(
                start_outcomes in (["started"], ["closed"]),
                f"start/stop outcomes at iteration {index}: {start_outcomes!r}",
            )
            require(
                stop_outcomes == [True], f"stop outcome at iteration {index}: {stop_outcomes!r}"
            )
            for worker in (backend._reader, backend._edge_worker):
                require(
                    worker is None or not worker.is_alive(),
                    f"receiver worker leaked at iteration {index}",
                )
        finally:
            backend.stop(timeout_s=1.0)
            peer.stop(timeout_s=1.0)


def probe_timeout_terminality() -> None:
    with tcp_pair() as (server, client):
        try:
            client.exchange("probe/timeout", b"first", timeout_ms=50)
        except SerialLinkTimeout:
            pass
        else:
            raise ProbeFailure("silent EXCHANGE did not time out")
        require(not client.connected, "EXCHANGE timeout left a reusable link")
        require(not client._reader.is_alive(), "timeout left reader alive")
        try:
            client.exchange("probe/timeout", b"second", timeout_ms=100)
        except SerialLinkClosed:
            pass
        else:
            raise ProbeFailure("retry on timed-out link was not rejected")
        require(
            wait_until(lambda: not server.connected),
            "peer remained connected after timeout closure",
        )
        require(server._inbound_frame_count == 0, "closed peer retained inbound frames")


def probe_bye_ordering() -> None:
    local, peer = socket.socketpair()
    link = TcpSerialLink(local, "blue")
    close_errors: list[BaseException] = []
    exchange_errors: list[BaseException] = []
    bye_sent = threading.Event()
    release_bye = threading.Event()
    original_send_locked = link._send_frame_locked

    try:
        require(read_frame(peer) == bytes([1, 4]) + b"blue", "constructor HELLO mismatch")
        peer_hello = bytes([1, 6]) + b"yellow"
        peer.sendall(struct.pack(">I", len(peer_hello)) + peer_hello)
        require(link._hello_received.wait(timeout=1.0), "peer HELLO was not consumed")

        def hold_after_bye(payload: bytes, *, deadline: float | None = None) -> None:
            original_send_locked(payload, deadline=deadline)
            if payload == bytes([OP_BYE]):
                bye_sent.set()
                release_bye.wait(timeout=2.0)

        link._send_frame_locked = hold_after_bye  # type: ignore[method-assign]

        def close() -> None:
            try:
                link.close()
            except BaseException as exc:  # noqa: BLE001 - retain teardown error
                close_errors.append(exc)

        def exchange() -> None:
            try:
                link.exchange("probe/close-race", b"payload", timeout_ms=5000)
            except BaseException as exc:  # noqa: BLE001 - assert terminal error
                exchange_errors.append(exc)

        close_thread = threading.Thread(target=close, name="probe-close-race", daemon=True)
        exchange_thread = threading.Thread(target=exchange, name="probe-exchange-race", daemon=True)
        close_thread.start()
        require(bye_sent.wait(timeout=1.0), "close did not send BYE")
        require(read_frame(peer) == bytes([OP_BYE]), "first terminal frame was not BYE")
        exchange_thread.start()
        exchange_thread.join(timeout=0.1)
        require(exchange_thread.is_alive(), "exchange bypassed the held close lock")
        readable, _writable, _exceptional = select.select([peer], [], [], 0.1)
        require(readable == [], "EXCHANGE was written after BYE")
        release_bye.set()
        close_thread.join(timeout=2.0)
        exchange_thread.join(timeout=2.0)
        require(not close_thread.is_alive(), "close race worker leaked")
        require(not exchange_thread.is_alive(), "exchange race worker leaked")
        require(not close_errors, f"close race errors: {close_errors!r}")
        require(
            exchange_errors and isinstance(exchange_errors[0], SerialLinkClosed),
            f"unexpected close-race exchange result: {exchange_errors!r}",
        )
    finally:
        release_bye.set()
        link.close()
        peer.close()


class UntrustedTcpSocket:
    """Minimal connected-socket facade; no remote socket is opened."""

    family = socket.AF_INET

    def getpeername(self) -> tuple[str, int]:
        return "192.0.2.1", 4242

    def setblocking(self, _enabled: bool) -> None:
        return None

    def close(self) -> None:
        return None


def probe_raw_socket_boundary() -> None:
    socket_facade = UntrustedTcpSocket()
    try:
        NetworkBackend(socket_facade)  # type: ignore[arg-type]
    except ValueError as exc:
        require("localhost-only" in str(exc), f"wrong NetworkBackend error: {exc}")
    else:
        raise ProbeFailure("NetworkBackend accepted a synthetic non-loopback peer")
    try:
        TcpSerialLink(socket_facade, "blue")  # type: ignore[arg-type]
    except ValueError as exc:
        require("localhost-only" in str(exc), f"wrong TcpSerialLink error: {exc}")
    else:
        raise ProbeFailure("TcpSerialLink accepted a synthetic non-loopback peer")


def main() -> int:
    probes = (
        ("concurrent_exchange_load", probe_concurrent_exchange_load),
        ("shutdown_overlap", probe_shutdown_overlap),
        ("receiver_start_race", probe_receiver_start_race),
        ("receiver_stop_race", probe_receiver_stop_race),
        ("timeout_terminality", probe_timeout_terminality),
        ("bye_ordering", probe_bye_ordering),
        ("raw_socket_boundary", probe_raw_socket_boundary),
    )
    started = time.monotonic()
    for name, probe in probes:
        probe_started = time.monotonic()
        try:
            probe()
        except Exception:  # noqa: BLE001 - print a complete failure report
            print(f"FAIL {name}", file=sys.stderr)
            traceback.print_exc()
            return 1
        print(f"PASS {name} ({time.monotonic() - probe_started:.3f}s)")
    print(
        f"network concurrency probe: PASS ({len(probes)} probes, {time.monotonic() - started:.3f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
