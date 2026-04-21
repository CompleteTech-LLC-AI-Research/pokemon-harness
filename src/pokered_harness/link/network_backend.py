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

Either side can be master at any moment. The master's ``on_edge`` sends
an ``EDGE_REQ`` and waits for the peer's ``EDGE_RESP``; meanwhile a
background reader thread on both sides demuxes incoming frames. When a
reader sees ``EDGE_REQ`` (peer is master, we are slave) it advances our
local :class:`SerialCore` via ``apply_external_edge`` and responds with
our outgoing bit. When it sees ``EDGE_RESP`` (peer is slave responding
to our master request) it hands the bit to the blocking ``on_edge``
waiter via a response queue.

Slave IRQ
---------

When the reader-thread path completes the 8th edge on the local slave
core, it fires the optional ``irq_callback`` so halted code wakes up
(Pokemon's ``halt; wait for serial IRQ`` idiom). Wire this to
``pyboy.mb.cpu.set_interruptflag(INTR_SERIAL)`` on attach.

Threading model
---------------

``on_edge`` blocks for one socket round-trip. The caller is expected
to be PyBoy's main tick thread. The reader thread runs in the
background and uses a response queue to hand back RESP payloads;
REQ frames are processed inline. A single write-lock ensures the
reader's RESP-sends don't collide with ``on_edge``'s REQ-sends on the
same socket.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading
from typing import Callable, Optional


# Opcodes:
#   EDGE_REQ  = master → slave: "here's my outgoing bit"
#   EDGE_RESP = slave → master: "here's my outgoing bit, bit just applied"
#
# Using two opcodes instead of one lets a reader thread on each side
# demux incoming frames without knowing the current role — peers can
# freely swap master/slave between transfers (which Pokemon's
# handshake does during nibble-sync).
_OP_EDGE_REQ: int = 0x10
_OP_EDGE_RESP: int = 0x11

_FRAME = struct.Struct(">BB")  # opcode, payload


class NetworkBackendError(RuntimeError):
    """Wraps socket errors + protocol errors from :class:`NetworkBackend`."""


class NetworkBackend:
    """Bit-level SerialBackend over a TCP socket.

    Symmetric: both peers instantiate a ``NetworkBackend`` over the
    same socket. Call :meth:`start_receiver` on each side, supplying
    the local :class:`SerialCore` and an IRQ callback, to enable
    slave-side behavior (incoming EDGE_REQ drives our local core).
    Call :meth:`stop` to tear down.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._write_lock = threading.Lock()
        # Responses from peer (when we're master) land here.
        self._resp_queue: queue.Queue[int] = queue.Queue()
        self._closed = False
        # Slave-mode config — set by start_receiver.
        self._local_core: Optional[object] = None
        self._irq_callback: Optional[Callable[[], None]] = None
        self._reader: Optional[threading.Thread] = None

    @classmethod
    def listen(
        cls,
        port: int,
        *,
        host: str = "127.0.0.1",
        backlog: int = 1,
    ) -> tuple["NetworkBackend", socket.socket]:
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
    def connect(
        cls, host: str, port: int, *, timeout_s: float = 10.0
    ) -> "NetworkBackend":
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

    # --- slave-side receiver ------------------------------------------

    def start_receiver(
        self,
        local_core: object,
        irq_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """Start the reader thread.

        When an incoming ``EDGE_REQ`` arrives (the peer is acting as
        master), apply the bit to ``local_core`` via
        ``apply_external_edge`` and respond with our
        ``peek_out_bit()``. If ``apply_external_edge`` returns True
        (8th-edge completion) and ``irq_callback`` is provided, the
        callback fires so halted slave code wakes.

        ``EDGE_RESP`` frames (responses to our own master-side
        ``on_edge`` requests) are put on the response queue for the
        blocking ``on_edge`` call to pick up.
        """
        if self._reader is not None:
            raise RuntimeError("receiver already started")
        self._local_core = local_core
        self._irq_callback = irq_callback
        self._reader = threading.Thread(
            target=self._reader_loop, name="NetworkBackend.reader", daemon=True
        )
        self._reader.start()

    # --- SerialBackend.on_edge (master path) --------------------------

    def on_edge(self, our_bit: int, our_role: int) -> int:
        """Master-mode: send ``our_bit`` as EDGE_REQ, wait for EDGE_RESP.

        Blocks until the peer's reader thread responds. Timeout raises
        :class:`NetworkBackendError`. If the reader on our own side
        isn't running yet, incoming responses will be lost — callers
        must call :meth:`start_receiver` before master-mode transfers.
        """
        frame_out = _FRAME.pack(_OP_EDGE_REQ, our_bit & 1)
        try:
            with self._write_lock:
                if self._closed:
                    raise NetworkBackendError("backend closed")
                self._sock.sendall(frame_out)
        except OSError as exc:
            raise NetworkBackendError(
                f"failed to send EDGE_REQ: {exc}"
            ) from exc
        try:
            return self._resp_queue.get(timeout=10.0)
        except queue.Empty as exc:
            raise NetworkBackendError(
                "no EDGE_RESP from peer within 10s"
            ) from exc

    # --- lifecycle ----------------------------------------------------

    def close(self) -> None:
        self.stop()

    def stop(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    # --- internals ----------------------------------------------------

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                frame = self._recv_exactly(2)
                opcode, payload = _FRAME.unpack(frame)
                if opcode == _OP_EDGE_REQ:
                    self._handle_edge_req(payload & 1)
                elif opcode == _OP_EDGE_RESP:
                    self._resp_queue.put(payload & 1)
                else:
                    # Unknown opcode — drop. A strict implementation
                    # would raise and tear down; we log-and-continue
                    # to keep the trade robust to transient noise.
                    continue
        except NetworkBackendError:
            # Peer closed or malformed frame. Surface via closed flag;
            # any pending on_edge waiter will time out.
            self._closed = True
        except OSError:
            self._closed = True

    def _handle_edge_req(self, peer_bit: int) -> None:
        """Peer is master, we are slave. Apply edge to local_core,
        respond with our bit."""
        core = self._local_core
        if (
            core is not None
            and getattr(core, "transfer_enabled", 0)
            and not getattr(core, "internal_clock", 0)
        ):
            our_bit = core.peek_out_bit()
            completed = core.apply_external_edge(peer_bit)
        else:
            # Not armed as slave — return pull-up (matches NullBackend
            # semantics).
            our_bit = 1
            completed = False
        try:
            with self._write_lock:
                if not self._closed:
                    self._sock.sendall(_FRAME.pack(_OP_EDGE_RESP, our_bit & 1))
        except OSError:
            self._closed = True
            return
        if completed and self._irq_callback is not None:
            try:
                self._irq_callback()
            except Exception:
                # IRQ callback errors shouldn't kill the reader thread.
                pass

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
