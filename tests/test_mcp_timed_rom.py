"""BYO canonical ROM smoke through the actual timed MCP module entry point.

Nine ordered orientations, three public frames each; no gameplay qualification.
The invoking interpreter/runtime is inherited, including native selection. No
stdout filtering, emulator hooks, RAM preparation, or child wrapper is used.

Historical Q256/rearm32/cap16/lateness32/op5 Blue-color/Yellow runs failed
at frame 1 in source and native. The selected aligned diagnostic profile
matches main's 240-frame probe; it neither resolves that history nor qualifies
commercial gameplay or the complete MCP matrix.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from scripts.probe_timed_rom_pair import resolve_assets
from tests._rom_assets import fixture_path, rom_path, sym_path
from tests.test_mcp_timed_stdio import EXIT_BOUND, ProcessClient, _both, _mode

ROOT = Path(__file__).resolve().parents[1]
PIPE_CAP = 2 * 1024 * 1024
PAIR_BOUND = 180.0
FAMILIES = ("red_color", "blue_color", "yellow")
NARROW_PROFILE = {
    "rearm_budget": 32,
    "rearm_instruction_cap": 16,
    "max_edge_lateness": 32,
    "quantum_cycles": 256,
    "operation_timeout": 5.0,
    "max_wait_attempts": 64,
    "inbound_capacity": 256,
    "queue_capacity": 16,
    "request_timeout": 10.0,
    "lock_timeout": 2.0,
    "close_timeout": 5.0,
}
# Explicit alignment with main's 240-frame probe, not a qualified default.
ALIGNED_DIAGNOSTIC_PROFILE = {
    **NARROW_PROFILE,
    "rearm_budget": 4096,
    "rearm_instruction_cap": 1024,
    "max_edge_lateness": 4096,
}
POLICY = ALIGNED_DIAGNOSTIC_PROFILE


def _assets(version):
    family = version.split("_")[0]
    paths = (
        rom_path(family, color=family != "yellow", project_root=ROOT),
        sym_path(family, ROOT),
        fixture_path(family, project_root=ROOT),
    )
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        pytest.skip("missing BYO assets: " + ", ".join(missing))
    # Present but unreadable, malformed, or mismatched inputs must fail.
    return resolve_assets(version, ROOT)


class RomClient(ProcessClient):
    """Reuse only bounded wire operations; actual main emits no child report."""

    last_step_result = None

    async def request(self, method, params):
        is_step = method == "tools/call" and params.get("name") == "step"
        if is_step:
            self.last_step_result = None
        try:
            result = await super().request(method, params)
        except AssertionError as exc:
            if isinstance(exc.__cause__, TimeoutError):
                data = params.get("arguments", {}).get("data")
                if isinstance(data, str) and data:
                    # Preserve the same exception, cause and request deadline;
                    # the shared driver's params repr must not expose state.
                    exc.args = tuple(
                        arg.replace(data, "<redacted-state-data>") if isinstance(arg, str) else arg
                        for arg in exc.args
                    )
            raise
        if is_step:
            # Retain the complete bounded RPC result before tool() checks
            # isError. Never retain save-state base64 in failure diagnostics.
            self.last_step_result = result
        return result

    def group_alive(self):
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def signal_group(self, sig):
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass

    async def eof(self):
        self.process.stdin.close()
        async with asyncio.timeout(EXIT_BOUND):
            await self.process.stdin.wait_closed()
            # No outstanding RPCs: any trailing stdout is a protocol violation.
            trailing = await self.process.stdout.read(PIPE_CAP + 1)
            assert trailing == b"", f"unexpected stdout: {trailing[:1024]!r}"
            await self.process.wait()
            await self.stderr_task
        assert self.process.returncode == 0, self.diagnostics()
        assert not self.forced, self.diagnostics()
        assert not self.group_alive(), self.diagnostics()

    async def cleanup(self):
        # Failure cleanup is never evidence of spontaneous EOF success. Kill the
        # whole session, including descendants even if the leader already exited.
        if self.group_alive():
            self.forced = True
            self.signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), 3.0)
        except TimeoutError:
            self.signal_group(signal.SIGKILL)
            await asyncio.wait_for(self.process.wait(), 3.0)
        finally:
            if self.group_alive():
                self.signal_group(signal.SIGKILL)
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.stderr_task, 3.0)
            finally:
                if not self.stderr_task.done():
                    self.stderr_task.cancel()
                    await asyncio.gather(self.stderr_task, return_exceptions=True)
        async with asyncio.timeout(3.0):
            while self.group_alive():
                await asyncio.sleep(0.02)


@asynccontextmanager
async def _pair(tmp_path, assets):
    clients = []
    original_failure = None
    try:
        for index, asset in enumerate(assets):
            directory = tmp_path / str(index)
            directory.mkdir()
            copied = {}
            for name in ("rom", "sym"):
                copied[name] = directory / asset[name].name
                shutil.copyfile(asset[name], copied[name])
            env = {
                key: value for key, value in os.environ.items() if not key.startswith("POKERED_")
            }
            env.update(
                {
                    "POKERED_ROM_PATH": str(copied["rom"]),
                    "POKERED_ROM_VERSION": asset["family"],
                    "POKERED_SYM_PATH": str(copied["sym"]),
                    "POKERED_ROM_SHA1": asset["pins"]["expected_rom_sha1"],
                    "POKERED_SYM_SHA1": asset["pins"]["expected_symbol_sha1"],
                    "POKERED_VERSIONS_PATH": str(ROOT / "VERSIONS.md"),
                    "POKERED_LINK_TRANSPORT": "timed",
                    "PYTHONUNBUFFERED": "1",
                }
            )
            env.update(
                {f"POKERED_TIMED_{key.upper()}": str(value) for key, value in POLICY.items()}
            )
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "pokered_harness.mcp_server",
                    cwd=ROOT,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=PIPE_CAP,
                    start_new_session=True,
                ),
                timeout=20.0,
            )
            clients.append(RomClient(process))
        yield clients
    except BaseException as exc:
        original_failure = exc
        raise
    finally:
        try:
            await _both(*(client.cleanup() for client in clients))
        except BaseException as cleanup_error:
            if original_failure is None:
                raise
            original_failure.add_note(f"TIMED_ROM_FALLBACK_CLEANUP_ERROR {cleanup_error!r}")


async def _prelink(client, asset):
    await client.initialize()
    listed = await client.request("tools/list", {})
    assert {"load_state", "save_state"} <= {row["name"] for row in listed["tools"]}
    resources = await client.request("resources/list", {})
    assert {"pokered://game-state", "pokered://link-status"} <= {
        row["uri"] for row in resources["resources"]
    }
    loaded = await client.tool(
        "load_state",
        {
            "data": base64.b64encode(asset["state"]).decode("ascii"),
        },
    )
    assert loaded == {"ok": True}
    before = await client.request("resources/read", {"uri": "pokered://game-state"})
    assert isinstance(json.loads(before["contents"][0]["text"]), dict)
    saved = await client.tool("save_state")
    assert set(saved) == {"data"} and isinstance(saved["data"], str), saved
    assert base64.b64decode(saved["data"], validate=True)
    assert await client.tool("press", {"button": "a", "duration": 1}) == {"ok": True}
    assert await client.tool("release", {"button": "a"}) == {"ok": True}
    # load_state restores the board, not queued input. Consume immediate and
    # delayed events through ordinary execution before restoring the fixture.
    drained = await client.tool("step", {"count": 2})
    assert set(drained) == {"tick"} and type(drained["tick"]) is int, drained
    assert await client.tool("load_state", saved) == {"ok": True}
    assert await client.request("resources/read", {"uri": "pokered://game-state"}) == before
    assert await client.tool("save_state") == saved


def test_rom_client_load_state_timeout_redacts_data(monkeypatch):
    """Asset-free immediate timeout through the real inherited request wrapper."""
    client = object.__new__(RomClient)
    client.sequence = 0
    timeout = TimeoutError("deliberate immediate transport timeout")

    async def fail_send(_message):
        raise timeout

    monkeypatch.setattr(client, "send", fail_send)
    monkeypatch.setattr(client, "diagnostics", lambda: "asset-free fake transport")
    data = base64.b64encode(b"private state bytes for redaction regression").decode("ascii")
    with pytest.raises(AssertionError) as caught:
        asyncio.run(
            client.request(
                "tools/call",
                {
                    "name": "load_state",
                    "arguments": {"data": data},
                },
            )
        )
    assert caught.value.__cause__ is timeout
    rendered = "".join(traceback.format_exception(caught.value))
    assert data not in rendered
    assert "<redacted-state-data>" in rendered
    assert "load_state" in rendered and "exceeded 20.0s" in rendered


async def _step_frame(clients, versions, frame, previous):
    try:
        return await _both(*(client.tool("step", {"count": 1}) for client in clients))
    except Exception as exc:
        # Both step requests have settled before these read-only cached calls.
        # Each request retains the driver's deadline and pipe bounds. A failed
        # observation is evidence too; it must not replace the original failure.
        observations = await asyncio.gather(
            *(client.tool("link_status") for client in clients), return_exceptions=True
        )
        for index, (client, observation) in enumerate(zip(clients, observations)):
            record = {
                "phase": "paired_step",
                "frame": frame,
                "requested_count": 1,
                "role": "listener" if index == 0 else "connector",
                "version": versions[index],
                "policy": POLICY,
                "structured_step_result": client.last_step_result,
                "previous_status": previous[index],
                "cached_status_after_failure": (
                    {"observation_error": repr(observation)}
                    if isinstance(observation, BaseException)
                    else observation
                ),
                "process": client.diagnostics(),
            }
            exc.add_note("TIMED_ROM_FAILURE " + json.dumps(record, sort_keys=True))

        # Only settled, synchronized RPC streams may receive another request.
        # EOF is attempted even if public disconnect fails. These observations
        # never convert the original strict frame failure into a passing test.
        async def disconnect(client, observation):
            if client.last_step_result is None or isinstance(observation, BaseException):
                return {"not_attempted": "RPC stream settlement not confirmed"}
            result = await client.tool("link_disconnect")
            assert result["mode"] == "idle" and result["transport"] == "timed", result
            return result

        disconnected = await asyncio.gather(
            *(
                disconnect(client, observation)
                for client, observation in zip(clients, observations)
            ),
            return_exceptions=True,
        )
        exits = await asyncio.gather(*(client.eof() for client in clients), return_exceptions=True)
        for index, (client, result, exited) in enumerate(zip(clients, disconnected, exits)):
            exc.add_note(
                "TIMED_ROM_FAILURE_CLEANUP "
                + json.dumps(
                    {
                        "phase": "paired_step_failure_cleanup",
                        "frame": frame,
                        "role": "listener" if index == 0 else "connector",
                        "disconnect": repr(result) if isinstance(result, BaseException) else result,
                        "eof": repr(exited)
                        if isinstance(exited, BaseException)
                        else "spontaneous_zero_exit",
                        "returncode": client.process.returncode,
                        "forced": client.forced,
                        "group_alive": client.group_alive(),
                    },
                    sort_keys=True,
                )
            )
        raise


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listener", "connector"),
    [pytest.param(a, b, id=f"{a}-listen-{b}-connect") for a in FAMILIES for b in FAMILIES],
)
async def test_timed_rom_stdio_pair(tmp_path, listener, connector):
    assets = [_assets(listener), _assets(connector)]
    async with _pair(tmp_path, assets) as clients:
        async with asyncio.timeout(PAIR_BOUND):
            await _both(*(_prelink(client, asset) for client, asset in zip(clients, assets)))
            left, right = clients
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            listening = await left.tool(
                "link_listen",
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "rom_version": assets[0]["family"],
                    "peer_rom_version": assets[1]["family"],
                    "timeout_s": 10,
                },
            )
            assert listening["mode"] == "listening"
            assert listening["transport"] == "timed"
            connected = await right.tool(
                "link_connect",
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "rom_version": assets[1]["family"],
                    "peer_rom_version": assets[0]["family"],
                    "timeout_s": 10,
                },
            )
            assert connected["mode"] == "connected"
            assert connected["transport"] == "timed"
            baseline = await _both(*(_mode(client, "connected") for client in clients))
            previous = baseline
            for frame in range(1, 4):
                steps = await _step_frame(clients, (listener, connector), frame, previous)
                statuses = await _both(*(_mode(client, "connected") for client in clients))
                for index, (client, step, status) in enumerate(zip(clients, steps, statuses)):
                    tick = baseline[index]["tick"] + frame
                    assert step == {"tick": tick}, step
                    assert (
                        status["tick"] == status["primary_tick"] == status["current_tick"] == tick
                    )
                    assert status["peer_rom_version"] == assets[1 - index]["family"]
                    accounting = status["accounting"]
                    assert (
                        accounting["local_half_cycles"]
                        > previous[index]["accounting"]["local_half_cycles"]
                    )
                    assert (
                        accounting["raw_cpu_clock"] > previous[index]["accounting"]["raw_cpu_clock"]
                    )
                    assert not accounting["closed"] and not accounting["cancelled"]
                    assert accounting["terminal_reason"] is None
                    # With no action in flight, consecutive cached reads must
                    # not enter native execution or refresh its observation.
                    assert type(status["native_observed_at"]) in (int, float)
                    again = await client.tool("link_status")
                    assert again["native_observed_at"] == status["native_observed_at"]
                    assert again["accounting"] == accounting
                    assert again["tick"] == tick
                    resource = await client.request(
                        "resources/read", {"uri": "pokered://link-status"}
                    )
                    assert json.loads(resource["contents"][0]["text"]) == status
                previous = statuses
            disconnected = await _both(*(client.tool("link_disconnect") for client in clients))
            assert all(
                row["mode"] == "idle" and row["transport"] == "timed" for row in disconnected
            )
            await _both(*(client.eof() for client in clients))
            report = json.dumps(
                {
                    "profile": "aligned_diagnostic_4096_1024_4096",
                    "policy": POLICY,
                    "listener": listener,
                    "connector": connector,
                    "completed_frames": 3,
                    "prelink_press_release_restored": True,
                    "prelink_input_drain_frames": 2,
                    "baseline_statuses": baseline,
                    "final_statuses": previous,
                    "processes": [
                        {
                            "pid": client.process.pid,
                            "returncode": client.process.returncode,
                            "forced": client.forced,
                            "group_alive": client.group_alive(),
                        }
                        for client in clients
                    ],
                },
                sort_keys=True,
            )
            assert len(report.encode("utf-8")) <= 64 * 1024, "smoke report exceeds bound"
            print("TIMED_ROM_SMOKE " + report, flush=True)
