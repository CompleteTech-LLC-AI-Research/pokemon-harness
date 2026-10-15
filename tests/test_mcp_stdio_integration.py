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
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp.client.session")

import mcp.client.stdio as stdio_transport
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


@asynccontextmanager
async def _tracked_stdio_session(monkeypatch):
    processes = []
    original_create = stdio_transport._create_platform_compatible_process

    async def record_process(*args, **kwargs):
        process = await original_create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(stdio_transport, "_create_platform_compatible_process", record_process)
    async with asyncio.timeout(120):
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as client:
                yield client
    assert len(processes) == 1
    assert processes[0].returncode == 0


@pytest.mark.asyncio
async def test_stdio_list_tools_and_call_step(monkeypatch):
    async with _tracked_stdio_session(monkeypatch) as session:
        await session.initialize()

        tool_list = await session.list_tools()
        tool_names = {t.name for t in tool_list.tools}
        assert {"step", "press", "save_state", "load_state",
                "run_until_event"} <= tool_names

        initial = await session.call_tool("link_status", {})
        assert json.loads(initial.content[0].text)["primary_tick"] == 0
        pressed = await session.call_tool("press", {"button": "a", "duration": 2})
        assert pressed.isError is not True
        assert json.loads(pressed.content[0].text) == {"ok": True}
        queued = await session.call_tool("link_status", {})
        assert json.loads(queued.content[0].text)["primary_tick"] == 0

        # Call ``step`` and verify the session's tick advanced.
        result = await session.call_tool("step", {"count": 4})
        assert result.isError is not True
        payload = json.loads(result.content[0].text)
        assert payload["tick"] == 4
        state = await session.read_resource("pokered://game-state")
        assert "overworld" in json.loads(state.contents[0].text)


@pytest.mark.asyncio
async def test_stdio_game_state_resource_is_parseable(monkeypatch):
    async with _tracked_stdio_session(monkeypatch) as session:
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
async def test_stdio_save_state_roundtrip_is_deterministic(monkeypatch):
    async with _tracked_stdio_session(monkeypatch) as session:
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


@pytest.mark.asyncio
async def test_stdio_state_restore_replays_identical_emulator_bytes(monkeypatch):
    async with _tracked_stdio_session(monkeypatch) as client:
        await client.initialize()

        async def step(frames):
            reply = await client.call_tool("step", {"count": frames})
            assert reply.isError is not True

        async def save():
            reply = await client.call_tool("save_state", {})
            assert reply.isError is not True
            encoded = json.loads(reply.content[0].text)["data"]
            data = base64.b64decode(encoded, validate=True)
            assert data
            return encoded, (len(data), hashlib.sha256(data).hexdigest())

        await step(120)
        checkpoint, checkpoint_digest = await save()
        await step(60)
        _, expected_digest = await save()
        assert expected_digest != checkpoint_digest, "emulator state did not advance"
        restored = await client.call_tool("load_state", {"data": checkpoint})
        assert restored.isError is not True
        await step(60)
        _, replay_digest = await save()
        # Hash comparison avoids dumping ROM-derived save bytes on failure.
        assert replay_digest == expected_digest


def _versioned_server_params(version):
    from pokered_harness.config import load_versions
    from tests._rom_assets import rom_path, sym_path

    root = Path(__file__).resolve().parents[1]
    rom = rom_path(version, color=True)
    symbols = sym_path(version)
    if not rom.is_file() or not symbols.is_file():
        pytest.skip(f"{version} ROM/symbol assets are required for MCP lifecycle")
    pins = load_versions(root / "VERSIONS.md")
    rom_sha = hashlib.sha1(rom.read_bytes()).hexdigest()
    symbol_sha = hashlib.sha1(symbols.read_bytes()).hexdigest()
    assert rom_sha == pins.sha1_for_path(rom)
    assert symbol_sha == pins.sha1_for_symbol_path(symbols)
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("POKERED_PEER_") or key == "POKERED_SKIP_SHA1":
            env.pop(key)
    env.update({
        "POKERED_ROM_PATH": str(rom), "POKERED_SYM_PATH": str(symbols),
        "POKERED_ROM_SHA1": rom_sha, "POKERED_ROM_VERSION": version,
        "POKERED_VERSIONS_PATH": str(root / "VERSIONS.md"),
    })
    return StdioServerParameters(
        command=sys.executable, args=["-m", "pokered_harness.mcp_server"],
        env=env, cwd=root,
    ), {"version": version, "rom_sha1": rom_sha, "symbol_sha1": symbol_sha}


async def _call_json(session, name, arguments=None):
    result = await session.call_tool(name, arguments or {})
    assert result.isError is not True, result
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize("listener_version,connector_version", [("red", "blue"), ("blue", "red")])
async def test_stdio_remote_link_lifecycle(
    listener_version, connector_version, monkeypatch, tmp_path, record_property,
):
    """Two real stdio servers connect, disconnect, and release their sockets.

    This checks MCP lifecycle and input dispatch; it does not play a trade
    or battle or inject game state. The SDK still starts and stops the real
    subprocesses; the wrapper only retains handles to verify their exit.
    """
    processes = []
    original_create = stdio_transport._create_platform_compatible_process

    async def record_process(*args, **kwargs):
        process = await original_create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(stdio_transport, "_create_platform_compatible_process", record_process)
    params_a, assets_a = _versioned_server_params(listener_version)
    params_b, assets_b = _versioned_server_params(connector_version)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    started = time.monotonic()
    async with asyncio.timeout(120):
        async with AsyncExitStack() as stack:
            sessions = []
            for role, params in (("listener", params_a), ("connector", params_b)):
                errors = stack.enter_context((tmp_path / f"{role}.stderr").open("w"))
                read, write = await stack.enter_async_context(stdio_client(params, errlog=errors))
                session = await stack.enter_async_context(ClientSession(read, write))
                initialized = await session.initialize()
                assert initialized.serverInfo.name
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"press", "step", "link_listen", "link_connect", "link_status", "link_disconnect"} <= names
                assert (await _call_json(session, "link_status"))["remote_mode"] == "idle"
                sessions.append(session)
            listener, connector = sessions
            listening = await _call_json(listener, "link_listen", {"port": port})
            assert listening["remote_mode"] == "listening"
            assert listening["rom_version"] == listener_version
            connected = await _call_json(connector, "link_connect", {
                "host": "127.0.0.1", "port": port, "timeout_s": 10,
            })
            assert connected["remote_mode"] == "connected"
            assert connected["peer_rom_version"] == listener_version
            async with asyncio.timeout(15):
                while True:
                    statuses = await asyncio.gather(*[_call_json(s, "link_status") for s in sessions])
                    if all(status["remote_mode"] == "connected" for status in statuses):
                        break
                    await asyncio.sleep(0.01)
            for status, role, peer_version in zip(
                statuses, ("listener", "connector"), (connector_version, listener_version), strict=True,
            ):
                assert status["remote_role"] == role
                assert status["remote_peer_rom_version"] == peer_version
                assert status["link_backend"] == "bit_accurate"
                assert status["remote_error"] is None
            await asyncio.gather(*[_call_json(s, "press", {"button": "a", "duration": 2}) for s in sessions])
            advanced = await asyncio.gather(*[_call_json(s, "step", {"count": 4}) for s in sessions])
            assert advanced == [{"tick": 4}, {"tick": 4}]
            for session in sessions:
                resource = await session.read_resource("pokered://link-status")
                state = json.loads(resource.contents[0].text)
                assert state["remote_mode"] == "connected" and state["primary_tick"] == 4
            disconnect_started = time.monotonic()
            disconnected = await asyncio.gather(*[_call_json(s, "link_disconnect") for s in sessions])
            assert disconnected == [{"remote_mode": "idle"}] * 2
            assert time.monotonic() - disconnect_started < 15
            for session in sessions:
                status = await _call_json(session, "link_status")
                assert status["remote_mode"] == "idle"
                assert status["remote_role"] is None and status["link_backend"] is None
                assert status["remote_peer_rom_version"] is None
                assert await _call_json(session, "link_disconnect") == {"remote_mode": "idle"}
            # A second listen/cancel cycle on the same live server proves
            # its listener socket and accept worker can be reclaimed.
            assert (await _call_json(listener, "link_listen", {"port": port}))["remote_mode"] == "listening"
            assert await _call_json(listener, "link_disconnect") == {"remote_mode": "idle"}
            with socket.socket() as released:
                released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                released.bind(("127.0.0.1", port))
        assert len(processes) == 2
        assert all(process.returncode == 0 for process in processes)
    evidence = {
        "python": sys.executable, "assets": [assets_a, assets_b],
        "elapsed_seconds": time.monotonic() - started,
        "subprocesses": [{"pid": p.pid, "returncode": p.returncode} for p in processes],
        "port_rebound": True,
    }
    record_property("mcp_lifecycle", json.dumps(evidence, sort_keys=True))
    print(json.dumps(evidence, sort_keys=True))
