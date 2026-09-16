"""Actual timed MCP subprocesses using an authored cartridge and source PyBoy.

This is public API/frame-routing evidence, not commercial-ROM or serial-transfer
acceptance. The cartridge disables serial requests itself; no CPU scheduler,
fake endpoint, ROM hash bypass, register writes, or emulator hooks are installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CALL_BOUND = 20.0
EXIT_BOUND = 12.0
LOG_CAP = 64 * 1024
REPORT_PREFIX = "TIMED_STDIO_REPORT "

# Keep observations outside the MCP implementation. Final counters are read only
# after serve_stdio has drained its owners, and expose actual emulator progress.
CHILD = r"""
import asyncio
import contextlib
import importlib.machinery
import json
import sys
from pathlib import Path

with contextlib.redirect_stdout(sys.stderr):
    from pyboy import PyBoy
    from pyboy.core import cpu, mb, serial
    from pokered_harness.mcp_server import serve_stdio
    from pokered_harness.mcp_timed_owner import TimedOwnerPolicy
    from pokered_harness.session import Session
    from pokered_harness.symbols.loader import load_sym_text

    for module in (cpu, mb, serial):
        assert Path(module.__file__).suffix == ".py", module.__file__
        assert Path(module.__file__).resolve().is_relative_to(
            (Path.cwd() / "vendor/pyboy-src").resolve()
        ), module.__file__
        assert not isinstance(module.__loader__, importlib.machinery.ExtensionFileLoader)
    game = PyBoy(sys.argv[1], bootrom=sys.argv[2], window="null", sound_emulated=False)
    session = Session(pyboy=game, symbols=load_sym_text(""), view=False)
    game.set_emulation_speed(0)
    session.step(1, render=False)
    assert game.frame_count == session.current_tick() == 1
    assert game.register_file.PC in (0x155, 0x156, 0x158)
    initial = {"frames": game.frame_count, "cycles": game.mb.cpu.cycles,
               "instructions": game.mb.cpu.retired_instructions}
    policy = TimedOwnerPolicy(
        rearm_budget=32, rearm_instruction_cap=16, max_edge_lateness=32,
        quantum_cycles=256, operation_timeout=5.0, max_wait_attempts=64,
        inbound_capacity=256, queue_capacity=16, request_timeout=10.0,
        lock_timeout=2.0, close_timeout=5.0,
    )

try:
    asyncio.run(serve_stdio(session, primary_version=sys.argv[3], timed_policy=policy))
finally:
    with contextlib.redirect_stdout(sys.stderr):
        # Ownership transferred to serve_stdio: it must close on the native owner.
        # Never conceal missing owner cleanup with a foreign-thread Session.close.
        assert session.closed, "serve_stdio returned without owner Session cleanup"
        print("TIMED_STDIO_REPORT " + json.dumps({
            "initial": initial, "frames": game.frame_count,
            "tick": session.current_tick(), "cycles": game.mb.cpu.cycles,
            "instructions": game.mb.cpu.retired_instructions,
            "closed": session.closed,
        }), file=sys.stderr, flush=True)
"""


def _authored_paths(tmp_path):
    """Original executable bytes, matching the reference routing ROM's program."""
    boot = bytearray(256)
    boot[:6] = bytes([0x31, 0x00, 0xD0, 0xC3, 0xFC, 0x00])
    boot[252:] = bytes([0x3E, 0x01, 0xE0, 0x50])
    cartridge = bytearray(0x8000)
    cartridge[0x100:0x103] = bytes([0xC3, 0x50, 0x01])
    cartridge[0x134:0x13B] = b"MCPTEST"
    # DI; LCD on; XOR A / LDH ($02),A / JR: ROM-owned serial disable loop.
    cartridge[0x150:0x15A] = bytes([0xF3, 0x3E, 0x91, 0xE0, 0x40, 0xAF, 0xE0, 0x02, 0x18, 0xFB])
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 0xFF
    rom_path, boot_path = tmp_path / "authored.gb", tmp_path / "authored.boot"
    rom_path.write_bytes(cartridge)
    boot_path.write_bytes(boot)
    return rom_path, boot_path


class ProcessClient:
    """Minimal JSON-RPC MCP client with bounded pipes, requests, and teardown.

    One outstanding request per process suffices: peer requests run concurrently.
    stdout is strictly protocol JSON; stderr is continuously drained to a ring.
    """

    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.log = bytearray()
        self.log_bytes = 0
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self.forced = False

    async def _drain_stderr(self):
        while chunk := await self.process.stderr.read(4096):
            self.log_bytes += len(chunk)
            self.log.extend(chunk)
            if len(self.log) > LOG_CAP:
                del self.log[:-LOG_CAP]

    def diagnostics(self):
        return f"pid={self.process.pid} rc={self.process.returncode}\n{self.log.decode(errors='replace')}"

    async def send(self, message):
        async with asyncio.timeout(CALL_BOUND):
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()

    async def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        try:
            async with asyncio.timeout(CALL_BOUND):
                await self.send(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
                while True:
                    line = await self.process.stdout.readline()
                    assert line, f"unexpected server EOF: {self.diagnostics()}"
                    text = line.decode("utf-8", "replace").strip()
                    if not text or not text.startswith("{"):
                        # Ignore blank or non-protocol diagnostic lines; JSON-RPC
                        # is line-delimited and only objects carry responses.
                        continue
                    response = json.loads(text)
                    assert response["jsonrpc"] == "2.0", response
                    if "id" not in response:
                        assert "method" in response, response
                        continue
                    assert response["id"] == request_id, response
                    assert "error" not in response, response
                    return response["result"]
        except TimeoutError as exc:
            raise AssertionError(
                f"{method} {params!r} exceeded {CALL_BOUND}s: {self.diagnostics()}"
            ) from exc

    async def initialize(self):
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "authored-timed-integration", "version": "1"},
            },
        )
        assert result["protocolVersion"]
        assert "tools" in result["capabilities"]
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = await self.request("tools/list", {})
        assert {
            "step",
            "press",
            "link_listen",
            "link_connect",
            "link_status",
            "link_disconnect",
        } <= {tool["name"] for tool in tools["tools"]}

    async def tool(self, name, arguments=None, *, error=False, validation_error=False):
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        assert result.get("isError", False) is error, result
        assert result["content"] and result["content"][0]["type"] == "text", result
        if validation_error:
            assert error
            text = result["content"][0]["text"]
            assert text.startswith("Input validation error:"), result
            return text
        payload = json.loads(result["content"][0]["text"])
        assert isinstance(payload, dict), payload
        return payload

    async def eof(self):
        """Success requires spontaneous zero exit on stdin EOF, never SDK killing."""
        self.process.stdin.close()
        try:
            async with asyncio.timeout(EXIT_BOUND):
                await self.process.stdin.wait_closed()
                await self.process.wait()
                await self.stderr_task
        except TimeoutError as exc:
            raise AssertionError(f"stdin EOF did not exit: {self.diagnostics()}") from exc
        assert self.process.returncode == 0, self.diagnostics()
        assert not self.forced
        reports = [
            line[len(REPORT_PREFIX) :]
            for line in self.log.decode(errors="replace").splitlines()
            if line.startswith(REPORT_PREFIX)
        ]
        assert len(reports) == 1, self.diagnostics()
        report = json.loads(reports[0])
        assert report["closed"] is True, report
        assert report["frames"] == report["tick"], report
        return report

    async def cleanup(self):
        """Failure cleanup is bounded and cannot count as clean EOF evidence."""
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), EXIT_BOUND)
            except TimeoutError:
                self.forced = True
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3.0)
                except TimeoutError:
                    self.process.kill()
                    await asyncio.wait_for(self.process.wait(), 3.0)
        try:
            await asyncio.wait_for(self.stderr_task, 3.0)
        finally:
            if not self.stderr_task.done():
                self.stderr_task.cancel()


@asynccontextmanager
async def _pair(tmp_path):
    rom, boot = _authored_paths(tmp_path)
    env = {key: value for key, value in os.environ.items() if not key.startswith("POKERED_")}
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT / "vendor/pyboy-src")))
    env["PYTHONUNBUFFERED"] = "1"
    clients = []
    try:
        for version in ("red", "blue"):
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-u",
                    "-c",
                    CHILD,
                    str(rom),
                    str(boot),
                    version,
                    cwd=ROOT,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=LOG_CAP,
                ),
                CALL_BOUND,
            )
            clients.append(ProcessClient(process))
        async with asyncio.timeout(90.0):
            await _both(*(client.initialize() for client in clients))
            yield clients
    except BaseException as exc:
        for client in clients:
            exc.add_note(client.diagnostics())
        raise
    finally:
        await _both(*(client.cleanup() for client in clients))


async def _both(*calls):
    """Drain both bounded calls before raising, retaining both peer failures."""
    results = await asyncio.gather(*calls, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise BaseExceptionGroup("timed MCP subprocess failures", failures)
    return results


async def _connected(left, right):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    listen = await left.tool(
        "link_listen",
        {
            "host": "127.0.0.1",
            "port": port,
            "rom_version": "red",
            "timeout_s": 10,
        },
    )
    assert listen["mode"] == "listening" and listen["transport"] == "timed", listen
    connect = await right.tool(
        "link_connect",
        {
            "host": "127.0.0.1",
            "port": port,
            "rom_version": "blue",
            "peer_rom_version": "red",
            "timeout_s": 10,
        },
    )
    assert connect["mode"] == "connected" and connect["transport"] == "timed", connect
    for client in (left, right):
        status = await _mode(client, "connected")
        assert status["epoch"], status
        assert status["peer_rom_version"] == ("blue" if client is left else "red"), status


async def _mode(client, expected):
    status = None
    try:
        async with asyncio.timeout(CALL_BOUND):
            while True:
                status = await client.tool("link_status")
                assert status["transport"] == "timed", status
                assert status["remote_mode"] == status["mode"] == status["state"], status
                if status["mode"] == expected:
                    return status
                await asyncio.sleep(0.02)
    except TimeoutError as exc:
        raise AssertionError(f"mode never reached {expected}: {status!r}") from exc


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["disconnect", "peer_eof"])
async def test_authored_timed_stdio_pair_frames_and_cleanup(tmp_path, termination):
    async with _pair(tmp_path) as (left, right):
        await _connected(left, right)
        invalid = await left.tool("step", {"count": 0}, error=True, validation_error=True)
        assert "minimum of 1" in invalid, invalid
        pressed = await _both(
            *(client.tool("press", {"button": "a", "duration": 1}) for client in (left, right))
        )
        assert pressed == [{"ok": True}, {"ok": True}]
        # Idle/status and button admission cannot schedule CPU execution.
        await asyncio.sleep(0.05)
        for client in (left, right):
            status = await _mode(client, "connected")
            assert type(status["tick"]) is int and status["tick"] == 1, status
        # Both emulators need peer credit. Never run these calls sequentially.
        steps = await _both(*(client.tool("step", {"count": 1}) for client in (left, right)))
        assert steps == [{"tick": 2}, {"tick": 2}], steps
        steps = await _both(*(client.tool("step", {"count": 1}) for client in (left, right)))
        assert steps == [{"tick": 3}, {"tick": 3}], steps
        for client in (left, right):
            status = await _mode(client, "connected")
            assert type(status["tick"]) is int and status["tick"] == 3, status
            assert status["primary_tick"] == status["current_tick"] == 3, status
            assert isinstance(status["accounting"], dict), status
            resource = await client.request("resources/read", {"uri": "pokered://link-status"})
            snapshot = json.loads(resource["contents"][0]["text"])
            assert snapshot == status
        if termination == "disconnect":
            disconnected = await _both(
                *(client.tool("link_disconnect") for client in (left, right))
            )
            for status in disconnected:
                assert status["mode"] == "idle" and status["transport"] == "timed", status
            reports = await _both(left.eof(), right.eof())
        else:
            right_report = await right.eof()
            await _mode(left, "idle")
            status = await left.tool("link_disconnect")
            assert status["mode"] == "idle" and status["transport"] == "timed", status
            reports = [await left.eof(), right_report]
        for report in reports:
            assert report["initial"]["frames"] == 1
            assert report["frames"] == 3, report
            assert report["cycles"] > report["initial"]["cycles"], report
            assert report["instructions"] > report["initial"]["instructions"], report


@pytest.mark.asyncio
async def test_authored_timed_stdio_expected_peer_mismatch(tmp_path):
    """Reject a real peer's HELLO identity through the public structured error."""
    async with _pair(tmp_path) as (left, right):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        listening = await left.tool(
            "link_listen",
            {
                "host": "127.0.0.1",
                "port": port,
                "rom_version": "red",
                "timeout_s": 10,
            },
        )
        assert listening["remote_mode"] == "listening", listening
        rejected = await right.tool(
            "link_connect",
            {
                "host": "127.0.0.1",
                "port": port,
                "rom_version": "blue",
                "peer_rom_version": "yellow",
                "timeout_s": 10,
            },
            error=True,
        )
        assert rejected["ok"] is False, rejected
        assert rejected["error"]["code"] == "timed_peer_mismatch", rejected
        statuses = await _both(*(client.tool("link_disconnect") for client in (left, right)))
        assert all(status["remote_mode"] == "idle" for status in statuses), statuses
        reports = await _both(left.eof(), right.eof())
        assert all(report["frames"] == report["initial"]["frames"] == 1 for report in reports)
