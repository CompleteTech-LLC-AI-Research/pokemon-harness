"""End-to-end integration test: spawn the MCP server as a subprocess and
drive it with the real MCP client SDK over stdio.

Skipped when ``POKERED_ROM_PATH`` is not set — the test requires a real
Game Boy ROM and its matching .sym file, which the repo doesn't ship.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.client.session")

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pokered_harness.config import load_versions
from tests._rom_assets import rom_path, sym_path

ROM_PATH = os.environ.get("POKERED_ROM_PATH")
SYM_PATH = os.environ.get("POKERED_SYM_PATH")
ROM_SHA1 = os.environ.get("POKERED_ROM_SHA1")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _server_params_for_paths(
    rom: Path,
    sym: Path,
    sha1: str | None,
    *,
    version: str | None = None,
) -> StdioServerParameters:
    env = os.environ.copy()
    # Keep subprocesses single-session for this test module.  In particular,
    # an operator's peer variables must not silently change list_tools().
    for name in (
        "POKERED_PEER_ROM_PATH",
        "POKERED_PEER_SYM_PATH",
        "POKERED_PEER_ROM_SHA1",
        "POKERED_PEER_ROM_VERSION",
        "POKERED_PEER_SYM_SHA1",
        "POKERED_SKIP_SHA1",
    ):
        env.pop(name, None)
    env["POKERED_ROM_PATH"] = str(rom)
    env["POKERED_SYM_PATH"] = str(sym)
    env["POKERED_VERSIONS_PATH"] = str(PROJECT_ROOT / "VERSIONS.md")
    if sha1:
        env["POKERED_ROM_SHA1"] = sha1
    else:
        env.pop("POKERED_ROM_SHA1", None)
    if version is not None:
        env["POKERED_ROM_VERSION"] = version

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pokered_harness.mcp_server"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


def _server_params() -> StdioServerParameters:
    if not (ROM_PATH and SYM_PATH):
        pytest.skip(
            "POKERED_ROM_PATH / POKERED_SYM_PATH not set; "
            "skipping real-ROM MCP integration"
        )
    return _server_params_for_paths(
        Path(ROM_PATH), Path(SYM_PATH), ROM_SHA1
    )


def _pinned_server_params(version: str) -> StdioServerParameters:
    rom = rom_path(version)
    sym = sym_path(version)
    if not rom.is_file() or not sym.is_file():
        pytest.skip(f"{version} ROM and symbols are not available")
    pins = load_versions(PROJECT_ROOT / "VERSIONS.md")
    sha1 = pins.sha1_for_path(rom)
    if sha1 is None:
        pytest.skip(f"no VERSIONS.md SHA-1 pin for {rom}")
    return _server_params_for_paths(rom, sym, sha1, version=version)


def _payload(result):
    assert result.isError is not True, result
    assert result.content
    return json.loads(result.content[0].text)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_remote_mode(client: ClientSession, mode: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 8.0
    status = {}
    while asyncio.get_running_loop().time() < deadline:
        status = _payload(await client.call_tool("link_status", {}))
        if status.get("remote_mode") == mode:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"remote_mode never reached {mode!r}: {status!r}")


@pytest.mark.asyncio
async def test_stdio_list_tools_and_call_step():
    async with (
        stdio_client(_server_params()) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        tool_list = await session.list_tools()
        tool_names = {t.name for t in tool_list.tools}
        assert {"step", "press", "save_state", "load_state",
                "run_until_event"} <= tool_names
        resources = await session.list_resources()
        resource_names = {resource.name for resource in resources.resources}
        assert {"Game State", "Event Log", "Link Status"} <= resource_names

        # Call ``step`` and verify the session's tick advanced.
        result = await session.call_tool("step", {"count": 4})
        assert result.isError is not True
        payload = json.loads(result.content[0].text)
        assert payload["tick"] == 4

        press = await session.call_tool(
            "press", {"button": "a", "duration": 2}
        )
        assert _payload(press) == {"ok": True}


@pytest.mark.asyncio
async def test_stdio_game_state_resource_is_parseable():
    async with (
        stdio_client(_server_params()) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        # Advance a bit so memory isn't all-zero uninitialised noise.
        await session.call_tool("step", {"count": 120})

        result = await session.read_resource("pokered://game-state")
        # ReadResourceResult.contents is a list of ResourceContents.
        assert result.contents, "expected at least one resource content"
        body = json.loads(result.contents[0].text)  # type: ignore[union-attr]
        # Schema assertions — not value assertions, since the game is
        # still booting and no specific state is guaranteed.
        assert "overworld" in body
        assert "party" in body
        assert "battle" in body
        assert "progress" in body
        assert "map_id" in body["overworld"]


@pytest.mark.asyncio
async def test_stdio_save_state_roundtrip_is_deterministic():
    async with (
        stdio_client(_server_params()) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        await session.call_tool("step", {"count": 200})
        expected = await session.read_resource("pokered://game-state")
        expected_body = json.loads(expected.contents[0].text)  # type: ignore[union-attr]
        save = await session.call_tool("save_state", {})
        blob_b64 = json.loads(save.content[0].text)["data"]
        blob = base64.b64decode(blob_b64)
        assert len(blob) > 0

        # Advance further, then load the saved state back.
        await session.call_tool("step", {"count": 300})
        load_result = await session.call_tool(
            "load_state", {"data": blob_b64}
        )
        assert _payload(load_result) == {"ok": True}
        restored = await session.read_resource("pokered://game-state")
        restored_body = json.loads(restored.contents[0].text)  # type: ignore[union-attr]
        assert restored_body == expected_body


@pytest.mark.asyncio
async def test_stdio_remote_link_lifecycle_and_eof_cleanup():
    """Exercise the real MCP API, including process EOF without disconnect."""
    port = _free_port()
    listener_params = _pinned_server_params("red")
    connector_params = _pinned_server_params("blue")

    async with stdio_client(listener_params) as (listener_read, listener_write):  # noqa: SIM117 - connector must close before listener
        async with ClientSession(listener_read, listener_write) as listener:
            await listener.initialize()
            listener_tools = {tool.name for tool in (await listener.list_tools()).tools}
            assert {
                "link_listen",
                "link_connect",
                "link_status",
                "link_disconnect",
            } <= listener_tools

            listen = _payload(
                await listener.call_tool(
                    "link_listen",
                    {
                        "host": "127.0.0.1",
                        "port": port,
                        "rom_version": "red",
                        "timeout_s": 10,
                    },
                )
            )
            assert listen["remote_mode"] == "listening"

            # The connector context is intentionally left without a
            # link_disconnect call.  Closing its stdio input sends EOF to the
            # real server, which must close its TCP worker and owned session.
            async with stdio_client(connector_params) as (connector_read, connector_write):  # noqa: SIM117 - connector EOF is observed while listener remains alive
                async with ClientSession(connector_read, connector_write) as connector:
                    await connector.initialize()
                    connect = _payload(
                        await connector.call_tool(
                            "link_connect",
                            {
                                "host": "127.0.0.1",
                                "port": port,
                                "rom_version": "blue",
                                "peer_rom_version": "red",
                                "timeout_s": 15,
                            },
                        )
                    )
                    assert connect["remote_mode"] == "connected"
                    assert connect["peer_rom_version"] == "red"
                    connector_status = _payload(
                        await connector.call_tool("link_status", {})
                    )
                    assert connector_status["remote_mode"] == "connected"

                # ClientSession/std_io context teardown is the EOF under test.

            listener_status = await _wait_remote_mode(listener, "idle")
            assert listener_status["remote_error"] is None
            assert _payload(
                await listener.call_tool("link_disconnect", {})
            ) == {"remote_mode": "idle"}
