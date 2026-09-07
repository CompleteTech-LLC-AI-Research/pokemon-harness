"""Remote serial-link transport for two-agent paired play.

The in-process :class:`LinkTransport` in ``transport.py`` is a byte-queue
used by the single-process :class:`LinkPair`. That model doesn't fit two
independent MCP processes (one PyBoy + one session per process) each
wanting to trade/battle with the other over a cable. This module is the
remote counterpart — a socket-level peer-to-peer RPC channel that the
:class:`SerialBridge` can call into when its hooks fire.

Wire protocol
-------------

All frames are length-prefixed::

    +--------+------------------+
    | 4 B    | payload          |
    | be u32 |                  |
    +--------+------------------+

The payload's first byte is an opcode:

``0x01 HELLO``
    Body: ``rom_version`` as a length-prefixed UTF-8 string. Sent by
    the peer that calls :meth:`TcpSerialLink.connect`; the listener
    replies with its own HELLO. Both peers learn the peer's ROM
    version (``"red"`` / ``"blue"`` / ``"yellow"``) which
    :class:`SerialBridge` uses for cross-version symbol address
    translation.

``0x10 EXCHANGE``
    Body: ``kind`` (length-prefixed ASCII) + ``data`` (length-prefixed
    bytes). The two peers are expected to both send an EXCHANGE with
    matching ``kind`` values; each side's :meth:`SerialLink.exchange`
    call returns the peer's data bytes.

``0xFE BYE``
    Peer gracefully disconnects.

Matching is FIFO within a given ``kind`` — important because the game
may issue ``Serial_ExchangeBytes`` multiple times during one trade and
we can't rely on strict one-at-a-time pairing if either peer runs a
few frames ahead.

Threading model
---------------

:class:`TcpSerialLink` spins a background reader thread that
deserializes inbound frames and routes ``EXCHANGE`` payloads into
per-kind :class:`queue.Queue` instances. The main thread (where the
emulator and :class:`SerialBridge` hooks run) calls
:meth:`SerialLink.exchange`, which synchronously writes an outbound
frame and blocks on the per-kind queue for the peer's matching frame.

All socket writes are serialized behind a single ``_write_lock`` so the
main thread and the reader thread (which only reads) never collide on
the socket.

Timeouts
--------

:meth:`SerialLink.exchange` accepts a ``timeout_ms`` argument. On
timeout the call raises :class:`SerialLinkTimeout` and the bridge can
decide whether to treat that as a "cable disconnected" hardware error
(which pret's ``Serial_*`` code handles gracefully) or to abort.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol


# --- opcodes ---------------------------------------------------------------

OP_HELLO: int = 0x01
OP_EXCHANGE: int = 0x10
OP_BYE: int = 0xFE


# --- exceptions ------------------------------------------------------------


class SerialLinkError(RuntimeError):
    """Base class for all serial-link errors."""


class SerialLinkClosed(SerialLinkError):
    """Peer disconnected or the link was closed before the operation completed."""


class SerialLinkTimeout(SerialLinkError):
    """:meth:`SerialLink.exchange` blocked past the configured deadline."""


class SerialLinkProtocolError(SerialLinkError):
    """Received a malformed frame or an unexpected opcode."""


class _ClosedSignal:
    """Private queue marker used to wake exchanges during link teardown."""


_CLOSED_SIGNAL = _ClosedSignal()


# --- Protocol --------------------------------------------------------------


class SerialLink(Protocol):
    """Abstraction over the peer serial channel.

    The :class:`SerialBridge` talks to this instead of reading peer
    memory directly. Concrete implementations: :class:`TcpSerialLink`
    for two-process setups; an in-process shim can implement the same
    surface for unit tests without spinning sockets.
    """

    @property
    def connected(self) -> bool: ...

    @property
    def peer_rom_version(self) -> str:
        """``"red"`` / ``"blue"`` / ``"yellow"`` as the peer announced
        in its HELLO. Raises :class:`SerialLinkError` if HELLO didn't
        complete."""
        ...

    def exchange(
        self, kind: str, my_bytes: bytes, *, timeout_ms: int = 5000
    ) -> bytes:
        """Send ``my_bytes`` tagged with ``kind`` and block until peer
        sends a matching EXCHANGE for the same ``kind``. Returns peer's
        bytes.

        FIFO within kind: two rapid back-to-back exchanges of the same
        kind pair up in order.
        """
        ...

    def close(self) -> None: ...


# --- wire helpers ----------------------------------------------------------


def _pack_lp_str(s: str) -> bytes:
    """Length-prefixed UTF-8 string (1-byte length, up to 255 bytes)."""
    enc = s.encode("utf-8")
    if len(enc) > 0xFF:
        raise ValueError(f"string too long for length-prefix: {len(enc)}")
    return bytes([len(enc)]) + enc


def _pack_lp_bytes(b: bytes) -> bytes:
    """Length-prefixed byte blob (2-byte length, up to 65535 bytes)."""
    if len(b) > 0xFFFF:
        raise ValueError(f"blob too long for length-prefix: {len(b)}")
    return struct.pack(">H", len(b)) + bytes(b)


def _read_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes from ``sock`` or raise
    :class:`SerialLinkClosed` if the peer hangs up early."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SerialLinkClosed("peer closed socket mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def _read_lp_str(data: memoryview, offset: int) -> tuple[str, int]:
    length = data[offset]
    offset += 1
    s = bytes(data[offset : offset + length]).decode("utf-8")
    return s, offset + length


def _read_lp_bytes(data: memoryview, offset: int) -> tuple[bytes, int]:
    (length,) = struct.unpack(">H", data[offset : offset + 2])
    offset += 2
    b = bytes(data[offset : offset + length])
    return b, offset + length


# --- TCP implementation ----------------------------------------------------


@dataclass
class _Hello:
    rom_version: str


class TcpSerialLink:
    """A :class:`SerialLink` over a single TCP connection.

    Construct via the :meth:`connect` (client) or :meth:`listen` (server)
    classmethods — the raw ``__init__`` takes an already-connected
    socket and is primarily used by the two factory methods and by
    tests that want to pre-create a pair of in-memory socket ends.
    """

    def __init__(self, sock: socket.socket, local_rom_version: str) -> None:
        self._sock = sock
        self._local_rom_version = local_rom_version
        self._peer_rom_version: str | None = None
        self._closed = False

        self._write_lock = threading.Lock()
        # Per-kind inbound queue; created lazily when first message of
        # that kind arrives or is awaited.
        self._inbound: dict[str, queue.Queue[bytes | _ClosedSignal]] = defaultdict(queue.Queue)
        self._inbound_lock = threading.Lock()
        self._reader_exc: Exception | None = None
        self._hello_received = threading.Event()

        self._reader = threading.Thread(
            target=self._reader_loop, name="TcpSerialLink.reader", daemon=True
        )
        self._reader.start()

        # Send our HELLO. The reader thread on the other side will
        # capture the peer's HELLO into ``_peer_rom_version``.
        self._send_frame(bytes([OP_HELLO]) + _pack_lp_str(local_rom_version))

    # --- construction --------------------------------------------------

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        local_rom_version: str,
        *,
        timeout_s: float = 10.0,
    ) -> "TcpSerialLink":
        """Open an outbound connection to a peer that is
        :meth:`listen`-ing on ``(host, port)``."""
        sock = socket.create_connection((host, port), timeout=timeout_s)
        sock.settimeout(None)  # blocking reads in reader thread
        return cls(sock, local_rom_version)

    @classmethod
    def listen(
        cls, port: int, local_rom_version: str, *, host: str = "127.0.0.1"
    ) -> "TcpSerialLink":
        """Bind ``(host, port)`` and block until a peer connects, then
        return the established link."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(1)
        try:
            conn, _peer_addr = listener.accept()
        finally:
            listener.close()
        conn.settimeout(None)
        return cls(conn, local_rom_version)

    # --- SerialLink surface -------------------------------------------

    @property
    def connected(self) -> bool:
        return not self._closed and self._reader.is_alive()

    @property
    def peer_rom_version(self) -> str:
        # If HELLO hasn't arrived yet (e.g. first call racing startup),
        # block briefly for it.
        if not self._hello_received.wait(timeout=5.0):
            raise SerialLinkError("peer never sent HELLO")
        if self._peer_rom_version is None:
            raise SerialLinkError("peer HELLO arrived without rom_version")
        return self._peer_rom_version

    def exchange(
        self, kind: str, my_bytes: bytes, *, timeout_ms: int = 5000
    ) -> bytes:
        if self._closed:
            raise SerialLinkClosed("link is closed")
        self._raise_if_reader_failed()

        frame = bytes([OP_EXCHANGE]) + _pack_lp_str(kind) + _pack_lp_bytes(my_bytes)
        self._send_frame(frame)

        with self._inbound_lock:
            q = self._inbound[kind]
        try:
            payload = q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty as exc:
            self._raise_if_reader_failed()
            raise SerialLinkTimeout(
                f"no peer EXCHANGE for kind={kind!r} within {timeout_ms}ms"
            ) from exc
        if payload is _CLOSED_SIGNAL:
            self._raise_if_reader_failed()
            raise SerialLinkClosed(f"link closed while waiting for kind={kind!r}")
        return payload

    def close(self) -> None:
        with self._write_lock:
            if self._closed:
                return
            try:
                self._send_frame_locked(bytes([OP_BYE]))
            except Exception:
                pass
            self._closed = True
        self._wake_inbound_waiters()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    # --- reader thread --------------------------------------------------

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                header = _read_exactly(self._sock, 4)
                (size,) = struct.unpack(">I", header)
                if size == 0:
                    raise SerialLinkProtocolError("zero-length frame")
                if size > (1 << 20):
                    raise SerialLinkProtocolError(f"frame too large: {size}")
                body = _read_exactly(self._sock, size)
                self._dispatch(memoryview(body))
        except SerialLinkClosed:
            # Clean peer close; no error, just mark closed.
            self._closed = True
        except Exception as exc:  # noqa: BLE001
            self._reader_exc = exc
            self._closed = True
        finally:
            self._wake_inbound_waiters()
            self._hello_received.set()  # unblock peer_rom_version waiters

    def _dispatch(self, body: memoryview) -> None:
        opcode = body[0]
        if opcode == OP_HELLO:
            rom_version, _ = _read_lp_str(body, 1)
            self._peer_rom_version = rom_version
            self._hello_received.set()
        elif opcode == OP_EXCHANGE:
            kind, offset = _read_lp_str(body, 1)
            payload, _ = _read_lp_bytes(body, offset)
            with self._inbound_lock:
                q = self._inbound[kind]
            q.put(payload)
        elif opcode == OP_BYE:
            self._closed = True
            self._wake_inbound_waiters()
        else:
            raise SerialLinkProtocolError(f"unknown opcode 0x{opcode:02x}")

    # --- helpers --------------------------------------------------------

    def _send_frame(self, payload: bytes) -> None:
        with self._write_lock:
            self._send_frame_locked(payload)

    def _send_frame_locked(self, payload: bytes) -> None:
        if self._closed:
            raise SerialLinkClosed("link is closed")
        header = struct.pack(">I", len(payload))
        self._sock.sendall(header + payload)

    def _wake_inbound_waiters(self) -> None:
        with self._inbound_lock:
            for q in self._inbound.values():
                q.put_nowait(_CLOSED_SIGNAL)

    def _raise_if_reader_failed(self) -> None:
        if self._reader_exc is not None:
            raise SerialLinkError(
                f"reader thread failed: {type(self._reader_exc).__name__}: "
                f"{self._reader_exc}"
            ) from self._reader_exc


# --- in-process loopback (for unit tests) ---------------------------------


class InProcessSerialLink:
    """A :class:`SerialLink` for single-process tests.

    Pair of ``InProcessSerialLink`` instances exchange bytes through
    shared queues without any socket I/O. :meth:`pair` creates the
    pair and wires them together. Useful when testing the bridge
    semantics without the socket/threading complications of
    :class:`TcpSerialLink`.
    """

    def __init__(
        self,
        local_rom_version: str,
        peer_rom_version: str,
        outgoing: dict[str, queue.Queue[bytes]],
        incoming: dict[str, queue.Queue[bytes]],
        locks: tuple[threading.Lock, threading.Lock],
    ) -> None:
        self._local_rom = local_rom_version
        self._peer_rom = peer_rom_version
        self._out = outgoing
        self._in = incoming
        self._out_lock, self._in_lock = locks
        self._closed = False

    @classmethod
    def pair(
        cls, a_rom_version: str, b_rom_version: str
    ) -> tuple["InProcessSerialLink", "InProcessSerialLink"]:
        a_to_b: dict[str, queue.Queue[bytes]] = defaultdict(queue.Queue)
        b_to_a: dict[str, queue.Queue[bytes]] = defaultdict(queue.Queue)
        a_lock = threading.Lock()
        b_lock = threading.Lock()
        a = cls(a_rom_version, b_rom_version, a_to_b, b_to_a, (a_lock, b_lock))
        b = cls(b_rom_version, a_rom_version, b_to_a, a_to_b, (b_lock, a_lock))
        return a, b

    @property
    def connected(self) -> bool:
        return not self._closed

    @property
    def peer_rom_version(self) -> str:
        return self._peer_rom

    def exchange(
        self, kind: str, my_bytes: bytes, *, timeout_ms: int = 5000
    ) -> bytes:
        if self._closed:
            raise SerialLinkClosed("link is closed")
        with self._out_lock:
            self._out[kind].put(bytes(my_bytes))
        with self._in_lock:
            q = self._in[kind]
        try:
            return q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty as exc:
            raise SerialLinkTimeout(
                f"no peer EXCHANGE for kind={kind!r} within {timeout_ms}ms"
            ) from exc

    def close(self) -> None:
        self._closed = True


__all__ = [
    "InProcessSerialLink",
    "SerialLink",
    "SerialLinkClosed",
    "SerialLinkError",
    "SerialLinkProtocolError",
    "SerialLinkTimeout",
    "TcpSerialLink",
    "OP_BYE",
    "OP_EXCHANGE",
    "OP_HELLO",
]
