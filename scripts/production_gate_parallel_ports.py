"""Owner-held listening sockets for concurrent runtime jobs (#107 F6).

A number-only reservation (find a free port, close it, hand the number on) leaves a window in which
any other process can bind the port.  Here the owner BINDS AND LISTENS the socket itself and keeps
it; the listener that will really serve the port receives that very socket over a unix socket
(SCM_RIGHTS), validates it and acknowledges, and only then does the owner close its copy.  A
request for a port the owner never leased is answered "not leased" so ordinary binds are unchanged.
The unix socket lives in the abstract namespace under a random name, which avoids path-length
limits, and every peer must carry this process's uid (SO_PEERCRED).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import uuid
from typing import Any

LOOPBACK = "127.0.0.1"
HANDOFF_ENV = "POKERED_LISTENER_HANDOFF"
_SOCKET = socket.socket  # the real class, immune to tests replacing socket.socket
ACK_TIMEOUT_SECONDS = 30.0
MESSAGE_LIMIT = 4096


class ListenerBroker:
    """Binds and keeps loopback listeners from a job's port range until a listener adopts them."""

    def __init__(
        self,
        first_port: int = 0,
        count: int = 0,
        *,
        host: str = LOOPBACK,
        ack_timeout: float = ACK_TIMEOUT_SECONDS,
    ) -> None:
        self.name = uuid.uuid4().hex
        self.first_port, self.count, self.host = first_port, count, host
        self.ack_timeout = ack_timeout
        self._leases: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> ListenerBroker:
        server = _SOCKET(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        server.bind("\0" + self.name)
        server.listen(16)
        server.settimeout(0.2)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="listener-broker", daemon=True)
        self._thread.start()
        return self

    def lease(self) -> int:
        """Bind and listen a port of the range (or any free port when no range) and keep it."""

        candidates = range(self.first_port, self.first_port + self.count) if self.count else [0]
        for port in candidates:
            with self._lock:
                if port and port in self._leases:
                    continue
            sock = _SOCKET(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, port))
                sock.listen(1)
            except OSError:
                sock.close()
                continue
            bound = int(sock.getsockname()[1])
            with self._lock:
                self._leases[bound] = {"sock": sock, "state": "leased", "note": ""}
            return bound
        raise OSError("no loopback port is left in this job's reserved range")

    def state(self, port: int) -> str | None:
        with self._lock:
            lease = self._leases.get(port)
            return None if lease is None else str(lease["state"])

    def _serve(self) -> None:
        assert self._server is not None
        while not self._closing.is_set():
            try:
                descriptor, _ = self._server._accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn = _SOCKET(
                self._server.family, self._server.type, self._server.proto, fileno=descriptor
            )
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _same_user(conn: socket.socket) -> bool:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1] == os.getuid()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(self.ack_timeout)
            try:
                if not self._same_user(conn):
                    return
                request = json.loads(conn.recv(MESSAGE_LIMIT))
                operation = request.get("op") if isinstance(request, dict) else None
                if operation == "lease":
                    conn.send(json.dumps({"ok": True, "port": self.lease()}).encode("ascii"))
                elif operation == "release":
                    self._release(request.get("port"))
                    conn.send(b'{"ok":true}')
                elif operation == "take":
                    self._take(conn, request)
            except (OSError, ValueError, TimeoutError):
                return

    def _release(self, port: object) -> None:
        with self._lock:
            lease = self._leases.get(port) if type(port) is int else None
            if lease is not None and lease["sock"] is not None and lease["state"] == "leased":
                lease["sock"].close()
                lease["sock"], lease["state"] = None, "released"

    def _take(self, conn: socket.socket, request: dict[str, Any]) -> None:
        port = request.get("port")
        with self._lock:
            lease = self._leases.get(port) if type(port) is int else None
            if lease is None or lease["state"] != "leased" or request.get("host") != self.host:
                conn.send(b'{"ok":false,"error":"not leased"}')
                return
            lease["state"] = "transferred"
            sock = lease["sock"]
        socket.send_fds(conn, [b'{"ok":true}'], [sock.fileno()])
        try:
            answer = json.loads(conn.recv(MESSAGE_LIMIT))
        except (OSError, ValueError, TimeoutError):
            answer = {}
        with self._lock:
            if isinstance(answer, dict) and answer.get("op") == "ack":
                sock.close()
                lease["sock"], lease["state"] = None, "acknowledged"
            else:
                lease["state"], lease["note"] = "leased", "no acknowledgment: socket kept"

    def close(self) -> dict[str, Any]:
        """Stop serving, close every socket still held, and return what happened to each lease."""

        self._closing.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            ledger = []
            for port, lease in sorted(self._leases.items()):
                ledger.append({"port": port, "state": lease["state"], "note": lease["note"]})
                if lease["sock"] is not None:
                    lease["sock"].close()
                    lease["sock"] = None
        return {
            "leases": ledger,
            "unconsumed": [
                item["port"] for item in ledger if item["state"] in ("leased", "released")
            ],
            "unacknowledged": [item["port"] for item in ledger if item["state"] == "transferred"],
        }


class HandoffRegistry:
    """One broker per job: opened before its worker exists, closed with the job's cleanup."""

    def __init__(self) -> None:
        self._brokers: dict[str, ListenerBroker] = {}

    def open(self, job_id: str, first_port: int, count: int) -> str:
        broker = ListenerBroker(first_port, count).start()
        self._brokers[job_id] = broker
        return broker.name

    def close(self, job_id: str) -> dict[str, Any] | None:
        broker = self._brokers.pop(job_id, None)
        return None if broker is None else broker.close()

    def close_all(self) -> None:
        for job_id in list(self._brokers):
            self.close(job_id)


__all__ = ("HandoffRegistry", "ListenerBroker")
