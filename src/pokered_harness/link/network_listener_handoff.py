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
from collections.abc import Callable
from typing import Any

HANDOFF_ENV = "POKERED_LISTENER_HANDOFF"
_SOCKET = socket.socket  # the real class: tests may replace socket.socket afterwards
HANDOFF_TIMEOUT_SECONDS = 10.0
MESSAGE_LIMIT = 4096


class HandoffError(RuntimeError):
    """A handed-over socket failed validation, or the owner violated the protocol."""


def broker_name() -> str | None:
    return os.environ.get(HANDOFF_ENV) or None


def _connect(name: str) -> socket.socket:
    conn = _SOCKET(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    conn.settimeout(HANDOFF_TIMEOUT_SECONDS)
    try:
        conn.connect("\0" + name)
    except OSError:
        conn.close()
        raise
    return conn


def lease_port(host: str = "127.0.0.1") -> int | None:
    """Ask the owner to bind and keep a listening port; None when no owner is configured."""

    name = broker_name()
    if name is None:
        return None
    with _connect(name) as conn:
        conn.send(json.dumps({"op": "lease", "host": host}).encode("ascii"))
        reply = json.loads(conn.recv(MESSAGE_LIMIT))
    if (
        not isinstance(reply, dict)
        or reply.get("ok") is not True
        or type(reply.get("port")) is not int
    ):
        raise HandoffError(f"the listener owner refused a lease: {reply!r}")
    return reply["port"]


def release_port(port: int) -> None:
    name = broker_name()
    if name is None:
        return
    with _connect(name) as conn:
        conn.send(json.dumps({"op": "release", "port": port}).encode("ascii"))
        conn.recv(MESSAGE_LIMIT)


def validate_listener(listener: socket.socket, host: str, port: int) -> None:
    """A handed-over socket must be a listening loopback TCP socket on exactly (host, port)."""

    if listener.type != socket.SOCK_STREAM or listener.family not in (
        socket.AF_INET,
        socket.AF_INET6,
    ):
        raise HandoffError("the handed-over socket is not a TCP stream socket")
    if not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
        raise HandoffError("the handed-over socket is not listening")
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
    with _connect(name) as conn:
        conn.send(json.dumps({"op": "take", "host": host, "port": port}).encode("ascii"))
        data, descriptors, _flags, _address = socket.recv_fds(conn, MESSAGE_LIMIT, 1)
        reply = json.loads(data)
        if not descriptors:
            raise HandoffError(f"the listener owner did not lease {host}:{port}: {reply!r}")
        listener = socket_class(fileno=descriptors[0])
        try:
            validate_listener(listener, host, port)
        except BaseException:
            conn.send(b'{"op":"nack"}')
            listener.close()
            raise
        conn.send(b'{"op":"ack"}')
        return listener


__all__ = (
    "HANDOFF_ENV",
    "HandoffError",
    "broker_name",
    "lease_port",
    "release_port",
    "take_listener",
    "validate_listener",
)
