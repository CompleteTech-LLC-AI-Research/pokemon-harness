#!/usr/bin/env python3
"""Bounded concurrency and network-boundary acceptance probe.

This probe is intentionally ROM-free.  It validates the transport seams that
sit below the real-ROM tiers:

* independent native and compatibility links make progress concurrently;
* request-less exchanges become terminal after a timeout;
* duplicate protocol handshakes fail closed;
* listener cancellation releases its port for a later bind; and
* both connection factories re-check resolver answers before opening TCP.

The probe never opens a non-loopback TCP connection.  It is kept as a script
instead of being added to the pytest tier manifest so this lane owns the whole
acceptance surface without changing the repository's shared classification.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from unittest.mock import patch

from pokered_harness.link import serial_link as serial_link_module
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_core import CYCLES_PER_BYTE_DMG, SerialCore
from pokered_harness.link.serial_link import (
    InProcessSerialLink,
    SerialLinkClosed,
    SerialLinkError,
    SerialLinkProtocolError,
    SerialLinkTimeout,
    TcpSerialLink,
)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _make_tcp_pair() -> tuple[TcpSerialLink, TcpSerialLink]:
    port = _free_port()
    ready = threading.Event()
    holder: dict[str, TcpSerialLink] = {}
    errors: list[BaseException] = []

    def listen() -> None:
        try:
            holder["server"] = TcpSerialLink.listen(
                port,
                "blue",
                accept_timeout_s=5.0,
                ready_event=ready,
            )
        except BaseException as exc:  # noqa: BLE001 - propagate setup failure
            errors.append(exc)

    listener = threading.Thread(target=listen, name="probe-accept-listener")
    listener.start()
    _require(ready.wait(timeout=2.0), "listener did not bind before the deadline")
    client = TcpSerialLink.connect("127.0.0.1", port, "yellow", timeout_s=2.0)
    listener.join(timeout=2.0)
    _require(not listener.is_alive(), "listener did not finish after accepting")
    _require(not errors, f"listener setup failed: {errors!r}")
    return holder["server"], client


def _wire_frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _probe_resolver_recheck() -> None:
    for connect, error_type, safe_calls in (
        (
            lambda: NetworkBackend.connect("localhost", 1, timeout_s=0.1),
            NetworkBackendError,
            2,
        ),
        (
            lambda: TcpSerialLink.connect("localhost", 1, "blue", timeout_s=0.1),
            SerialLinkError,
            1,
        ),
    ):
        safe = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 1))]
        unsafe = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.1", 1))]
        calls = 0

        def changing_resolution(
            *_args,
            _safe=safe,
            _unsafe=unsafe,
            _safe_calls=safe_calls,
            **_kwargs,
        ):
            nonlocal calls
            calls += 1
            return _safe if calls <= _safe_calls else _unsafe

        opened: list[object] = []

        def unexpected_socket(*_args, _opened=opened, **_kwargs):
            _opened.append((_args, _kwargs))
            raise RuntimeError("unsafe resolver answer reached socket creation")

        with (
            patch.object(socket, "getaddrinfo", changing_resolution),
            patch.object(socket, "socket", unexpected_socket),
        ):
            try:
                connect()
            except error_type as exc:
                _require("unsafe" in str(exc), f"wrong resolver error: {exc}")
            else:
                raise RuntimeError("unsafe resolver answer was accepted")
        _require(calls == safe_calls + 1, f"resolver call count was {calls}")
        _require(not opened, "socket creation was attempted for an unsafe answer")


def _probe_raw_constructor_boundary() -> None:
    constructors: tuple[Callable[[object], object], ...] = (
        lambda sock: NetworkBackend(sock),
        lambda sock: TcpSerialLink(sock, "blue"),
    )

    class UnsafePeerSocket:
        family = socket.AF_INET

        def __init__(self) -> None:
            self.closed = False

        def getpeername(self):
            return ("192.0.2.1", 4242)

        def close(self) -> None:
            self.closed = True

    for constructor in constructors:
        sock = UnsafePeerSocket()
        try:
            constructor(sock)
        except ValueError as exc:
            _require("localhost-only" in str(exc), f"wrong peer error: {exc}")
        else:
            raise RuntimeError("raw constructor accepted a non-loopback peer")
        _require(sock.closed, "unsafe raw socket was not closed")


def _probe_timeout_terminal() -> None:
    server, client = _make_tcp_pair()
    try:
        try:
            client.exchange("timeout", b"first", timeout_ms=50)
        except SerialLinkTimeout:
            pass
        else:
            raise RuntimeError("TCP timeout did not raise SerialLinkTimeout")
        _require(not client.connected, "timed-out TCP stream remained reusable")
        try:
            client.exchange("timeout", b"second", timeout_ms=500)
        except SerialLinkClosed:
            pass
        else:
            raise RuntimeError("closed TCP stream accepted a later exchange")
    finally:
        client.close()
        server.close()

    local, peer = InProcessSerialLink.pair("blue", "yellow")
    try:
        try:
            local.exchange("timeout", b"first", timeout_ms=50)
        except SerialLinkTimeout:
            pass
        else:
            raise RuntimeError("in-process timeout did not raise SerialLinkTimeout")
        _require(not local.connected and not peer.connected, "in-process timeout stayed open")
        try:
            local.exchange("timeout", b"second", timeout_ms=500)
        except SerialLinkClosed:
            pass
        else:
            raise RuntimeError("closed in-process stream accepted a later exchange")
    finally:
        local.close()
        peer.close()


def _probe_duplicate_hello() -> None:
    native_a, native_b = NetworkBackend.pair()
    native_a.start_receiver(local_core=None)
    compat_sock, compat_peer = socket.socketpair()
    compat = TcpSerialLink(compat_sock, "blue")
    try:
        native_body = bytes([0x01, (1 << 4) | 1])  # protocol 1, ROM red
        native_b._sock.sendall(native_body + native_body)
        compat_body = bytes([serial_link_module.OP_HELLO]) + b"\x03red"
        compat_peer.sendall(_wire_frame(compat_body) + _wire_frame(compat_body))

        deadline = time.monotonic() + 1.0
        while (
            native_a._reader_exc is None or compat._reader_exc is None
        ) and time.monotonic() < deadline:
            time.sleep(0.005)
        _require(
            isinstance(native_a._reader_exc, NetworkBackendError),
            f"native duplicate HELLO was not rejected: {native_a._reader_exc!r}",
        )
        _require(
            "duplicate peer HELLO" in str(native_a._reader_exc),
            f"wrong native duplicate error: {native_a._reader_exc}",
        )
        _require(
            isinstance(compat._reader_exc, SerialLinkProtocolError),
            f"compat duplicate HELLO was not rejected: {compat._reader_exc!r}",
        )
        _require(
            "duplicate HELLO" in str(compat._reader_exc),
            f"wrong compatibility duplicate error: {compat._reader_exc}",
        )
        _require(not native_a.connected and not compat.connected, "duplicate stayed connected")
    finally:
        native_a.stop()
        native_b.stop()
        compat.close()
        compat_peer.close()


def _probe_native_load(pair_count: int, rounds: int) -> None:
    start = threading.Barrier(pair_count)
    errors: list[tuple[int, BaseException]] = []
    errors_lock = threading.Lock()

    def exercise(index: int) -> None:
        backend_a, backend_b = NetworkBackend.pair()
        master = SerialCore(backend=backend_a)
        slave = SerialCore()
        backend_a.start_receiver(local_core=master)
        backend_b.start_receiver(local_core=slave)
        try:
            start.wait(timeout=5.0)
            for round_no in range(rounds):
                master_byte = (index * 17 + round_no) & 0xFF
                slave_byte = (0xFF - index * 9 - round_no) & 0xFF
                master.set_SB(master_byte)
                slave.set_SB(slave_byte)
                master.set_SC(0x81)
                slave.set_SC(0x80)
                _require(
                    master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG),
                    f"native pair {index} round {round_no} did not complete",
                )
                _require(master.SB == slave_byte, f"native pair {index} master byte mismatch")
                _require(slave.SB == master_byte, f"native pair {index} slave byte mismatch")
        except BaseException as exc:  # noqa: BLE001 - aggregate worker failures
            with errors_lock:
                errors.append((index, exc))
        finally:
            backend_a.stop(timeout_s=1.0)
            backend_b.stop(timeout_s=1.0)

    workers = [
        threading.Thread(target=exercise, args=(index,), name=f"probe-native-{index}")
        for index in range(pair_count)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20.0)
    _require(
        not [worker.name for worker in workers if worker.is_alive()],
        "native load worker exceeded the bounded join deadline",
    )
    _require(not errors, f"native load errors: {errors!r}")


def _probe_tcp_load(pair_count: int, rounds: int) -> None:
    pairs = [_make_tcp_pair() for _ in range(pair_count)]
    start = threading.Barrier(pair_count * 2)
    finished = [threading.Barrier(2) for _ in range(pair_count)]
    errors: list[tuple[int, bool, BaseException]] = []
    errors_lock = threading.Lock()

    def exercise(index: int, link: TcpSerialLink, is_server: bool) -> None:
        try:
            start.wait(timeout=5.0)
            for round_no in range(rounds):
                kind = f"load/{round_no % 3}"
                value = (index * 31 + round_no + (1 if is_server else 2)) & 0xFF
                expected = bytes([(index * 31 + round_no + (2 if is_server else 1)) & 0xFF])
                got = link.exchange(kind, bytes([value]), timeout_ms=3000)
                _require(got == expected, f"TCP pair {index} returned the wrong byte")
            finished[index].wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001 - aggregate worker failures
            with errors_lock:
                errors.append((index, is_server, exc))

    workers: list[threading.Thread] = []
    try:
        for index, (server, client) in enumerate(pairs):
            workers.extend(
                [
                    threading.Thread(
                        target=exercise,
                        args=(index, server, True),
                        name=f"probe-tcp-server-{index}",
                    ),
                    threading.Thread(
                        target=exercise,
                        args=(index, client, False),
                        name=f"probe-tcp-client-{index}",
                    ),
                ]
            )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20.0)
        _require(
            not [worker.name for worker in workers if worker.is_alive()],
            "TCP load worker exceeded the bounded join deadline",
        )
        _require(not errors, f"TCP load errors: {errors!r}")
    finally:
        for server, client in pairs:
            server.close()
            client.close()


def _probe_listener_reconnect(cycles: int) -> None:
    for cycle in range(cycles):
        port = _free_port()
        ready = threading.Event()
        cancel = threading.Event()
        errors: list[BaseException] = []

        def listen_until_cancelled(
            port: int = port,
            cancel: threading.Event = cancel,
            ready: threading.Event = ready,
            errors: list[BaseException] = errors,
        ) -> None:
            try:
                TcpSerialLink.listen(
                    port,
                    "blue",
                    accept_timeout_s=5.0,
                    cancel_event=cancel,
                    ready_event=ready,
                )
            except BaseException as exc:  # noqa: BLE001 - assert exact outcome
                errors.append(exc)

        worker = threading.Thread(
            target=listen_until_cancelled,
            name=f"probe-cancel-listener-{cycle}",
        )
        worker.start()
        _require(ready.wait(timeout=2.0), f"listener cycle {cycle} did not bind")
        cancel.set()
        worker.join(timeout=2.0)
        _require(not worker.is_alive(), f"listener cycle {cycle} did not cancel")
        _require(len(errors) == 1, f"listener cycle {cycle} had errors {errors!r}")
        _require(
            isinstance(errors[0], SerialLinkClosed),
            f"listener cycle {cycle} returned {errors[0]!r}",
        )

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()


def _run(args: argparse.Namespace) -> None:
    _probe_resolver_recheck()
    _probe_raw_constructor_boundary()
    _probe_timeout_terminal()
    _probe_duplicate_hello()
    _probe_native_load(args.native_pairs, args.rounds)
    _probe_tcp_load(args.tcp_pairs, args.rounds)
    _probe_listener_reconnect(args.reconnect_cycles)


def _positive_bounded(value: str, name: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
    if not 1 <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be in the range 1..{maximum}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--native-pairs",
        type=lambda value: _positive_bounded(value, "native-pairs", 32),
        default=6,
    )
    parser.add_argument(
        "--tcp-pairs",
        type=lambda value: _positive_bounded(value, "tcp-pairs", 16),
        default=4,
    )
    parser.add_argument(
        "--rounds",
        type=lambda value: _positive_bounded(value, "rounds", 256),
        default=16,
    )
    parser.add_argument(
        "--reconnect-cycles",
        type=lambda value: _positive_bounded(value, "reconnect-cycles", 64),
        default=8,
    )
    args = parser.parse_args()
    started = time.monotonic()
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - report one bounded probe failure
        print(f"network concurrency probe: FAIL: {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.monotonic() - started
    print(
        "network concurrency probe: PASS "
        f"(native_pairs={args.native_pairs}, tcp_pairs={args.tcp_pairs}, "
        f"rounds={args.rounds}, reconnect_cycles={args.reconnect_cycles}, "
        f"elapsed_s={elapsed:.3f})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
