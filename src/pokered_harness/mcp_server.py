"""MCP server exposing a long-lived :class:`Session` over stdio.

Actions (mutating the emulator) are MCP **tools**; observations (reading
game state, events) are MCP **resources**. That split matches the ADR.

The server is intentionally thin — every operation delegates to the
session, which is where lifecycle, version enforcement, and hooks live.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from pokered_harness.events.hooks import GameEvent
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import Session

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


# -- tool definitions --------------------------------------------------------


def _tool_specs() -> list[mcp_types.Tool]:
    return [
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
    ]


# -- dispatch helpers --------------------------------------------------------


def _text_reply(payload: Any) -> list[mcp_types.TextContent]:
    return [mcp_types.TextContent(type="text", text=json.dumps(payload))]


def dispatch_tool(session: Session, name: str, arguments: dict[str, Any]) -> Any:
    """Pure dispatch — no asyncio, no MCP types. Unit-testable on its own."""
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
        session.load_state(base64.b64decode(arguments["data"]))
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


def read_resource(session: Session, uri: str) -> str:
    """Resource reader — returns JSON text."""
    if uri == _URI_GAME_STATE:
        return json.dumps(to_jsonable(session.read_game_state()))
    if uri == _URI_EVENT_LOG:
        events: list[GameEvent] = list(session.events)
        return json.dumps([to_jsonable(e) for e in events])
    raise ValueError(f"unknown resource: {uri!r}")


def _resource_specs() -> list[mcp_types.Resource]:
    return [
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
    ]


# -- server wiring -----------------------------------------------------------


def build_server(session: Session) -> Server:
    """Construct an MCP ``Server`` bound to ``session``.

    The server stays un-run — caller invokes ``server.run`` via an
    appropriate transport. This makes the wiring itself testable without
    spawning stdio pipes.
    """
    server: Server = Server("pokered-harness")

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return _tool_specs()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[mcp_types.TextContent]:
        args = arguments or {}
        result = dispatch_tool(session, name, args)
        return _text_reply(result)

    @server.list_resources()
    async def _list_resources() -> list[mcp_types.Resource]:
        return _resource_specs()

    @server.read_resource()
    async def _read_resource(uri: Any) -> str:
        return read_resource(session, str(uri))

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


async def serve_stdio(session: Session) -> None:
    server = build_server(session)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """CLI entry point: ``python -m pokered_harness.mcp_server``.

    Required environment variables:

    * ``POKERED_ROM_PATH`` — path to the .gb ROM.
    * ``POKERED_SYM_PATH`` — path to the matching .sym file.

    Optional:

    * ``POKERED_ROM_SHA1`` — explicit pin. When unset, the server reads
      ``VERSIONS.md`` (relative to cwd) and enforces the SHA-1 from it.
      Set ``POKERED_SKIP_SHA1=1`` to opt out of SHA-1 enforcement
      entirely (useful for ad-hoc testing on non-stock ROMs).
    """
    import contextlib
    import sys

    from pokered_harness.config import VersionsConfigError, load_versions

    rom = os.environ.get("POKERED_ROM_PATH")
    sym = os.environ.get("POKERED_SYM_PATH")
    if not rom or not sym:
        raise SystemExit(
            "set POKERED_ROM_PATH and POKERED_SYM_PATH before launching"
        )

    expected_sha: str | None = os.environ.get("POKERED_ROM_SHA1")
    if expected_sha is None and not os.environ.get("POKERED_SKIP_SHA1"):
        try:
            expected_sha = load_versions().rom_sha1
        except VersionsConfigError:
            # No VERSIONS.md in cwd — skip enforcement rather than fail
            # hard, so the server can boot from arbitrary working dirs.
            expected_sha = None

    # CRITICAL: PyBoy's init writes ~70KB of `.sym`-skip warnings to
    # stdout via a logging handler it installs before we get a chance
    # to reconfigure it. MCP's stdio transport uses stdout exclusively
    # for JSON-RPC, so anything the emulator prints would corrupt the
    # stream and wedge the client at ``initialize``. Capture all stdout
    # during boot and re-emit it on stderr so the noise is still
    # visible but out of the RPC channel.
    with contextlib.redirect_stdout(sys.stderr):
        session = Session.from_files(
            Path(rom),
            Path(sym),
            expected_rom_sha1=expected_sha,
        )
        register_default_hooks(session)

    try:
        asyncio.run(serve_stdio(session))
    finally:
        session.close()


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_HOOKS",
    "build_server",
    "dispatch_tool",
    "main",
    "read_resource",
    "register_default_hooks",
    "serve_stdio",
]
