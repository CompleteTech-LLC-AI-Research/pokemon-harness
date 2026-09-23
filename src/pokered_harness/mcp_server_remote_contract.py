"""Remote argument validation and handshake helpers.

Split from ``pokered_harness.mcp_server`` for issue #125 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on ``pokered_harness.mcp_server`` stay visible.
"""

from __future__ import annotations

import math
import socket
import threading
import time
from typing import Any

from pokered_harness.config import SUPPORTED_ROM_VERSIONS
from pokered_harness.link.network_backend import NetworkBackendError, validate_loopback_host
from pokered_harness.link.serial_link import SerialLinkError

# Direct names this module calls; cyclic back-edges go through _entry.
from pokered_harness.mcp_server_model import (
    _MAX_REMOTE_TIMEOUT_S,
    LinkState,
    McpHarnessError,
    _ListenerCancelled,
)
from pokered_harness.session import Session


def _require_remote_idle_locked(link: LinkState) -> None:
    """Validate the remote lifecycle while ``link.state()`` is held."""
    listener_thread = link._listener_thread
    if isinstance(listener_thread, threading.Thread) and not listener_thread.is_alive():
        link._listener_thread = None
        listener_thread = None
    if link._disconnecting:
        raise McpHarnessError(
            "link_busy",
            "link teardown is in progress; retry after disconnect completes",
        )
    if (
        link.remote_mode != "idle"
        or link._connect_in_progress
        or link._listener_socket is not None
        or link.remote_link is not None
        or link.remote_endpoint is not None
        or link.network_session is not None
        or link._pending_remote_link is not None
        or link._pending_remote_endpoint is not None
        or link._pending_network_session is not None
        or link._pending_listener_socket is not None
    ):
        raise McpHarnessError(
            "remote_busy",
            f"remote link busy (mode={link.remote_mode!r}); call link_disconnect first",
        )
    if listener_thread is not None:
        raise McpHarnessError(
            "remote_busy",
            "remote listener cleanup is still in progress; retry after the listener worker exits",
        )
    if link._listener_start_in_progress:
        raise McpHarnessError(
            "remote_busy",
            "remote listener startup is still in progress; retry after the listener call unwinds",
        )


def _require_remote_idle(link: LinkState) -> None:
    with link.state():
        _require_remote_idle_locked(link)


def _require_pair_inactive_locked(link: LinkState) -> None:
    """Validate the in-process mode while ``link.state()`` is held."""
    with link._pair_lock:
        if (
            (link.pair is not None and link.pair.paired)
            or (link.local_link_session is not None)
            or link._local_pair_in_progress
            or (link._pending_local_link_session is not None)
        ):
            raise McpHarnessError(
                "pair_active",
                "in-process pair is active; call link_unpair before using remote link tools",
            )


def _require_pair_inactive(link: LinkState) -> None:
    with link.state():
        _require_pair_inactive_locked(link)


def _refresh_remote_state(link: LinkState, session: Session) -> None:
    """Reflect background-thread state into fields readable from the
    calling thread. Called by link_status before returning."""
    # If the peer disconnected, the TCP reader thread marks the link
    # closed; surface that as mode=idle so callers don't keep polling
    # a dead endpoint.
    with link.state():
        rl = link.remote_link
        connected = link.remote_mode == "connected"
        if rl is None or not connected or rl.connected:
            return
        stale_generation = link._generation
        stale_error = getattr(rl, "_reader_exc", None)
    # Reuse the lifecycle owner so status cleanup marks the link as
    # disconnecting until socket/backend/callback cleanup has completed. This
    # prevents a concurrent reconnect from overlapping a dead transport.
    try:
        _entry._disconnect_remote(link, session)
    except McpHarnessError:
        # Cleanup failures are retained on LinkState for the next status poll;
        # status itself should remain a read-mostly diagnostic operation.
        pass
    with link.state():
        # A new lifecycle may have started after teardown. Never attach the
        # old reader error to that new connection.
        if (
            link._generation == stale_generation + 1
            and link.remote_mode == "idle"
            and stale_error is not None
            and link._remote_error is None
        ):
            link._remote_error = stale_error


def _positive_port(value: Any) -> int:
    if isinstance(value, bool):
        raise McpHarnessError("invalid_port", "port must be an integer")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise McpHarnessError("invalid_port", f"invalid port: {value!r}") from exc
    if port < 1 or port > 65535:
        raise McpHarnessError("invalid_port", f"port must be in 1..65535, got {port}")
    return port


def _validate_remote_host(host: str) -> str:
    try:
        return validate_loopback_host(host)
    except ValueError as exc:
        raise McpHarnessError(
            "unsafe_remote_host",
            "remote TCP links are localhost-only; use 127.0.0.1, localhost, or ::1",
        ) from exc


def _validate_rom_version(version: str) -> str:
    normalized = version.strip().lower()
    if normalized not in SUPPORTED_ROM_VERSIONS:
        raise McpHarnessError(
            "unsupported_rom_version",
            f"ROM version must be one of {sorted(SUPPORTED_ROM_VERSIONS)}, got {version!r}",
        )
    return normalized


def _optional_rom_version(value: Any) -> str | None:
    if value is None:
        return None
    return _validate_rom_version(str(value))


def _require_primary_rom_version(link: LinkState, version: str) -> None:
    """Do not let a caller self-identify a session as another ROM."""
    primary = _validate_rom_version(link.primary_version)
    if version != primary:
        raise McpHarnessError(
            "rom_version_mismatch",
            f"requested ROM version {version!r} does not match the "
            f"session's configured primary version {primary!r}",
        )


def _validate_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise McpHarnessError("invalid_timeout", "timeout_s must be a number")
    try:
        timeout_s = float(value)
    except (TypeError, ValueError) as exc:
        raise McpHarnessError("invalid_timeout", f"invalid timeout_s: {value!r}") from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise McpHarnessError(
            "invalid_timeout", f"timeout_s must be finite and positive, got {value!r}"
        )
    if timeout_s > _MAX_REMOTE_TIMEOUT_S:
        raise McpHarnessError(
            "invalid_timeout",
            f"timeout_s must be <= {_MAX_REMOTE_TIMEOUT_S:g}, got {timeout_s:g}",
        )
    return timeout_s


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _peer_rom_version_if_ready(link: Any) -> str | None:
    hello_event = getattr(link, "_hello_received", None)
    if hello_event is not None and not hello_event.is_set():
        return None
    return link.peer_rom_version


def _supports_bit_accurate_network(session: Session) -> bool:
    """Return whether a session exposes the pinned PyBoy serial contract."""
    pyboy = getattr(session, "_pyboy", None)
    serial = getattr(getattr(pyboy, "mb", None), "serial", None)
    if serial is None or getattr(serial, "backend", None) is None:
        return False
    return callable(getattr(serial, "apply_external_edge", None)) and callable(
        getattr(serial, "peek_out_bit", None)
    )


def _is_real_pyboy_instance(session: Session) -> bool:
    """Identify a real PyBoy object without importing or probing internals."""
    pyboy = getattr(session, "_pyboy", None)
    return type(pyboy).__module__.startswith("pyboy.")


def _require_native_network_contract(session: Session, *, supported: bool, role: str) -> None:
    """Fail closed before an implicit link can mutate or open transport."""
    if supported:
        return
    raise McpHarnessError(
        "unsupported_runtime",
        f"{role} PyBoy does not expose the pinned bit-accurate serial "
        "contract; install the bundled PyBoy source runtime",
    )


def _reject_semantic_pair_on_native_runtime(
    pair: _entry.LinkPair,
    session: Session,
    peer: Session,
) -> None:
    """Keep injected semantic pairs out of native-capable endpoints.

    A pre-built ``LinkPair`` is intentionally retained as a deterministic
    compatibility seam for tiny fake sessions.  It must not, however, take
    ownership of a real or native-capable endpoint: that would silently
    replace ROM-generated serial edges with semantic hook writes.
    """
    candidates: list[tuple[Session, str]] = [
        (session, "primary"),
        (peer, "peer"),
    ]
    for endpoint, role in (("primary", "primary"), ("peer", "peer")):
        candidate = getattr(pair, endpoint, None)
        if candidate is not None and candidate is not session and candidate is not peer:
            candidates.append((candidate, role))
    for candidate, role in candidates:
        if _entry._supports_bit_accurate_network(candidate) or _is_real_pyboy_instance(candidate):
            raise McpHarnessError(
                "unsupported_runtime",
                f"{role} PyBoy requires the pinned bit-accurate serial "
                "runtime; semantic link pairs are only available for "
                "explicit non-native test doubles",
            )


def _attach_network_backend(
    session: Session,
    backend: _entry.NetworkBackend,
    *,
    is_internal_clock: bool,
    local_rom_version: str,
    network_hello_timeout_s: float | None = None,
) -> _entry.PyBoyLinkSession:
    """Attach a TCP backend while honoring the MCP handshake deadline.

    ``PyBoyLinkSession`` owns the native attach sequence, including its HELLO
    wait. MCP supplies the request deadline here until that lower-level API
    exposes a per-attach timeout.
    """
    network_session = _entry.PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=is_internal_clock,
        local_rom_version=local_rom_version,
    )
    if network_hello_timeout_s is not None:
        network_session._NETWORK_HELLO_TIMEOUT_SECONDS = network_hello_timeout_s
    with session.locked(timeout_s=_entry._DEFAULT_CLEANUP_TIMEOUT_S):
        network_session.attach(session._pyboy)
    return network_session


def _negotiate_network_clock_role(
    session: Session,
    network_session: _entry.PyBoyLinkSession,
    peer_rom_version: str,
    *,
    timeout_s: float,
) -> bool | None:
    """Negotiate network clock-role metadata while owning the Session lock.

    ``PyBoyLinkSession.negotiate_network_clock_role`` intentionally owns the
    link-session lifecycle and serial gate. It only updates session pacing
    metadata; the native PyBoy serial cores and ROM-owned state are left
    untouched. MCP callers must also hold the owning :class:`Session` lock so
    a concurrent step, state operation, or teardown cannot observe or modify
    the session metadata mid-negotiation.
    """
    with session.locked(timeout_s=max(0.0, timeout_s)):
        return network_session.negotiate_network_clock_role(peer_rom_version)


def _bind_listener(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(1)
        # A short accept timeout lets the cancellation event be observed
        # without requiring a platform-specific wakeup socket.
        listener.settimeout(0.1)
        return listener
    except OSError as exc:
        try:
            listener.close()
        except OSError:
            pass
        raise McpHarnessError("listen_failed", f"unable to bind {host}:{port}: {exc}") from exc


def _wait_for_remote_hello(
    link: _entry.TcpSerialLink,
    cancel: threading.Event | None,
    timeout_s: float,
) -> None:
    """Wait for HELLO without using TcpSerialLink's fixed five-second wait."""
    hello_event = getattr(link, "_hello_received", None)
    if hello_event is None:
        # The current TCP implementation exposes the event privately. Keep
        # a safe compatibility fallback for alternate SerialLink adapters.
        try:
            _ = link.peer_rom_version
        except SerialLinkError as exc:
            raise McpHarnessError("link_handshake_failed", str(exc)) from exc
        if not link.connected:
            raise McpHarnessError("link_handshake_failed", "remote link closed during HELLO")
        return

    deadline = time.monotonic() + timeout_s
    while not hello_event.is_set():
        if cancel is not None and cancel.is_set():
            raise McpHarnessError("link_cancelled", "remote connection cancelled")
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise McpHarnessError("timeout", f"peer HELLO not received within {timeout_s:g}s")
        hello_event.wait(timeout=min(0.05, remaining))
    try:
        peer_version = link.peer_rom_version
    except SerialLinkError as exc:
        raise McpHarnessError("link_handshake_failed", str(exc)) from exc
    if not peer_version:
        raise McpHarnessError("link_handshake_failed", "peer HELLO had no ROM version")
    if not link.connected:
        raise McpHarnessError("link_handshake_failed", "remote link closed during HELLO")


def _wait_for_network_hello(
    link: _entry.NetworkBackend,
    cancel: threading.Event | None,
    timeout_s: float,
    *,
    expected_peer_rom_version: str | None = None,
) -> str:
    """Wait for the bit-level transport handshake with cancellation."""
    hello_event = link._hello_received
    deadline = time.monotonic() + timeout_s
    while not hello_event.is_set():
        if cancel is not None and cancel.is_set():
            raise _ListenerCancelled
        if link._reader_exc is not None:
            raise NetworkBackendError(
                f"peer HELLO failed: {link._reader_exc}"
            ) from link._reader_exc
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise NetworkBackendError(f"peer HELLO not received within {timeout_s:g}s")
        hello_event.wait(timeout=min(0.05, remaining))
    if link._reader_exc is not None:
        raise NetworkBackendError(f"peer HELLO failed: {link._reader_exc}") from link._reader_exc
    if not link.connected:
        raise NetworkBackendError("peer link closed during HELLO")
    peer_version = link.peer_rom_version
    if expected_peer_rom_version is not None and peer_version != expected_peer_rom_version:
        error = NetworkBackendError(
            f"peer ROM version {peer_version!r} does not match expected "
            f"{expected_peer_rom_version!r}"
        )
        link._mark_closed(error)
        raise error
    return peer_version


# Call-time indirection so facade-level monkeypatches stay visible here.
import pokered_harness.mcp_server as _entry
