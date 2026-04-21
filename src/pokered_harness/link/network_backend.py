"""TCP-backed :class:`SerialBackend` for two-process PyBoy linking.

Milestone 8 of the design doc (:doc:`docs/pyboy_serial_overhaul_design.md`).
Where :class:`CoordinatedBackend` exchanges bits through direct access
to a peer :class:`SerialCore` in the same process, :class:`NetworkBackend`
exchanges bits through a TCP socket so two independent Python processes
(potentially on different machines) can drive linked PyBoys.

Protocol
--------

Byte-oriented, half-duplex, framed::

    struct EdgeFrame {
        uint8_t opcode;   // 0x10 = EDGE request / reply
        uint8_t payload;  // bit value: 0 or 1; no other bits used
    };

Each side, at each master-mode edge, sends an ``EDGE`` frame with its
outgoing bit and blocks reading the peer's ``EDGE`` frame carrying the
peer's outgoing bit. The peer's side-process does the same; both bits
are exchanged symmetrically. Slave-mode edges don't use the backend
(they're driven by the coordinator / peer's master).

Slave IRQ semantics
-------------------

On the slave side the peer-driven completion needs to raise
``INTR_SERIAL`` on the slave's CPU so halted game code wakes (matches
the in-process :class:`CoordinatedBackend` callback). For the network
case, the slave detects completion locally — when its CPU's own
``serial.tick`` returns True — which already fires the IRQ via the
motherboard, so no extra hook is needed.

*However*, the slave's transfer is advanced externally (by edges
pushed through its backend from the peer process's master). The slave
process's PyBoy must still be running so its CPU can read the
completed SB. This means both processes must tick their PyBoys in
parallel while the TCP exchange happens.

Threading model
---------------

:class:`NetworkBackend.on_edge` blocks for one TCP round-trip. The
caller is expected to be PyBoy's main tick thread for that side —
that thread blocks briefly on the socket, then resumes. The *other*
process's PyBoy thread must be alive and consuming to unblock us.

Typical driver pattern::

    link = PyBoyLinkSession.listen(port=9999)   # blocks until peer
    link.attach(my_pyboy)
    while not done:
        my_pyboy.tick(1)    # one side's frame; on_edge blocks on peer
"""

from __future__ import annotations

import socket
import struct
import threading
from typing import Optional


_OP_EDGE = 0x10  # bit-exchange request/reply
_FRAME = struct.Struct(">BB")  # opcode, payload


class NetworkBackendError(RuntimeError):
    """Wraps socket errors + protocol errors from :class:`NetworkBackend`."""


class NetworkBackend:
    """Bit-level SerialBackend over a TCP socket.

    Symmetric: both peers instantiate a ``NetworkBackend`` over the
    same socket. Each master-mode edge sends one bit and reads one bit
    back. Slave-mode edges are driven by the peer and don't touch this
    backend.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        # Serialize access so the main tick thread and any background
        # reader thread can coexist (currently only the main thread
        # uses the socket, but keep the lock to future-proof).
        self._lock = threading.Lock()

    @classmethod
    def listen(cls, port: int, *, host: str = "127.0.0.1", backlog: int = 1) -> tuple["NetworkBackend", socket.socket]:
        """Bind to ``(host, port)`` and block until a peer connects.

        Returns ``(backend, listener_sock)``; the caller keeps the
        listener socket to close later if needed. A fresh accepted
        socket is wrapped by the backend.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(backlog)
        conn, _ = listener.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(conn), listener

    @classmethod
    def connect(cls, host: str, port: int, *, timeout_s: float = 10.0) -> "NetworkBackend":
        """Open an outbound connection to a ``listen``-ing peer."""
        sock = socket.create_connection((host, port), timeout=timeout_s)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock)

    @classmethod
    def pair(cls) -> tuple["NetworkBackend", "NetworkBackend"]:
        """Build a :func:`socket.socketpair` pair of backends — useful
        for in-process testing without real TCP."""
        a, b = socket.socketpair()
        return cls(a), cls(b)

    # --- SerialBackend surface -------------------------------------

    def on_edge(self, our_bit: int, our_role: int) -> int:
        """Send ``our_bit`` to the peer and read peer's bit.

        Blocks for one socket round-trip. If either send or receive
        raises (peer hung up, etc.), wraps in :class:`NetworkBackendError`.
        """
        frame_out = _FRAME.pack(_OP_EDGE, our_bit & 1)
        with self._lock:
            try:
                self._sock.sendall(frame_out)
                frame_in = self._recv_exactly(2)
            except OSError as exc:
                raise NetworkBackendError(
                    f"network edge-exchange failed: {exc}"
                ) from exc
        opcode, payload = _FRAME.unpack(frame_in)
        if opcode != _OP_EDGE:
            raise NetworkBackendError(
                f"unexpected opcode from peer: 0x{opcode:02x}"
            )
        return payload & 1

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    # --- internals --------------------------------------------------

    def _recv_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise NetworkBackendError("peer closed socket mid-frame")
            buf.extend(chunk)
        return bytes(buf)


__all__ = [
    "NetworkBackend",
    "NetworkBackendError",
]
