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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from pokered_harness.config import SUPPORTED_ROM_VERSIONS
from pokered_harness.events.hooks import GameEvent
from pokered_harness.link.emulated_time import CoordinatorClosed, EmulatedTimeError
from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
    validate_loopback_host,
)
from pokered_harness.link.pair import LinkPair
from pokered_harness.link.pyboy_link_session import (
    PairedSessionOperationError,
    PyBoyLinkSession,
)
from pokered_harness.link.remote import RemoteLinkEndpoint
from pokered_harness.link.serial_link import (
    SerialLink,
    SerialLinkError,
    SerialLinkTimeout,
    TcpSerialLink,
)
from pokered_harness.link.timed_wire import (
    Cancelled,
    ChannelClosed,
    DeadlineExceeded,
    ProtocolError,
    WireError,
)
from pokered_harness.mcp_timed_owner import (
    TimedOwner,
    TimedOwnerError,
    TimedOwnerPolicy,
)
from pokered_harness.ownership import (
    EmulatorOwnershipError,
    assert_no_emulator_scope,
)
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import (
    InvalidStateError,
    RomHashMismatch,
    RomNotFoundError,
    Session,
    SessionClosedError,
    SessionCloseError,
    SessionCloseTimeout,
    SessionConfigurationError,
    SessionEpoch,
    SessionError,
    SessionLockTimeout,
    StateSnapshot,
    SymbolHashMismatch,
    SymbolNotFoundError,
    VersionMismatch,
    locked_sessions,
)

try:
    from pyboy.core.serial import SerialBackendError as _SerialBackendError
except ImportError:
    # The optional latched backend-failure type was added by the bundled
    # runtime.  Keep source imports compatible with older PyBoy forks while
    # retaining the stable code when the type is available.
    _SERIAL_BACKEND_ERRORS: tuple[type[Exception], ...] = ()
else:
    _SERIAL_BACKEND_ERRORS = (_SerialBackendError,)


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
_MAX_STEP_TICKS = 10_000
_MAX_BUTTON_DURATION = 10_000
_MAX_RUN_TICKS = 100_000
_MAX_RUN_CHUNK = 1_000
_MAX_STATE_BYTES = 16 * 1024 * 1024
_MAX_EVENT_NAMES = 64
_MAX_EVENT_NAME_LENGTH = 128
_DIRECT_LINK_HOOKS = (
    "Serial_ExchangeBytes",
    "Serial_ExchangeNybble",
    "Serial_ExchangeLinkMenuSelection",
    "Serial_TryEstablishingExternallyClockedConnection",
)
_TIMED_OWNER_ERROR_CODES = frozenset(
    {
        "timed_cancelled",
        "timed_deadline",
        "timed_busy",
        "timed_owner_closed",
        "timed_queue_full",
        "timed_stale_generation",
        "timed_peer_mismatch",
        "timed_cleanup_failed",
    }
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
_URI_STATE_EPOCH = "pokered://state-epoch"
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
    - ``"starting"`` — a listener has reserved the lifecycle while its
      socket is being bound (transient).
    - ``"listening"`` — :meth:`TcpSerialLink.listen` running on a
      background thread; no peer yet.
    - ``"connecting"`` — an outbound connection and HELLO are in progress.
    - ``"connected"`` — peer attached; ``remote_endpoint`` installed and
      ready for in-game serial exchanges.
    - ``"disconnecting"`` — teardown has claimed the lifecycle (transient).
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
        # A native provider is published here before attach begins so a
        # failed/cancelled setup cannot lose the only cleanup handle.  It is
        # moved to ``local_link_session`` only after the generation check and
        # rollback boundary have both settled.
        self._pending_local_link_session: PyBoyLinkSession | None = None
        # Remote (two-process) state.
        self.remote_link: SerialLink | None = None
        self.remote_endpoint: RemoteLinkEndpoint | None = None
        self.network_session: PyBoyLinkSession | None = None
        self.remote_mode: str = "idle"
        self.remote_role: str | None = None  # "listener" | "connector"
        self.remote_bind_port: int | None = None
        self._listener_thread: threading.Thread | None = None
        self._listener_start_in_progress = False
        self._listener_start_owner: int | None = None
        self._listener_error: Exception | None = None
        self._remote_error: Exception | None = None
        self._listener_socket: socket.socket | None = None
        self._listener_cancel = threading.Event()
        self._connect_cancel = threading.Event()
        # Resources that could not be fully torn down remain owned by the
        # LinkState until a later link_disconnect retry completes.  Dropping
        # these references would let a live worker/socket outlive the MCP
        # lifecycle while the public state incorrectly returned to idle.
        self._pending_remote_link: Any | None = None
        self._pending_remote_endpoint: RemoteLinkEndpoint | None = None
        self._pending_network_session: PyBoyLinkSession | None = None
        self._pending_listener_socket: socket.socket | None = None
        self._disconnect_done = threading.Event()
        self._disconnect_done.set()
        self._disconnect_owner: int | None = None
        self._connect_done = threading.Event()
        self._connect_done.set()
        self._connect_in_progress = False
        self._state_lock = threading.RLock()
        # Re-entrant so shutdown can take a bounded outer acquisition before
        # calling the normal dispatcher, which takes the same guard again.
        self._operation_lock = threading.RLock()
        # Pair/native-link mutations and transport snapshots must not race.
        # This is deliberately separate from ``_operation_lock`` so a
        # read-only status request stays responsive while a remote connect is
        # waiting on the network.
        self._pair_lock = threading.RLock()
        self._disconnecting = False
        self._generation = 0
        # Local attach/pair setup has no transport socket for a concurrent
        # ``link_disconnect`` to wake.  Keep a separate generation token so
        # disconnect can cancel setup without dismantling an already
        # published local pair.
        self._local_generation = 0
        self._local_pair_in_progress = False
        self._local_setup_deadline: float | None = None
        self._local_setup_done = threading.Event()
        self._local_setup_done.set()

    @contextmanager
    def operation(self, *, timeout_s: float | None = None) -> Iterator[None]:
        """Serialize emulator/link mutations without blocking disconnect.

        Normal request dispatch keeps its historical wait-until-complete
        semantics.  Shutdown callers can provide a finite timeout so an
        in-flight request cannot strand stdio teardown behind this guard.
        """
        assert_no_emulator_scope("mutating link operation")
        timeout: float | None = None
        if timeout_s is None:
            acquired = self._operation_lock.acquire()
        else:
            timeout = _validate_timeout(timeout_s)
            acquired = self._operation_lock.acquire(timeout=timeout)
        if not acquired:
            raise McpHarnessError(
                "link_operation_timeout",
                "link operation did not release before the "
                f"{timeout:g}s shutdown deadline",
            )
        try:
            yield
        finally:
            self._operation_lock.release()

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
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_STEP_TICKS,
                    },
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
                    "duration": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_BUTTON_DURATION,
                        "default": 1,
                    },
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
                "properties": {
                    "data": {
                        "type": "string",
                        "maxLength": ((_MAX_STATE_BYTES + 2) // 3) * 4,
                    }
                },
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
                        "items": {
                            "type": "string",
                            "maxLength": _MAX_EVENT_NAME_LENGTH,
                        },
                        "minItems": 1,
                        "maxItems": _MAX_EVENT_NAMES,
                    },
                    "max_ticks": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_RUN_TICKS,
                    },
                    "chunk": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_RUN_CHUNK,
                        "default": 16,
                    },
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
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_STEP_TICKS,
                    },
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
                    "duration": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_BUTTON_DURATION,
                        "default": 1,
                    },
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
                "Default pacing role: listener leads frame turns; the ROM owns "
                "native serial registers and connection-status bytes. HELLO "
                "negotiation selects the non-Yellow pacing leader for "
                "cross-family pairs."
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
                    "peer_rom_version": {
                        "type": "string",
                        "description": "Optional expected peer ROM label; "
                                       "the connection is rejected on mismatch.",
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
                "Blocks until HELLO completes. Default pacing role: connector "
                "follows frame turns; the ROM owns native serial registers "
                "and connection-status bytes. HELLO negotiation selects the "
                "non-Yellow pacing leader for cross-family pairs."
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
                    "peer_rom_version": {
                        "type": "string",
                        "description": "Optional expected peer ROM label; "
                                       "the connection is rejected on mismatch.",
                    },
                    "timeout_s": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": _MAX_REMOTE_TIMEOUT_S,
                        "default": _DEFAULT_REMOTE_HELLO_TIMEOUT_S,
                    },
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


def _error_payload(exc: Exception) -> dict[str, Any]:
    """Build the stable error data shared by tool and resource failures."""
    return {
        "ok": False,
        "error": {
            "code": _error_code(exc),
            "message": str(exc) or type(exc).__name__,
            "type": type(exc).__name__,
        },
    }


def _error_reply(exc: Exception) -> mcp_types.CallToolResult:
    """Convert an internal exception into a stable MCP error envelope."""
    payload = _error_payload(exc)
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
        isError=True,
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, McpHarnessError):
        return exc.code
    if isinstance(exc, TimedOwnerError):
        # TimedOwnerError is the typed boundary for the persistent timed
        # owner. Only its published protocol codes may cross MCP; do not
        # trust an arbitrary ``.code`` attribute on another RuntimeError.
        if exc.code in _TIMED_OWNER_ERROR_CODES:
            return exc.code
        return "internal_error"
    if isinstance(exc, PairedSessionOperationError):
        return "paired_session_operation"
    if isinstance(exc, _SERIAL_BACKEND_ERRORS):
        return "serial_backend_error"
    if isinstance(exc, EmulatorOwnershipError):
        return "emulator_ownership_error"
    if isinstance(exc, SessionClosedError):
        return "session_closed"
    for error_type, code in (
        (RomNotFoundError, "rom_not_found"),
        (SymbolNotFoundError, "symbol_not_found"),
        (RomHashMismatch, "rom_hash_mismatch"),
        (SymbolHashMismatch, "symbol_hash_mismatch"),
        (SessionCloseTimeout, "session_close_timeout"),
        (SessionCloseError, "session_close_failed"),
        (SessionLockTimeout, "session_lock_timeout"),
    ):
        if isinstance(exc, error_type):
            return code
    for error_type, code in (
        (Cancelled, "timed_cancelled"),
        (DeadlineExceeded, "timed_deadline_exceeded"),
        (ProtocolError, "timed_protocol_error"),
        (ChannelClosed, "timed_channel_closed"),
        (CoordinatorClosed, "timed_coordinator_closed"),
        (EmulatedTimeError, "timed_execution_error"),
        (WireError, "timed_wire_error"),
    ):
        if isinstance(exc, error_type):
            return code
    if isinstance(exc, VersionMismatch):
        return "version_mismatch"
    if isinstance(exc, SessionConfigurationError):
        return "invalid_session_configuration"
    if isinstance(exc, InvalidStateError):
        return "invalid_state"
    if isinstance(exc, SessionError):
        return "session_error"
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
    max_encoded = ((_MAX_STATE_BYTES + 2) // 3) * 4
    if len(value) > max_encoded:
        raise McpHarnessError(
            "invalid_state",
            f"load_state.data exceeds the {_MAX_STATE_BYTES} byte limit",
        )
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise McpHarnessError(
            "invalid_state", "load_state.data is not valid base64"
        ) from exc
    if len(decoded) > _MAX_STATE_BYTES:
        raise McpHarnessError(
            "invalid_state",
            f"load_state.data exceeds the {_MAX_STATE_BYTES} byte limit",
        )
    return decoded


def _bounded_positive_int(value: Any, name: str, maximum: int) -> int:
    """Validate a finite positive integer accepted by a direct tool caller."""
    if isinstance(value, bool):
        raise McpHarnessError("invalid_argument", f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise McpHarnessError("invalid_argument", f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise McpHarnessError(
            "invalid_argument", f"{name} must be an integer"
        ) from exc
    if result <= 0:
        raise McpHarnessError(
            "invalid_argument", f"{name} must be positive, got {result}"
        )
    if result > maximum:
        raise McpHarnessError(
            "invalid_argument",
            f"{name} must be <= {maximum}, got {result}",
        )
    return result


def _event_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise McpHarnessError(
            "invalid_argument", "event_names must be a non-empty array"
        )
    if not value or len(value) > _MAX_EVENT_NAMES:
        raise McpHarnessError(
            "invalid_argument",
            f"event_names must contain 1..{_MAX_EVENT_NAMES} names",
        )
    names: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name:
            raise McpHarnessError(
                "invalid_argument", "event_names must contain non-empty strings"
            )
        if len(name) > _MAX_EVENT_NAME_LENGTH:
            raise McpHarnessError(
                "invalid_argument",
                f"event names must be <= {_MAX_EVENT_NAME_LENGTH} characters",
            )
        names.append(name)
    return names


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
        count = _bounded_positive_int(
            arguments["count"], "count", _MAX_STEP_TICKS
        )
        session.step(count, render=bool(arguments.get("render", False)))
        return {"tick": session.current_tick()}

    if name == "press":
        duration = _bounded_positive_int(
            arguments.get("duration", 1), "duration", _MAX_BUTTON_DURATION
        )
        session.press(arguments["button"], duration=duration)
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
        max_ticks = _bounded_positive_int(
            arguments["max_ticks"], "max_ticks", _MAX_RUN_TICKS
        )
        chunk = _bounded_positive_int(
            arguments.get("chunk", 16), "chunk", _MAX_RUN_CHUNK
        )
        result = session.run_until_event(
            _event_names(arguments["event_names"]),
            max_ticks=max_ticks,
            chunk=chunk,
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
    *,
    timed_owner: TimedOwner | None = None,
) -> Any:
    """Pure dispatch — no asyncio, no MCP types. Unit-testable on its own."""
    if timed_owner is not None:
        if name == "link_status":
            return _timed_status(timed_owner)
        request = _timed_tool_request(timed_owner, session, name, arguments, link)
        if name == "link_listen":
            try:
                return _timed_status_payload(
                    request.ready.result(timeout=max(0.0, request.deadline - time.monotonic()))
                )
            except TimeoutError as exc:
                if request.ready.done():
                    raise
                request.cancel()
                raise McpHarnessError(
                    "timed_deadline", "listener readiness deadline expired"
                ) from exc
        result = request.result()
        return _timed_status(timed_owner) if name in {"link_connect", "link_disconnect"} else result
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
            _require_remote_idle_locked(link)
            if (
                link._local_pair_in_progress
                or link._pending_local_link_session is not None
            ):
                raise McpHarnessError(
                    "link_busy",
                    "local setup cleanup is pending; call link_disconnect "
                    "before starting another pair",
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
        primary_supports_native = _supports_bit_accurate_network(session)
        peer_supports_native = _supports_bit_accurate_network(peer)
        if pair is None:
            _require_native_network_contract(
                session,
                supported=primary_supports_native,
                role="primary",
            )
            _require_native_network_contract(
                peer,
                supported=peer_supports_native,
                role="peer",
            )
        else:
            _reject_semantic_pair_on_native_runtime(pair, session, peer)

        # Reserve a local setup generation only after all admission checks.
        # ``link_disconnect`` can then cancel attach/pair setup without
        # acquiring the operation lock or touching a completed local pair.
        with link.state():
            local_generation = link._local_generation + 1
            link._local_generation = local_generation
            link._local_pair_in_progress = True
            link._local_setup_done.clear()

        if pair is None and primary_supports_native and peer_supports_native:
            local_link_session: PyBoyLinkSession | None = None
            lock_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
            try:
                local_link_session = PyBoyLinkSession.local()
                with link.state():
                    link._pending_local_link_session = local_link_session
                    link._local_setup_deadline = lock_deadline
                # Use a stable lock order; both sessions are owned by this
                # MCP server and no callback is invoked while attaching.
                with (
                    locked_sessions(
                        session,
                        peer,
                        timeout_s=_remaining(lock_deadline),
                    ),
                    link._pair_lock,
                ):
                    local_link_session.attach(session._pyboy)
                    local_link_session.attach(peer._pyboy)
                    if link._local_generation != local_generation:
                        raise McpHarnessError(
                            "link_cancelled", "local link pairing cancelled"
                        )
            except Exception as exc:
                cleanup_errors: list[Exception] = []
                if local_link_session is not None:
                    try:
                        _detach_local_link_session(
                            link,
                            session,
                            local_link_session,
                            timeout_s=_remaining(lock_deadline),
                        )
                    except Exception as cleanup_error:  # noqa: BLE001
                        cleanup_errors.append(cleanup_error)
                        # Preserve the attach failure; a later unpair/close
                        # can retry if restoration itself was interrupted.
                        exc.add_note(
                            f"local attach cleanup failed: {cleanup_error!r}"
                        )
                try:
                    hook_errors = _deactivate_link_hooks(
                        session,
                        peer,
                        timeout_s=_remaining(lock_deadline),
                    )
                    cleanup_errors.extend(hook_errors)
                except Exception as cleanup_error:  # noqa: BLE001
                    cleanup_errors.append(cleanup_error)
                    exc.add_note(
                        f"local hook cleanup failed: {cleanup_error!r}"
                    )
                if cleanup_errors:
                    with link.state():
                        # Keep ownership visible when restoration failed; the
                        # provider remains inspectable for a bounded retry.
                        link._pending_local_link_session = local_link_session
                        link._local_pair_in_progress = False
                        link._local_setup_deadline = None
                else:
                    with link.state():
                        if link._pending_local_link_session is local_link_session:
                            link._pending_local_link_session = None
                        if link._local_pair_in_progress:
                            link._local_pair_in_progress = False
                        if link._local_setup_deadline == lock_deadline:
                            link._local_setup_deadline = None
                link._local_setup_done.set()
                raise
            assert local_link_session is not None
            with link.state():
                if link._local_generation != local_generation:
                    cancelled = True
                else:
                    cancelled = False
                    link.local_link_session = local_link_session
                    if link._pending_local_link_session is local_link_session:
                        link._pending_local_link_session = None
                    if link._local_pair_in_progress:
                        link._local_pair_in_progress = False
                    if link._local_setup_deadline == lock_deadline:
                        link._local_setup_deadline = None
                    link._local_setup_done.set()
            if cancelled:
                cleanup_errors: list[Exception] = []
                try:
                    _detach_local_link_session(
                        link,
                        session,
                        local_link_session,
                        timeout_s=_remaining(lock_deadline),
                    )
                except Exception as cleanup_error:  # noqa: BLE001
                    cleanup_errors.append(cleanup_error)
                finally:
                    try:
                        cleanup_errors.extend(
                            _deactivate_link_hooks(
                                session,
                                peer,
                                timeout_s=_remaining(lock_deadline),
                            )
                        )
                    except Exception as cleanup_error:  # noqa: BLE001
                        cleanup_errors.append(cleanup_error)
                with link.state():
                    if not cleanup_errors:
                        if link._pending_local_link_session is local_link_session:
                            link._pending_local_link_session = None
                        if link.local_link_session is local_link_session:
                            link.local_link_session = None
                        if link._local_pair_in_progress:
                            link._local_pair_in_progress = False
                        if link._local_setup_deadline == lock_deadline:
                            link._local_setup_deadline = None
                    else:
                        # Keep the provider published for a later bounded
                        # retry; disconnect must not report idle while
                        # attached cores remain.
                        link._pending_local_link_session = local_link_session
                        link._local_pair_in_progress = False
                        link._local_setup_deadline = None
                link._local_setup_done.set()
                raise McpHarnessError(
                    "link_cancelled", "local link pairing cancelled"
                )
            return {
                "paired": True,
                "primary_version": link.primary_version,
                "peer_version": link.peer_version,
            }

        pair_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
        try:
            if pair is None:
                pair = LinkPair(
                    session,
                    peer,
                    version_primary=link.primary_version,
                    version_peer=link.peer_version,
                )
                with link.state():
                    link.pair = pair
            with (
                locked_sessions(
                    session,
                    peer,
                    timeout_s=_remaining(pair_deadline),
                ),
                link._pair_lock,
            ):
                assert pair is not None
                pair.pair()
                if link._local_generation != local_generation:
                    raise McpHarnessError(
                        "link_cancelled", "local link pairing cancelled"
                    )
            # Lock release can run integration callbacks or admit a racing
            # disconnect. Publish completion atomically with the final token
            # check, just as the native provider path does above.
            with link.state():
                if link._local_generation != local_generation:
                    raise McpHarnessError(
                        "link_cancelled", "local link pairing cancelled"
                    )
                link._local_pair_in_progress = False
        except Exception as exc:
            try:
                if pair is not None and pair.paired:
                    with (
                        locked_sessions(
                            session,
                            peer,
                            allow_closed=True,
                            timeout_s=_remaining(pair_deadline),
                        ),
                        link._pair_lock,
                    ):
                        pair.unpair()
            except Exception as cleanup_error:  # noqa: BLE001
                # Preserve the operation/cancellation error. The normal
                # unpair path remains available for a later retry.
                exc.add_note(f"local pair rollback failed: {cleanup_error!r}")
            if created:
                with link.state():
                    if pair is not None and link.pair is pair:
                        link.pair = None
            _deactivate_link_hooks(
                session,
                peer,
                timeout_s=_remaining(pair_deadline),
            )
            raise
        finally:
            with link.state():
                if link._local_pair_in_progress:
                    link._local_pair_in_progress = False
        return {
            "paired": True,
            "primary_version": link.primary_version,
            "peer_version": link.peer_version,
        }

    if name == "link_unpair":
        with link.state():
            if link._local_pair_in_progress:
                raise McpHarnessError(
                    "link_busy", "local setup is still unwinding; retry cleanup after it finishes"
                )
            pair = link.pair
            local_link_session = (
                link.local_link_session or link._pending_local_link_session
            )
        cleanup_errors: list[Exception] = []
        cleanup_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
        if local_link_session is not None:
            try:
                _detach_local_link_session(
                    link,
                    session,
                    local_link_session,
                    timeout_s=_remaining(cleanup_deadline),
                )
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            else:
                with link.state():
                    if link.local_link_session is local_link_session:
                        link.local_link_session = None
                    if link._pending_local_link_session is local_link_session:
                        link._pending_local_link_session = None
                    link._local_setup_deadline = None
                    link._local_setup_done.set()
        if pair is not None and pair.paired:
            try:
                peer = _require_peer(link)
                with (
                    locked_sessions(
                        session,
                        peer,
                        allow_closed=True,
                        timeout_s=_remaining(cleanup_deadline),
                    ),
                    link._pair_lock,
                ):
                    pair.unpair()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            else:
                with link.state():
                    if link.pair is pair:
                        link.pair = None
            finally:
                # Even a partially failing pair teardown must not leave raw
                # callbacks able to reach a discarded bridge.
                _deactivate_link_hooks(
                    session,
                    pair.peer,
                    timeout_s=_remaining(cleanup_deadline),
                )
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
        count = _bounded_positive_int(
            arguments["count"], "count", _MAX_STEP_TICKS
        )
        # A TCP reader can observe EOF between requests. Reconcile that
        # background state before taking a snapshot so a stale endpoint is
        # never stepped after its transport has closed.
        _refresh_remote_state(link, session)
        with link.state():
            network_session = link.network_session
            local_link_session = link.local_link_session
            remote_endpoint = link.remote_endpoint
            remote_mode = link.remote_mode
            remote_link = link.remote_link
            remote_error = link._remote_error
        if network_session is not None:
            session.step(count, render=bool(arguments.get("render", False)))
            return {"primary_tick": session.current_tick(), "peer_tick": None}
        if remote_endpoint is not None:
            # The semantic endpoint ticks through Session.step() but performs
            # its hardware serial tick immediately afterward. Keep that
            # memory mutation inside the same emulator lock as the frame so
            # status/resource readers and disconnect cannot observe a half
            # completed endpoint step.
            with session.locked(timeout_s=_DEFAULT_CLEANUP_TIMEOUT_S):
                remote_endpoint.step(
                    count, render=bool(arguments.get("render", False))
                )
            return {"primary_tick": session.current_tick(), "peer_tick": None}
        if local_link_session is not None:
            peer = _require_peer(link)
            # The interleaved path drives PyBoy directly rather than through
            # Session.step(), so retain both canonical owner locks through
            # provider work and bookkeeping.  The provider lock is acquired
            # only after those owner locks to keep one global order.
            lock_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
            with (
                locked_sessions(
                    session,
                    peer,
                    timeout_s=_remaining(lock_deadline),
                ),
                link._pair_lock,
            ):
                local_link_session.step_interleaved(
                    count, render=bool(arguments.get("render", False))
                )
                # PyBoyLinkSession drives the underlying emulators
                # directly; keep Session-level bookkeeping aligned with
                # the same frame count.
                session.reset_tick(session.current_tick() + count)
                peer.reset_tick(peer.current_tick() + count)
                return {
                    "primary_tick": session.current_tick(),
                    "peer_tick": peer.current_tick(),
                }
        if remote_mode != "idle" or remote_link is not None or remote_error is not None:
            raise McpHarnessError(
                "remote_not_connected",
                f"remote link is not ready for stepping (mode={remote_mode!r})",
            )
        pair = _require_pair(link)
        peer = _require_peer(link)
        lock_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
        with (
            locked_sessions(
                session,
                peer,
                timeout_s=_remaining(lock_deadline),
            ),
            link._pair_lock,
        ):
            pair.step(count, render=bool(arguments.get("render", False)))
        return {
            "primary_tick": session.current_tick(),
            "peer_tick": link.peer_session.current_tick() if link.peer_session else None,
        }

    if name == "link_peer_press":
        peer = _require_peer(link)
        duration = _bounded_positive_int(
            arguments.get("duration", 1), "duration", _MAX_BUTTON_DURATION
        )
        peer.press(arguments["button"], duration=duration)
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
            remote_mode = link.remote_mode
            remote_role = link.remote_role
            remote_bind_port = link.remote_bind_port
            remote_link = link.remote_link
            network_session = link.network_session
            remote_error = link._remote_error or link._listener_error
        with link._pair_lock:
            paired = pair is not None and pair.paired
            paired = paired or (
                local_link_session is not None and local_link_session.paired
            )
            transport_snapshot: dict[str, Any] = (
                pair.transport.snapshot()
                if pair is not None
                else {"a_to_b": [], "b_to_a": []}
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
        # Validate the complete request and prove native serial capability
        # before reserving lifecycle state or binding a socket.  An
        # unsupported runtime must be observationally inert.
        port = _positive_port(arguments.get("port"))
        host = _validate_remote_host(str(arguments.get("host", "127.0.0.1")))
        rom_version = _validate_rom_version(
            str(arguments.get("rom_version") or link.primary_version)
        )
        _require_primary_rom_version(link, rom_version)
        timeout_s = _validate_timeout(
            arguments.get("timeout_s", _DEFAULT_REMOTE_HELLO_TIMEOUT_S)
        )
        expected_peer_version = _optional_rom_version(
            arguments.get("peer_rom_version")
        )
        cancel = threading.Event()
        start_owner = threading.get_ident()
        with link.state():
            # The idle check and reservation must be one critical section.
            # Otherwise link_disconnect can observe an apparently idle state,
            # return, and then race this call into publishing a listener it
            # never had a chance to cancel.
            _require_remote_idle_locked(link)
            _require_pair_inactive_locked(link)
            native_supports = _supports_bit_accurate_network(session)
            _require_native_network_contract(
                session,
                supported=native_supports,
                role="primary",
            )

            # Every new remote attempt gets its own generation.  A status
            # request may be finishing cleanup of an older dead transport
            # while a reconnect starts; without a new token, that status
            # request could publish the old reader error onto the new idle
            # lifecycle.
            generation = link._generation + 1
            link._generation = generation
            link._listener_cancel = cancel
            link._listener_start_in_progress = True
            link._listener_start_owner = start_owner
            link.remote_mode = "starting"
            link.remote_role = "listener"
            link.remote_bind_port = port
            link._listener_error = None
            link._remote_error = None

        try:
            try:
                listener = _bind_listener(host, port)
            except Exception:
                cancel.set()
                with link.state():
                    if (
                        link._generation == generation
                        and link.remote_mode == "starting"
                    ):
                        link.remote_mode = "idle"
                        link.remote_role = None
                        link.remote_bind_port = None
                        if link._listener_cancel is cancel:
                            link._listener_cancel = threading.Event()
                raise

            def _accept() -> None:
                _accept_remote(
                    link,
                    session,
                    listener,
                    cancel,
                    generation,
                    rom_version,
                    timeout_s,
                    expected_peer_version,
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
                    if (
                        link._disconnecting
                        or link._generation != generation
                        or link.remote_mode != "starting"
                    ):
                        raise McpHarnessError(
                            "link_cancelled", "remote listener cancelled"
                        )
                    link._listener_socket = listener
                    link.remote_mode = "listening"
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
                    if (
                        link._generation == generation
                        and not link._disconnecting
                    ):
                        link.remote_mode = "idle"
                        link.remote_role = None
                        link.remote_bind_port = None
                        if link._listener_cancel is cancel:
                            link._listener_cancel = threading.Event()
                raise
            return {
                "remote_mode": "listening",
                "remote_role": "listener",
                "remote_bind_port": port,
                "host": host,
                "rom_version": rom_version,
                "timeout_s": timeout_s,
            }
        finally:
            # A listener bind runs in the caller's thread, so it cannot be
            # joined by link_disconnect. Keep reconnects out until this call
            # has unwound and the cancelled socket has been closed.
            with link.state():
                if link._listener_start_owner == start_owner:
                    link._listener_start_in_progress = False
                    link._listener_start_owner = None

    if name == "link_connect":
        # Perform all argument and native-runtime validation before lifecycle
        # reservation, transport construction, or any socket I/O.
        host = _validate_remote_host(str(arguments["host"]))
        port = _positive_port(arguments.get("port"))
        rom_version = _validate_rom_version(
            str(arguments.get("rom_version") or link.primary_version)
        )
        _require_primary_rom_version(link, rom_version)
        timeout_s = _validate_timeout(arguments.get("timeout_s", 10.0))
        expected_peer_version = _optional_rom_version(
            arguments.get("peer_rom_version")
        )
        with link.state():
            # Claim the remote lifecycle before releasing the state lock so
            # a concurrent disconnect cannot miss an in-progress connect.
            _require_remote_idle_locked(link)
            _require_pair_inactive_locked(link)
            native_supports = _supports_bit_accurate_network(session)
            _require_native_network_contract(
                session,
                supported=native_supports,
                role="primary",
            )
            # Reserve a fresh token for this attempt so a racing status
            # cleanup cannot attach an older reader failure to its result.
            generation = link._generation + 1
            link._generation = generation
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
            if native_supports:
                transport = NetworkBackend.connect(
                    host,
                    port,
                    timeout_s=timeout_s,
                    local_rom_version=rom_version,
                    cancel_event=connect_cancel,
                )
                _track_unpublished_remote_resource(
                    link, generation, transport=transport
                )
                network_session = _attach_network_backend(
                    session,
                    transport,
                    is_internal_clock=False,
                    local_rom_version=rom_version,
                    network_hello_timeout_s=max(0.0, _remaining(deadline)),
                )
                _track_unpublished_remote_resource(
                    link, generation, network_session=network_session
                )
                peer_version = _wait_for_network_hello(
                    transport,
                    connect_cancel,
                    _remaining(deadline),
                    expected_peer_rom_version=expected_peer_version,
                )
                _negotiate_network_clock_role(
                    session,
                    network_session,
                    peer_version,
                    timeout_s=_remaining(deadline),
                )
            else:  # pragma: no cover - admission above is fail-closed
                raise McpHarnessError(
                    "unsupported_runtime",
                    "primary PyBoy lost the pinned serial contract during "
                    "remote connection setup",
                )
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
                if link._pending_remote_link is transport:
                    link._pending_remote_link = None
                if link._pending_remote_endpoint is endpoint:
                    link._pending_remote_endpoint = None
                if link._pending_network_session is network_session:
                    link._pending_network_session = None
                link.remote_mode = "connected"
                link.remote_role = "connector"
                link.remote_bind_port = None
                link._remote_error = None
        except McpHarnessError:
            _cleanup_unpublished_remote(
                session,
                network_session,
                transport,
                link=link,
                endpoint=endpoint,
                generation=generation,
            )
            raise
        except _ListenerCancelled as exc:
            _cleanup_unpublished_remote(
                session,
                network_session,
                transport,
                link=link,
                endpoint=endpoint,
                generation=generation,
            )
            raise McpHarnessError(
                "link_cancelled", "remote connection cancelled"
            ) from exc
        except (SerialLinkError, NetworkBackendError, OSError, TimeoutError) as exc:
            _cleanup_unpublished_remote(
                session,
                network_session,
                transport,
                link=link,
                endpoint=endpoint,
                generation=generation,
            )
            if connect_cancel.is_set():
                raise McpHarnessError(
                    "link_cancelled", "remote connection cancelled"
                ) from exc
            if _error_code(exc) == "timeout":
                raise McpHarnessError("timeout", str(exc)) from exc
            raise McpHarnessError("link_connect_failed", str(exc)) from exc
        except BaseException:
            # Until state publication succeeds, this call owns every
            # transport/backend it has created.  Always release unpublished
            # resources even when an unexpected runtime or hook failure
            # escapes the protocol-specific handlers above.
            _cleanup_unpublished_remote(
                session,
                network_session,
                transport,
                link=link,
                endpoint=endpoint,
                generation=generation,
            )
            raise
        finally:
            with link.state():
                if link._generation == generation and link.remote_link is None:
                    pending = (
                        link._pending_remote_link is not None
                        or link._pending_remote_endpoint is not None
                        or link._pending_network_session is not None
                    )
                    link.remote_mode = "disconnecting" if pending else "idle"
                    link.remote_role = None
                    link.remote_bind_port = None
                    if not pending:
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
            f"remote link busy (mode={link.remote_mode!r}); call "
            f"link_disconnect first",
        )
    if listener_thread is not None:
        raise McpHarnessError(
            "remote_busy",
            "remote listener cleanup is still in progress; retry after "
            "the listener worker exits",
        )
    if link._listener_start_in_progress:
        raise McpHarnessError(
            "remote_busy",
            "remote listener startup is still in progress; retry after "
            "the listener call unwinds",
        )


def _require_remote_idle(link: LinkState) -> None:
    with link.state():
        _require_remote_idle_locked(link)


def _require_pair_inactive_locked(link: LinkState) -> None:
    """Validate the in-process mode while ``link.state()`` is held."""
    with link._pair_lock:
        if (link.pair is not None and link.pair.paired) or (
            link.local_link_session is not None
        ) or link._local_pair_in_progress or (
            link._pending_local_link_session is not None
        ):
            raise McpHarnessError(
                "pair_active",
                "in-process pair is active; call link_unpair before using "
                "remote link tools",
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
        if (
            rl is None
            or not connected
            or rl.connected
        ):
            return
        stale_generation = link._generation
        stale_error = getattr(rl, "_reader_exc", None)
    # Reuse the lifecycle owner so status cleanup marks the link as
    # disconnecting until socket/backend/callback cleanup has completed. This
    # prevents a concurrent reconnect from overlapping a dead transport.
    try:
        _disconnect_remote(link, session)
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
    if serial is None or getattr(serial, "backend", None) is None:
        return False
    return callable(getattr(serial, "apply_external_edge", None)) and callable(
        getattr(serial, "peek_out_bit", None)
    )


def _is_real_pyboy_instance(session: Session) -> bool:
    """Identify a real PyBoy object without importing or probing internals."""
    pyboy = getattr(session, "_pyboy", None)
    return type(pyboy).__module__.startswith("pyboy.")


def _require_native_network_contract(
    session: Session, *, supported: bool, role: str
) -> None:
    """Fail closed before an implicit link can mutate or open transport."""
    if supported:
        return
    raise McpHarnessError(
        "unsupported_runtime",
        f"{role} PyBoy does not expose the pinned bit-accurate serial "
        "contract; install the bundled PyBoy source runtime",
    )


def _reject_semantic_pair_on_native_runtime(
    pair: LinkPair,
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
        if _supports_bit_accurate_network(candidate) or _is_real_pyboy_instance(
            candidate
        ):
            raise McpHarnessError(
                "unsupported_runtime",
                f"{role} PyBoy requires the pinned bit-accurate serial "
                "runtime; semantic link pairs are only available for "
                "explicit non-native test doubles",
            )


def _attach_network_backend(
    session: Session,
    backend: NetworkBackend,
    *,
    is_internal_clock: bool,
    local_rom_version: str,
    network_hello_timeout_s: float | None = None,
) -> PyBoyLinkSession:
    """Attach a TCP backend while honoring the MCP handshake deadline.

    ``PyBoyLinkSession`` owns the native attach sequence, including its HELLO
    wait. MCP supplies the request deadline here until that lower-level API
    exposes a per-attach timeout.
    """
    network_session = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=is_internal_clock,
        local_rom_version=local_rom_version,
    )
    if network_hello_timeout_s is not None:
        network_session._NETWORK_HELLO_TIMEOUT_SECONDS = network_hello_timeout_s
    with session.locked(timeout_s=_DEFAULT_CLEANUP_TIMEOUT_S):
        network_session.attach(session._pyboy)
    return network_session


def _negotiate_network_clock_role(
    session: Session,
    network_session: PyBoyLinkSession,
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
        if not link.connected:
            raise McpHarnessError(
                "link_handshake_failed", "remote link closed during HELLO"
            )
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
    if not link.connected:
        raise McpHarnessError(
            "link_handshake_failed", "remote link closed during HELLO"
        )


def _wait_for_network_hello(
    link: NetworkBackend,
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
            raise NetworkBackendError(
                f"peer HELLO not received within {timeout_s:g}s"
            )
        hello_event.wait(timeout=min(0.05, remaining))
    if link._reader_exc is not None:
        raise NetworkBackendError(
            f"peer HELLO failed: {link._reader_exc}"
        ) from link._reader_exc
    if not link.connected:
        raise NetworkBackendError("peer link closed during HELLO")
    peer_version = link.peer_rom_version
    if (
        expected_peer_rom_version is not None
        and peer_version != expected_peer_rom_version
    ):
        error = NetworkBackendError(
            f"peer ROM version {peer_version!r} does not match expected "
            f"{expected_peer_rom_version!r}"
        )
        link._mark_closed(error)
        raise error
    return peer_version


def _track_unpublished_remote_resource(
    link: LinkState,
    generation: int,
    *,
    transport: Any | None = None,
    endpoint: RemoteLinkEndpoint | None = None,
    network_session: PyBoyLinkSession | None = None,
) -> None:
    """Publish setup ownership before a remote handshake completes.

    A listener/connect worker can block in HELLO while ``link_disconnect`` is
    already trying to cancel it. Keeping these handles on ``LinkState`` gives
    teardown a way to close the transport and detach the backend immediately;
    the worker still owns the local variables and performs idempotent cleanup
    when it unwinds.
    """
    with link.state():
        if link._generation != generation and not link._disconnecting:
            raise McpHarnessError(
                "link_cancelled", "remote connection cancelled"
            )
        if transport is not None:
            link._pending_remote_link = transport
        if endpoint is not None:
            link._pending_remote_endpoint = endpoint
        if network_session is not None:
            link._pending_network_session = network_session


def _accept_remote(
    link: LinkState,
    session: Session,
    listener: socket.socket,
    cancel: threading.Event,
    generation: int,
    rom_version: str,
    timeout_s: float,
    expected_peer_rom_version: str | None,
) -> None:
    try:
        while not cancel.is_set():
            transport: Any | None = None
            network_session: PyBoyLinkSession | None = None
            endpoint: RemoteLinkEndpoint | None = None
            accepted_conn: socket.socket | None = None
            try:
                accepted_conn, _peer_addr = listener.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if cancel.is_set():
                    return
                raise McpHarnessError("listen_failed", str(exc)) from exc

            try:
                accepted_conn.settimeout(None)
                native_supports = _supports_bit_accurate_network(session)
                if native_supports:
                    transport = NetworkBackend(
                        accepted_conn, local_rom_version=rom_version
                    )
                    accepted_conn = None
                    _track_unpublished_remote_resource(
                        link, generation, transport=transport
                    )
                    network_session = _attach_network_backend(
                        session,
                        transport,
                        is_internal_clock=True,
                        local_rom_version=rom_version,
                        network_hello_timeout_s=timeout_s,
                    )
                    _track_unpublished_remote_resource(
                        link, generation, network_session=network_session
                    )
                    _wait_for_network_hello(
                        transport,
                        cancel,
                        timeout_s,
                        expected_peer_rom_version=expected_peer_rom_version,
                    )
                    _negotiate_network_clock_role(
                        session,
                        network_session,
                        transport.peer_rom_version,
                        timeout_s=timeout_s,
                    )
                else:  # pragma: no cover - dispatcher admission is fail-closed
                    raise McpHarnessError(
                        "unsupported_runtime",
                        "primary PyBoy lost the pinned serial contract while "
                        "accepting a remote connection",
                    )
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
                    if link._pending_remote_link is transport:
                        link._pending_remote_link = None
                    if link._pending_remote_endpoint is endpoint:
                        link._pending_remote_endpoint = None
                    if link._pending_network_session is network_session:
                        link._pending_network_session = None
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
                endpoint = None
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
            finally:
                # The accepted connection is not published to LinkState until
                # HELLO succeeds.  Therefore link_disconnect can cancel this
                # worker while it is waiting for HELLO, leaving these locals
                # as the only owners.  Always release them here, including
                # the cancellation path, or the transport reader can outlive
                # the MCP link and retain the peer socket indefinitely.
                if (
                    transport is not None
                    or endpoint is not None
                    or network_session is not None
                ):
                    cleanup_errors = _cleanup_unpublished_remote(
                        session,
                        network_session,
                        transport,
                        link=link,
                        endpoint=endpoint,
                        generation=generation,
                    )
                    if cleanup_errors:
                        cancel.set()
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
    deadline = time.monotonic() + max(0.0, timeout_s)
    close_ok = True
    try:
        if isinstance(link, NetworkBackend):
            close_ok = bool(
                link.close(timeout_s=max(0.0, deadline - time.monotonic()))
            )
        else:
            link.close()
    except Exception:  # noqa: BLE001 - cleanup must report failure, not mask it
        close_ok = False
    workers: list[threading.Thread] = []
    seen_workers: set[int] = set()
    for attr in ("_reader", "_edge_worker"):
        worker = getattr(link, attr, None)
        if not isinstance(worker, threading.Thread):
            continue
        if id(worker) in seen_workers:
            continue
        seen_workers.add(id(worker))
        workers.append(worker)
    for worker in workers:
        if worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return close_ok and not any(worker.is_alive() for worker in workers)


def _uninstall_remote_endpoint(
    session: Session,
    endpoint: Any,
    *,
    timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S,
) -> list[Exception]:
    """Release an endpoint's owned callbacks during transport teardown.

    The MCP lifecycle retains a legacy symbol-based cleanup fallback for
    endpoint implementations that predate ``RemoteLinkEndpoint.uninstall``.
    Calling the owned teardown first lets current endpoints clear both their
    physical hooks and their endpoint/session ownership records.
    """
    uninstall = getattr(endpoint, "uninstall", None)
    if not callable(uninstall):
        return []
    try:
        # RemoteLinkEndpoint.uninstall() calls deactivate_hooks_at once per
        # owned symbol. Acquire the Session lock once with the caller's
        # remaining deadline so those re-entrant calls cannot each consume a
        # fresh five-second lock timeout. ``allow_closed`` is intentional:
        # hook guards must still be released during terminal cleanup.
        with session.locked(
            timeout_s=max(0.0, timeout_s), allow_closed=True
        ):
            uninstall()
    except Exception as exc:  # noqa: BLE001 - cleanup must continue
        return [exc]
    return []


def _cleanup_unpublished_remote(
    session: Session,
    network_session: PyBoyLinkSession | None,
    transport: Any | None,
    *,
    link: LinkState | None = None,
    endpoint: RemoteLinkEndpoint | None = None,
    generation: int | None = None,
) -> list[Exception]:
    """Release resources created before a remote connection was published.

    Connection setup has several failure points after the socket and native
    serial backend exist. Cleanup must be best-effort and independent: a
    failing detach must not prevent the transport from closing, and a close
    failure must not leave raw link hooks active. The original connection
    exception remains the client-visible failure.
    """
    cleanup_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
    cleanup_errors: list[Exception] = []
    transport_close_failed = False
    network_detach_failed = False
    hook_cleanup_failed = False
    if network_session is not None:
        try:
            with session.locked(timeout_s=_remaining(cleanup_deadline)):
                network_session.detach_all()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
            network_detach_failed = True
            cleanup_errors.append(exc)
    if transport is not None:
        try:
            transport_closed = _close_serial_link(
                transport,
                timeout_s=_remaining(cleanup_deadline),
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
            transport_closed = False
            cleanup_errors.append(exc)
        if not transport_closed:
            transport_close_failed = True
            cleanup_errors.append(
                TimeoutError(
                    "unpublished remote transport did not close before "
                    "the cleanup deadline"
                )
            )
    if endpoint is not None:
        endpoint_errors = _uninstall_remote_endpoint(
            session,
            endpoint,
            timeout_s=_remaining(cleanup_deadline),
        )
        if endpoint_errors:
            hook_cleanup_failed = True
            cleanup_errors.extend(endpoint_errors)
    try:
        hook_errors = _deactivate_link_hooks(
            session,
            timeout_s=_remaining(cleanup_deadline),
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
        hook_errors = [exc]
    if hook_errors:
        hook_cleanup_failed = True
        cleanup_errors.extend(hook_errors)

    if link is not None:
        with link.state():
            # A disconnect may have advanced the generation while this worker
            # was unwinding. Its teardown finalizer still preserves pending
            # handles published during that disconnect, but never attach an
            # old worker to a newly started lifecycle.
            same_lifecycle = link._generation == generation
            if link.remote_link is None and (same_lifecycle or link._disconnecting):
                if (
                    link._pending_remote_link is transport
                    and not transport_close_failed
                ):
                    link._pending_remote_link = None
                if (
                    link._pending_remote_endpoint is endpoint
                    and not hook_cleanup_failed
                ):
                    link._pending_remote_endpoint = None
                if (
                    link._pending_network_session is network_session
                    and not network_detach_failed
                    and not hook_cleanup_failed
                ):
                    link._pending_network_session = None
            if cleanup_errors and link.remote_link is None and (
                same_lifecycle or link._disconnecting
            ):
                if transport_close_failed and transport is not None:
                    link._pending_remote_link = transport
                if hook_cleanup_failed and endpoint is not None:
                    link._pending_remote_endpoint = endpoint
                if (
                    (network_detach_failed or hook_cleanup_failed)
                    and network_session is not None
                ):
                    link._pending_network_session = network_session
                link.remote_mode = "disconnecting"
                link.remote_role = None
                link.remote_bind_port = None
                link._remote_error = cleanup_errors[0]
    return cleanup_errors


def _detach_local_link_session(
    link: LinkState,
    session: Session,
    local_link_session: PyBoyLinkSession,
    *,
    timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S,
) -> None:
    """Detach a local native link while both owned emulators are locked."""
    peer = link.peer_session
    # Acquire emulator owners before the provider/pair lock.  This keeps
    # local stepping, detach, and raw provider callbacks on one order even
    # when the primary/peer objects were supplied in reverse identity order.
    targets = [session]
    if peer is not None and peer is not session:
        targets.append(peer)
    with locked_sessions(
        *targets,
        allow_closed=True,
        timeout_s=max(0.0, timeout_s),
    ), link._pair_lock:
        local_link_session.detach_all()


def _deactivate_link_hooks(
    session: Session,
    peer: Session | None = None,
    *,
    timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S,
) -> list[Exception]:
    """Disable and deregister link callbacks within one total deadline."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    errors: list[Exception] = []
    sessions: list[Session] = [session]
    if peer is not None and peer is not session:
        sessions.append(peer)
    try:
        # Acquire all emulator owners canonically before deactivating any
        # provider hook.  Nested Session calls are re-entrant, so this single
        # deadline also prevents primary/peer cleanup from observing a half
        # detached pair.
        with locked_sessions(
            *sessions,
            allow_closed=True,
            timeout_s=_remaining(deadline),
        ):
            for target in sessions:
                try:
                    target.deactivate_serial_hooks(timeout_s=0.0)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                # A few legacy endpoint hooks are installed directly on
                # PyBoy and cannot be reached through Session.serial_hook.
                # Remove those by symbol where the runtime supports
                # hook_deregister.
                for symbol_name in _DIRECT_LINK_HOOKS:
                    try:
                        target.deactivate_hooks_at(
                            symbol_name, timeout_s=0.0
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        errors.append(exc)
    return errors


def _disconnect_remote(link: LinkState, session: Session) -> None:
    """Cancel listener/connect work, close sockets, and join workers.

    A teardown failure is not allowed to look like a clean idle transition.
    Any resource whose close/detach/join did not complete remains owned by the
    ``LinkState`` as a pending cleanup handle, so a later ``link_disconnect``
    can retry it and reconnect cannot overlap a live worker or socket.
    """
    current_thread_id = threading.get_ident()
    cancel_local_setup = False
    retry_local_cleanup = False
    pending_local: PyBoyLinkSession | None = None
    local_peer: Session | None = None
    with link.state():
        if link._local_pair_in_progress:
            if not link._local_setup_done.is_set():
                # Local setup is owned by the pairing worker and may be
                # holding both emulator locks. Advance its generation and
                # return so the worker performs exactly-once rollback; never
                # dismantle a completed local pair from this path.
                link._local_generation += 1
                cancel_local_setup = True
            elif link._pending_local_link_session is not None:
                # A worker which already returned after a failed rollback
                # leaves its provider published for an explicit retry.
                retry_local_cleanup = True
                pending_local = link._pending_local_link_session
            local_peer = link.peer_session
        elif link._pending_local_link_session is not None:
            retry_local_cleanup = True
            pending_local = link._pending_local_link_session
            local_peer = link.peer_session
        if cancel_local_setup or retry_local_cleanup:
            pass
        elif link._disconnecting:
            done = link._disconnect_done
            owned_by_current_thread = link._disconnect_owner == current_thread_id
        else:
            done = None
            owned_by_current_thread = False
        listener_thread = link._listener_thread
        if isinstance(listener_thread, threading.Thread) and not listener_thread.is_alive():
            link._listener_thread = None
            listener_thread = None
        if cancel_local_setup or retry_local_cleanup:
            pass
        elif link._disconnecting:
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
            or link._pending_remote_link is not None
            or link._pending_remote_endpoint is not None
            or link._pending_network_session is not None
            or link._pending_listener_socket is not None
            or link._pending_local_link_session is not None
            or link._connect_in_progress
            or link._listener_start_in_progress
            or listener_thread is not None
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
            if listener is None:
                listener = link._pending_listener_socket
            remote = (
                link.remote_link
                if link.remote_link is not None
                else link._pending_remote_link
            )
            remote_endpoint = (
                link.remote_endpoint
                if link.remote_endpoint is not None
                else link._pending_remote_endpoint
            )
            network_session = (
                link.network_session
                if link.network_session is not None
                else link._pending_network_session
            )
            connect_in_progress = link._connect_in_progress
            connect_done = link._connect_done
            link._listener_cancel.set()
            link._connect_cancel.set()
            link._listener_socket = None
            link._pending_listener_socket = None
            link.remote_link = None
            link.remote_endpoint = None
            link.network_session = None
            link._pending_remote_link = None
            link._pending_remote_endpoint = None
            link._pending_network_session = None
            link.remote_mode = "disconnecting"
            link.remote_role = None
            link.remote_bind_port = None

    if cancel_local_setup:
        # Signal cancellation before attempting hook cleanup.  The pairing
        # worker may currently hold both owner locks; this call is bounded by
        # the normal cleanup contract and does not dismantle a published
        # local pair because setup has not completed yet.
        with link.state():
            setup_deadline = link._local_setup_deadline
        hook_timeout = (
            _remaining(setup_deadline)
            if setup_deadline is not None
            else _DEFAULT_CLEANUP_TIMEOUT_S
        )
        _deactivate_link_hooks(session, local_peer, timeout_s=hook_timeout)
        return

    if retry_local_cleanup:
        assert pending_local is not None
        cleanup_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
        cleanup_errors: list[Exception] = []
        try:
            _detach_local_link_session(
                link,
                session,
                pending_local,
                timeout_s=_remaining(cleanup_deadline),
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_errors.append(exc)
        try:
            cleanup_errors.extend(
                _deactivate_link_hooks(
                    session,
                    local_peer,
                    timeout_s=_remaining(cleanup_deadline),
                )
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_errors.append(exc)
        with link.state():
            if cleanup_errors:
                link._pending_local_link_session = pending_local
            else:
                if link._pending_local_link_session is pending_local:
                    link._pending_local_link_session = None
            link._local_pair_in_progress = False
            link._local_setup_deadline = None
            link._local_setup_done.set()
        if cleanup_errors:
            details = "; ".join(
                f"{type(exc).__name__}: {exc}" for exc in cleanup_errors
            )
            raise McpHarnessError("link_teardown_failed", details)
        return

    if done is not None:
        if not owned_by_current_thread:
            if not done.wait(timeout=_DEFAULT_CLEANUP_TIMEOUT_S):
                raise McpHarnessError(
                    "link_teardown_timeout",
                    "another link teardown is still in progress after the "
                    f"{_DEFAULT_CLEANUP_TIMEOUT_S:g}s cleanup deadline",
                )
            with link.state():
                teardown_error = link._remote_error or link._listener_error
            if teardown_error is not None:
                raise McpHarnessError(
                    "link_teardown_failed", str(teardown_error)
                )
        return

    cleanup_errors: list[Exception] = []
    cleanup_deadline = time.monotonic() + _DEFAULT_CLEANUP_TIMEOUT_S
    listener_close_failed = False
    remote_close_failed = False
    network_detach_failed = False
    hook_cleanup_failed = False
    try:
        if listener is not None:
            try:
                listener.close()
            except Exception as exc:  # noqa: BLE001 - preserve the handle for retry
                listener_close_failed = True
                cleanup_errors.append(exc)
        if remote is not None:
            try:
                remote_closed = _close_serial_link(
                    remote,
                    timeout_s=max(0.0, cleanup_deadline - time.monotonic()),
                )
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                remote_closed = False
                cleanup_errors.append(exc)
            if not remote_closed:
                remote_close_failed = True
                cleanup_errors.append(
                    TimeoutError(
                        "remote link worker did not stop before cleanup deadline"
                    )
                )
        if network_session is not None:
            try:
                with session.locked(
                    timeout_s=max(0.0, cleanup_deadline - time.monotonic())
                ):
                    network_session.detach_all()
            except Exception as exc:  # noqa: BLE001
                network_detach_failed = True
                cleanup_errors.append(exc)
        if (
            isinstance(listener_thread, threading.Thread)
            and listener_thread is not threading.current_thread()
        ):
            listener_thread.join(
                timeout=max(0.0, cleanup_deadline - time.monotonic())
            )
        if connect_in_progress and not connect_done.wait(
            timeout=max(0.0, cleanup_deadline - time.monotonic())
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
            if remote_endpoint is not None:
                endpoint_errors = _uninstall_remote_endpoint(
                    session,
                    remote_endpoint,
                    timeout_s=max(0.0, cleanup_deadline - time.monotonic()),
                )
                if endpoint_errors:
                    hook_cleanup_failed = True
                    cleanup_errors.extend(endpoint_errors)
            hook_errors = _deactivate_link_hooks(
                session,
                timeout_s=max(0.0, cleanup_deadline - time.monotonic()),
            )
            if hook_errors:
                hook_cleanup_failed = True
                cleanup_errors.extend(hook_errors)
    finally:
        with link.state():
            if listener_thread is not None and listener_thread.is_alive():
                cleanup_errors.append(
                    TimeoutError(
                        "listener worker did not stop before cleanup deadline"
                    )
                )
            pending_listener = link._pending_listener_socket
            pending_remote = link._pending_remote_link
            pending_endpoint = link._pending_remote_endpoint
            pending_network = link._pending_network_session
            if not cleanup_errors and any(
                resource is not None
                for resource in (
                    pending_listener,
                    pending_remote,
                    pending_endpoint,
                    pending_network,
                )
            ):
                # A connect/accept worker may have finished its own cleanup
                # after this teardown captured its first snapshot. Pending
                # handles are still live ownership, not a successful idle
                # transition; force the caller to retry link_disconnect.
                cleanup_errors.append(
                    TimeoutError(
                        "remote setup cleanup remained pending after teardown"
                    )
                )
            if cleanup_errors:
                link._listener_error = cleanup_errors[0]
                link._remote_error = cleanup_errors[0]
                # Keep every resource that still needs another cleanup pass.
                # The public remote fields stay empty while this state is
                # disconnecting, so no emulator operation can use a resource
                # during teardown.
                link._pending_listener_socket = (
                    listener
                    if listener_close_failed
                    else pending_listener
                )
                link._pending_remote_link = (
                    remote if remote_close_failed else pending_remote
                )
                link._pending_remote_endpoint = (
                    remote_endpoint
                    if hook_cleanup_failed
                    else pending_endpoint
                )
                link._pending_network_session = (
                    network_session
                    if network_detach_failed or hook_cleanup_failed
                    else pending_network
                )
            else:
                link._listener_error = None
                link._remote_error = None
                link._pending_listener_socket = None
                link._pending_remote_link = None
                link._pending_remote_endpoint = None
                link._pending_network_session = None
            # Failed cleanup remains visibly non-idle and can be retried by a
            # subsequent link_disconnect. A clean teardown is the only path
            # that returns the lifecycle to idle.
            link.remote_mode = "disconnecting" if cleanup_errors else "idle"
            link.remote_role = None
            link.remote_bind_port = None
            if listener_thread is None or not listener_thread.is_alive():
                link._listener_thread = None
            else:
                # Retain ownership of a worker that outlived the bounded
                # join. _require_remote_idle() blocks a new listener until
                # this handle becomes non-live, avoiding overlap with stale
                # socket/backend cleanup.
                link._listener_thread = listener_thread
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


def _epoch_payload(epoch: SessionEpoch) -> dict[str, int]:
    return {
        "tick": epoch.tick,
        "load_generation": epoch.load_generation,
        "reset_generation": epoch.reset_generation,
        "session_id": epoch.session_id,
    }


def _game_state_payload(snapshot: StateSnapshot) -> dict[str, Any]:
    # The epoch is embedded in the same JSON object as the parsed state, so a
    # client never has to correlate two independent resource reads.  The
    # standalone `pokered://state-epoch` resource is retained for polling.
    payload = to_jsonable(snapshot.state)
    payload["epoch"] = _epoch_payload(snapshot.epoch)
    return payload


def read_resource(
    session: Session,
    uri: str,
    link: LinkState | None = None,
    *,
    timed_owner: TimedOwner | None = None,
) -> str:
    """Resource reader — returns JSON text."""
    if timed_owner is not None:
        if timed_owner.session is not session:
            raise McpHarnessError(
                "invalid_timed_configuration", "timed owner belongs to another session"
            )
        if uri in {_URI_PEER_GAME_STATE, _URI_LINK_TRANSPORT}:
            raise McpHarnessError(
                "timed_unsupported_resource", "timed remote transport has no local peer"
            )
        if uri == _URI_LINK_STATUS:
            return json.dumps(_timed_status(timed_owner))
        return timed_owner.submit(lambda owned: read_resource(owned, uri, link)).result()
    if uri == _URI_GAME_STATE:
        return json.dumps(_game_state_payload(session.read_state_snapshot()))
    if uri == _URI_STATE_EPOCH:
        # Read-only session bookkeeping: lets a client pair a game-state
        # snapshot with a monotonic tick and reject a stale observation after
        # a later step/load without touching emulator state.  The monotonic
        # load_generation advances on every load_state, which the tick does
        # not, so a rewound state is still detectable.  session_id
        # distinguishes a replacement session, and all three counters are
        # captured together (read_epoch) so the payload is never torn.
        return json.dumps(_epoch_payload(session.read_epoch()))
    if uri == _URI_EVENT_LOG:
        events: list[GameEvent] = session.event_snapshot()
        return json.dumps([to_jsonable(e) for e in events])
    if uri == _URI_PEER_GAME_STATE:
        if link is None or link.peer_session is None:
            raise McpHarnessError("peer_not_configured", "peer session not configured")
        with link.state():
            peer = link.peer_session
        return json.dumps(_game_state_payload(peer.read_state_snapshot()))
    if uri == _URI_LINK_TRANSPORT:
        if link is None:
            return json.dumps({"a_to_b": [], "b_to_a": []})
        with link.state():
            pair = link.pair
        if pair is None:
            return json.dumps({"a_to_b": [], "b_to_a": []})
        # The pair object can be unpaired concurrently with a resource read.
        # Keep the snapshot serialized with pair stepping/unpairing, but do
        # not take the global operation lock: status/resources must remain
        # responsive while a remote operation is waiting on TCP.
        with link._pair_lock:
            return json.dumps(pair.transport.snapshot())
    if uri == _URI_LINK_STATUS:
        if link is None:
            link = LinkState()
        return json.dumps(dispatch_tool(session, "link_status", {}, link=link))
    raise McpHarnessError("invalid_resource", f"unknown resource: {uri!r}")


def _resource_specs(has_peer: bool = False) -> list[mcp_types.Resource]:
    specs = [
        mcp_types.Resource(
            uri=_URI_GAME_STATE,  # type: ignore[arg-type]
            name="Game State",
            description=(
                "Current parsed game state snapshot (JSON). Includes an "
                "`epoch` object (`tick`, `load_generation`, "
                "`reset_generation`, `session_id`) captured in the same lock "
                "scope as the state, so a client can reject a stale snapshot "
                "without a second resource read. Battle fields expose the raw "
                "`wBattleResult` byte as `battle.raw_battle_result` and a "
                "`battle.terminal_result` that is set only for a non-zero "
                "outcome byte surviving teardown; an ambiguous zero at the "
                "falling edge stays null because win, blackout, and escape "
                "all leave or clear zero. Transient fields "
                "(`battle.move_menu_type`, `battle.player_move_list_index`, "
                "`battle.current_menu_item`, `battle.player_selected_move`, "
                "`battle.enemy_selected_move`, "
                "`battle.action_result_or_took_turn`, "
                "`battle.in_handle_player_mon_fainted`) and decoded "
                "`battle.player_stat_stages`/`battle.enemy_stat_stages` "
                "report null when their symbol is absent. `battle.phase` "
                "reports `command_selection` only from session "
                "execution-hook evidence (`battle.menu_open`; "
                "`battle.menu_evidence` names the hooked ROM labels), never "
                "from a persistent mode byte."
            ),
            mimeType="application/json",
        ),
        mcp_types.Resource(
            uri=_URI_STATE_EPOCH,  # type: ignore[arg-type]
            name="State Epoch",
            description=(
                "Monotonic session tick, load/reset generations, and session "
                "identity captured atomically for `pokered://game-state`. The "
                "tick advances with stepping; the load generation advances on "
                "every `load_state` and the reset generation on every "
                "`reset_tick`, so a rewound clock remains detectable; "
                "`session_id` distinguishes a replacement session. Compare "
                "successive reads to reject stale snapshots."
            ),
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
                description=(
                    "Parsed game state of the peer session (JSON), including "
                    "the peer's atomically captured `epoch`."
                ),
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


_TIMED_INTEGER_FIELDS = (
    "rearm_budget",
    "rearm_instruction_cap",
    "max_edge_lateness",
    "quantum_cycles",
    "max_wait_attempts",
    "inbound_capacity",
    "queue_capacity",
)
_TIMED_TIMEOUT_FIELDS = (
    "operation_timeout",
    "request_timeout",
    "lock_timeout",
    "close_timeout",
)


def load_timed_policy_from_env() -> TimedOwnerPolicy | None:
    """Select timed transport explicitly; no policy value has an implicit default.

    Set POKERED_LINK_TRANSPORT=timed and POKERED_TIMED_<FIELD> for every
    TimedOwnerPolicy field. Cycle/count fields are integers; timeout fields
    are finite positive seconds. Legacy remains the default transport.
    """
    mode = os.environ.get("POKERED_LINK_TRANSPORT", "legacy").strip().lower()
    if mode not in {"legacy", "timed"}:
        raise McpHarnessError(
            "invalid_timed_configuration",
            "POKERED_LINK_TRANSPORT must be legacy or timed",
        )
    fields = _TIMED_INTEGER_FIELDS + _TIMED_TIMEOUT_FIELDS
    values = {field: os.environ.get(f"POKERED_TIMED_{field.upper()}") for field in fields}
    if mode == "legacy":
        if any(value is not None for value in values.values()):
            raise McpHarnessError(
                "invalid_timed_configuration",
                "POKERED_TIMED_* policy requires POKERED_LINK_TRANSPORT=timed",
            )
        return None
    missing = [
        f"POKERED_TIMED_{field.upper()}"
        for field, value in values.items()
        if value is None or not value.strip()
    ]
    if missing:
        raise McpHarnessError(
            "invalid_timed_configuration", "missing explicit timed policy: " + ", ".join(missing)
        )
    try:
        parsed = {
            field: (int(value) if field in _TIMED_INTEGER_FIELDS else float(value))
            for field, value in values.items()
        }
        return TimedOwnerPolicy(**parsed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise McpHarnessError("invalid_timed_configuration", str(exc)) from exc


def _timed_status_payload(snapshot: Any) -> dict[str, Any]:
    # The owner snapshot is immutable and contains no foreign-thread reads.
    def thaw(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [thaw(item) for item in value]
        return to_jsonable(value)

    payload = thaw(snapshot)
    payload["transport"] = "timed"
    payload["mode"] = payload.get("state", "unknown")
    payload["remote_mode"] = payload["mode"]
    payload["primary_tick"] = payload.get("tick")
    payload["peer_tick"] = None
    payload["paired"] = False
    payload["link_backend"] = "timed"
    payload["remote_bind_port"] = payload.get("port")
    payload["remote_peer_rom_version"] = payload.get("peer_rom_version")
    payload["remote_error"] = payload.get("error")
    payload["remote_role"] = payload.get("role")
    return payload


def _timed_status(owner: TimedOwner) -> dict[str, Any]:
    return _timed_status_payload(owner.status())


def _timed_tool_request(
    owner: TimedOwner,
    session: Session,
    name: str,
    args: dict[str, Any],
    link: LinkState | None,
) -> Any:
    if owner.session is not session:
        raise McpHarnessError(
            "invalid_timed_configuration", "timed owner belongs to another session"
        )
    if name in {"link_listen", "link_connect"}:
        version = _validate_rom_version(
            args.get("rom_version", link.primary_version if link else "red")
        )
        if link is not None:
            _require_primary_rom_version(link, version)
        peer_version = _optional_rom_version(args.get("peer_rom_version"))
        deadline = None
        if "timeout_s" in args:
            deadline = time.monotonic() + _validate_timeout(args["timeout_s"])
        host = _validate_remote_host(args.get("host", "127.0.0.1"))
        if host == "localhost":
            host = "127.0.0.1"
        port = _positive_port(args["port"])
        operation = owner.listen if name == "link_listen" else owner.connect
        return operation(host, port, version, deadline=deadline, peer_rom_version=peer_version)
    if name == "link_disconnect":
        return owner.disconnect()
    if name == "link_step":

        def step(owned: Session) -> Any:
            if owner.status()["state"] != "connected":
                raise McpHarnessError(
                    "timed_not_connected", "link_step requires a timed connection"
                )
            result = _dispatch_session_tool(owned, "step", args)
            return {"primary_tick": result["tick"], "peer_tick": None}

        return owner.submit(step)
    if name.startswith("link_"):
        raise McpHarnessError(
            "timed_unsupported_tool", f"{name} is not supported by timed remote transport"
        )
    return owner.submit(lambda owned: _dispatch_session_tool(owned, name, args))


async def _await_owner_request(request: Any, *, ready: bool = False) -> Any:
    """Isolate asyncio cancellation while the owner retains the queue outcome."""
    deadline = asyncio.timeout(max(0.0, request.deadline - time.monotonic()))
    try:
        async with deadline:
            return await (request.wait_ready() if ready else request.wait())
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        request.cancel()
        # Deadline cancellation can race the owner publishing a terminal
        # protocol failure. The shielded canonical future retains that reason;
        # preserve its identity instead of replacing it with a wrapper timeout.
        if request.future.done() and not request.future.cancelled():
            terminal = request.future.exception()
            if terminal is not None and getattr(terminal, "code", None) != "timed_cancelled":
                raise terminal
        raise McpHarnessError("timed_deadline", "timed owner request deadline expired") from exc
    except asyncio.CancelledError:
        request.cancel()
        raise


async def _shutdown_timed_owner(
    owner: TimedOwner, policy: TimedOwnerPolicy, tasks: _McpTaskRegistry
) -> None:
    """Cancel execution out of band; detach only through the owner queue."""
    deadline = time.monotonic() + policy.close_timeout
    owner.cancel()
    owner.disconnect()

    def join_owner() -> bool:
        remaining = deadline - time.monotonic()
        return remaining > 0 and owner.close(timeout=remaining)

    # Only the bounded join uses the executor. Session access, cancellation
    # recovery, endpoint detach and Session.close remain on the native owner.
    worker = tasks.start(join_owner)
    try:
        closed = await asyncio.wait_for(
            asyncio.shield(worker), max(0.0, deadline - time.monotonic())
        )
    except TimeoutError:
        closed = False
    if not closed:
        raise McpHarnessError(
            "timed_cleanup_pending", "timed owner retained pending cleanup; session remains owned"
        )


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve a late task outcome after its owner timed out or cancelled."""
    if task.cancelled():
        return
    task.exception()


class _McpTaskRegistry:
    """Track thread-backed request work until it has actually finished.

    Cancelling an asyncio task does not stop the executor thread created by
    :func:`asyncio.to_thread`.  Keeping those tasks in a server-owned registry
    gives the stdio shutdown path a final opportunity to wake remote work and
    wait for the emulator operation before the owning session is closed.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def start(self, operation: Callable[[], Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(asyncio.to_thread(operation))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        _consume_task_exception(task)

    async def wait_until(self, deadline: float) -> bool:
        """Wait for all tracked work without cancelling executor tasks."""
        while True:
            pending = tuple(task for task in self._tasks if not task.done())
            if not pending:
                return True
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            if remaining <= 0.0:
                return False
            await asyncio.wait(pending, timeout=remaining)


async def _wait_task_until(
    task: asyncio.Task[Any],
    deadline: float,
) -> bool:
    """Wait for a task without cancelling it, bounded by ``deadline``."""
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    if remaining <= 0.0:
        task.add_done_callback(_consume_task_exception)
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except TimeoutError:
        # ``shield`` deliberately leaves the thread-backed task alive. It
        # still owns the emulator operation and must be allowed to settle;
        # consume its eventual result so a late exception is not reported as
        # an unhandled asyncio task failure.
        task.add_done_callback(_consume_task_exception)
        return False
    except BaseException:  # noqa: BLE001 - cancellation cleanup is best effort
        return True
    return True


async def _await_blocking_task(
    task: asyncio.Task[Any],
    *,
    task_registry: _McpTaskRegistry,
    cancel_cleanup: Callable[[], Any] | None,
) -> Any:
    """Await a thread-backed request while retaining cancellation ownership."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cleanup_deadline = (
            asyncio.get_running_loop().time() + _DEFAULT_CLEANUP_TIMEOUT_S
        )
        if cancel_cleanup is not None:
            cleanup = task_registry.start(cancel_cleanup)
            await _wait_task_until(cleanup, cleanup_deadline)
        await _wait_task_until(task, cleanup_deadline)
        raise


def _close_sessions_independently(
    peer_session: Session | None,
    session: Session | None,
) -> list[tuple[str, Exception]]:
    """Close each owned session even when another close attempt fails."""
    errors: list[tuple[str, Exception]] = []
    for role, target in (
        ("peer", peer_session),
        ("primary", session),
    ):
        if target is None:
            continue
        try:
            target.close(timeout_s=_DEFAULT_CLEANUP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - preserve independent cleanup
            errors.append((role, exc))
    return errors


def build_server(
    session: Session,
    *,
    peer_session: Session | None = None,
    primary_version: str = "red",
    peer_version: str = "red",
    link: LinkState | None = None,
    timed_policy: TimedOwnerPolicy | None = None,
    timed_owner: TimedOwner | None = None,
) -> Server:
    """Construct an MCP ``Server`` bound to ``session``.

    When ``peer_session`` (or a pre-built ``link`` container) is provided,
    the server additionally exposes the ``link_*`` tools and peer
    resources. The pair itself is constructed lazily on the first
    ``link_pair`` call.

    The server stays un-run — caller invokes ``server.run`` via an
    appropriate transport. This makes the wiring itself testable without
    spawning stdio pipes.

    ``timed_policy`` explicitly selects single-session timed remote mode.
    Every live Session operation then runs on ``_pokered_timed_owner``;
    status is a cached observation. The caller must close that owner (which
    closes its Session) or use ``serve_stdio`` for managed shutdown. A false
    owner close result retains ownership and must never trigger foreign
    Session teardown. Supplying a local peer or active legacy link is invalid.
    """
    if link is None:
        link = LinkState(
            peer_session=peer_session,
            primary_version=primary_version,
            peer_version=peer_version,
        )

    if timed_owner is not None and timed_policy is None:
        raise McpHarnessError("invalid_timed_configuration", "timed_owner requires timed_policy")
    if timed_policy is not None and (link.peer_session is not None or peer_session is not None):
        raise McpHarnessError(
            "invalid_timed_configuration", "timed transport requires a single local session"
        )
    if timed_policy is not None and (
        link.remote_mode != "idle"
        or link.pair is not None
        or link.local_link_session is not None
        or link._pending_local_link_session is not None
        or link._local_pair_in_progress
        or link.remote_link is not None
        or link.remote_endpoint is not None
        or link.network_session is not None
        or link._pending_remote_link is not None
        or link._pending_remote_endpoint is not None
        or link._pending_network_session is not None
        or link._pending_listener_socket is not None
        or link._listener_socket is not None
        or link._listener_start_in_progress
        or link._connect_in_progress
        or link._disconnecting
    ):
        raise McpHarnessError(
            "invalid_timed_configuration", "timed transport requires an idle legacy link"
        )
    if timed_owner is not None and (
        timed_owner.session is not session or timed_owner.policy != timed_policy
    ):
        raise McpHarnessError(
            "invalid_timed_configuration", "timed owner session or policy does not match server"
        )
    if timed_policy is not None:
        timed_owner = timed_owner or TimedOwner(session, timed_policy)

    server: Server = Server("pokered-harness")
    request_tasks = _McpTaskRegistry()
    # ``serve_stdio`` uses this private handle during transport EOF cleanup.
    # Keeping it on the server also makes the ownership explicit for callers
    # that construct a server first and select their transport later.
    server._pokered_request_tasks = request_tasks  # type: ignore[attr-defined]
    server._pokered_timed_owner = timed_owner  # type: ignore[attr-defined]

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        if timed_owner is not None:
            specs = [
                spec
                for spec in _tool_specs(has_peer=True)
                if spec.name not in _LOCAL_LINK_TOOL_NAMES or spec.name == "link_step"
            ]
            for spec in specs:
                if spec.name in {"link_listen", "link_connect"}:
                    spec.inputSchema["properties"]["timeout_s"].pop("default", None)
                    spec.description = (
                        "Bind timed listener and return when ready; poll link_status for connection."
                        if spec.name == "link_listen"
                        else "Connect timed transport within the explicit setup deadline."
                    )
                elif spec.name == "link_step":
                    spec.description = (
                        "Advance this session by count ticks through its timed endpoint."
                    )
            return specs
        return _tool_specs(has_peer=link.peer_session is not None)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> mcp_types.CallToolResult:
        args = arguments or {}
        if timed_owner is not None:
            try:
                if name == "link_status":
                    return _text_reply(_timed_status(timed_owner))
                request = _timed_tool_request(timed_owner, session, name, args, link)
                result = await _await_owner_request(request, ready=name == "link_listen")
                if name in {"link_listen", "link_connect", "link_disconnect"}:
                    result = (
                        _timed_status_payload(result)
                        if name == "link_listen"
                        else _timed_status(timed_owner)
                    )
                return _text_reply(result)
            except Exception as exc:  # noqa: BLE001
                return _error_reply(exc)
        # ``asyncio.to_thread`` cannot cancel a Python worker that is already
        # executing. Keep an explicit Task and shield it so a cancelled MCP
        # request does not orphan an emulator operation. For link-related
        # work, close the remote transport from a second thread to wake a
        # serial hook or an in-progress connect before waiting for the worker
        # to settle.
        worker = request_tasks.start(lambda: dispatch_tool(session, name, args, link))
        try:
            # The emulator is not asyncio-aware. Run the blocking operation
            # off the event loop while the Session/LinkState locks preserve
            # single-emulator ordering. This also leaves the loop responsive
            # to status/resource requests while a bounded TCP call waits.
            result = await _await_blocking_task(
                worker,
                task_registry=request_tasks,
                cancel_cleanup=(
                    None if name == "link_status" else lambda: _disconnect_remote(link, session)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_reply(exc)
        return _text_reply(result)

    @server.list_resources()
    async def _list_resources() -> list[mcp_types.Resource]:
        return _resource_specs(has_peer=link.peer_session is not None)

    @server.read_resource()
    async def _read_resource(uri: Any) -> str:
        resource_uri = str(uri)
        if timed_owner is not None:
            try:
                if resource_uri == _URI_LINK_STATUS:
                    return json.dumps(_timed_status(timed_owner))
                if resource_uri in {_URI_PEER_GAME_STATE, _URI_LINK_TRANSPORT}:
                    raise McpHarnessError(
                        "timed_unsupported_resource", "timed remote transport has no local peer"
                    )
                return await _await_owner_request(
                    timed_owner.submit(lambda owned: read_resource(owned, resource_uri, link))
                )
            except Exception as exc:
                raise McpError(
                    mcp_types.ErrorData(
                        code=0, message=str(exc) or type(exc).__name__, data=_error_payload(exc)
                    )
                ) from exc
        worker = request_tasks.start(lambda: read_resource(session, resource_uri, link))
        try:
            return await _await_blocking_task(
                worker,
                task_registry=request_tasks,
                # A resource read may be waiting on a remote endpoint or a
                # session lock.  The same cancellation wake-up used by tools
                # is required so EOF cannot strand a thread outside server
                # cleanup.
                cancel_cleanup=lambda: _disconnect_remote(link, session),
            )
        except Exception as exc:
            # The low-level MCP server turns McpError into a protocol
            # ErrorData response. Without this boundary, a resource failure
            # becomes an untyped JSON-RPC error even though tool failures
            # already expose a stable machine-readable envelope.
            raise McpError(
                mcp_types.ErrorData(
                    code=0,
                    message=str(exc) or type(exc).__name__,
                    data=_error_payload(exc),
                )
            ) from exc

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
    timed_policy: TimedOwnerPolicy | None = None,
    timed_owner: TimedOwner | None = None,
) -> None:
    """Serve MCP; timed mode transfers Session cleanup to its persistent owner."""
    owned_link = link or LinkState(
        peer_session=peer_session,
        primary_version=primary_version,
        peer_version=peer_version,
    )
    timed_options: dict[str, Any] = {}
    if timed_policy is not None or timed_owner is not None:
        timed_options = {"timed_policy": timed_policy, "timed_owner": timed_owner}
    server = build_server(
        session,
        peer_session=peer_session,
        primary_version=primary_version,
        peer_version=peer_version,
        link=owned_link,
        **timed_options,
    )
    owner = getattr(server, "_pokered_timed_owner", None)
    request_tasks = getattr(server, "_pokered_request_tasks", None)
    if owner is not None:
        try:
            async with stdio_server() as (read_stream, write_stream):
                with contextlib.redirect_stdout(sys.stderr):
                    await server.run(
                        read_stream, write_stream, server.create_initialization_options()
                    )
        finally:
            active_exception = sys.exc_info()[1]
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    await _shutdown_timed_owner(owner, timed_policy, request_tasks)
            except Exception as exc:
                if active_exception is not None:
                    active_exception.add_note(f"MCP timed cleanup failed: {exc}")
                else:
                    raise
        return
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
        active_exception = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        with contextlib.redirect_stdout(sys.stderr):
            try:
                _disconnect_remote(owned_link, session)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            # A stdio client can terminate while either link mode is active.
            # Restore native serial backends before the owning Sessions are
            # closed by main().
            try:
                # ``dispatch_tool`` normally waits for the operation guard
                # without a deadline.  During stdio EOF cleanup, a request
                # that is still inside a link operation must not turn that
                # wait into an unbounded server shutdown.
                with owned_link.operation(timeout_s=_DEFAULT_CLEANUP_TIMEOUT_S):
                    dispatch_tool(session, "link_unpair", {}, link=owned_link)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            if request_tasks is not None:
                try:
                    drained = await request_tasks.wait_until(
                        asyncio.get_running_loop().time()
                        + _DEFAULT_CLEANUP_TIMEOUT_S
                    )
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
                else:
                    if not drained:
                        cleanup_errors.append(
                            TimeoutError(
                                "MCP request worker did not stop before the "
                                f"{_DEFAULT_CLEANUP_TIMEOUT_S:g}s shutdown deadline"
                            )
                        )
        if cleanup_errors:
            details = "; ".join(
                f"{type(exc).__name__}: {exc}" for exc in cleanup_errors
            )
            if active_exception is not None:
                active_exception.add_note(f"MCP cleanup failed: {details}")
            else:
                raise McpHarnessError("server_cleanup_failed", details)


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
    * ``POKERED_SYM_SHA1`` / ``POKERED_PEER_SYM_SHA1`` — explicit symbol-file
      pins for wheel launches without a local ``VERSIONS.md``. In a source
      checkout, matching per-path symbol pins are selected automatically.
    * ``POKERED_SKIP_SHA1=1`` — rejected by this production entry point;
      ad-hoc diagnostics must use a separate, explicitly non-production
      driver.
    * ``POKERED_PEER_ROM_PATH`` / ``POKERED_PEER_SYM_PATH`` /
      ``POKERED_PEER_ROM_SHA1`` — when set, a peer Session is constructed
      at startup and the link-cable tools become usable. The pair is NOT
      auto-paired — invoke ``link_pair`` explicitly.
    * ``POKERED_LINK_TRANSPORT=timed`` — explicitly select timed remote
      transport. Every ``POKERED_TIMED_<FIELD>`` listed by
      :func:`load_timed_policy_from_env` is required; no timing policy is
      qualified or selected implicitly. Local peer configuration is invalid.
    """
    from pokered_harness.config import (
        VersionsConfigError,
        load_peer_env,
        load_primary_env,
        load_versions,
    )

    try:
        timed_policy = load_timed_policy_from_env()
    except McpHarnessError as exc:
        raise SystemExit(f"invalid timed MCP configuration: {exc}") from exc

    if _env_flag("POKERED_SKIP_SHA1"):
        raise SystemExit(
            "POKERED_SKIP_SHA1 is diagnostic-only and rejected by the MCP "
            "production entry point; unset it and provide the documented "
            "ROM and symbol SHA-1 pins"
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
    if timed_policy is not None and peer_rom is not None:
        raise SystemExit(
            "timed MCP transport requires a single local session; unset POKERED_PEER_*"
        )
    peer_symbol_sha_override = os.environ.get("POKERED_PEER_SYM_SHA1")
    if (
        peer_sym is None
        and peer_symbol_sha_override is not None
        and peer_symbol_sha_override.strip()
    ):
        raise SystemExit(
            "POKERED_PEER_SYM_SHA1 requires POKERED_PEER_SYM_PATH and a "
            "configured peer ROM"
        )

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

    primary_symbol_sha = os.environ.get("POKERED_SYM_SHA1")
    if primary_symbol_sha is not None:
        primary_symbol_sha = primary_symbol_sha.strip() or None
    if primary_symbol_sha is None and versions is not None:
        primary_symbol_sha = versions.symbol_sha1_for_path(primary_sym)
    if primary_symbol_sha is None and not _env_flag("POKERED_SKIP_SHA1"):
        raise SystemExit(
            "set POKERED_SYM_SHA1 or provide a matching symbol-file pin "
            "for POKERED_SYM_PATH in VERSIONS.md"
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

    peer_symbol_sha: str | None = None
    if peer_sym is not None:
        peer_symbol_sha = os.environ.get("POKERED_PEER_SYM_SHA1")
        if peer_symbol_sha is not None:
            peer_symbol_sha = peer_symbol_sha.strip() or None
        if peer_symbol_sha is None and versions is not None:
            peer_symbol_sha = versions.symbol_sha1_for_path(peer_sym)
        if peer_symbol_sha is None and not _env_flag("POKERED_SKIP_SHA1"):
            raise SystemExit(
                "set POKERED_PEER_SYM_SHA1 or provide a matching peer "
                "symbol-file pin for POKERED_PEER_SYM_PATH in VERSIONS.md"
            )
    if versions is not None:
        expected_pyboy = versions.pyboy_version
        expected_pyboy_revision = versions.pyboy_revision
        if expected_pyboy_revision is None:
            raise SystemExit(
                "VERSIONS.md is missing the pinned PyBoy fork revision"
            )
    elif _env_flag("POKERED_SKIP_SHA1"):
        expected_pyboy = None
        expected_pyboy_revision = None
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
        expected_pyboy_revision = revision

    # CRITICAL: PyBoy's init writes ~70KB of `.sym`-skip warnings to
    # stdout via a logging handler it installs before we get a chance
    # to reconfigure it. MCP's stdio transport uses stdout exclusively
    # for JSON-RPC, so anything the emulator prints would corrupt the
    # stream and wedge the client at ``initialize``. Capture all stdout
    # during boot and re-emit it on stderr so the noise is still
    # visible but out of the RPC channel.
    peer_session: Session | None = None
    session: Session | None = None
    timed_owner: TimedOwner | None = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            session = Session.from_files(
                primary_rom,
                primary_sym,
                expected_rom_sha1=expected_sha,
                expected_symbol_sha1=primary_symbol_sha,
                expected_pyboy_version=expected_pyboy,
                expected_pyboy_revision=expected_pyboy_revision,
            )
            register_default_hooks(session)
            session.enable_battle_menu_observation()
            if peer_rom is not None and peer_sym is not None:
                peer_session = Session.from_files(
                    peer_rom,
                    peer_sym,
                    expected_rom_sha1=peer_expected_sha,
                    expected_symbol_sha1=peer_symbol_sha,
                    expected_pyboy_version=expected_pyboy,
                    expected_pyboy_revision=expected_pyboy_revision,
                )
                register_default_hooks(peer_session)
                peer_session.enable_battle_menu_observation()

        timed_options: dict[str, Any] = {}
        if timed_policy is not None:
            timed_owner = TimedOwner(session, timed_policy)
            timed_options = {"timed_policy": timed_policy, "timed_owner": timed_owner}
        asyncio.run(
            serve_stdio(
                session,
                peer_session=peer_session,
                primary_version=primary_env.version,
                peer_version=peer_env.version,
                **timed_options,
            )
        )
    finally:
        active_exception = sys.exc_info()[1]
        owner_stopped = timed_owner is None
        owner_error: Exception | None = None
        if timed_owner is not None:
            try:
                owner_stopped = timed_owner.close(timeout=timed_policy.close_timeout)
            except Exception as exc:  # noqa: BLE001 - ownership remains transferred
                owner_error = exc
        cleanup_errors = _close_sessions_independently(
            peer_session, session if timed_owner is None else None
        )
        if not owner_stopped:
            cleanup_errors.append(
                (
                    "primary",
                    owner_error
                    or McpHarnessError(
                        "timed_cleanup_pending",
                        "Session retained by unresolved timed owner; foreign close refused",
                    ),
                )
            )
        if cleanup_errors:
            details = "; ".join(
                f"{role} {type(exc).__name__}: {exc}" for role, exc in cleanup_errors
            )
            if active_exception is not None:
                active_exception.add_note(f"MCP session cleanup failed: {details}")
            else:
                raise McpHarnessError("server_cleanup_failed", details)


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
    "TimedOwner",
    "TimedOwnerPolicy",
    "build_server",
    "dispatch_tool",
    "load_timed_policy_from_env",
    "main",
    "read_resource",
    "register_default_hooks",
    "serve_stdio",
]
