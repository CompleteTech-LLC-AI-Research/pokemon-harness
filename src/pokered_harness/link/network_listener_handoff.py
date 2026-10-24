"""Client side of the owner-held listening-socket handoff used by concurrent runtime jobs.

A job may be given ``POKERED_LISTENER_HANDOFF``: the name of an abstract unix socket on which its
owner (the orchestrator, or a test) keeps real bound, listening loopback sockets.  A listener asks
for exactly ``(host, port)``, receives the open socket by SCM_RIGHTS, validates it and acknowledges;
the owner closes its own copy only after that acknowledgment, so the port is never unbound between
"reserved" and "listening" and no competing binder can slip in.  Without the variable nothing
changes and callers bind normally; with it, an endpoint the owner did not lease is refused
(``HandoffError``) and never bound in the ordinary way.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from collections.abc import Callable
from typing import Any

from .listener_handoff_custody import INT_SIZE, ReceiveBook, ReceiveRecord

HANDOFF_ENV = "POKERED_LISTENER_HANDOFF"
_SOCKET = socket.socket  # the real class: tests may replace socket.socket afterwards
HANDOFF_TIMEOUT_SECONDS = 10.0
MESSAGE_LIMIT = 4096
_CLOCK = time.monotonic
_BOOK = ReceiveBook()


class HandoffError(RuntimeError):
    """A handed-over socket failed validation, or the owner violated the protocol."""


def broker_name() -> str | None:
    return os.environ.get(HANDOFF_ENV) or None


def _record() -> ReceiveRecord:
    buffer_bytes = socket.CMSG_SPACE(INT_SIZE)
    try:
        return _BOOK.reserve(buffer_bytes // INT_SIZE, _CLOCK() + HANDOFF_TIMEOUT_SECONDS, _CLOCK)
    except RuntimeError as error:
        raise HandoffError(str(error)) from error


def _open(record: ReceiveRecord, name: str) -> None:
    record.remaining()
    record.control_state = "constructing"
    record.control = _SOCKET(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    record.control_state = "open"
    record.effect("connect", "\0" + name)


def _connect(name: str) -> socket.socket:
    record = _record()
    try:
        _open(record, name)
        return record.transfer_control()  # original raw object; ordinary transfer only
    except BaseException as error:
        record.fail(error)
        raise


def _request(body: dict[str, Any]) -> bytes:
    record = _record()
    try:
        _open(record, broker_name())
        record.send(json.dumps(body).encode("ascii"))
        reply = record.effect("recv", MESSAGE_LIMIT)
        record.close_control()
        record.book.finish(record)
        return reply
    except BaseException as error:
        record.fail(error)
        raise


def lease_port(host: str = "127.0.0.1") -> int | None:
    """Ask the owner to keep a listening port; None only without an owner."""
    if broker_name() is None:
        return None
    reply = json.loads(_request({"op": "lease", "host": host}))
    if (
        not isinstance(reply, dict)
        or reply.get("ok") is not True
        or type(reply.get("port")) is not int
    ):
        raise HandoffError(f"the listener owner refused a lease: {reply!r}")
    return reply["port"]


def release_port(port: int) -> None:
    if broker_name() is not None:
        _request({"op": "release", "port": port})


def validate_listener(listener: socket.socket, host: str, port: int) -> None:
    """A handed-over socket must be a listening loopback TCP socket on exactly (host, port)."""
    _validate_listener(listener, host, port, lambda: None)


def _validate_listener(listener: Any, host: str, port: int, checkpoint: Any) -> None:
    """A handed-over socket must be a listening loopback TCP socket on exactly (host, port)."""

    if listener.type != socket.SOCK_STREAM or listener.family not in (
        socket.AF_INET,
        socket.AF_INET6,
    ):
        raise HandoffError("the handed-over socket is not a TCP stream socket")
    checkpoint()
    if not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
        raise HandoffError("the handed-over socket is not listening")
    checkpoint()
    address = listener.getsockname()
    if address[1] != port:
        raise HandoffError("the handed-over socket is bound to a different port")
    local = ipaddress.ip_address(address[0])
    if not local.is_loopback or local.version != ipaddress.ip_address(host).version:
        raise HandoffError("the handed-over socket is not bound to the requested loopback host")


def take_listener(
    host: str, port: int, *, socket_class: Callable[..., Any] = _SOCKET
) -> socket.socket | None:
    """Receive and acknowledge the leased listening socket for (host, port).

    None only when NO owner is configured (standalone use).  With an owner configured, anything
    but a validated and acknowledged leased socket raises ``HandoffError``: a configured parallel
    job never falls back to an ordinary bind, and an endpoint it was not leased is refused.
    """

    name = broker_name()
    if name is None:
        return None
    if type(port) is not int or port <= 0:
        raise HandoffError(
            f"a configured parallel job can only listen on a leased port, not {port!r}"
        )
    record = _record()
    validating = False
    try:
        _open(record, name)
        record.send(json.dumps({"op": "take", "host": host, "port": port}).encode("ascii"))
        record.phase = "recv_attempted"
        record.raw_tuple = record.effect("recvmsg", MESSAGE_LIMIT, socket.CMSG_SPACE(INT_SIZE))
        record.phase = "raw_returned"
        try:
            data = record.classify()
            reply = json.loads(data)
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                raise ValueError("listener owner refused the handoff")
        except ValueError as error:
            raise HandoffError(str(error)) from error
        listener = record.wrap(socket_class)
        validating = True
        record.remaining()
        _validate_listener(listener, host, port, record.remaining)
        validating = False
        record.phase = "ack_attempted"
        record.send(b'{"op":"ack"}')
        record.phase = "acknowledged"
        record.close_control()  # ACK never discards unresolved local control custody
        return record.transfer_listener()
    except BaseException as error:
        if record.phase == "recv_attempted" and record.raw_tuple is None:
            record.uncertain("listener receive return unproven")
        record.fail(error, nack=validating)
        raise


__all__ = (
    "HANDOFF_ENV",
    "HandoffError",
    "broker_name",
    "lease_port",
    "release_port",
    "take_listener",
    "validate_listener",
)
