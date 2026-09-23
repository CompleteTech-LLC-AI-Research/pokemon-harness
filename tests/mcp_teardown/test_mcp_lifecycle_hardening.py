"""Focused MCP teardown, cancellation, and remote-EOF regressions."""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import mcp.types as mcp_types
import pytest

from pokered_harness.events import EventBus
from pokered_harness.mcp_server import (
    LinkState,
    McpHarnessError,
    _close_sessions_independently,
    build_server,
    dispatch_tool,
    serve_stdio,
)
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy


def _session() -> Session:
    return Session(
        pyboy=FakePyBoy(DictMemory()),
        symbols=load_sym_text("00:216F Serial_ExchangeBytes\n"),
        event_bus=EventBus(),
    )


def _call_tool_handler(server):
    return server.request_handlers[mcp_types.CallToolRequest]


class _CloseProbe:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[float] = []

    def close(self, *, timeout_s: float) -> None:
        self.calls.append(timeout_s)
        if self.error is not None:
            raise self.error


def test_owned_sessions_close_independently() -> None:
    peer = _CloseProbe(error=RuntimeError("peer stop failed"))
    primary = _CloseProbe()

    errors = _close_sessions_independently(peer, primary)  # type: ignore[arg-type]

    assert [role for role, _error in errors] == ["peer"]
    assert len(peer.calls) == 1
    assert len(primary.calls) == 1
    assert peer.calls[0] == primary.calls[0]


class _ClosedRemote:
    connected = False
    _reader_exc = EOFError("peer sent EOF")

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _EndpointProbe:
    def __init__(self) -> None:
        self.step_calls = 0

    def step(self, _count: int, *, render: bool = False) -> None:
        del render
        self.step_calls += 1


def test_link_step_reconciles_eof_before_using_remote_endpoint() -> None:
    session = _session()
    remote = _ClosedRemote()
    endpoint = _EndpointProbe()
    link = LinkState()
    link.remote_mode = "connected"
    link.remote_link = remote  # type: ignore[assignment]
    link.remote_endpoint = endpoint  # type: ignore[assignment]

    with pytest.raises(McpHarnessError) as exc_info:
        dispatch_tool(session, "link_step", {"count": 1}, link=link)

    assert exc_info.value.code == "remote_not_connected"
    assert endpoint.step_calls == 0
    assert remote.close_calls == 1
    assert link.remote_mode == "idle"


def test_every_mcp_session_lock_call_has_an_explicit_deadline() -> None:
    source_dir = Path(__file__).parents[2] / "src/pokered_harness"
    # ``mcp_server`` was split into focused modules (issue #125); the same
    # invariant must hold across the facade and every split module.
    source_paths = sorted(source_dir.glob("mcp_server*.py"))
    assert source_paths
    lock_calls = [
        node
        for source_path in source_paths
        for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "locked"
    ]

    assert lock_calls
    assert all(
        any(keyword.arg == "timeout_s" for keyword in node.keywords)
        for node in lock_calls
    )


def test_cancelled_mcp_request_has_a_bounded_cleanup_and_worker_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_entered = threading.Event()
    worker_release = threading.Event()
    worker_done = threading.Event()
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    cleanup_done = threading.Event()

    def blocked_dispatch(*_args, **_kwargs):
        worker_entered.set()
        assert worker_release.wait(timeout=2.0)
        worker_done.set()
        return {"tick": 1}

    def blocked_cleanup(*_args, **_kwargs):
        cleanup_entered.set()
        assert cleanup_release.wait(timeout=2.0)
        cleanup_done.set()

    monkeypatch.setattr(
        "pokered_harness.mcp_server.dispatch_tool", blocked_dispatch
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._disconnect_remote", blocked_cleanup
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.05
    )

    server = build_server(object())  # type: ignore[arg-type]
    handler = _call_tool_handler(server)
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="step", arguments={"count": 1}
        )
    )

    async def scenario() -> None:
        task = asyncio.create_task(handler(request))
        deadline = asyncio.get_running_loop().time() + 1.0
        while not worker_entered.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 0.5

        # The bounded cancellation path does not cancel thread-backed work;
        # release both owners before asyncio.run shuts down its executor.
        cleanup_release.set()
        worker_release.set()
        deadline = asyncio.get_running_loop().time() + 1.0
        while not (cleanup_done.is_set() and worker_done.is_set()):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert cleanup_entered.is_set()
    assert cleanup_done.is_set()
    assert worker_done.is_set()


def test_cancelled_mcp_resource_has_a_bounded_cleanup_and_worker_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resource EOF/cancellation must not orphan its executor thread."""
    worker_entered = threading.Event()
    worker_release = threading.Event()
    worker_done = threading.Event()
    cleanup_called = threading.Event()

    def blocked_resource(*_args, **_kwargs):
        worker_entered.set()
        assert worker_release.wait(timeout=2.0)
        worker_done.set()
        return "{}"

    def cancel_remote(*_args, **_kwargs):
        cleanup_called.set()
        worker_release.set()

    monkeypatch.setattr(
        "pokered_harness.mcp_server.read_resource", blocked_resource
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._disconnect_remote", cancel_remote
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.05
    )

    server = build_server(object())  # type: ignore[arg-type]
    handler = server.request_handlers[mcp_types.ReadResourceRequest]
    request = mcp_types.ReadResourceRequest(
        params=mcp_types.ReadResourceRequestParams(uri="pokered://game-state")
    )

    async def scenario() -> None:
        task = asyncio.create_task(handler(request))
        deadline = asyncio.get_running_loop().time() + 1.0
        while not worker_entered.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cleanup_called.is_set()
    assert worker_done.is_set()


def test_stdio_shutdown_bounds_unpair_operation_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF cleanup must not wait forever for an in-flight link operation."""
    session = _session()
    link = LinkState()
    operation_entered = threading.Event()
    operation_release = threading.Event()

    def hold_operation() -> None:
        with link.operation():
            operation_entered.set()
            operation_release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_operation, name="test-link-operation")
    holder.start()
    assert operation_entered.wait(timeout=1.0)

    class _FinishedServer:
        def create_initialization_options(self):
            return None

        async def run(self, *_args, **_kwargs):
            return None

    @asynccontextmanager
    async def fake_stdio_server():
        yield object(), object()

    monkeypatch.setattr(
        "pokered_harness.mcp_server.build_server",
        lambda *_args, **_kwargs: _FinishedServer(),
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server.stdio_server", fake_stdio_server
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.05
    )

    started = time.monotonic()
    try:
        with pytest.raises(McpHarnessError, match="link operation") as exc_info:
            asyncio.run(serve_stdio(session, link=link))
        assert exc_info.value.code == "server_cleanup_failed"
        assert time.monotonic() - started < 0.5
    finally:
        operation_release.set()
        holder.join(timeout=1.0)
        session.close()

    assert not holder.is_alive()
