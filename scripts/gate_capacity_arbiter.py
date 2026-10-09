"""Child side of the live parent arbiter for concurrent runtime jobs (#107 F3).

In a concurrent job every row reservation, release and loss is reported to the parent over a bounded,
authenticated, acknowledged protocol, and a slot is granted only when the parent says so.  The wire is
JSON of at most ``MAX_MESSAGE_BYTES`` over an abstract unix SEQPACKET socket; every message carries the
job, a strictly increasing sequence number and an HMAC-SHA256 under the job's key, and every reply
echoes that sequence number with an explicit result under its own MAC.  The first timeout, bad reply,
missing acknowledgment or ``closed`` result latches the client closed for good, which closes admission.
Outside a concurrent job nothing is configured and :meth:`ArbiterClient.from_environment` returns None.
The key reaches the job through its environment, so the MAC proves job binding, not secrecy against a
hostile process of the same UID.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import os
import socket
import threading
from collections.abc import Mapping, MutableMapping
from typing import Any

ENV_NAME = "POKERED_ARBITER_NAME"
ENV_KEY = "POKERED_ARBITER_KEY"
ENV_JOB = "POKERED_ARBITER_JOB"
ENV_POLICY = "POKERED_ARBITER_POLICY"
ARBITER_ENV_NAMES = (ENV_NAME, ENV_KEY, ENV_JOB, ENV_POLICY)
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 2048
MAX_TEXT = 200
REQUEST_TIMEOUT_SECONDS = 5.0
HEARTBEAT_SECONDS = 2.0
_SOCKET = socket.socket  # the real class, immune to tests replacing socket.socket


class ArbiterError(RuntimeError):
    """The arbiter is unavailable, silent, forged or has closed admission."""


def sign(key: bytes, body: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {k: v for k, v in body.items() if k != "mac"}, sort_keys=True, separators=(",", ":")
    )
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def seal(key: bytes, body: Mapping[str, Any]) -> bytes:
    data = json.dumps({**body, "mac": sign(key, body)}, sort_keys=True, separators=(",", ":"))
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ArbiterError("message exceeds the protocol bound")
    return encoded


def peek(data: bytes) -> dict[str, Any]:
    """Decode without trusting: bounded, strict JSON object."""

    if not data or len(data) > MAX_MESSAGE_BYTES:
        raise ArbiterError("message is empty or exceeds the protocol bound")

    def refuse(_constant: str) -> object:
        raise ValueError("non-finite number")

    try:
        body = json.loads(data.decode("utf-8"), parse_constant=refuse)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ArbiterError("message is not strict JSON") from exc
    if not isinstance(body, dict):
        raise ArbiterError("message is not an object")
    return body


def unseal(key: bytes, data: bytes) -> dict[str, Any]:
    body = peek(data)
    mac = body.get("mac")
    if not isinstance(mac, str) or not hmac.compare_digest(mac, sign(key, body)):
        raise ArbiterError("message is not authenticated by the job key")
    return body


def clip(value: object) -> str:
    return str(value)[:MAX_TEXT]


class ArbiterClient:
    """One gate process's live, acknowledged session with the parent arbiter."""

    def __init__(
        self,
        name: str,
        key: bytes,
        job: str,
        policy: str = "",
        *,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        heartbeat: float | None = HEARTBEAT_SECONDS,
    ) -> None:
        self.job, self.closed_reason = job, ""
        self._key, self._seq, self._lock = key, 0, threading.Lock()
        self._stop = threading.Event()
        self._conn = _SOCKET(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._conn.settimeout(timeout)
        try:
            self._conn.connect("\0" + name)
        except OSError as exc:
            self._conn.close()
            raise ArbiterError(
                f"the parent arbiter cannot be reached: {type(exc).__name__}"
            ) from exc
        try:
            self._call("hello", policy=policy)
        except ArbiterError:
            self._conn.close()
            raise
        if heartbeat:
            threading.Thread(target=self._beat, args=(heartbeat,), daemon=True).start()
        atexit.register(self.close)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, policy: str | None = None
    ) -> ArbiterClient | None:
        """None outside a concurrent job; inside one, any gap or failure raises (no static fallback).

        ``policy`` is the digest of the capacity policy bytes this process actually loaded; it is what
        the parent compares with the plan-pinned digest (the environment's copy is only a default).
        """

        source = os.environ if environ is None else environ
        name = source.get(ENV_NAME)
        if not name:
            return None
        key, job = source.get(ENV_KEY, ""), source.get(ENV_JOB, "")
        try:
            raw = bytes.fromhex(key)
        except ValueError:
            raw = b""
        if len(raw) != 32 or not job:
            raise ArbiterError("the arbiter environment is incomplete or malformed")
        return cls(name, raw, job, policy if policy is not None else source.get(ENV_POLICY, ""))

    def _latch(self, reason: str) -> ArbiterError:
        self.closed_reason = self.closed_reason or reason
        self._stop.set()
        return ArbiterError(self.closed_reason)

    def _call(self, op: str, **fields: object) -> dict[str, Any]:
        with self._lock:
            if self.closed_reason:
                raise ArbiterError(self.closed_reason)
            self._seq += 1
            body = {"v": PROTOCOL_VERSION, "job": self.job, "seq": self._seq, "op": op}
            body.update({k: clip(v) for k, v in fields.items()})
            try:
                self._conn.send(seal(self._key, body))
                reply = unseal(self._key, self._conn.recv(MAX_MESSAGE_BYTES + 1))
            except ArbiterError as exc:
                raise self._latch(f"the parent arbiter sent a bad reply: {exc}") from exc
            except (OSError, TimeoutError) as exc:
                raise self._latch(f"the parent arbiter was lost: {type(exc).__name__}") from exc
            if reply.get("ack") != self._seq or reply.get("job") != self.job:
                raise self._latch("the parent arbiter did not acknowledge the request")
            if reply.get("result") == "closed":
                self.closed_reason = clip(reply.get("reason") or "admission closed by the parent")
                self._stop.set()
            return reply

    def _beat(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self._call("ping")
            except ArbiterError:
                return

    def reserve(self, pair: str) -> tuple[str, str]:
        """("granted" | "denied" | "closed", reason); an unreachable arbiter is "closed"."""

        try:
            reply = self._call("reserve", pair=pair)
        except ArbiterError as exc:
            return "closed", str(exc)
        result = reply.get("result")
        if result not in ("granted", "denied", "closed"):
            return "closed", str(self._latch("the parent arbiter sent an unknown result"))
        return str(result), clip(reply.get("reason", ""))

    def release(self, pair: str) -> None:
        self._call("release", pair=pair)

    def event(self, kind: str, pair: str, reason: str = "") -> None:
        self._call("event", kind=kind, pair=pair, reason=reason)

    def close(self) -> None:
        if self._stop.is_set() and self.closed_reason:
            self._conn.close()
            return
        try:
            self._call("bye")
        except ArbiterError:
            pass
        self._stop.set()
        self._conn.close()


def enroll_production(
    session: Any, *, policy_sha256: str | None, environ: MutableMapping[str, str] | None = None
) -> ArbiterClient | None:
    """Explicit production enrolment of a gate's capacity session in the parent's live book.

    Only the production entry point calls this, with the digest of the policy bytes it parsed.  It
    reads the job identity and key from the environment ONCE, binds that digest, connects, attaches
    the client to the session's admission controller and REMOVES the credentials from the
    environment so no child process (pytest, matrix workers) can inherit them.  Outside a concurrent
    job it returns None; inside one a missing digest or any failure raises, so the gate never runs
    un-enrolled.  A session built anywhere else (a synthetic control, a unit test) never connects.
    """

    source = os.environ if environ is None else environ
    try:
        if source.get(ENV_NAME) and not policy_sha256:
            raise ArbiterError("a concurrent job needs the digest of the policy bytes it parsed")
        client = ArbiterClient.from_environment(source, policy=policy_sha256)
    finally:
        for name in ARBITER_ENV_NAMES:
            source.pop(name, None)
    if client is not None:
        session.admission.arbiter = client
    return client


__all__ = (
    "ARBITER_ENV_NAMES",
    "ENV_JOB",
    "ENV_KEY",
    "ENV_NAME",
    "ENV_POLICY",
    "MAX_MESSAGE_BYTES",
    "ArbiterClient",
    "ArbiterError",
    "peek",
    "seal",
    "sign",
    "unseal",
)
