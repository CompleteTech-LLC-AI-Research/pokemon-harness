"""MCP server exposing a long-lived :class:`Session` over stdio.

Actions (mutating the emulator) are MCP **tools**; observations (reading
game state, events) are MCP **resources**. That split matches the ADR.

The server is intentionally thin — every operation delegates to the
session, which is where lifecycle, version enforcement, and hooks live.

When a peer :class:`Session` is configured (via ``POKERED_PEER_*`` env
vars), the server also exposes link-cable tools (``link_pair``,
``link_step``, ``link_peer_press``, ...) and peer resources
(``pokered://peer-game-state``, ``pokered://peer-party-records``,
``pokered://link-transport``). The peer
Session is constructed at startup but not *paired* — callers must invoke
``link_pair`` explicitly.
"""

# ruff: noqa: F401

from __future__ import annotations

import sys as _sys

if __name__ == "__main__":
    _sys.modules.setdefault("pokered_harness.mcp_server", _sys.modules[__name__])

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import math
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
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
from pokered_harness.mcp_server_link_dispatch import (
    _dispatch_link_tool,
)

# The split modules' names are re-exported verbatim so the module surface
# (and every ``mcp_server.<name>`` monkeypatch target the tests rely on) is unchanged.
from pokered_harness.mcp_server_model import (
    _DEFAULT_CLEANUP_TIMEOUT_S,
    _DEFAULT_REMOTE_HELLO_TIMEOUT_S,
    _DIRECT_LINK_HOOKS,
    _LOCAL_LINK_TOOL_NAMES,
    _MAX_BUTTON_DURATION,
    _MAX_EVENT_NAME_LENGTH,
    _MAX_EVENT_NAMES,
    _MAX_REMOTE_TIMEOUT_S,
    _MAX_RUN_CHUNK,
    _MAX_RUN_TICKS,
    _MAX_STATE_BYTES,
    _MAX_STEP_TICKS,
    _SERIAL_BACKEND_ERRORS,
    _TIMED_OWNER_ERROR_CODES,
    _TIMED_UNSUPPORTED_TOOL_NAMES,
    _URI_EVENT_LOG,
    _URI_GAME_STATE,
    _URI_LINK_STATUS,
    _URI_LINK_TRANSPORT,
    _URI_PARTY_RECORDS,
    _URI_PEER_EVENTS,
    _URI_PEER_GAME_STATE,
    _URI_PEER_PARTY_RECORDS,
    _URI_STATE_EPOCH,
    DEFAULT_HOOKS,
    LinkState,
    McpHarnessError,
    _bounded_positive_int,
    _decode_state_data,
    _error_code,
    _error_payload,
    _error_reply,
    _event_names,
    _ListenerCancelled,
    _require_pair,
    _require_peer,
    _text_reply,
)
from pokered_harness.mcp_server_remote import (
    _accept_remote,
    _cleanup_unpublished_remote,
    _close_serial_link,
    _deactivate_link_hooks,
    _detach_local_link_session,
    _disconnect_remote,
    _track_unpublished_remote_resource,
    _uninstall_remote_endpoint,
)
from pokered_harness.mcp_server_remote_contract import (
    _attach_network_backend,
    _bind_listener,
    _is_real_pyboy_instance,
    _negotiate_network_clock_role,
    _optional_rom_version,
    _peer_rom_version_if_ready,
    _positive_port,
    _refresh_remote_state,
    _reject_semantic_pair_on_native_runtime,
    _remaining,
    _require_native_network_contract,
    _require_pair_inactive,
    _require_pair_inactive_locked,
    _require_primary_rom_version,
    _require_remote_idle,
    _require_remote_idle_locked,
    _supports_bit_accurate_network,
    _validate_remote_host,
    _validate_rom_version,
    _validate_timeout,
    _wait_for_network_hello,
    _wait_for_remote_hello,
)
from pokered_harness.mcp_server_resources import (
    _epoch_payload,
    _game_state_payload,
    _resource_specs,
    read_resource,
)
from pokered_harness.mcp_server_serve import (
    _PEER_STATE_PATH_ENV,
    _PEER_STATE_SHA1_ENV,
    _env_flag,
    _peer_startup_state_from_env,
    build_server,
    main,
    register_default_hooks,
    serve_stdio,
)
from pokered_harness.mcp_server_timed import (
    _TIMED_INTEGER_FIELDS,
    _TIMED_TIMEOUT_FIELDS,
    _await_blocking_task,
    _await_owner_request,
    _close_sessions_independently,
    _consume_task_exception,
    _McpTaskRegistry,
    _shutdown_timed_owner,
    _timed_status,
    _timed_status_payload,
    _timed_tool_request,
    _wait_task_until,
    load_timed_policy_from_env,
)
from pokered_harness.mcp_server_tools import (
    _dispatch_session_tool,
    _tool_specs,
    dispatch_tool,
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
