"""Parent arbiter for concurrent runtime jobs: the one live book of row reservations (#107 F3).

Every child gate reports each row reservation, release and loss here over the bounded, authenticated,
acknowledged protocol of ``gate_capacity_arbiter``, and a child slot exists only because this hub
granted it: a reservation is granted only while the job is under its share, the run is under the
single global pair budget, and admission is open.  ``lose(reason)`` closes admission for every job at
once and invokes the owner's callback (which signals every owned guardian immediately); a silent job
(no heartbeat inside the lease), a policy mismatch and a repeated protocol violation do the same.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from scripts.gate_capacity_arbiter import (
    ENV_JOB,
    ENV_KEY,
    ENV_NAME,
    ENV_POLICY,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ArbiterError,
    clip,
    peek,
    seal,
    unseal,
)

LEASE_SECONDS = 15.0  # heartbeats arrive every 2 s; an estimate, not a qualification
VIOLATION_LIMIT = 3
EVENT_KINDS = frozenset({"blocked", "expired", "abandoned", "capacity_loss"})
MAX_EVENTS = 500
_SOCKET = socket.socket


class _Job:
    def __init__(self, key: bytes, pairs: int, policy: str, clock: float) -> None:
        self.key, self.pairs, self.policy = key, pairs, policy
        self.active: set[str] = set()
        self.grants = self.denials = self.releases = self.stray_releases = self.violations = 0
        self.events: deque[dict[str, str]] = deque(maxlen=MAX_EVENTS)
        self.hello = self.finished = self.dropped = False
        self.last_seen = clock
        self.peer_pid: int | None = None


class ArbiterHub:
    """Owns the live book; one instance per run, started before any job exists."""

    def __init__(
        self,
        total_pairs: int,
        *,
        on_loss: Callable[[str], None] | None = None,
        lease_seconds: float = LEASE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name, self.total = uuid.uuid4().hex, total_pairs
        self.on_loss, self.lease_seconds, self._clock = on_loss, lease_seconds, clock
        self.closed_reason = ""
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> ArbiterHub:
        server = _SOCKET(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        server.bind("\0" + self.name)
        server.listen(16)
        server.settimeout(0.2)
        self._server = server
        self._thread = threading.Thread(target=self._accept, name="arbiter-hub", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def register(self, job_id: str, pairs: int, policy: str = "") -> dict[str, str]:
        """Add a job before its worker exists and return the environment that reaches its gate."""

        key = os.urandom(32)
        with self._lock:
            self._jobs[job_id] = _Job(key, pairs, policy, self._clock())
        return {ENV_NAME: self.name, ENV_KEY: key.hex(), ENV_JOB: job_id, ENV_POLICY: policy}

    def unregister(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return None
        return {
            "hello": job.hello,
            "finished": job.finished,
            "dropped": job.dropped,
            "peer_pid": job.peer_pid,
            "grants": job.grants,
            "denials": job.denials,
            "releases": job.releases,
            "stray_releases": job.stray_releases,
            "violations": job.violations,
            "unreleased": sorted(job.active),
            "events": list(job.events),
        }

    def lose(self, reason: str) -> None:
        """Close admission for every job at once and tell the owner (idempotent, first reason wins)."""

        with self._lock:
            first = not self.closed_reason
            self.closed_reason = self.closed_reason or clip(reason)
        if first and self.on_loss is not None:
            self.on_loss(self.closed_reason)

    def check_leases(self) -> None:
        now = self._clock()
        with self._lock:
            silent = [
                job_id
                for job_id, job in self._jobs.items()
                if job.hello and not job.finished and now - job.last_seen > self.lease_seconds
            ]
        if silent:
            self.lose(f"{silent[0]}: the child arbiter lease expired")

    def _accept(self) -> None:
        assert self._server is not None
        while not self._stopping.is_set():
            try:
                descriptor, _ = self._server._accept()  # avoids socket.socket()
            except TimeoutError:
                continue
            except OSError:
                return
            conn = _SOCKET(
                self._server.family, self._server.type, self._server.proto, fileno=descriptor
            )
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _peer(conn: socket.socket) -> tuple[int, int]:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", raw)
        return pid, uid

    def _serve(self, conn: socket.socket) -> None:
        job: _Job | None = None
        last_seq = 0
        with conn:
            conn.settimeout(self.lease_seconds * 2)
            try:
                pid, uid = self._peer(conn)
                if uid != os.getuid():
                    return
                while not self._stopping.is_set():
                    data = conn.recv(MAX_MESSAGE_BYTES + 1)
                    if not data:
                        break
                    outcome = self._handle(conn, data, job, last_seq, pid)
                    if outcome is None:
                        break
                    job, last_seq = outcome
            except (OSError, TimeoutError, ArbiterError):
                pass
            finally:
                if job is not None and not job.finished:
                    with self._lock:
                        job.dropped = True
                        job.active.clear()

    def _handle(
        self, conn: socket.socket, data: bytes, job: _Job | None, last_seq: int, pid: int
    ) -> tuple[_Job, int] | None:
        body = peek(data)
        with self._lock:
            found = self._jobs.get(str(body.get("job")))
        if found is None:
            return None
        try:
            body = unseal(found.key, data)
        except ArbiterError:
            self._violation(found)
            return None
        seq = body.get("seq")
        if (
            body.get("v") != PROTOCOL_VERSION
            or type(seq) is not int
            or seq <= last_seq
            or (job is not None and job is not found)
        ):
            self._violation(found)
            return None
        result, reason = self._dispatch(found, body, pid)
        if result is None:
            self._violation(found)
            return None
        reply = {"v": PROTOCOL_VERSION, "job": body["job"], "ack": seq, "result": result}
        reply["reason"] = clip(reason)
        conn.send(seal(found.key, reply))
        return None if body["op"] == "bye" else (found, seq)

    def _violation(self, job: _Job) -> None:
        with self._lock:
            job.violations += 1
            tripped = job.violations >= VIOLATION_LIMIT
        if tripped:
            self.lose("a child repeatedly violated the arbiter protocol")

    def _reserve(self, job: _Job, pair: str, closed: str) -> tuple[str, str]:
        """Grant only under the job's share, the single global budget and an open admission."""

        if closed:
            return "closed", closed
        if pair in job.active:
            return "granted", "already held"
        if len(job.active) >= job.pairs:
            job.denials += 1
            return "denied", "the job's share is fully reserved"
        if sum(len(j.active) for j in self._jobs.values()) >= self.total:
            job.denials += 1
            return "denied", "the single global pair budget is fully reserved"
        job.active.add(pair)
        job.grants += 1
        return "granted", ""

    def _dispatch(self, job: _Job, body: dict[str, Any], pid: int) -> tuple[str | None, str]:
        """Return (result, reason); a None result is a protocol violation."""

        operation, pair, loss = body.get("op"), clip(body.get("pair", "")), ""
        with self._lock:
            job.last_seen = self._clock()
            closed = self.closed_reason
            if operation == "hello":
                if job.policy and body.get("policy") != job.policy:
                    loss = f"{body.get('job')}: the child policy differs from the plan-pinned one"
                else:
                    job.hello, job.peer_pid = True, pid
                result, reason = ("closed", closed) if closed else ("ok", "")
            elif operation in ("ping", "bye"):
                job.finished = job.finished or operation == "bye"
                result, reason = ("closed", closed) if closed else ("ok", "")
            elif operation == "reserve" and pair:
                result, reason = self._reserve(job, pair, closed)
            elif operation == "release" and pair:
                if pair in job.active:
                    job.active.discard(pair)
                    job.releases += 1
                else:
                    job.stray_releases += 1
                result, reason = "ok", ""
            elif operation == "event" and body.get("kind") in EVENT_KINDS:
                note = {
                    "kind": str(body["kind"]),
                    "pair": pair,
                    "reason": clip(body.get("reason", "")),
                }
                job.events.append(note)
                if body["kind"] == "capacity_loss":
                    loss = f"{body.get('job')}: {note['reason']}"
                result, reason = "ok", ""
            else:
                return None, ""
        if loss:
            self.lose(loss)
            return "closed", self.closed_reason
        return result, reason


__all__ = ("ArbiterHub",)
