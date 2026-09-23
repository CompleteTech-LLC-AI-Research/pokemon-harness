"""MCP tool catalogue and top-level tool dispatch.

Split from ``pokered_harness.mcp_server`` for issue #125 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on ``pokered_harness.mcp_server`` stay visible.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import mcp.types as mcp_types

# Direct names this module calls; cyclic back-edges go through _entry.
from pokered_harness.mcp_server_model import (
    _DEFAULT_REMOTE_HELLO_TIMEOUT_S,
    _LOCAL_LINK_TOOL_NAMES,
    _MAX_BUTTON_DURATION,
    _MAX_EVENT_NAME_LENGTH,
    _MAX_EVENT_NAMES,
    _MAX_REMOTE_TIMEOUT_S,
    _MAX_RUN_CHUNK,
    _MAX_RUN_TICKS,
    _MAX_STATE_BYTES,
    _MAX_STEP_TICKS,
    LinkState,
    McpHarnessError,
    _bounded_positive_int,
    _decode_state_data,
    _event_names,
)
from pokered_harness.mcp_timed_owner import TimedOwner
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import Session

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
                            "a",
                            "b",
                            "start",
                            "select",
                            "up",
                            "down",
                            "left",
                            "right",
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
                "Tick forward until any of the named events fires or the tick budget is exhausted."
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
            name="link_frame_barrier",
            description=(
                "Enable or disable the network frame barrier on a connected "
                "remote link. Both endpoints must call this at the same "
                "ROM-owned boundary; the ROM still owns all serial registers."
            ),
            inputSchema={
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"],
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


def _dispatch_session_tool(session: Session, name: str, arguments: dict[str, Any]) -> Any:
    if name == "step":
        count = _bounded_positive_int(arguments["count"], "count", _MAX_STEP_TICKS)
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
        max_ticks = _bounded_positive_int(arguments["max_ticks"], "max_ticks", _MAX_RUN_TICKS)
        chunk = _bounded_positive_int(arguments.get("chunk", 16), "chunk", _MAX_RUN_CHUNK)
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
            return _entry._timed_status(timed_owner)
        request = _entry._timed_tool_request(timed_owner, session, name, arguments, link)
        if name == "link_listen":
            try:
                return _entry._timed_status_payload(
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
        return (
            _entry._timed_status(timed_owner)
            if name in {"link_connect", "link_disconnect"}
            else result
        )
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
        return _entry._dispatch_link_tool(session, name, arguments, link)
    with link.operation():
        return _entry._dispatch_link_tool(session, name, arguments, link)


# Call-time indirection so facade-level monkeypatches stay visible here.
import pokered_harness.mcp_server as _entry
