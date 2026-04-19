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
import json
import os
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from pokered_harness.events.hooks import GameEvent
from pokered_harness.link.pair import LinkPair
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
_URI_PEER_GAME_STATE = "pokered://peer-game-state"
_URI_LINK_TRANSPORT = "pokered://link-transport"


# -- server state container --------------------------------------------------


class LinkState:
    """Mutable link-cable state owned by the server.

    Holds an optional peer :class:`Session` (constructed at startup when
    env vars are set) and an optional :class:`LinkPair` (built lazily on
    the first ``link_pair`` tool call). When ``peer_session is None`` the
    server runs in single-session mode and every ``link_*`` tool errors.
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
    ]


# -- dispatch helpers --------------------------------------------------------


def _text_reply(payload: Any) -> list[mcp_types.TextContent]:
    return [mcp_types.TextContent(type="text", text=json.dumps(payload))]


def _require_peer(link: LinkState) -> Session:
    if link.peer_session is None:
        raise ValueError(
            "peer session not configured; set POKERED_PEER_ROM_PATH and "
            "POKERED_PEER_SYM_PATH at server startup"
        )
    return link.peer_session


def _require_pair(link: LinkState) -> LinkPair:
    if link.pair is None or not link.pair.paired:
        raise ValueError("not paired; call link_pair first")
    return link.pair


def dispatch_tool(
    session: Session,
    name: str,
    arguments: dict[str, Any],
    link: LinkState | None = None,
) -> Any:
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

    # -- link-cable tools -------------------------------------------------
    if name.startswith("link_"):
        if link is None:
            link = LinkState()
        return _dispatch_link_tool(session, name, arguments, link)

    raise ValueError(f"unknown tool: {name!r}")


def _dispatch_link_tool(
    session: Session,
    name: str,
    arguments: dict[str, Any],
    link: LinkState,
) -> Any:
    if name == "link_pair":
        peer = _require_peer(link)
        if link.pair is not None and link.pair.paired:
            raise ValueError("already paired; call link_unpair first")
        if link.pair is None:
            link.pair = LinkPair(
                session,
                peer,
                version_primary=link.primary_version,
                version_peer=link.peer_version,
            )
        link.pair.pair()
        return {
            "paired": True,
            "primary_version": link.primary_version,
            "peer_version": link.peer_version,
        }

    if name == "link_unpair":
        if link.pair is not None and link.pair.paired:
            link.pair.unpair()
        link.pair = None
        return {"paired": False}

    if name == "link_step":
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
        paired = link.pair is not None and link.pair.paired
        transport_snapshot: dict[str, Any] = (
            link.pair.transport.snapshot() if link.pair is not None else {"a_to_b": [], "b_to_a": []}
        )
        return {
            "paired": paired,
            "transport": transport_snapshot,
            "primary_tick": session.current_tick(),
            "peer_tick": link.peer_session.current_tick() if link.peer_session else None,
        }

    raise ValueError(f"unknown tool: {name!r}")


def read_resource(
    session: Session,
    uri: str,
    link: LinkState | None = None,
) -> str:
    """Resource reader — returns JSON text."""
    if uri == _URI_GAME_STATE:
        return json.dumps(to_jsonable(session.read_game_state()))
    if uri == _URI_EVENT_LOG:
        events: list[GameEvent] = list(session.events)
        return json.dumps([to_jsonable(e) for e in events])
    if uri == _URI_PEER_GAME_STATE:
        if link is None or link.peer_session is None:
            raise ValueError("peer session not configured")
        return json.dumps(to_jsonable(link.peer_session.read_game_state()))
    if uri == _URI_LINK_TRANSPORT:
        if link is None or link.pair is None:
            return json.dumps({"a_to_b": [], "b_to_a": []})
        return json.dumps(link.pair.transport.snapshot())
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
        return _tool_specs()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[mcp_types.TextContent]:
        args = arguments or {}
        result = dispatch_tool(session, name, args, link=link)
        return _text_reply(result)

    @server.list_resources()
    async def _list_resources() -> list[mcp_types.Resource]:
        return _resource_specs(has_peer=link.peer_session is not None)

    @server.read_resource()
    async def _read_resource(uri: Any) -> str:
        return read_resource(session, str(uri), link=link)

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
) -> None:
    server = build_server(
        session,
        peer_session=peer_session,
        primary_version=primary_version,
        peer_version=peer_version,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """CLI entry point: ``python -m pokered_harness.mcp_server``.

    Required environment variables:

    * ``POKERED_ROM_PATH`` — path to the primary .gb ROM.
    * ``POKERED_SYM_PATH`` — path to the matching .sym file.

    Optional:

    * ``POKERED_ROM_SHA1`` — explicit pin. When unset, the server reads
      ``VERSIONS.md`` (relative to cwd) and enforces the SHA-1 from it.
      Set ``POKERED_SKIP_SHA1=1`` to opt out of SHA-1 enforcement
      entirely (useful for ad-hoc testing on non-stock ROMs).
    * ``POKERED_PEER_ROM_PATH`` / ``POKERED_PEER_SYM_PATH`` /
      ``POKERED_PEER_ROM_SHA1`` — when set, a peer Session is constructed
      at startup and the link-cable tools become usable. The pair is NOT
      auto-paired — invoke ``link_pair`` explicitly.
    """
    import contextlib
    import sys

    from pokered_harness.config import (
        VersionsConfigError,
        load_peer_env,
        load_primary_env,
        load_versions,
    )

    primary_env = load_primary_env()
    peer_env = load_peer_env()
    if not primary_env.rom_path or not primary_env.sym_path:
        raise SystemExit(
            "set POKERED_ROM_PATH and POKERED_SYM_PATH before launching"
        )

    expected_sha: str | None = primary_env.rom_sha1
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
    peer_session: Session | None = None
    with contextlib.redirect_stdout(sys.stderr):
        session = Session.from_files(
            Path(primary_env.rom_path),
            Path(primary_env.sym_path),
            expected_rom_sha1=expected_sha,
        )
        register_default_hooks(session)
        if peer_env.rom_path and peer_env.sym_path:
            peer_session = Session.from_files(
                Path(peer_env.rom_path),
                Path(peer_env.sym_path),
                expected_rom_sha1=peer_env.rom_sha1,
            )
            register_default_hooks(peer_session)

    try:
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
        session.close()


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
