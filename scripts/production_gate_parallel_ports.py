"""Root-local listening-socket handoff custody (#107 B2-L prerequisite).

ACK permits a checked sender-copy close, never proof of exported-copy retirement.
Unknown sends/ACKs stay retained and cannot be offered again. Recipient authority,
linked resource release and shared adoption remain separate J1/C/B1/B3 obligations.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import struct
import threading
import time
import uuid
from typing import Any

from scripts.production_gate_parallel_handler_custody import ConnectionCustody, stop_hub
from scripts.production_gate_parallel_handoff_custody import HandoffBook, HandoffView, Refused
from scripts.production_gate_parallel_job_lifecycle import dispose_listener, start_listener

LOOPBACK = "127.0.0.1"
HANDOFF_ENV = "POKERED_LISTENER_HANDOFF"
_SOCKET = socket.socket
ACK_TIMEOUT_SECONDS = 30.0
MESSAGE_LIMIT = 4096


class ListenerBroker:
    def __init__(
        self,
        first_port: int = 0,
        count: int = 0,
        *,
        host: str = LOOPBACK,
        ack_timeout: float = ACK_TIMEOUT_SECONDS,
        clock: Any = time.monotonic,
        on_loss: Any = None,
        lease_limit: int | None = None,
        handler_limit: int | None = None,
    ) -> None:
        limit = max(4, count) if count else 64
        if lease_limit is not None:
            limit = lease_limit
        self.name = uuid.uuid4().hex
        self.first_port, self.count, self.host = first_port, count, host
        self.ack_timeout, self._clock, self.on_loss = ack_timeout, clock, on_loss
        self._closing = threading.Event()
        self._stopping = self._closing  # stop_hub adapter, same Event
        self._server: Any = None
        self._thread: Any = None
        self._startup_state, self._server_state, self._thread_state = "new", "none", "none"
        self._thread_dispose_attempted = False
        self._connections = ConnectionCustody(
            max(4, 2 * limit + 2) if handler_limit is None else handler_limit, self.lose
        )
        self._book = HandoffBook(
            self._connections,
            limit,
            self.current_clock,
            lambda: getattr(self, "_startup_deadline", None),
            self.lose,
            ack_timeout,
        )
        self._lock = self._connections.lock

    def current_clock(self) -> float:
        return getattr(self, "_startup_clock", self._clock)()

    def lose(self, reason: str) -> None:
        first = self._book.lose(reason)
        self._closing.set()
        if first and self.on_loss is not None:
            self.on_loss(reason)

    def start(self) -> ListenerBroker:
        return start_listener(self, _SOCKET, threading.Thread, self._serve, "listener-broker")

    @property
    def startup_disposed(self) -> bool:
        return (
            self._server_state in ("none", "closed")
            and self._thread_state in ("none", "quiescent")
            and self._connections.disposed()
            and self._book.disposed()
        )

    def lease(self) -> int:
        with self._book.operation() as operation:
            return self._lease(operation)

    def _lease(self, operation: Any) -> int:
        candidates = range(self.first_port, self.first_port + self.count) if self.count else [0]
        for port in candidates:
            lease = self._book.reserve(operation, port)
            if lease is None:
                continue
            try:
                self._book.remaining(operation)
                lease.sock = _SOCKET(socket.AF_INET, socket.SOCK_STREAM)
                lease.close_state, lease.state = "open", "configuring"
                self._book.remaining(operation)
                lease.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    self._book.remaining(operation)
                    lease.sock.bind((self.host, port))
                except OSError as error:
                    if error.errno != errno.EADDRINUSE:
                        raise
                    self._book.close_lease(lease, operation)
                    lease.state = "bind_conflict"
                    self._book.retire(lease)
                    continue  # known failed candidate only; original operation cap
                self._book.remaining(operation)
                lease.sock.listen(1)
                self._book.remaining(operation)
                bound = int(lease.sock.getsockname()[1])
                if not self._book.publish(lease, bound):
                    self._book.close_lease(lease, operation)
                    self._book.retire(lease)
                    raise Refused("handoff stopped during lease setup")
                return bound
            except Refused:
                raise  # known stop refusal after checked disposal, not a retry
            except BaseException as error:
                self._book.fault(
                    operation, "listener allocation/configuration unproven", lease, primary=error
                )
                raise
        raise Refused("no loopback port is left in this job's reserved range")

    def state(self, port: int) -> str | None:
        return self._book.state(port)

    def _serve(self) -> None:
        assert self._server is not None
        pool = self._connections
        try:
            while not self._closing.is_set():
                record = pool.reserve()
                if record is None:
                    return
                try:
                    record.raw_result = self._server._accept()
                except (TimeoutError, BlockingIOError):
                    pool.unreadable(record)
                    continue
                except BaseException as error:
                    pool.fail(record, "handoff accept return unproven", accept=True)
                    if isinstance(error, OSError):
                        return
                    raise
                pool.accepted(record)
                record.handoff_cap = self.current_clock() + self.ack_timeout
                bound = getattr(self, "_startup_deadline", None)
                if bound is not None:
                    record.handoff_cap = min(record.handoff_cap, bound)
                pool.wrap(record, _SOCKET, self._server)

                def serve(_view: Any, record: Any = record) -> None:
                    with self._book.operation(cap=record.handoff_cap) as operation:
                        self._handle(HandoffView(pool, record, self._book, operation), operation)

                pool.dispatch(record, threading.Thread, serve)
        finally:
            pool.complete_accept()

    @staticmethod
    def _same_user(conn: Any) -> bool:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1] == os.getuid()

    def _handle(self, conn: Any, existing_operation: Any = None) -> None:
        with self._book.operation(existing_operation) as operation, conn:
            conn.settimeout(self._book.remaining(operation))
            try:
                if not self._same_user(conn):
                    return
                conn.settimeout(self._book.remaining(operation))
                request = json.loads(conn.recv(MESSAGE_LIMIT))
                kind = request.get("op") if isinstance(request, dict) else None
                if kind == "lease":
                    reply = json.dumps({"ok": True, "port": self._lease(operation)}).encode("ascii")
                    conn.settimeout(self._book.remaining(operation))
                    conn.send(reply)
                elif kind == "release":
                    self._release(request.get("port"), operation)
                    conn.settimeout(self._book.remaining(operation))
                    conn.send(b'{"ok":true}')
                elif kind == "take":
                    self._take(conn, request, operation)
            except (OSError, ValueError, TimeoutError):
                return  # operation/lease uncertainty is committed by the effect path

    def _release(self, port: object, existing_operation: Any = None) -> None:
        with self._book.operation(existing_operation) as operation:
            self._book.remaining(operation)
            lease = self._book.claim(port, operation, "release_claimed")
            if lease is not None:
                self._book.close_lease(lease, operation)
                lease.state = "released"
                self._book.retire(lease)

    def _take(self, conn: Any, request: dict[str, Any], existing_operation: Any = None) -> None:
        with self._book.operation(existing_operation) as operation:
            lease = (
                self._book.claim(request.get("port"), operation, "offer_claimed")
                if request.get("host") == self.host
                else None
            )
            if lease is None:
                conn.settimeout(self._book.remaining(operation))
                conn.send(b'{"ok":false,"error":"not leased"}')
                return
            try:
                self._book.mark(lease, "send_attempted")
                conn.settimeout(self._book.remaining(operation))
                descriptor = lease.sock.fileno()
                lease.sent = socket.send_fds(conn, [b'{"ok":true}'], [descriptor])
                lease.exported = True
                if lease.sent != len(b'{"ok":true}'):
                    raise OSError("short listener rights send; no continuation allowed")
                self._book.mark(lease, "sent_await_ack")
                conn.settimeout(self._book.remaining(operation))
                answer = json.loads(conn.recv(MESSAGE_LIMIT))
                self._book.remaining(operation)  # late ACK never upgrades the offer
                if not isinstance(answer, dict) or answer.get("op") != "ack":
                    raise ValueError("listener acknowledgment absent or negative")
                self._book.mark(lease, "ack_validated")
                self._book.close_lease(lease, operation)
                lease.state = "acknowledged"
                self._book.retire(lease)
            except BaseException as error:
                lease.note = "listener offer unproven; socket kept, never offered again"
                self._book.fault(operation, "listener offer/ACK unproven", lease, primary=error)
                raise

    def service(self) -> None:
        self._connections.housekeep()  # owner leaf; no socket repair or process waits

    def close(self) -> dict[str, Any]:
        stop_hub(self, dispose_listener)
        self._book.dispose_idle(self)
        rows = self._book.snapshot()
        return {
            "leases": rows,
            "unconsumed": [x["port"] for x in rows if x["state"] in ("leased", "released")],
            "unacknowledged": [
                x["port"]
                for x in rows
                if x["unknown"] or x["state"] in ("send_attempted", "sent_await_ack")
            ],
            "pending": not self.startup_disposed,
            "first_reason": self._book.reason(),
        }


class HandoffRegistry:
    def __init__(self, *, job_ids: Any = None, max_brokers: int = 64) -> None:
        keys = None if job_ids is None else frozenset(job_ids)
        maximum = max_brokers if keys is None else len(keys)
        if type(maximum) is not int or maximum < 1:
            raise ValueError("a finite positive broker bound is required")
        self._keys, self._maximum = keys, maximum
        self._issued: set[str] = set()
        self._retired: set[str] = set()
        self._brokers: dict[str, ListenerBroker] = {}
        self._loss: Any = None
        self._loss_bound = False

    def bind_loss(self, sink: Any) -> None:
        if self._loss_bound or self._issued:
            raise RuntimeError("handoff loss binding cannot be retried or delayed")
        self._loss, self._loss_bound = sink, True

    def open(self, job_id: str, first_port: int, count: int, *, launch: Any = None) -> str:
        if job_id in self._issued:
            raise RuntimeError("handoff startup cannot be retried")
        if (
            len(self._issued) >= self._maximum
            or self._keys is not None
            and job_id not in self._keys
        ):
            raise Refused("handoff registry admission exceeded its fixed job bound")
        self._issued.add(job_id)
        self._brokers[job_id] = ListenerBroker(first_port, count, on_loss=self._loss)
        broker = self._brokers[job_id]
        if launch is not None:
            launch.broker = broker
            broker._startup_deadline, broker._startup_clock = launch.cleanup_cap, launch.clock
        broker.start()
        return broker.name

    def close(self, job_id: str) -> dict[str, Any] | None:
        broker = self._brokers.get(job_id)
        if broker is None:
            return None
        record = broker.close()
        if broker.startup_disposed:
            del self._brokers[job_id]
            self._retired.add(job_id)
        return record

    def _visit(self, method: str) -> None:
        first = None
        for job_id, broker in tuple(self._brokers.items()):
            try:
                self.close(job_id) if method == "close" else broker.service()
            except Exception as error:  # noqa: BLE001 - service siblings then preserve first failure
                first = first or error
        if first is not None:
            raise first

    def service(self) -> None:
        self._visit("service")

    @property
    def startup_disposed(self) -> bool:
        return not self._brokers and self._issued == self._retired

    def close_all(self) -> None:
        self._visit("close")


__all__ = ("HandoffRegistry", "ListenerBroker")
