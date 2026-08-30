"""MCP server exposing a long-lived :class:`Session` over stdio.

Actions (mutating the emulator) are MCP **tools**; observations (reading
game state, events) are MCP **resources**. That split matches the ADR.

The server is intentionally thin — every operation delegates to the
session, which is where lifecycle, version enforcement, and hooks live.

When a peer :class:`Session` is configured (via ``POKERED_PEER_*`` env
vars), the server also exposes link-cable tools (``link_pair``,
``link_step``, ``link_peer_press``, ...) and peer resources
(``pokered://peer-game-state``, ``pokered://link-transport``). The peer
Session is constructed at startup but not *paired* — callers must invoke
``link_pair`` explicitly.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import math
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from pokered_harness.config import SUPPORTED_ROM_VERSIONS
from pokered_harness.events.hooks import GameEvent
from pokered_harness.link.pair import LinkPair
from pokered_harness.link.remote import RemoteLinkEndpoint
from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
    validate_loopback_host,
)
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_link import (
    SerialLink,
    SerialLinkError,
    SerialLinkTimeout,
    TcpSerialLink,
)
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import (
    InvalidStateError,
    Session,
    SessionClosedError,
    SessionConfigurationError,
    VersionMismatch,
)


class McpHarnessError(ValueError):
    """A client-visible MCP failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ListenerCancelled(Exception):
    """Internal sentinel for an intentional listener shutdown."""


_DEFAULT_REMOTE_HELLO_TIMEOUT_S = 10.0
_DEFAULT_CLEANUP_TIMEOUT_S = 5.0
_MAX_REMOTE_TIMEOUT_S = 300.0
_DIRECT_LINK_HOOKS = (
    "Serial_ExchangeBytes",
    "Serial_ExchangeNybble",
    "Serial_ExchangeLinkMenuSelection",
    "Serial_TryEstablishingExternallyClockedConnection",
)
_LOCAL_LINK_TOOL_NAMES = frozenset(
    {
        "link_pair",
        "link_unpair",
        "link_step",
        "link_peer_press",
        "link_peer_hold",
        "link_peer_release",
    }
)

# Default hooks registered at session start. Keep this list short — each
# entry is a semantic event downstream agents are expected to react to.
#
# ``PrintText`` fires whenever the text engine actually draws text (intro
# cutscene, naming screens, battle text, menu descriptions, NPC dialog).
# ``DisplayTextID`` is a *narrower* wrapper used by the overworld engine
# when the player interacts with an NPC / sign / script tile — it does
# NOT fire during intro or naming screens. Both are useful; they have
# distinct semantics.
DEFAULT_HOOKS: tuple[tuple[str, str], ...] = (
    ("PrintText", "text_shown"),
    ("DisplayTextID", "overworld_dialog"),
    ("YesNoChoice", "yes_no_prompt"),
    ("TryEvolvingMon", "evolution_check"),
    ("SetLastBlackoutMap", "blackout"),
)


# URIs for resources exposed by this server.
_URI_GAME_STATE = "pokered://game-state"
_URI_EVENT_LOG = "pokered://events"
_URI_PEER_GAME_STATE = "pokered://peer-game-state"
_URI_LINK_TRANSPORT = "pokered://link-transport"
_URI_LINK_STATUS = "pokered://link-status"


# -- server state container --------------------------------------------------


class LinkState:
    """Mutable link-cable state owned by the server.

    Holds optional state for two distinct link modes:

    1. **In-process pair** (``pair``/``local_link_session``) — a
       bit-accurate :class:`PyBoyLinkSession` for the pinned PyBoy runtime,
       with the older semantic :class:`LinkPair` retained only for test
       doubles that do not expose PyBoy's native serial backend.
    2. **Remote endpoint** (``remote_endpoint``/``network_session``) — a
       bit-accurate :class:`PyBoyLinkSession` backed by a localhost TCP
       :class:`NetworkBackend`. The older semantic endpoint remains only as a
       compatibility path for test doubles that do not expose PyBoy's serial
       motherboard.

    The two modes are mutually exclusive — calling ``link_listen`` /
    ``link_connect`` while an in-process pair is active (or vice versa)
    errors. ``remote_mode`` tracks the remote lifecycle:

    - ``"idle"`` — no remote link at all.
    - ``"listening"`` — :meth:`TcpSerialLink.listen` running on a
      background thread; no peer yet.
    - ``"connected"`` — peer attached; ``remote_endpoint`` installed and
      ready for in-game serial exchanges.
    """

    def __init__(
        self,
        peer_session: Session | None = None,
        *,
        primary_version: str = "red",
        peer_version: str = "red",
    ) -> None:
        self.peer_session = peer_session
        self.primary_version = primary_version
        self.peer_version = peer_version
        self.pair: LinkPair | None = None
        self.local_link_session: PyBoyLinkSession | None = None
        # Remote (two-process) state.
        self.remote_link: SerialLink | None = None
        self.remote_endpoint: RemoteLinkEndpoint | None = None
        self.network_session: PyBoyLinkSession | None = None
        self.remote_mode: str = "idle"
        self.remote_role: str | None = None  # "listener" | "connector"
        self.remote_bind_port: int | None = None
        self._listener_thread: threading.Thread | None = None
        self._listener_error: Exception | None = None
        self._remote_error: Exception | None = None
        self._listener_socket: socket.socket | None = None
        self._listener_cancel = threading.Event()
        self._connect_cancel = threading.Event()
        self._disconnect_done = threading.Event()
        self._disconnect_done.set()
        self._disconnect_owner: int | None = None
        self._connect_done = threading.Event()
        self._connect_done.set()
        self._connect_in_progress = False
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._disconnecting = False
        self._generation = 0

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Serialize emulator/link mutations without blocking disconnect."""
        with self._operation_lock:
            yield

    @contextmanager
    def state(self) -> Iterator[None]:
        """Protect link lifecycle fields shared with the listener thread."""
        with self._state_lock:
            yield


# -- tool definitions --------------------------------------------------------


def _tool_specs(*, has_peer: bool = False) -> list[mcp_types.Tool]:
    specs = [
        mcp_types.Tool(
            name="step",
            description="Advance the emulator by `count` ticks (frames).",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1},
                    "render": {"type": "boolean", "default": False},
                },
                "required": ["count"],
            },
        ),
        mcp_types.Tool(
            name="press",
            description="Press a button for `duration` ticks and auto-release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "button": {
                        "type": "string",
                        "enum": [
                            "a", "b", "start", "select",
                            "up", "down", "left", "right",
                        ],
                    },
                    "duration": {"type": "integer", "minimum": 1, "default": 1},
                },
                "required": ["button"],
            },
        ),
        mcp_types.Tool(
            name="hold",
            description="Press and hold a button until explicitly released.",
            inputSchema={
                "type": "object",
                "properties": {"button": {"type": "string"}},
                "required": ["button"],
            },
        ),
        mcp_types.Tool(
            name="release",
            description="Release a previously held button.",
            inputSchema={
                "type": "object",
                "properties": {"button": {"type": "string"}},
                "required": ["button"],
            },
        ),
        mcp_types.Tool(
            name="save_state",
            description="Capture emulator save-state as base64 bytes.",
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="load_state",
            description="Restore emulator from a base64-encoded save-state.",
            inputSchema={
                "type": "object",
                "properties": {"data": {"type": "string"}},
                "required": ["data"],
            },
        ),
        mcp_types.Tool(
            name="run_until_event",
            description=(
                "Tick forward until any of the named events fires or the "
                "tick budget is exhausted."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "max_ticks": {"type": "integer", "minimum": 1},
                    "chunk": {"type": "integer", "minimum": 1, "default": 16},
                },
                "required": ["event_names", "max_ticks"],
            },
        ),
        # -- link-cable tools -------------------------------------------
        mcp_types.Tool(
            name="link_pair",
            description=(
                "Pair the primary session with the configured peer and install "
                "the serial bridge. Requires POKERED_PEER_* env vars at startup."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="link_unpair",
            description="Tear down the current pair (no-op if not paired).",
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="link_step",
            description="Advance BOTH sessions by `count` ticks each, interleaved.",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1},
                    "render": {"type": "boolean", "default": False},
                },
                "required": ["count"],
            },
        ),
        mcp_types.Tool(
            name="link_peer_press",
            description="Press a button on the PEER session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "button": {"type": "string"},
                    "duration": {"type": "integer", "minimum": 1, "default": 1},
                },
                "required": ["button"],
            },
        ),
        mcp_types.Tool(
            name="link_peer_hold",
            description="Hold a button on the PEER session until released.",
            inputSchema={
                "type": "object",
                "properties": {"button": {"type": "string"}},
                "required": ["button"],
            },
        ),
        mcp_types.Tool(
            name="link_peer_release",
            description="Release a previously held button on the PEER session.",
            inputSchema={
                "type": "object",
                "properties": {"button": {"type": "string"}},
                "required": ["button"],
            },
        ),
        mcp_types.Tool(
            name="link_status",
            description="Return the current link-cable state (safe to call anytime).",
            inputSchema={"type": "object", "properties": {}},
        ),
        # -- remote (two-process) link-cable tools ----------------------
        mcp_types.Tool(
            name="link_listen",
            description=(
                "Bind a TCP port and wait for a peer MCP server to connect. "
                "Returns immediately; poll `link_status` until mode=='connected'. "
                "Role: internal-clock master (status byte 0x02)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "host": {"type": "string", "default": "127.0.0.1"},
                    "rom_version": {
                        "type": "string",
                        "description": "Local ROM version announced in HELLO; "
                                       "defaults to primary_version at startup.",
                    },
                    "timeout_s": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": _MAX_REMOTE_TIMEOUT_S,
                        "default": _DEFAULT_REMOTE_HELLO_TIMEOUT_S,
                    },
                },
                "required": ["port"],
            },
        ),
        mcp_types.Tool(
            name="link_connect",
            description=(
                "Open a TCP connection to a peer MCP server's listener. "
                "Blocks until HELLO completes. Role: external-clock slave "
                "(status byte 0x01)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "rom_version": {
                        "type": "string",
                        "description": "Local ROM version announced in HELLO; "
                                       "defaults to primary_version at startup.",
                    },
                    "timeout_s": {"type": "number", "default": 10.0},
                },
                "required": ["host", "port"],
            },
        ),
        mcp_types.Tool(
            name="link_disconnect",
            description=(
                "Close the current remote link (no-op if idle). "
                "Does not affect the in-process pair."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]
    if has_peer:
        return specs
    return [spec for spec in specs if spec.name not in _LOCAL_LINK_TOOL_NAMES]


# -- dispatch helpers --------------------------------------------------------


def _text_reply(payload: Any) -> mcp_types.CallToolResult:
    """Return both legacy JSON text and MCP structured tool content."""
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload if isinstance(payload, dict) else None,
        isError=False,
    )


def _error_reply(exc: Exception) -> mcp_types.CallToolResult:
    """Convert an internal exception into a stable MCP error envelope."""
    payload = {
        "ok": False,
        "error": {
            "code": _error_code(exc),
            "message": str(exc) or type(exc).__name__,
            "type": type(exc).__name__,
        },
    }
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
        isError=True,
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, McpHarnessError):
        return exc.code
    if isinstance(exc, SessionClosedError):
        return "session_closed"
    if isinstance(exc, VersionMismatch):
        return "version_mismatch"
    if isinstance(exc, SessionConfigurationError):
        return "invalid_session_configuration"
    if isinstance(exc, InvalidStateError):
        return "invalid_state"
    if isinstance(exc, (SerialLinkTimeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, NetworkBackendError):
        if "timed out" in str(exc) or "within" in str(exc):
            return "timeout"
        return "link_error"
    if isinstance(exc, SerialLinkError):
        return "link_error"
    if isinstance(exc, (KeyError, TypeError, ValueError, binascii.Error)):
        return "invalid_argument"
    if isinstance(exc, OSError):
        return "transport_error"
    return "internal_error"


def _decode_state_data(value: Any) -> bytes:
    """Decode the wire representation used by the ``load_state`` tool."""
    if not isinstance(value, str):
        raise McpHarnessError(
            "invalid_state", "load_state.data must be a base64 string"
        )
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise McpHarnessError(
            "invalid_state", "load_state.data is not valid base64"
        ) from exc


def _require_peer(link: LinkState) -> Session:
    if link.peer_session is None:
        raise McpHarnessError(
            "peer_not_configured",
            "peer session not configured; set POKERED_PEER_ROM_PATH and "
            "POKERED_PEER_SYM_PATH at server startup"
        )
    return link.peer_session


def _require_pair(link: LinkState) -> LinkPair:
    if link.pair is None or not link.pair.paired:
        raise McpHarnessError("not_paired", "not paired; call link_pair first")
    return link.pair


def _dispatch_session_tool(
    session: Session, name: str, arguments: dict[str, Any]
) -> Any:
    if name == "step":
        session.step(int(arguments["count"]), render=bool(arguments.get("render", False)))
        return {"tick": session.current_tick()}

    if name == "press":
        session.press(arguments["button"], duration=int(arguments.get("duration", 1)))
        return {"ok": True}

    if name == "hold":
        session.hold(arguments["button"])
        return {"ok": True}

    if name == "release":
        session.release(arguments["button"])
        return {"ok": True}

    if name == "save_state":
        blob = session.save_state()
        return {"data": base64.b64encode(blob).decode("ascii")}

    if name == "load_state":
        session.load_state(_decode_state_data(arguments["data"]))
        return {"ok": True}

    if name == "run_until_event":
        result = session.run_until_event(
            list(arguments["event_names"]),
            max_ticks=int(arguments["max_ticks"]),
            chunk=int(arguments.get("chunk", 16)),
        )
        return {
            "reached": result.reached,
            "ticks_spent": result.ticks_spent,
            "event": to_jsonable(result.event) if result.event else None,
        }

    raise ValueError(f"unknown tool: {name!r}")


def dispatch_tool(
    session: Session,
    name: str,
    arguments: dict[str, Any],
    link: LinkState | None = None,
) -> Any:
    """Pure dispatch — no asyncio, no MCP types. Unit-testable on its own."""
    if not name.startswith("link_"):
        # A server can receive multiple MCP requests concurrently. Session's
        # own lock protects individual calls, while this operation lock keeps
        # compound actions (notably run_until_event and save/load) from being
        # interleaved with link operations. Direct library callers that do not
        # provide a LinkState still get Session-level serialization.
        if link is None:
            return _dispatch_session_tool(session, name, arguments)
        with link.operation():
            return _dispatch_session_tool(session, name, arguments)

    # -- link-cable tools -------------------------------------------------
    if link is None:
        link = LinkState()
    # Disconnect deliberately bypasses the operation lock. A serial hook
    # may be blocked waiting for a peer; closing its socket must be able
    # to proceed so the blocked operation can unwind. All other mutating
    # link calls are serialized, while status remains read-mostly.
    if name == "link_disconnect" or name == "link_status":
        return _dispatch_link_tool(session, name, arguments, link)
    with link.operation():
        return _dispatch_link_tool(session, name, arguments, link)


def _dispatch_link_tool(
    session: Session,
    name: str,
    arguments: dict[str, Any],
    link: LinkState,
) -> Any:
    if name == "link_pair":
        peer = _require_peer(link)
        with link.state():
            if link._disconnecting:
                raise McpHarnessError(
                    "link_busy", "link teardown is still in progress"
                )
            if link.pair is not None and link.pair.paired:
                raise McpHarnessError(
                    "already_paired", "already paired; call link_unpair first"
                )
            if link.local_link_session is not None:
                raise McpHarnessError(
                    "already_paired", "already paired; call link_unpair first"
                )
            pair = link.pair
            created = pair is None

        # Real sessions must use the forked PyBoy serial implementation so
        # every link edge is generated by the ROM and native serial core.
        # The semantic LinkPair below remains useful for deterministic fake
        # sessions and compatibility tests, but is not a production backend.
        if (
            pair is None
            and _supports_bit_accurate_network(session)
            and _supports_bit_accurate_network(peer)
        ):
            local_link_session = PyBoyLinkSession.local()
            try:
                # Use a stable lock order; both sessions are owned by this
                # MCP server and no callback is invoked while attaching.
                with session.locked():
                    with peer.locked():
                        local_link_session.attach(session._pyboy)
                        local_link_session.attach(peer._pyboy)
            except Exception:
                local_link_session.detach_all()
                raise
            with link.state():
                link.local_link_session = local_link_session
            return {
                "paired": True,
                "primary_version": link.primary_version,
                "peer_version": link.peer_version,
            }

        if pair is None:
            pair = LinkPair(
                session,
                peer,
                version_primary=link.primary_version,
                version_peer=link.peer_version,
            )
            with link.state():
                link.pair = pair
        try:
            pair.pair()
        except Exception:
            if created:
                with link.state():
                    if link.pair is pair:
                        link.pair = None
            _deactivate_link_hooks(session, peer)
            raise
        return {
            "paired": True,
            "primary_version": link.primary_version,
            "peer_version": link.peer_version,
        }

    if name == "link_unpair":
        with link.state():
            pair = link.pair
            local_link_session = link.local_link_session
        cleanup_errors: list[Exception] = []
        if local_link_session is not None:
            try:
                local_link_session.detach_all()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            else:
                with link.state():
                    if link.local_link_session is local_link_session:
                        link.local_link_session = None
        if pair is not None and pair.paired:
            try:
                pair.unpair()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            else:
                _deactivate_link_hooks(session, pair.peer)
                with link.state():
                    if link.pair is pair:
                        link.pair = None
        elif pair is not None:
            with link.state():
                if link.pair is pair:
                    link.pair = None
        if cleanup_errors:
            details = "; ".join(
                f"{type(exc).__name__}: {exc}" for exc in cleanup_errors
            )
            raise McpHarnessError("link_teardown_failed", details)
        return {"paired": False}

    if name == "link_step":
        with link.state():
            network_session = link.network_session
            local_link_session = link.local_link_session
        if network_session is not None:
            count = int(arguments["count"])
            session.step(count, render=bool(arguments.get("render", False)))
            return {"primary_tick": session.current_tick(), "peer_tick": None}
        if local_link_session is not None:
            count = int(arguments["count"])
            if count <= 0:
                raise ValueError(f"count must be positive, got {count}")
            peer = _require_peer(link)
            # The interleaved path drives PyBoy directly rather than through
            # Session.step(), so acquire both Session locks explicitly. This
            # prevents an ordinary MCP step/save/load/resource call from
            # mutating either motherboard concurrently with link stepping.
            with session.locked(), peer.locked():
                local_link_session.step_interleaved(
                    count, render=bool(arguments.get("render", False))
                )
                # PyBoyLinkSession drives the underlying emulators directly;
                # keep Session-level bookkeeping aligned with the same frame
                # count.
                session.reset_tick(session.current_tick() + count)
                peer.reset_tick(peer.current_tick() + count)
                return {
                    "primary_tick": session.current_tick(),
                    "peer_tick": peer.current_tick(),
                }
        pair = _require_pair(link)
        pair.step(int(arguments["count"]), render=bool(arguments.get("render", False)))
        return {
            "primary_tick": session.current_tick(),
            "peer_tick": link.peer_session.current_tick() if link.peer_session else None,
        }

    if name == "link_peer_press":
        peer = _require_peer(link)
        peer.press(arguments["button"], duration=int(arguments.get("duration", 1)))
        return {"ok": True}

    if name == "link_peer_hold":
        peer = _require_peer(link)
        peer.hold(arguments["button"])
        return {"ok": True}

    if name == "link_peer_release":
        peer = _require_peer(link)
        peer.release(arguments["button"])
        return {"ok": True}

    if name == "link_status":
        # Surface listener-thread failures lazily on status checks so
        # callers polling for "connected" see the error rather than
        # hanging forever.
        _refresh_remote_state(link, session)
        with link.state():
            pair = link.pair
            local_link_session = link.local_link_session
            paired = pair is not None and pair.paired
            paired = paired or (
                local_link_session is not None and local_link_session.paired
            )
            remote_mode = link.remote_mode
            remote_role = link.remote_role
            remote_bind_port = link.remote_bind_port
            remote_link = link.remote_link
            network_session = link.network_session
            remote_error = link._remote_error or link._listener_error
        transport_snapshot: dict[str, Any] = (
            pair.transport.snapshot() if pair is not None else {"a_to_b": [], "b_to_a": []}
        )
        peer_rom: str | None = None
        if remote_link is not None and remote_mode == "connected":
            try:
                peer_rom = _peer_rom_version_if_ready(remote_link)
            except SerialLinkError:
                peer_rom = None
        return {
            "paired": paired,
            "link_backend": (
                "bit_accurate"
                if local_link_session is not None or network_session is not None
                else ("semantic_compat" if pair is not None else None)
            ),
            "transport": transport_snapshot,
            "primary_tick": session.current_tick(),
            "peer_tick": link.peer_session.current_tick() if link.peer_session else None,
            "remote_mode": remote_mode,
            "remote_role": remote_role,
            "remote_bind_port": remote_bind_port,
            "remote_peer_rom_version": peer_rom,
            "remote_error": (
                f"{type(remote_error).__name__}: {remote_error}"
                if remote_error is not None
                else None
            ),
        }

    if name == "link_listen":
        _require_remote_idle(link)
        _require_pair_inactive(link)
        port = _positive_port(arguments.get("port"))
        host = _validate_remote_host(str(arguments.get("host", "127.0.0.1")))
        rom_version = _validate_rom_version(
            str(arguments.get("rom_version") or link.primary_version)
        )
        _require_primary_rom_version(link, rom_version)
        timeout_s = _validate_timeout(
            arguments.get("timeout_s", _DEFAULT_REMOTE_HELLO_TIMEOUT_S)
        )

        listener = _bind_listener(host, port)
        cancel = threading.Event()
        with link.state():
            if link._disconnecting:
                listener.close()
                raise McpHarnessError(
                    "link_busy", "link teardown is still in progress"
                )
            generation = link._generation
            link._listener_cancel = cancel
            link._listener_socket = listener
            link.remote_mode = "listening"
            link.remote_role = "listener"
            link.remote_bind_port = port
            link._listener_error = None
            link._remote_error = None

        def _accept() -> None:
            _accept_remote(
                link,
                session,
                listener,
                cancel,
                generation,
                rom_version,
                timeout_s,
            )

        t = threading.Thread(
            target=_accept, name="mcp-link-listen", daemon=True
        )
        try:
            # Keep publication and start under the same lifecycle lock. If
            # disconnect races this point, it must not close the listener and
            # then leave us returning a stale "listening" state with a worker
            # that was started against a dead socket.
            with link.state():
                if link._disconnecting or link._generation != generation:
                    raise McpHarnessError(
                        "link_cancelled", "remote listener cancelled"
                    )
                link._listener_thread = t
                t.start()
        except Exception:
            cancel.set()
            try:
                listener.close()
            except OSError:
                pass
            with link.state():
                if link._listener_socket is listener:
                    link._listener_socket = None
                if link._listener_thread is t:
                    link._listener_thread = None
                if link._generation == generation and not link._disconnecting:
                    link.remote_mode = "idle"
                    link.remote_role = None
                    link.remote_bind_port = None
            raise
        return {
            "remote_mode": "listening",
            "remote_role": "listener",
            "remote_bind_port": port,
            "host": host,
            "rom_version": rom_version,
            "timeout_s": timeout_s,
        }

    if name == "link_connect":
        _require_remote_idle(link)
        _require_pair_inactive(link)
        host = _validate_remote_host(str(arguments["host"]))
        port = _positive_port(arguments.get("port"))
        rom_version = _validate_rom_version(
            str(arguments.get("rom_version") or link.primary_version)
        )
        _require_primary_rom_version(link, rom_version)
        timeout_s = _validate_timeout(arguments.get("timeout_s", 10.0))
        with link.state():
            generation = link._generation
            link._connect_cancel = threading.Event()
            connect_cancel = link._connect_cancel
            link._connect_in_progress = True
            link._connect_done.clear()
            link.remote_mode = "connecting"
            link.remote_role = "connector"
            link.remote_bind_port = None
            link._listener_error = None
            link._remote_error = None
        transport: Any | None = None
        endpoint: RemoteLinkEndpoint | None = None
        network_session: PyBoyLinkSession | None = None
        peer_version: str | None = None
        deadline = time.monotonic() + timeout_s
        try:
            if _supports_bit_accurate_network(session):
                transport = NetworkBackend.connect(
                    host,
                    port,
                    timeout_s=timeout_s,
                    local_rom_version=rom_version,
                    cancel_event=connect_cancel,
                )
                network_session = _attach_network_backend(
                    session,
                    transport,
                    is_internal_clock=False,
                    local_rom_version=rom_version,
                )
                peer_version = _wait_for_network_hello(
                    transport, connect_cancel, _remaining(deadline)
                )
            else:
                # Keep the semantic adapter available for lightweight test
                # doubles and older callers that do not expose PyBoy's
                # bit-accurate motherboard serial object.
                transport = TcpSerialLink.connect(
                    host,
                    port,
                    rom_version,
                    timeout_s=timeout_s,
                    cancel_event=connect_cancel,
                )
                _wait_for_remote_hello(
                    transport, connect_cancel, _remaining(deadline)
                )
                peer_version = transport.peer_rom_version
                endpoint = RemoteLinkEndpoint.as_connector(session, transport)
                endpoint.install()
            with link.state():
                if (
                    connect_cancel.is_set()
                    or link._disconnecting
                    or link._generation != generation
                ):
                    raise McpHarnessError(
                        "link_cancelled", "remote connection cancelled"
                    )
                link.remote_link = transport
                link.remote_endpoint = endpoint
                link.network_session = network_session
                link.remote_mode = "connected"
                link.remote_role = "connector"
                link.remote_bind_port = None
                link._remote_error = None
        except McpHarnessError:
            if network_session is not None:
                network_session.detach_all()
            if transport is not None:
                _close_serial_link(transport)
            _deactivate_link_hooks(session)
            raise
        except _ListenerCancelled as exc:
            if network_session is not None:
                network_session.detach_all()
            if transport is not None:
                _close_serial_link(transport)
            _deactivate_link_hooks(session)
            raise McpHarnessError(
                "link_cancelled", "remote connection cancelled"
            ) from exc
        except (SerialLinkError, NetworkBackendError, OSError, TimeoutError) as exc:
            if network_session is not None:
                network_session.detach_all()
            if transport is not None:
                _close_serial_link(transport)
            _deactivate_link_hooks(session)
            if connect_cancel.is_set():
                raise McpHarnessError(
                    "link_cancelled", "remote connection cancelled"
                ) from exc
            raise McpHarnessError("link_connect_failed", str(exc)) from exc
        finally:
            with link.state():
                if link._generation == generation and link.remote_link is None:
                    link.remote_mode = "idle"
                    link.remote_role = None
                    link.remote_bind_port = None
                    link._connect_cancel = threading.Event()
                link._connect_in_progress = False
                link._connect_done.set()
        return {
            "remote_mode": "connected",
            "remote_role": "connector",
            "host": host,
            "port": port,
            "rom_version": rom_version,
            "peer_rom_version": peer_version,
        }

    if name == "link_disconnect":
        _disconnect_remote(link, session)
        return {"remote_mode": "idle"}

    raise ValueError(f"unknown tool: {name!r}")


def _require_remote_idle(link: LinkState) -> None:
    with link.state():
        if link.remote_mode != "idle" or link._disconnecting or link._connect_in_progress:
            raise McpHarnessError(
                "remote_busy",
                f"remote link busy (mode={link.remote_mode!r}); call "
                f"link_disconnect first",
            )


def _require_pair_inactive(link: LinkState) -> None:
    with link.state():
        if (link.pair is not None and link.pair.paired) or (
            link.local_link_session is not None
        ):
            raise McpHarnessError(
                "pair_active",
                "in-process pair is active; call link_unpair before using "
                "remote link tools",
            )


def _refresh_remote_state(link: LinkState, session: Session) -> None:
    """Reflect background-thread state into fields readable from the
    calling thread. Called by link_status before returning."""
    # If the peer disconnected, the TCP reader thread marks the link
    # closed; surface that as mode=idle so callers don't keep polling
    # a dead endpoint.
    with link.state():
        rl = link.remote_link
        connected = link.remote_mode == "connected"
        if (
            rl is None
            or not connected
            or rl.connected
        ):
            return
        link.remote_mode = "idle"
        link.remote_role = None
        link.remote_bind_port = None
        link.remote_link = None
        link.remote_endpoint = None
        network_session = link.network_session
        link.network_session = None
        link._remote_error = getattr(rl, "_reader_exc", None)
    if network_session is not None:
        try:
            network_session.detach_all()
        except Exception:
            pass
    _close_serial_link(rl, timeout_s=0.25)
    _deactivate_link_hooks(session)


def _positive_port(value: Any) -> int:
    if isinstance(value, bool):
        raise McpHarnessError("invalid_port", "port must be an integer")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise McpHarnessError("invalid_port", f"invalid port: {value!r}") from exc
    if port < 1 or port > 65535:
        raise McpHarnessError(
            "invalid_port", f"port must be in 1..65535, got {port}"
        )
    return port


def _validate_remote_host(host: str) -> str:
    try:
        return validate_loopback_host(host)
    except ValueError as exc:
        raise McpHarnessError(
            "unsafe_remote_host",
            "remote TCP links are localhost-only; use 127.0.0.1, localhost, "
            "or ::1",
        ) from exc


def _validate_rom_version(version: str) -> str:
    normalized = version.strip().lower()
    if normalized not in SUPPORTED_ROM_VERSIONS:
        raise McpHarnessError(
            "unsupported_rom_version",
            f"ROM version must be one of {sorted(SUPPORTED_ROM_VERSIONS)}, "
            f"got {version!r}",
        )
    return normalized


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
        raise McpHarnessError(
            "invalid_timeout", f"invalid timeout_s: {value!r}"
        ) from exc
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
    return serial is not None and all(
        hasattr(serial, name)
        for name in ("backend", "apply_external_edge", "peek_out_bit")
    )


def _attach_network_backend(
    session: Session,
    backend: NetworkBackend,
    *,
    is_internal_clock: bool,
    local_rom_version: str,
) -> PyBoyLinkSession:
    """Attach a TCP bit-level backend to the session's existing PyBoy."""
    network_session = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=is_internal_clock,
        local_rom_version=local_rom_version,
    )
    with session.locked():
        network_session.attach(session._pyboy)
    return network_session


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
        raise McpHarnessError(
            "listen_failed", f"unable to bind {host}:{port}: {exc}"
        ) from exc


def _wait_for_remote_hello(
    link: TcpSerialLink,
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
        return

    deadline = time.monotonic() + timeout_s
    while not hello_event.is_set():
        if cancel is not None and cancel.is_set():
            raise McpHarnessError("link_cancelled", "remote connection cancelled")
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise McpHarnessError(
                "timeout", f"peer HELLO not received within {timeout_s:g}s"
            )
        hello_event.wait(timeout=min(0.05, remaining))
    try:
        peer_version = link.peer_rom_version
    except SerialLinkError as exc:
        raise McpHarnessError("link_handshake_failed", str(exc)) from exc
    if not peer_version:
        raise McpHarnessError("link_handshake_failed", "peer HELLO had no ROM version")


def _wait_for_network_hello(
    link: NetworkBackend,
    cancel: threading.Event | None,
    timeout_s: float,
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
            raise NetworkBackendError(
                f"peer HELLO not received within {timeout_s:g}s"
            )
        hello_event.wait(timeout=min(0.05, remaining))
    if link._reader_exc is not None and not link._peer_rom_version:
        raise NetworkBackendError(
            f"peer HELLO failed: {link._reader_exc}"
        ) from link._reader_exc
    return link.peer_rom_version


def _accept_remote(
    link: LinkState,
    session: Session,
    listener: socket.socket,
    cancel: threading.Event,
    generation: int,
    rom_version: str,
    timeout_s: float,
) -> None:
    try:
        while not cancel.is_set():
            transport: Any | None = None
            network_session: PyBoyLinkSession | None = None
            accepted_conn: socket.socket | None = None
            try:
                accepted_conn, _peer_addr = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if cancel.is_set():
                    return
                raise McpHarnessError("listen_failed", str(exc)) from exc

            try:
                accepted_conn.settimeout(None)
                if _supports_bit_accurate_network(session):
                    transport = NetworkBackend(
                        accepted_conn, local_rom_version=rom_version
                    )
                    accepted_conn = None
                    network_session = _attach_network_backend(
                        session,
                        transport,
                        is_internal_clock=True,
                        local_rom_version=rom_version,
                    )
                    _wait_for_network_hello(transport, cancel, timeout_s)
                    endpoint: RemoteLinkEndpoint | None = None
                else:
                    transport = TcpSerialLink(accepted_conn, rom_version)
                    accepted_conn = None
                    _wait_for_remote_hello(transport, cancel, timeout_s)
                    endpoint = RemoteLinkEndpoint.as_listener(session, transport)
                    endpoint.install()
                with link.state():
                    if (
                        cancel.is_set()
                        or link._disconnecting
                        or link._generation != generation
                    ):
                        raise _ListenerCancelled
                    link.remote_link = transport
                    link.remote_endpoint = endpoint
                    link.network_session = network_session
                    link.remote_mode = "connected"
                    link.remote_role = "listener"
                    link.remote_bind_port = None
                    link._listener_socket = None
                    link._listener_error = None
                    link._remote_error = None
                # Ownership moved into LinkState; the finalizer below must
                # not detach or close the live transport.
                transport = None
                network_session = None
                return
            except _ListenerCancelled:
                return
            except Exception as exc:  # noqa: BLE001
                if cancel.is_set():
                    return
                # A bad peer must not take down a listener that can still
                # accept a valid peer. Record the failure for diagnostics,
                # clean only this connection, then continue accepting.
                with link.state():
                    if link._generation == generation:
                        link._listener_error = exc
                if network_session is not None:
                    try:
                        network_session.detach_all()
                    except Exception:
                        pass
                if transport is not None:
                    _close_serial_link(transport)
                if accepted_conn is not None:
                    try:
                        accepted_conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        accepted_conn.close()
                    except OSError:
                        pass
    except _ListenerCancelled:
        return
    except Exception as exc:  # noqa: BLE001
        if not cancel.is_set():
            with link.state():
                if link._generation == generation:
                    link._listener_error = exc
                    link.remote_mode = "idle"
                    link.remote_role = None
                    link.remote_bind_port = None
    finally:
        try:
            listener.close()
        except OSError:
            pass
        with link.state():
            if link._listener_socket is listener:
                link._listener_socket = None


def _close_serial_link(
    link: Any, *, timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S
) -> bool:
    close_ok = True
    try:
        link.close()
    except Exception:
        close_ok = False
    workers = [
        getattr(link, "_reader", None),
        getattr(link, "_edge_worker", None),
    ]
    join_timeout = max(0.0, timeout_s)
    for worker in workers:
        if isinstance(worker, threading.Thread) and worker is not threading.current_thread():
            worker.join(timeout=join_timeout)
    return close_ok and not any(
        isinstance(worker, threading.Thread) and worker.is_alive()
        for worker in workers
    )


def _deactivate_link_hooks(session: Session, peer: Session | None = None) -> None:
    sessions: list[Session] = [session]
    if peer is not None and peer is not session:
        sessions.append(peer)
    for target in sessions:
        try:
            target.deactivate_serial_hooks()
        except Exception:
            pass
        # A few legacy endpoint hooks are installed directly on PyBoy and
        # cannot be reached through Session.serial_hook. Remove those by
        # symbol where the runtime supports hook_deregister.
        for symbol_name in _DIRECT_LINK_HOOKS:
            try:
                target.deactivate_hooks_at(symbol_name)
            except Exception:
                pass


def _disconnect_remote(link: LinkState, session: Session) -> None:
    """Cancel listener/connect work, close sockets, and join workers."""
    current_thread_id = threading.get_ident()
    with link.state():
        if link._disconnecting:
            done = link._disconnect_done
            owned_by_current_thread = link._disconnect_owner == current_thread_id
        else:
            done = None
            owned_by_current_thread = False
        if link._disconnecting:
            # A second disconnect should not report success while the first
            # caller is still tearing down sockets and callbacks. A
            # re-entrant call from the owner must return to avoid deadlock.
            pass
        elif not (
            link.remote_mode != "idle"
            or link._listener_socket is not None
            or link.remote_link is not None
            or link.remote_endpoint is not None
            or link.network_session is not None
            or link._connect_in_progress
        ):
            # In particular, do not deactivate local-pair hooks when the
            # caller uses the remote disconnect tool while no remote link is
            # active. The public contract says the two modes are independent.
            return
        else:
            link._disconnecting = True
            link._disconnect_owner = current_thread_id
            link._disconnect_done.clear()
            link._generation += 1
            listener = link._listener_socket
            listener_thread = link._listener_thread
            remote = link.remote_link
            remote_endpoint = link.remote_endpoint
            network_session = link.network_session
            connect_in_progress = link._connect_in_progress
            connect_done = link._connect_done
            link._listener_cancel.set()
            link._connect_cancel.set()
            link._listener_socket = None
            link.remote_link = None
            link.remote_endpoint = None
            link.network_session = None
            link.remote_mode = "idle"
            link.remote_role = None
            link.remote_bind_port = None

    if done is not None:
        if not owned_by_current_thread:
            done.wait(timeout=_DEFAULT_CLEANUP_TIMEOUT_S)
        return

    cleanup_errors: list[Exception] = []
    try:
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if remote is not None and not _close_serial_link(remote):
            cleanup_errors.append(
                TimeoutError("remote link worker did not stop before cleanup deadline")
            )
        if network_session is not None:
            try:
                network_session.detach_all()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
        if (
            isinstance(listener_thread, threading.Thread)
            and listener_thread is not threading.current_thread()
        ):
            listener_thread.join(timeout=_DEFAULT_CLEANUP_TIMEOUT_S)
        if connect_in_progress and not connect_done.wait(
            timeout=_DEFAULT_CLEANUP_TIMEOUT_S
        ):
            cleanup_errors.append(
                TimeoutError(
                    "connect worker did not stop before cleanup deadline"
                )
            )
        # A remote endpoint owns raw PyBoy callbacks; a listener that never
        # accepted a peer does not. This conditional also preserves an active
        # in-process pair when link_disconnect is called in its idle state.
        if remote_endpoint is not None or network_session is not None:
            _deactivate_link_hooks(session)
    finally:
        with link.state():
            if listener_thread is not None and listener_thread.is_alive():
                cleanup_errors.append(
                    TimeoutError(
                        "listener worker did not stop before cleanup deadline"
                    )
                )
            if cleanup_errors:
                link._listener_error = cleanup_errors[0]
                link._remote_error = cleanup_errors[0]
            else:
                link._listener_error = None
                link._remote_error = None
            link._listener_thread = None
            link._listener_cancel = threading.Event()
            if not connect_in_progress or connect_done.is_set():
                link._connect_cancel = threading.Event()
            link._disconnect_owner = None
            link._disconnecting = False
            link._disconnect_done.set()

    if cleanup_errors:
        details = "; ".join(
            f"{type(exc).__name__}: {exc}" for exc in cleanup_errors
        )
        raise McpHarnessError("link_teardown_failed", details)


def read_resource(
    session: Session,
    uri: str,
    link: LinkState | None = None,
) -> str:
    """Resource reader — returns JSON text."""
    if uri == _URI_GAME_STATE:
        return json.dumps(to_jsonable(session.read_game_state()))
    if uri == _URI_EVENT_LOG:
        events: list[GameEvent] = session.event_snapshot()
        return json.dumps([to_jsonable(e) for e in events])
    if uri == _URI_PEER_GAME_STATE:
        if link is None or link.peer_session is None:
            raise McpHarnessError("peer_not_configured", "peer session not configured")
        with link.state():
            peer = link.peer_session
        return json.dumps(to_jsonable(peer.read_game_state()))
    if uri == _URI_LINK_TRANSPORT:
        if link is None or link.pair is None:
            return json.dumps({"a_to_b": [], "b_to_a": []})
        with link.state():
            pair = link.pair
        return json.dumps(pair.transport.snapshot())
    if uri == _URI_LINK_STATUS:
        if link is None:
            link = LinkState()
        return json.dumps(dispatch_tool(session, "link_status", {}, link=link))
    raise ValueError(f"unknown resource: {uri!r}")


def _resource_specs(has_peer: bool = False) -> list[mcp_types.Resource]:
    specs = [
        mcp_types.Resource(
            uri=_URI_GAME_STATE,  # type: ignore[arg-type]
            name="Game State",
            description="Current parsed game state snapshot (JSON).",
            mimeType="application/json",
        ),
        mcp_types.Resource(
            uri=_URI_EVENT_LOG,  # type: ignore[arg-type]
            name="Event Log",
            description="All execution-hook events observed this session.",
            mimeType="application/json",
        ),
        mcp_types.Resource(
            uri=_URI_LINK_STATUS,  # type: ignore[arg-type]
            name="Link Status",
            description=(
                "Current link-cable state (in-process pair + remote TCP "
                "link). Equivalent to the `link_status` tool as a pollable "
                "resource."
            ),
            mimeType="application/json",
        ),
    ]
    if has_peer:
        specs.append(
            mcp_types.Resource(
                uri=_URI_PEER_GAME_STATE,  # type: ignore[arg-type]
                name="Peer Game State",
                description="Parsed game state of the peer session (JSON).",
                mimeType="application/json",
            )
        )
        specs.append(
            mcp_types.Resource(
                uri=_URI_LINK_TRANSPORT,  # type: ignore[arg-type]
                name="Link Transport",
                description="Current LinkTransport byte-queue snapshot (JSON).",
                mimeType="application/json",
            )
        )
    return specs


# -- server wiring -----------------------------------------------------------


def build_server(
    session: Session,
    *,
    peer_session: Session | None = None,
    primary_version: str = "red",
    peer_version: str = "red",
    link: LinkState | None = None,
) -> Server:
    """Construct an MCP ``Server`` bound to ``session``.

    When ``peer_session`` (or a pre-built ``link`` container) is provided,
    the server additionally exposes the ``link_*`` tools and peer
    resources. The pair itself is constructed lazily on the first
    ``link_pair`` call.

    The server stays un-run — caller invokes ``server.run`` via an
    appropriate transport. This makes the wiring itself testable without
    spawning stdio pipes.
    """
    if link is None:
        link = LinkState(
            peer_session=peer_session,
            primary_version=primary_version,
            peer_version=peer_version,
        )

    server: Server = Server("pokered-harness")

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return _tool_specs(has_peer=link.peer_session is not None)

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> mcp_types.CallToolResult:
        args = arguments or {}
        try:
            # The emulator is not asyncio-aware. Run the blocking operation
            # off the event loop while the Session/LinkState locks preserve
            # single-emulator ordering. This also leaves the loop responsive
            # to status/resource requests while a bounded TCP call waits.
            result = await asyncio.to_thread(
                dispatch_tool, session, name, args, link
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return _error_reply(exc)
        return _text_reply(result)

    @server.list_resources()
    async def _list_resources() -> list[mcp_types.Resource]:
        return _resource_specs(has_peer=link.peer_session is not None)

    @server.read_resource()
    async def _read_resource(uri: Any) -> str:
        return await asyncio.to_thread(read_resource, session, str(uri), link)

    return server


def register_default_hooks(session: Session) -> list[str]:
    """Register the ADR's canonical event hooks. Returns the event names
    that were wired up (skipping any whose symbol is missing)."""
    registered: list[str] = []
    for symbol_name, event_name in DEFAULT_HOOKS:
        if symbol_name not in session.symbols:
            continue
        session.register_hook(symbol_name, event_name)
        registered.append(event_name)
    return registered


# -- entry point -------------------------------------------------------------


async def serve_stdio(
    session: Session,
    *,
    peer_session: Session | None = None,
    primary_version: str = "red",
    peer_version: str = "red",
    link: LinkState | None = None,
) -> None:
    owned_link = link or LinkState(
        peer_session=peer_session,
        primary_version=primary_version,
        peer_version=peer_version,
    )
    server = build_server(
        session,
        peer_session=peer_session,
        primary_version=primary_version,
        peer_version=peer_version,
        link=owned_link,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            # stdio_server captures the real stdout file descriptor before
            # entering this block. Redirecting the Python stream only after
            # that capture keeps JSON-RPC on the original stdout while
            # routing emulator/library diagnostics printed during requests to
            # stderr as well as during boot.
            with contextlib.redirect_stdout(sys.stderr):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            _disconnect_remote(owned_link, session)
            # A stdio client can terminate while either link mode is active.
            # Restore native serial backends before the owning Sessions are
            # closed by main().
            try:
                dispatch_tool(session, "link_unpair", {}, link=owned_link)
            except Exception:
                pass


def main() -> None:
    """CLI entry point: ``python -m pokered_harness.mcp_server``.

    Required environment variables:

    * ``POKERED_ROM_PATH`` — path to the primary .gb ROM.
    * ``POKERED_SYM_PATH`` — path to the matching .sym file.

    Optional:

    * ``POKERED_ROM_SHA1`` — explicit pin. When unset, the server reads
      ``VERSIONS.md`` (relative to cwd) and enforces the SHA-1 from it. A
      wheel launched outside a checkout may use explicit primary and peer
      pins without a local ``VERSIONS.md``; the bundled runtime identity is
      still enforced.
      Set ``POKERED_SKIP_SHA1=1`` to opt out of SHA-1 enforcement
      entirely (useful for ad-hoc testing on non-stock ROMs).
    * ``POKERED_PEER_ROM_PATH`` / ``POKERED_PEER_SYM_PATH`` /
      ``POKERED_PEER_ROM_SHA1`` — when set, a peer Session is constructed
      at startup and the link-cable tools become usable. The pair is NOT
      auto-paired — invoke ``link_pair`` explicitly.
    """
    from pokered_harness.config import (
        VersionsConfigError,
        load_peer_env,
        load_primary_env,
        load_versions,
    )

    primary_env = load_primary_env()
    peer_env = load_peer_env()
    try:
        primary_env.validate(role="primary session")
        peer_env.validate(role="peer session")
    except VersionsConfigError as exc:
        raise SystemExit(f"invalid POKERED_* configuration: {exc}") from exc
    if primary_env.rom_path is None or primary_env.sym_path is None:
        raise SystemExit(
            "set POKERED_ROM_PATH and POKERED_SYM_PATH before launching"
        )

    primary_rom, primary_sym = primary_env.resolved_paths()
    assert primary_rom is not None and primary_sym is not None
    peer_rom, peer_sym = peer_env.resolved_paths()

    versions = None
    if not _env_flag("POKERED_SKIP_SHA1"):
        try:
            versions = load_versions()
        except VersionsConfigError as exc:
            # A wheel-installed server may be launched outside the source
            # checkout, where VERSIONS.md is intentionally not part of the
            # Python package. Explicit primary/peer hashes are sufficient
            # for ROM identity in that deployment; the bundled PyBoy marker
            # below still enforces the runtime contract. If either hash is
            # absent, fail closed rather than silently losing pin coverage.
            missing_explicit_hash = primary_env.rom_sha1 is None or (
                peer_rom is not None and peer_env.rom_sha1 is None
            )
            if missing_explicit_hash:
                raise SystemExit(
                    "unable to load ROM/PyBoy pins; set POKERED_VERSIONS_PATH, "
                    "provide explicit primary and peer ROM SHA-1 values, or "
                    "explicitly set POKERED_SKIP_SHA1=1: "
                    f"{exc}"
                ) from exc

    expected_sha: str | None = primary_env.rom_sha1
    if expected_sha is None and versions is not None:
        expected_sha = versions.sha1_for_path(primary_rom)
        if expected_sha is None:
            raise SystemExit(
                "no ROM SHA-1 pin matches POKERED_ROM_PATH; set "
                "POKERED_ROM_SHA1 explicitly"
            )
    if expected_sha is None and not _env_flag("POKERED_SKIP_SHA1"):
        raise SystemExit(
            "set POKERED_ROM_SHA1 or provide a matching per-ROM Path/SHA-1 "
            "entry in VERSIONS.md"
        )

    peer_expected_sha: str | None = peer_env.rom_sha1
    if peer_rom is not None and peer_expected_sha is None and versions is not None:
        peer_expected_sha = versions.sha1_for_path(peer_rom)
        if peer_expected_sha is None:
            raise SystemExit(
                "no ROM SHA-1 pin matches POKERED_PEER_ROM_PATH; set "
                "POKERED_PEER_ROM_SHA1 explicitly"
            )
    if peer_rom is not None and peer_expected_sha is None and not _env_flag(
        "POKERED_SKIP_SHA1"
    ):
        raise SystemExit(
            "set POKERED_PEER_ROM_SHA1 or provide a matching peer per-ROM "
            "Path/SHA-1 entry in VERSIONS.md"
        )
    if versions is not None:
        expected_pyboy = versions.pyboy_version
    elif _env_flag("POKERED_SKIP_SHA1"):
        expected_pyboy = None
    else:
        # Keep the runtime identity check active for a wheel launched without
        # a checkout-local VERSIONS.md. The vendored runtime exposes both
        # values; a stock or partially installed PyBoy must not pass as the
        # production runtime merely because its ROM hash was supplied.
        try:
            import pyboy as pyboy_module

            expected_pyboy = getattr(pyboy_module, "__version__", None)
            revision = getattr(
                pyboy_module, "__pokered_harness_revision__", None
            )
        except Exception as exc:  # pragma: no cover - environment-specific
            raise SystemExit(
                "unable to identify the bundled PyBoy runtime without "
                f"VERSIONS.md: {type(exc).__name__}: {exc}"
            ) from exc
        if not expected_pyboy or not revision:
            raise SystemExit(
                "installed PyBoy is missing the pokered-harness runtime "
                "identity; provide VERSIONS.md or install the bundled "
                "runtime"
            )

    # CRITICAL: PyBoy's init writes ~70KB of `.sym`-skip warnings to
    # stdout via a logging handler it installs before we get a chance
    # to reconfigure it. MCP's stdio transport uses stdout exclusively
    # for JSON-RPC, so anything the emulator prints would corrupt the
    # stream and wedge the client at ``initialize``. Capture all stdout
    # during boot and re-emit it on stderr so the noise is still
    # visible but out of the RPC channel.
    peer_session: Session | None = None
    session: Session | None = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            session = Session.from_files(
                primary_rom,
                primary_sym,
                expected_rom_sha1=expected_sha,
                expected_pyboy_version=expected_pyboy,
            )
            register_default_hooks(session)
            if peer_rom is not None and peer_sym is not None:
                peer_session = Session.from_files(
                    peer_rom,
                    peer_sym,
                    expected_rom_sha1=peer_expected_sha,
                    expected_pyboy_version=expected_pyboy,
                )
                register_default_hooks(peer_session)

        asyncio.run(
            serve_stdio(
                session,
                peer_session=peer_session,
                primary_version=primary_env.version,
                peer_version=peer_env.version,
            )
        )
    finally:
        if peer_session is not None:
            peer_session.close()
        if session is not None:
            session.close()


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_HOOKS",
    "LinkState",
    "build_server",
    "dispatch_tool",
    "main",
    "read_resource",
    "register_default_hooks",
    "serve_stdio",
]
