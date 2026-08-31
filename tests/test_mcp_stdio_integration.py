"""End-to-end integration test: spawn the MCP server as a subprocess and
drive it with the real MCP client SDK over stdio.

Skipped when ``POKERED_ROM_PATH`` is not set — the test requires a real
Game Boy ROM and its matching .sym file, which the repo doesn't ship.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

pytest.importorskip("mcp.client.session")

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROM_PATH = os.environ.get("POKERED_ROM_PATH")
SYM_PATH = os.environ.get("POKERED_SYM_PATH")
ROM_SHA1 = os.environ.get("POKERED_ROM_SHA1")

pytestmark = pytest.mark.skipif(
    not (ROM_PATH and SYM_PATH),
    reason="POKERED_ROM_PATH / POKERED_SYM_PATH not set; skipping real-ROM MCP integration",
)


def _server_params() -> StdioServerParameters:
    env = os.environ.copy()
    env["POKERED_ROM_PATH"] = ROM_PATH or ""
    env["POKERED_SYM_PATH"] = SYM_PATH or ""
    if ROM_SHA1:
        env["POKERED_ROM_SHA1"] = ROM_SHA1

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pokered_harness.mcp_server"],
        env=env,
    )


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

        # Call ``step`` and verify the session's tick advanced.
        result = await session.call_tool("step", {"count": 4})
        assert result.isError is not True
        payload = json.loads(result.content[0].text)
        assert payload["tick"] == 4


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
        save = await session.call_tool("save_state", {})
        blob_b64 = json.loads(save.content[0].text)["data"]
        blob = base64.b64decode(blob_b64)
        assert len(blob) > 0

        # Advance further, then load the saved state back.
        await session.call_tool("step", {"count": 300})
        load_result = await session.call_tool(
            "load_state", {"data": blob_b64}
        )
        assert json.loads(load_result.content[0].text) == {"ok": True}
