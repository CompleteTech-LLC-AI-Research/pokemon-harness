"""TCP-backed :class:`SerialBackend` for two-process PyBoy linking.

Milestone 8 of the design doc (:doc:`docs/pyboy_serial_overhaul_design.md`).
Where :class:`CoordinatedBackend` exchanges bits through direct access
to a peer :class:`SerialCore` in the same process, :class:`NetworkBackend`
exchanges bits through a TCP socket so two independent Python processes
(potentially on different machines) can drive linked PyBoys.

Protocol
--------

Byte-oriented, request/response, framed::

    struct EdgeFrame {
        uint8_t opcode;   // 0x10 EDGE_REQ  (master -> slave)
                          // 0x11 EDGE_RESP (slave -> master)
        uint8_t payload;  // bit value: 0 or 1
    };

Versioned TCP peers (after both sides have exchanged ``HELLO``) use the
same payload with a monotonically increasing request identifier appended:
``0x12`` is ``EDGE_REQ_ID`` and ``0x13`` is ``EDGE_RESP_ID``.  The legacy
two-byte frames remain available for unversioned in-process callers.  The
identifier lets a late response be rejected rather than consumed by a later
edge, and lets a duplicate request be replayed without applying the local
serial core twice.

Either side can be master at any moment. The master's ``on_edge`` sends
an ``EDGE_REQ`` and waits for the peer's ``EDGE_RESP``; meanwhile a
background reader thread on both sides demuxes incoming frames. In owner
dispatch mode, when a reader sees ``EDGE_REQ`` (peer is master, we are
slave) it queues the request for the emulator owner to advance our local
:class:`SerialCore` via ``apply_external_edge`` and respond with our
outgoing bit. When it sees ``EDGE_RESP`` (peer is slave responding
to our master request) it hands the bit to the blocking ``on_edge``
waiter via a response queue.

Slave IRQ
---------

When the owner-side dispatch path completes the 8th edge on the local slave
core, it fires the optional ``irq_callback`` so halted code wakes up
(Pokemon's ``halt; wait for serial IRQ`` idiom). Wire this to
``pyboy.mb.cpu.set_interruptflag(INTR_SERIAL)`` on attach.

Threading model
---------------

``on_edge`` blocks for one socket round-trip. The caller is expected
to be PyBoy's main tick thread. The reader thread runs in the
background and uses a response queue to hand back RESP payloads. In
production owner-dispatch mode, REQ frames are handed to the emulator
owner and a response-only worker sends the completed response; no network
thread touches emulator state. A single write-lock ensures response sends
do not collide with ``on_edge``'s REQ-sends on the same socket. The legacy
direct-worker mode remains available for low-level callers that explicitly
do not attach a PyBoy instance.
"""

from __future__ import annotations

import errno  # noqa: F401  (retained facade attribute: network_backend.errno)
import ipaddress  # noqa: F401  (retained facade attribute: network_backend.ipaddress)
import json  # noqa: F401  (retained facade attribute: network_backend.json)
import math  # noqa: F401  (retained facade attribute: network_backend.math)
import queue  # noqa: F401  (retained facade attribute: network_backend.queue)
import select  # noqa: F401  (retained facade attribute: network_backend.select)
import socket
import struct  # noqa: F401  (retained facade attribute: network_backend.struct)
import sys
import threading
import time
from collections import deque  # noqa: F401  (retained facade attribute: network_backend.deque)
from collections.abc import (
    Callable,  # noqa: F401  (retained facade attribute: network_backend.Callable)
    Iterator,  # noqa: F401  (retained facade attribute: network_backend.Iterator)
)
from contextlib import (
    contextmanager,  # noqa: F401  (retained facade attribute: network_backend.contextmanager)
)
from dataclasses import (
    dataclass,  # noqa: F401  (retained facade attribute: network_backend.dataclass)
)
from typing import Any  # noqa: F401  (retained facade attribute: network_backend.Any)

from pokered_harness.link._network_backend_io_mixin import _NetworkBackendIOMixin
from pokered_harness.link._network_backend_lifecycle_mixin import _NetworkBackendLifecycleMixin
from pokered_harness.link._network_backend_owner_mixin import _NetworkBackendOwnerMixin
from pokered_harness.link._network_backend_protocol_mixin import _NetworkBackendProtocolMixin
from pokered_harness.link._network_backend_support import (
    _ACTIVE_EXCHANGE_EDGE_THRESHOLD,  # noqa: F401  (retained facade attribute: network_backend._ACTIVE_EXCHANGE_EDGE_THRESHOLD)
    _ACTIVE_EXCHANGE_GRACE_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._ACTIVE_EXCHANGE_GRACE_SECONDS)
    _ACTIVE_EXCHANGE_REARM_WAIT_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._ACTIVE_EXCHANGE_REARM_WAIT_SECONDS)
    _CONTROL_QUEUE_MAXSIZE,  # noqa: F401  (retained facade attribute: network_backend._CONTROL_QUEUE_MAXSIZE)
    _DEFAULT_ACCEPT_TIMEOUT_SECONDS,
    _DEFAULT_SEND_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._DEFAULT_SEND_TIMEOUT_SECONDS)
    _EDGE_ID_FRAME,  # noqa: F401  (retained facade attribute: network_backend._EDGE_ID_FRAME)
    _EDGE_ID_MAX,  # noqa: F401  (retained facade attribute: network_backend._EDGE_ID_MAX)
    _EDGE_RESPONSE_HISTORY_MAX,  # noqa: F401  (retained facade attribute: network_backend._EDGE_RESPONSE_HISTORY_MAX)
    _EDGE_RESPONSE_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._EDGE_RESPONSE_TIMEOUT_SECONDS)
    _FRAME,  # noqa: F401  (retained facade attribute: network_backend._FRAME)
    _FRAME_READ_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._FRAME_READ_TIMEOUT_SECONDS)
    _LEN,  # noqa: F401  (retained facade attribute: network_backend._LEN)
    _MAX_SERIAL_TRANSCRIPT_ENTRIES,  # noqa: F401  (retained facade attribute: network_backend._MAX_SERIAL_TRANSCRIPT_ENTRIES)
    _OP_EDGE_REQ,  # noqa: F401  (retained facade attribute: network_backend._OP_EDGE_REQ)
    _OP_EDGE_REQ_ID,  # noqa: F401  (retained facade attribute: network_backend._OP_EDGE_REQ_ID)
    _OP_EDGE_RESP,  # noqa: F401  (retained facade attribute: network_backend._OP_EDGE_RESP)
    _OP_EDGE_RESP_ID,  # noqa: F401  (retained facade attribute: network_backend._OP_EDGE_RESP_ID)
    _OP_EXCHANGE,  # noqa: F401  (retained facade attribute: network_backend._OP_EXCHANGE)
    _OP_FRAME_ACK,  # noqa: F401  (retained facade attribute: network_backend._OP_FRAME_ACK)
    _OP_FRAME_DONE,  # noqa: F401  (retained facade attribute: network_backend._OP_FRAME_DONE)
    _OP_FRAME_TICK,  # noqa: F401  (retained facade attribute: network_backend._OP_FRAME_TICK)
    _OP_HELLO,  # noqa: F401  (retained facade attribute: network_backend._OP_HELLO)
    _OP_SYNC,  # noqa: F401  (retained facade attribute: network_backend._OP_SYNC)
    _POST_BYTE_REARM_GRACE_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._POST_BYTE_REARM_GRACE_SECONDS)
    _PROTOCOL_VERSION,  # noqa: F401  (retained facade attribute: network_backend._PROTOCOL_VERSION)
    _READ_POLL_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._READ_POLL_SECONDS)
    _REARM_WAIT_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._REARM_WAIT_SECONDS)
    _ROM_VERSION_CODES,  # noqa: F401  (retained facade attribute: network_backend._ROM_VERSION_CODES)
    _ROM_VERSION_NAMES,  # noqa: F401  (retained facade attribute: network_backend._ROM_VERSION_NAMES)
    _SEND_POLL_SECONDS,  # noqa: F401  (retained facade attribute: network_backend._SEND_POLL_SECONDS)
    NetworkBackendError,
    _coerce_payload,  # noqa: F401  (retained facade attribute: network_backend._coerce_payload)
    _connect_socket,
    _InboundEdge,  # noqa: F401  (retained facade attribute: network_backend._InboundEdge)
    _InboundEdgeQueue,  # noqa: F401  (retained facade attribute: network_backend._InboundEdgeQueue)
    _is_loopback_sockaddr,  # noqa: F401  (retained facade attribute: network_backend._is_loopback_sockaddr)
    _require_timeout,  # noqa: F401  (retained facade attribute: network_backend._require_timeout)
    _socket_family,
    _validate_connected_socket_loopback,  # noqa: F401  (retained facade attribute: network_backend._validate_connected_socket_loopback)
    _validate_id,  # noqa: F401  (retained facade attribute: network_backend._validate_id)
    _validate_optional_timeout,
    _validate_rom_version,  # noqa: F401  (retained facade attribute: network_backend._validate_rom_version)
    validate_loopback_host,
)
from pokered_harness.link.serial_coordinator import (
    SerialOperationGate,  # noqa: F401  (retained facade attribute: network_backend.SerialOperationGate)
)

# Loaded by package name as well as directly; register the loaded module
# under its canonical name first so the mixin/support modules'
# ``import pokered_harness.link.network_backend as _entry`` resolves to this
# same object instead of a second copy.
sys.modules.setdefault("pokered_harness.link.network_backend", sys.modules[__name__])


class NetworkBackend(
    _NetworkBackendLifecycleMixin,
    _NetworkBackendProtocolMixin,
    _NetworkBackendOwnerMixin,
    _NetworkBackendIOMixin,
):
    """Bit-level SerialBackend over a TCP socket.

    Symmetric: both peers instantiate a ``NetworkBackend`` over the
    same socket. Call :meth:`start_receiver` on each side, supplying
    the local :class:`SerialCore` and an IRQ callback, to enable
    slave-side behavior (incoming EDGE_REQ drives our local core).
    Call :meth:`stop` to tear down.
    """

    @classmethod
    def listen(
        cls,
        port: int,
        *,
        host: str = "127.0.0.1",
        backlog: int = 1,
        local_rom_version: str | None = None,
        accept_timeout_s: float = _DEFAULT_ACCEPT_TIMEOUT_SECONDS,
        cancel_event: threading.Event | None = None,
    ) -> tuple[NetworkBackend, socket.socket]:
        """Bind to ``(host, port)`` and wait until a peer connects.

        Returns ``(backend, listener_sock)``; the caller keeps the
        listener socket to close later if needed. A fresh accepted
        socket is wrapped by the backend. ``accept_timeout_s`` and
        ``cancel_event`` make the accept cancellable for lifecycle owners.
        The accept deadline is always finite; cancellation may end it sooner.
        """
        normalized_host = validate_loopback_host(host)
        accept_timeout_s = _validate_optional_timeout(accept_timeout_s, "accept_timeout_s")
        if accept_timeout_s is None:
            raise ValueError("accept_timeout_s must be finite and positive")
        family = _socket_family(normalized_host)
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_address = (
            (normalized_host, port, 0, 0) if family == socket.AF_INET6 else (normalized_host, port)
        )
        conn: socket.socket | None = None
        try:
            listener.bind(bind_address)
            listener.listen(backlog)
            deadline = time.monotonic() + accept_timeout_s
            while conn is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("listener accept cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkBackendError(
                        f"listener accept timed out after {accept_timeout_s:g}s"
                    )
                listener.settimeout(min(0.25, remaining))
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            backend = cls(conn, local_rom_version=local_rom_version)
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            try:
                listener.close()
            except OSError:
                pass
            raise
        return backend, listener

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        local_rom_version: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> NetworkBackend:
        """Open an outbound connection to a ``listen``-ing peer."""
        normalized_host = validate_loopback_host(host)
        sock = _connect_socket(normalized_host, port, timeout_s, cancel_event)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock, local_rom_version=local_rom_version)

    @classmethod
    def pair(cls) -> tuple[NetworkBackend, NetworkBackend]:
        """Build a :func:`socket.socketpair` pair of backends — useful
        for in-process testing without real TCP."""
        a, b = socket.socketpair()
        return cls(a), cls(b)


__all__ = [
    "NetworkBackend",
    "NetworkBackendError",
]
