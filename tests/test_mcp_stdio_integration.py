"""End-to-end integration test: spawn the MCP server as a subprocess and
drive it with the real MCP client SDK over stdio.

Skipped when ``POKERED_ROM_PATH`` is not set — the test requires a real
Game Boy ROM and its matching .sym file, which the repo doesn't ship.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("mcp.client.session")

import mcp.client.stdio as stdio_transport
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pokered_harness.config import load_versions
from tests._rom_assets import rom_path, sym_path

ROM_PATH = os.environ.get("POKERED_ROM_PATH")
SYM_PATH = os.environ.get("POKERED_SYM_PATH")
ROM_SHA1 = os.environ.get("POKERED_ROM_SHA1")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def _tracked_stdio_session(params_factory):
    processes = []
    original_factory = stdio_transport._create_platform_compatible_process

    async def record_process(*args, **kwargs):
        process = await original_factory(*args, **kwargs)
        processes.append(process)
        return process

    with patch.object(
        stdio_transport,
        "_create_platform_compatible_process",
        record_process,
    ):
        async with asyncio.timeout(120):
            async with stdio_client(params_factory()) as (read, write):
                async with ClientSession(read, write) as session:
                    yield session
    assert len(processes) == 1
    assert processes[0].returncode == 0


@asynccontextmanager
async def _tracked_stdio_pair_sessions(red_params, blue_params):
    """Track one red and one blue child across both link orientations."""
    processes = []
    original_factory = stdio_transport._create_platform_compatible_process

    async def record_process(*args, **kwargs):
        process = await original_factory(*args, **kwargs)
        processes.append(process)
        return process

    with patch.object(
        stdio_transport,
        "_create_platform_compatible_process",
        record_process,
    ):
        async with asyncio.timeout(120):
            async with stdio_client(red_params) as (red_read, red_write):
                async with ClientSession(red_read, red_write) as red:
                    async with stdio_client(blue_params) as (blue_read, blue_write):
                        async with ClientSession(blue_read, blue_write) as blue:
                            yield red, blue
    assert stdio_transport._create_platform_compatible_process is original_factory
    assert len(processes) == 2
    assert [process.returncode for process in processes] == [0, 0]


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
    sym_sha1 = load_versions(PROJECT_ROOT / "VERSIONS.md").sha1_for_path(sym)
    if sym_sha1 is not None:
        env["POKERED_SYM_SHA1"] = sym_sha1
    else:
        env.pop("POKERED_SYM_SHA1", None)
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
            "POKERED_ROM_PATH / POKERED_SYM_PATH not set; skipping real-ROM MCP integration"
        )
    return _server_params_for_paths(Path(ROM_PATH), Path(SYM_PATH), ROM_SHA1)


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


async def _link_status_resource(client: ClientSession) -> dict:
    result = await client.read_resource("pokered://link-status")
    assert result.contents, "expected a link-status resource content"
    return json.loads(result.contents[0].text)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stdio_list_tools_and_call_step():
    async with _tracked_stdio_session(_server_params) as session:
        await session.initialize()

        tool_list = await session.list_tools()
        tool_names = {t.name for t in tool_list.tools}
        assert {"step", "press", "save_state", "load_state", "run_until_event"} <= tool_names
        resources = await session.list_resources()
        resource_names = {resource.name for resource in resources.resources}
        assert {"Game State", "Event Log", "Link Status"} <= resource_names

        initial = await session.call_tool("link_status", {})
        assert _payload(initial)["primary_tick"] == 0
        pressed = await session.call_tool("press", {"button": "a", "duration": 2})
        assert pressed.isError is not True
        assert _payload(pressed) == {"ok": True}
        queued = await session.call_tool("link_status", {})
        assert _payload(queued)["primary_tick"] == 0

        # Call ``step`` and verify the session's tick advanced.
        result = await session.call_tool("step", {"count": 4})
        assert result.isError is not True
        payload = json.loads(result.content[0].text)
        assert payload["tick"] == 4

        press = await session.call_tool("press", {"button": "a", "duration": 2})
        assert _payload(press) == {"ok": True}


@pytest.mark.asyncio
async def test_stdio_game_state_resource_is_parseable():
    async with _tracked_stdio_session(_server_params) as session:
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
    async with _tracked_stdio_session(_server_params) as session:
        await session.initialize()

        # Preserve the original deterministic replay contract: save at 120,
        # advance 60, capture the expected state, restore, and replay those
        # same 60 frames.
        await session.call_tool("step", {"count": 120})
        checkpoint = await session.call_tool("save_state", {})
        assert checkpoint.isError is not True
        checkpoint_b64 = json.loads(checkpoint.content[0].text)["data"]
        checkpoint_blob = base64.b64decode(checkpoint_b64, validate=True)
        assert checkpoint_blob
        checkpoint_digest = hashlib.sha256(checkpoint_blob).hexdigest()

        await session.call_tool("step", {"count": 60})
        expected_replay = await session.call_tool("save_state", {})
        assert expected_replay.isError is not True
        expected_replay_b64 = json.loads(expected_replay.content[0].text)["data"]
        expected_replay_blob = base64.b64decode(expected_replay_b64, validate=True)
        assert expected_replay_blob
        assert len(expected_replay_blob) == len(checkpoint_blob)
        assert hashlib.sha256(expected_replay_blob).hexdigest() != checkpoint_digest
        assert expected_replay_blob != checkpoint_blob

        assert _payload(await session.call_tool("load_state", {"data": checkpoint_b64})) == {
            "ok": True
        }
        await session.call_tool("step", {"count": 60})
        replayed = await session.call_tool("save_state", {})
        assert replayed.isError is not True
        replayed_blob = base64.b64decode(
            json.loads(replayed.content[0].text)["data"], validate=True
        )
        assert len(replayed_blob) == len(expected_replay_blob)
        assert (
            hashlib.sha256(replayed_blob).hexdigest()
            == hashlib.sha256(expected_replay_blob).hexdigest()
        )
        assert replayed_blob == expected_replay_blob

        # Keep the original game-state roundtrip at its 200-frame checkpoint.
        await session.call_tool("step", {"count": 20})
        expected = await session.read_resource("pokered://game-state")
        expected_body = json.loads(expected.contents[0].text)  # type: ignore[union-attr]
        save = await session.call_tool("save_state", {})
        assert save.isError is not True
        blob_b64 = json.loads(save.content[0].text)["data"]
        blob = base64.b64decode(blob_b64, validate=True)
        assert len(blob) > 0
        blob_digest = hashlib.sha256(blob).hexdigest()

        # Advance further, then load the saved state back.
        await session.call_tool("step", {"count": 300})
        load_result = await session.call_tool("load_state", {"data": blob_b64})
        assert _payload(load_result) == {"ok": True}
        restored = await session.read_resource("pokered://game-state")
        restored_body = json.loads(restored.contents[0].text)  # type: ignore[union-attr]
        assert restored_body == expected_body
        saved_again = await session.call_tool("save_state", {})
        assert saved_again.isError is not True
        saved_again_blob = base64.b64decode(
            json.loads(saved_again.content[0].text)["data"], validate=True
        )
        assert len(saved_again_blob) == len(blob)
        assert hashlib.sha256(saved_again_blob).hexdigest() == blob_digest
        assert saved_again_blob == blob


@pytest.mark.asyncio
async def test_stdio_remote_link_lifecycle_and_explicit_disconnect():
    """Exercise both listener roles, same-port cancellation, and clean teardown."""
    red_params = _pinned_server_params("red")
    blue_params = _pinned_server_params("blue")

    async with _tracked_stdio_pair_sessions(red_params, blue_params) as (red, blue):
        await red.initialize()
        await blue.initialize()
        for client in (red, blue):
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {
                "press",
                "step",
                "link_listen",
                "link_connect",
                "link_status",
                "link_disconnect",
            } <= names

        async def status(client, mode, role, peer_version, tick):
            tool_state = _payload(await client.call_tool("link_status", {}))
            resource_state = await _link_status_resource(client)
            assert resource_state == tool_state
            assert tool_state["remote_mode"] == mode
            assert tool_state["remote_role"] == role
            assert tool_state["remote_peer_rom_version"] == peer_version
            assert tool_state["remote_error"] is None
            assert tool_state["link_backend"] == ("bit_accurate" if mode == "connected" else None)
            assert tool_state["primary_tick"] == tick
            return tool_state

        async def connect_pair(
            listener, connector, port, listener_version, connector_version, tick
        ):
            listening = _payload(
                await listener.call_tool(
                    "link_listen",
                    {
                        "host": "127.0.0.1",
                        "port": port,
                        "rom_version": listener_version,
                        "peer_rom_version": connector_version,
                        "timeout_s": 10,
                    },
                )
            )
            assert listening["remote_mode"] == "listening"
            assert listening["remote_role"] == "listener"
            assert listening["remote_bind_port"] == port
            listener_state = _payload(await listener.call_tool("link_status", {}))
            assert listener_state["remote_mode"] == "listening"
            assert listener_state["remote_role"] == "listener"
            assert listener_state["remote_bind_port"] == port
            connected = _payload(
                await connector.call_tool(
                    "link_connect",
                    {
                        "host": "127.0.0.1",
                        "port": port,
                        "rom_version": connector_version,
                        "peer_rom_version": listener_version,
                        "timeout_s": 15,
                    },
                )
            )
            assert connected["remote_mode"] == "connected"
            assert connected["remote_role"] == "connector"
            assert connected["peer_rom_version"] == listener_version
            assert (await _wait_remote_mode(listener, "connected"))["remote_role"] == "listener"
            await status(listener, "connected", "listener", connector_version, tick)
            await status(connector, "connected", "connector", listener_version, tick)

        async def exercise_input(red_tick):
            pressed = await asyncio.gather(
                red.call_tool("press", {"button": "a", "duration": 2}),
                blue.call_tool("press", {"button": "a", "duration": 2}),
            )
            assert [_payload(result) for result in pressed] == [{"ok": True}, {"ok": True}]
            stepped = await asyncio.gather(
                red.call_tool("step", {"count": 4}),
                blue.call_tool("step", {"count": 4}),
            )
            assert [_payload(result)["tick"] for result in stepped] == [red_tick, red_tick]

        async def disconnect_pair(listener, connector, tick):
            loop = asyncio.get_running_loop()
            started = loop.time()
            async with asyncio.timeout(15):
                disconnected = _payload(await connector.call_tool("link_disconnect", {}))
                assert disconnected["remote_mode"] == "idle"
                await _wait_remote_mode(listener, "idle")
                assert _payload(await listener.call_tool("link_disconnect", {})) == {
                    "remote_mode": "idle"
                }
                # Both public disconnect calls are idempotent after teardown.
                assert _payload(await connector.call_tool("link_disconnect", {})) == {
                    "remote_mode": "idle"
                }
                assert _payload(await listener.call_tool("link_disconnect", {})) == {
                    "remote_mode": "idle"
                }
            assert loop.time() - started < 15
            await status(listener, "idle", None, None, tick)
            await status(connector, "idle", None, None, tick)

        port_one = _free_port()
        await connect_pair(red, blue, port_one, "red", "blue", 0)
        await exercise_input(4)
        await disconnect_pair(red, blue, 4)

        port_two = _free_port()
        # Reverse listener/connector roles. Cancel the first blue listener and
        # prove the same process can bind the same port again before red joins.
        first_listen = _payload(
            await blue.call_tool(
                "link_listen",
                {
                    "host": "127.0.0.1",
                    "port": port_two,
                    "rom_version": "blue",
                    "peer_rom_version": "red",
                    "timeout_s": 10,
                },
            )
        )
        assert first_listen["remote_role"] == "listener"
        async with asyncio.timeout(15):
            assert _payload(await blue.call_tool("link_disconnect", {})) == {"remote_mode": "idle"}
        await status(blue, "idle", None, None, 4)
        await connect_pair(blue, red, port_two, "blue", "red", 4)
        await exercise_input(8)
        await disconnect_pair(blue, red, 8)
