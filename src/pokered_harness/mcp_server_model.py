"""Shared constants, LinkState, errors and small payload helpers.

Split from ``pokered_harness.mcp_server`` for issue #125 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on ``pokered_harness.mcp_server`` stay visible.
"""

from __future__ import annotations

import base64
import binascii
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import mcp.types as mcp_types

from pokered_harness.link.emulated_time import CoordinatorClosed, EmulatedTimeError
from pokered_harness.link.network_backend import NetworkBackendError
from pokered_harness.link.pyboy_link_session import PairedSessionOperationError
from pokered_harness.link.serial_link import SerialLink, SerialLinkError, SerialLinkTimeout
from pokered_harness.link.timed_wire import (
    Cancelled,
    ChannelClosed,
    DeadlineExceeded,
    ProtocolError,
    WireError,
)
from pokered_harness.mcp_timed_owner import TimedOwnerError
from pokered_harness.ownership import EmulatorOwnershipError, assert_no_emulator_scope
from pokered_harness.session import (
    InvalidStateError,
    RomHashMismatch,
    RomNotFoundError,
    Session,
    SessionClosedError,
    SessionCloseError,
    SessionCloseTimeout,
    SessionConfigurationError,
    SessionError,
    SessionLockTimeout,
    SymbolHashMismatch,
    SymbolNotFoundError,
    VersionMismatch,
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


# Tools that are advertised on a ROM-owned remote link but that the timed owner
# cannot perform.  ``link_frame_barrier`` toggles the connected network
# session's ROM-owned pacing control; the timed owner replaces that pacing model
# with its own policy and rejects the name unconditionally
# (``timed_unsupported_tool``), so timed ``tools/list`` must not offer it.  This
# is a mode boundary, not a permission: the legacy tool stays advertised and
# dispatched for every non-timed remote link.
_TIMED_UNSUPPORTED_TOOL_NAMES = frozenset({"link_frame_barrier"})


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
    # ``_AddEnemyMonToPlayerParty`` is the routine the Trade Center runs when it
    # appends the peer's received 44-byte record to the player's own party
    # (engine/link/cable_club.asm via the party compaction path).  It is the
    # ROM-owned marker that a trade actually copied a record: it never runs for
    # a party that merely sat in the trade menus, so an acceptance row can
    # reject a skipped copy even when both offered records hold identical bytes.
    ("_AddEnemyMonToPlayerParty", "trade_received"),
)


# URIs for resources exposed by this server.
_URI_GAME_STATE = "pokered://game-state"


_URI_STATE_EPOCH = "pokered://state-epoch"


_URI_EVENT_LOG = "pokered://events"


_URI_PEER_GAME_STATE = "pokered://peer-game-state"


_URI_LINK_TRANSPORT = "pokered://link-transport"


_URI_LINK_STATUS = "pokered://link-status"


_URI_PARTY_RECORDS = "pokered://party-records"


_URI_PEER_PARTY_RECORDS = "pokered://peer-party-records"


_URI_PEER_EVENTS = "pokered://peer-events"


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
        self.pair: _entry.LinkPair | None = None
        self.local_link_session: _entry.PyBoyLinkSession | None = None
        # A native provider is published here before attach begins so a
        # failed/cancelled setup cannot lose the only cleanup handle.  It is
        # moved to ``local_link_session`` only after the generation check and
        # rollback boundary have both settled.
        self._pending_local_link_session: _entry.PyBoyLinkSession | None = None
        # Remote (two-process) state.
        self.remote_link: SerialLink | None = None
        self.remote_endpoint: _entry.RemoteLinkEndpoint | None = None
        self.network_session: _entry.PyBoyLinkSession | None = None
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
        self._pending_remote_endpoint: _entry.RemoteLinkEndpoint | None = None
        self._pending_network_session: _entry.PyBoyLinkSession | None = None
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
            timeout = _entry._validate_timeout(timeout_s)
            acquired = self._operation_lock.acquire(timeout=timeout)
        if not acquired:
            raise McpHarnessError(
                "link_operation_timeout",
                f"link operation did not release before the {timeout:g}s shutdown deadline",
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
        raise McpHarnessError("invalid_state", "load_state.data must be a base64 string")
    max_encoded = ((_MAX_STATE_BYTES + 2) // 3) * 4
    if len(value) > max_encoded:
        raise McpHarnessError(
            "invalid_state",
            f"load_state.data exceeds the {_MAX_STATE_BYTES} byte limit",
        )
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise McpHarnessError("invalid_state", "load_state.data is not valid base64") from exc
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
        raise McpHarnessError("invalid_argument", f"{name} must be an integer") from exc
    if result <= 0:
        raise McpHarnessError("invalid_argument", f"{name} must be positive, got {result}")
    if result > maximum:
        raise McpHarnessError(
            "invalid_argument",
            f"{name} must be <= {maximum}, got {result}",
        )
    return result


def _event_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise McpHarnessError("invalid_argument", "event_names must be a non-empty array")
    if not value or len(value) > _MAX_EVENT_NAMES:
        raise McpHarnessError(
            "invalid_argument",
            f"event_names must contain 1..{_MAX_EVENT_NAMES} names",
        )
    names: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name:
            raise McpHarnessError("invalid_argument", "event_names must contain non-empty strings")
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
            "POKERED_PEER_SYM_PATH at server startup",
        )
    return link.peer_session


def _require_pair(link: LinkState) -> _entry.LinkPair:
    if link.pair is None or not link.pair.paired:
        raise McpHarnessError("not_paired", "not paired; call link_pair first")
    return link.pair


# Call-time indirection so facade-level monkeypatches stay visible here.
# Deferred (issue #240) instead of an eager back-edge, so this module can
# be imported first in a fresh interpreter.
from pokered_harness._mcp_facade_entry import entry as _entry
