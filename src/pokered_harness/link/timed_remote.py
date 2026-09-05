"""Bounded, unauthenticated local transport for revision-three timed sessions.

Factories own their sockets, negotiate before starting a reader, and return
unattached endpoints. Emulator attachment and control servicing belong to the
attaching thread. Nonces separate epochs; they do not authenticate either peer.
The caller's cancel_event remains active through attachment, controls and tick.
endpoint.cancel() also signals session cancellation from any thread; emulator
cleanup still requires close() on the attaching thread. No watcher runs CPU work.
TCP hosts must be numeric loopback addresses; literal localhost is rejected.
"""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import math
import secrets
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass

from .timed_link_session import TimedLinkSession
from .timed_wire import Cancelled, ChannelClosed, DeadlineExceeded, ProtocolError, TimedWireChannel

PRELUDE = struct.Struct(">4sBBBB16sI")
EPOCH_DOMAIN = b"pokered-timed-epoch-v3\0"
_ROM = {"red": 1, "blue": 2, "yellow": 3}
_SIDE = {"listener": 1, "connector": 2}
_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class RemoteMetadata:
    local_rom_version: str
    peer_rom_version: str
    side: str
    peer_side: str
    is_internal_clock: bool
    epoch: bytes
    local_prelude: bytes
    peer_prelude: bytes


def _remaining(deadline, cancel_event):
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("deadline must be finite absolute monotonic seconds")
    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise TypeError("cancel_event must be a threading.Event or None")
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("timed remote cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("timed remote deadline expired")
    return remaining


def _address(host):
    if not isinstance(host, str) or "%" in host:
        raise ValueError("host must be a numeric loopback address")
    address = ipaddress.ip_address(host)
    if not (getattr(address, "ipv4_mapped", None) or address).is_loopback:
        raise ValueError("timed TCP requires loopback addresses")
    return address


def _validate_socket(sock):
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise ValueError("timed remote requires a connected stream socket")
    local, peer = sock.getsockname(), sock.getpeername()
    if sock.family == getattr(socket, "AF_UNIX", None):
        # Socketpairs have no bound address on either end. Named filesystem
        # and abstract-namespace listeners are outside this factory's scope.
        if local not in ("", b"") or peer not in ("", b""):
            raise ValueError("timed remote requires an unnamed AF_UNIX socketpair")
        return
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        raise ValueError("timed remote requires loopback TCP or AF_UNIX")
    if sock.proto not in (0, socket.IPPROTO_TCP):
        raise ValueError("timed remote requires TCP")
    for endpoint in (local, peer):
        _address(endpoint[0])


def _ready(sock, *, writing, deadline, cancel_event):
    while True:
        budget = min(_POLL_SECONDS, _remaining(deadline, cancel_event))
        readable, writable, _ = select.select(
            [] if writing else [sock], [sock] if writing else [], [], budget
        )
        _remaining(deadline, cancel_event)
        if readable or writable:
            return


def _exchange_prelude(sock, local, *, deadline, cancel_event):
    # Both peers send first. Fixed small records and nonblocking partial I/O
    # avoid any dependence on packet boundaries or unbounded socket waits.
    offset = 0
    while offset < PRELUDE.size:
        _ready(sock, writing=True, deadline=deadline, cancel_event=cancel_event)
        try:
            count = sock.send(local[offset:])
        except (BlockingIOError, InterruptedError):
            continue
        if count == 0:
            raise ChannelClosed("peer closed during prelude send")
        offset += count
    peer = bytearray()
    while len(peer) < PRELUDE.size:
        _ready(sock, writing=False, deadline=deadline, cancel_event=cancel_event)
        try:
            chunk = sock.recv(PRELUDE.size - len(peer))
        except (BlockingIOError, InterruptedError):
            continue
        if not chunk:
            raise ChannelClosed("peer closed during prelude receive")
        peer.extend(chunk)
    _remaining(deadline, cancel_event)
    return bytes(peer)


def _metadata(local, peer, rom_version, side):
    magic, prelude_version, wire_version, rom, role, _nonce, capabilities = PRELUDE.unpack(peer)
    if magic != b"PKTH" or prelude_version != 1 or wire_version != 3 or capabilities != 1:
        raise ProtocolError("unsupported timed remote prelude")
    if rom not in _ROM.values() or role not in _SIDE.values() or role == _SIDE[side]:
        raise ProtocolError("invalid peer ROM or transport role")
    peer_rom = next(name for name, code in _ROM.items() if code == rom)
    peer_side = "connector" if side == "listener" else "listener"
    listener, connector = (local, peer) if side == "listener" else (peer, local)
    epoch = hashlib.sha256(EPOCH_DOMAIN + listener + connector).digest()[:16]
    internal = _ROM[rom_version] < rom if rom_version != peer_rom else side == "listener"
    return RemoteMetadata(rom_version, peer_rom, side, peer_side, internal, epoch, local, peer)


class TimedRemoteEndpoint:
    """Own a channel/session; attach explicitly before owner-thread controls."""

    def __init__(self, channel, session, metadata, operation_timeout=1.0, cancel_event=None):
        self.channel = channel
        self.session = session
        self._metadata = metadata
        self._cancel = threading.Event()
        self._owner = None
        self._operation_timeout = operation_timeout
        self._cancel_event = cancel_event

    @property
    def metadata(self):
        return self._metadata

    @property
    def peer_rom_version(self):
        return self.metadata.peer_rom_version

    @property
    def is_internal_clock(self):
        return self.metadata.is_internal_clock

    @property
    def epoch(self):
        return self.metadata.epoch

    @classmethod
    def from_connected_socket(
        cls,
        sock,
        *,
        side,
        rom_version,
        deadline,
        cancel_event=None,
        rearm_budget,
        rearm_instruction_cap,
        max_edge_lateness,
        quantum_cycles=256,
        operation_timeout=1.0,
        max_wait_attempts=16,
        inbound_capacity=64,
    ):
        """Own the socket on entry; cancel_event remains active for the session."""
        channel = session = None
        try:
            _remaining(deadline, cancel_event)
            if type(side) is not str or side not in _SIDE:
                raise ValueError("side must be listener or connector")
            if type(rom_version) is not str or rom_version not in _ROM:
                raise ValueError("rom_version must be red, blue, or yellow")
            _validate_socket(sock)
            sock.setblocking(False)
            local = PRELUDE.pack(
                b"PKTH", 1, 3, _ROM[rom_version], _SIDE[side], secrets.token_bytes(16), 1
            )
            peer = _exchange_prelude(sock, local, deadline=deadline, cancel_event=cancel_event)
            metadata = _metadata(local, peer, rom_version, side)
            _remaining(deadline, cancel_event)
            channel = TimedWireChannel(
                sock, epoch=metadata.epoch, inbound_capacity=inbound_capacity, revision=3
            )
            session = TimedLinkSession(
                channel,
                rearm_budget=rearm_budget,
                rearm_instruction_cap=rearm_instruction_cap,
                max_edge_lateness=max_edge_lateness,
                quantum_cycles=quantum_cycles,
                operation_timeout=operation_timeout,
                max_wait_attempts=max_wait_attempts,
                inbound_capacity=inbound_capacity,
                cancel_event=cancel_event,
            )
            channel.handshake(deadline=deadline, cancel_event=cancel_event)
            _remaining(deadline, cancel_event)
            return cls(channel, session, metadata, operation_timeout, cancel_event)
        except BaseException as exc:
            try:
                if session is not None:
                    session.close()
                elif channel is not None:
                    channel.close()
                else:
                    sock.close()
            except BaseException as cleanup:  # noqa: BLE001 - preserve the original failure
                exc.add_note(f"timed remote cleanup failed: {cleanup}")
            raise

    @classmethod
    def connect(cls, host, port, *, rom_version, deadline, cancel_event=None, **session_options):
        """Connect and negotiate using one caller-supplied absolute deadline."""
        _remaining(deadline, cancel_event)
        address = _address(host)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer in 1..65535")
        sock = socket.socket(
            socket.AF_INET if address.version == 4 else socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        try:
            sock.setblocking(False)
            result = sock.connect_ex((str(address), port))
            if result not in (0, errno.EISCONN):
                if result not in (
                    errno.EINPROGRESS,
                    errno.EWOULDBLOCK,
                    errno.EALREADY,
                    errno.EINTR,
                ):
                    raise OSError(result, "timed remote connect failed")
                _ready(sock, writing=True, deadline=deadline, cancel_event=cancel_event)
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error:
                    raise OSError(error, "timed remote connect failed")
            return cls.from_connected_socket(
                sock,
                side="connector",
                rom_version=rom_version,
                deadline=deadline,
                cancel_event=cancel_event,
                **session_options,
            )
        except BaseException:
            sock.close()
            raise

    @classmethod
    def listen(
        cls, port, *, host="127.0.0.1", rom_version, deadline, cancel_event=None, **session_options
    ):
        """Accept one peer; close the listening socket before negotiation."""
        _remaining(deadline, cancel_event)
        address = _address(host)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer in 1..65535")
        listener = socket.socket(
            socket.AF_INET if address.version == 4 else socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        conn = None
        try:
            listener.setblocking(False)
            listener.bind((str(address), port))
            listener.listen(1)
            while conn is None:
                _ready(listener, writing=False, deadline=deadline, cancel_event=cancel_event)
                try:
                    conn, _ = listener.accept()
                except (BlockingIOError, InterruptedError):
                    continue
        finally:
            listener.close()
        try:
            return cls.from_connected_socket(
                conn,
                side="listener",
                rom_version=rom_version,
                deadline=deadline,
                cancel_event=cancel_event,
                **session_options,
            )
        except BaseException:
            conn.close()
            raise

    def _check(self, deadline):
        self._check_cancelled()
        return _remaining(deadline, self._cancel)

    def _check_cancelled(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            self.cancel()
        if self._cancel.is_set():
            raise Cancelled("timed remote cancelled")

    def attach(self, pyboy, *, deadline):
        """Attach once; session owns native bootstrap and failure accounting."""
        try:
            self._check(deadline)
            if self._owner is not None and self._owner != threading.get_ident():
                raise RuntimeError("timed remote attach requires the owning thread")
            self._owner = threading.get_ident()
            self.session.attach(
                pyboy, deadline=deadline, startup_internal_clock=self.metadata.is_internal_clock
            )
            self._check(deadline)
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup:  # noqa: BLE001 - preserve the original failure
                exc.add_note(f"timed remote owner cleanup remains pending: {cleanup}")
            raise

    def announce_sync(self, marker, *, deadline):
        self._check(deadline)
        return self.session.announce_sync(marker, deadline=deadline)

    def poll_peer_sync(self, marker, *, deadline=None):
        """Passively service and consume one marker on the owner thread.

        An explicit deadline is never reset. Omitting it starts a new bounded
        poll using the configured operation timeout; no CPU work is performed.
        """
        if deadline is None:
            deadline = time.monotonic() + self._operation_timeout
        self.service_controls(deadline=deadline)
        return self.session.poll_peer_sync(marker)

    def service_controls(self, *, deadline):
        """Passively process queued controls at an owner boundary, without CPU work."""
        self._check(deadline)
        return self.session.service_controls(deadline=deadline)

    def wait_for_wire_idle(self, *, deadline):
        """Fence the sent prefix and observe local quiescence, without CPU work.

        Success is a bounded observation, not a claim that future traffic is idle.
        The peer must independently service its own owner-thread controls.
        """
        try:
            self._check(deadline)
            fence = self.session.start_fence(deadline=deadline)
            acknowledged = False
            while True:
                self._check(deadline)
                self.session.service_controls(deadline=deadline)
                acknowledged = acknowledged or self.session.poll_fence(fence)
                state = self.session.controls_snapshot()
                if acknowledged and state["quiescent"]:
                    self._check(deadline)
                    return state
                self._cancel.wait(min(_POLL_SECONDS, self._check(deadline)))
        except BaseException as exc:
            try:
                self.cancel()
            except BaseException as cleanup:  # noqa: BLE001 - preserve the original failure
                exc.add_note(f"timed remote cancellation cleanup failed: {cleanup}")
            raise

    def snapshot(self):
        """Read accounting on the attaching thread; metadata is safe on any thread."""
        if self._owner is not None and self._owner != threading.get_ident():
            raise RuntimeError("timed remote snapshot requires the attaching thread")
        return self.session.snapshot()

    def tick(self, count=1, render=True, sound=True):
        """Forward native execution; a cancelled endpoint still needs owner close()."""
        self._check_cancelled()
        result = self.session.tick(count, render=render, sound=sound)
        self._check_cancelled()
        return result

    def cancel(self):
        self._cancel.set()
        self.session.cancel()

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


__all__ = ["RemoteMetadata", "TimedRemoteEndpoint"]
