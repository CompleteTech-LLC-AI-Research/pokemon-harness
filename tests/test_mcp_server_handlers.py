from __future__ import annotations

import asyncio
import json
import threading
import time as _time
from contextlib import asynccontextmanager

import pytest
from mcp.shared.exceptions import McpError

from pokered_harness.mcp_server import (
    LinkState,
    McpHarnessError,
    build_server,
    dispatch_tool,
    read_resource,
    serve_stdio,
)
from tests._mcp_server_support import (
    _endpoint_session,
    _session,
)


def test_mcp_handler_returns_structured_client_error():
    """The actual low-level MCP handler preserves a stable error envelope."""
    import mcp.types as mcp_types

    s, _ = _endpoint_session()
    server = build_server(s)
    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(name="link_pair", arguments={})
    )
    response = asyncio.run(handler(request))
    payload = response.root.structuredContent
    assert response.root.isError is True
    assert payload is not None
    assert payload["error"]["code"] == "peer_not_configured"


@pytest.mark.parametrize(
    ("uri", "code"),
    [
        ("pokered://nope", "invalid_resource"),
        ("pokered://peer-game-state", "peer_not_configured"),
    ],
)
def test_mcp_resource_handler_returns_structured_client_error(uri, code):
    """Resource failures use MCP ErrorData with the stable harness code."""
    import mcp.types as mcp_types

    s, _ = _endpoint_session()
    server = build_server(s)
    handler = server.request_handlers[mcp_types.ReadResourceRequest]
    request = mcp_types.ReadResourceRequest(params=mcp_types.ReadResourceRequestParams(uri=uri))

    with pytest.raises(McpError) as exc_info:
        asyncio.run(handler(request))

    error = exc_info.value.error
    assert error.code == 0
    assert error.data == {
        "ok": False,
        "error": {
            "code": code,
            "message": (
                "unknown resource: 'pokered://nope'"
                if code == "invalid_resource"
                else "peer session not configured"
            ),
            "type": "McpHarnessError",
        },
    }


def test_link_status_resource_returns_same_shape_as_tool():
    """The `pokered://link-status` resource is intended as a pollable
    snapshot for clients that don't want to round-trip via a tool call.
    Its JSON must match the tool's output exactly."""
    s, _ = _endpoint_session()
    link = LinkState()
    tool_payload = dispatch_tool(s, "link_status", {}, link=link)
    resource_payload = json.loads(read_resource(s, "pokered://link-status", link=link))
    assert resource_payload == tool_payload
    assert resource_payload["remote_mode"] == "idle"


def test_list_tools_reflects_peer_configuration_and_advertises_listen_timeout():
    import mcp.types as mcp_types

    s, _, _ = _session()
    server = build_server(s)
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    response = asyncio.run(handler(mcp_types.ListToolsRequest()))
    names = {tool.name for tool in response.root.tools}
    assert {"step", "save_state", "load_state", "link_status", "link_listen"} <= names
    assert "link_pair" not in names
    listen = next(tool for tool in response.root.tools if tool.name == "link_listen")
    assert "timeout_s" in listen.inputSchema["properties"]
    connect = next(tool for tool in response.root.tools if tool.name == "link_connect")
    assert connect.inputSchema["properties"]["timeout_s"]["exclusiveMinimum"] == 0
    assert connect.inputSchema["properties"]["timeout_s"]["maximum"] == 300.0

    s_peer, _, _ = _session()
    try:
        peer_server = build_server(s, link=LinkState(peer_session=s_peer))
        peer_handler = peer_server.request_handlers[mcp_types.ListToolsRequest]
        peer_response = asyncio.run(peer_handler(mcp_types.ListToolsRequest()))
        peer_names = {tool.name for tool in peer_response.root.tools}
        assert {
            "link_pair",
            "link_unpair",
            "link_step",
            "link_peer_press",
            "link_peer_hold",
            "link_peer_release",
        } <= peer_names
    finally:
        s_peer.close()


def test_load_state_handler_returns_structured_invalid_state_error():
    import mcp.types as mcp_types

    s, _, _ = _session()
    server = build_server(s)
    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(name="load_state", arguments={"data": "%%%"})
    )
    response = asyncio.run(handler(request))
    payload = response.root.structuredContent
    assert response.root.isError is True
    assert payload is not None
    assert payload["error"]["code"] == "invalid_state"


def test_mcp_handler_cancellation_cleans_and_joins_worker(monkeypatch):
    """Cancelling a request must not abandon a running to_thread worker."""
    s, _ = _endpoint_session()
    server = build_server(s)
    import mcp.types as mcp_types

    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(name="step", arguments={"count": 1})
    )
    worker_entered = threading.Event()
    worker_released = threading.Event()
    worker_done = threading.Event()
    cleanup_called = threading.Event()

    def blocked_dispatch(*_args, **_kwargs):
        worker_entered.set()
        assert worker_released.wait(timeout=2.0)
        worker_done.set()
        return {"tick": 1}

    def cancel_remote(*_args, **_kwargs):
        cleanup_called.set()
        worker_released.set()

    monkeypatch.setattr("pokered_harness.mcp_server.dispatch_tool", blocked_dispatch)
    monkeypatch.setattr("pokered_harness.mcp_server._disconnect_remote", cancel_remote)

    async def scenario() -> None:
        task = asyncio.create_task(handler(request))
        deadline = _time.monotonic() + 1.0
        while not worker_entered.is_set() and _time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker_entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cleanup_called.is_set()
    assert worker_done.is_set()


def test_serve_stdio_attempts_unpair_when_remote_cleanup_fails(monkeypatch):
    class FakeServer:
        def create_initialization_options(self):
            return None

        async def run(self, *_args, **_kwargs):
            return None

    @asynccontextmanager
    async def fake_stdio_server():
        yield object(), object()

    monkeypatch.setattr("pokered_harness.mcp_server.stdio_server", fake_stdio_server)
    monkeypatch.setattr(
        "pokered_harness.mcp_server.build_server",
        lambda *_args, **_kwargs: FakeServer(),
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._disconnect_remote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("remote teardown failed")),
    )
    s, _, _ = _session()
    unpair_calls: list[str] = []
    original_dispatch = dispatch_tool

    def spy_dispatch(session, name, arguments, link=None):
        if name == "link_unpair":
            unpair_calls.append(name)
        return original_dispatch(session, name, arguments, link=link)

    monkeypatch.setattr("pokered_harness.mcp_server.dispatch_tool", spy_dispatch)

    with pytest.raises(McpHarnessError, match="remote teardown failed") as exc_info:
        asyncio.run(serve_stdio(s))
    assert exc_info.value.code == "server_cleanup_failed"
    assert unpair_calls == ["link_unpair"]


def test_serve_stdio_preserves_server_error_when_cleanup_fails(monkeypatch):
    class FailingServer:
        def create_initialization_options(self):
            return None

        async def run(self, *_args, **_kwargs):
            raise RuntimeError("server failed")

    @asynccontextmanager
    async def fake_stdio_server():
        yield object(), object()

    monkeypatch.setattr("pokered_harness.mcp_server.stdio_server", fake_stdio_server)
    monkeypatch.setattr(
        "pokered_harness.mcp_server.build_server",
        lambda *_args, **_kwargs: FailingServer(),
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._disconnect_remote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    s, _, _ = _session()

    with pytest.raises(RuntimeError, match="server failed") as exc_info:
        asyncio.run(serve_stdio(s))
    assert any("MCP cleanup failed" in note for note in exc_info.value.__notes__)


def test_serve_stdio_routes_runtime_stdout_to_stderr(monkeypatch, capsys):
    class FakeServer:
        def create_initialization_options(self):
            return None

        async def run(self, *_args, **_kwargs):
            print("runtime diagnostic")

    @asynccontextmanager
    async def fake_stdio_server():
        yield object(), object()

    monkeypatch.setattr("pokered_harness.mcp_server.stdio_server", fake_stdio_server)
    monkeypatch.setattr(
        "pokered_harness.mcp_server.build_server",
        lambda *_args, **_kwargs: FakeServer(),
    )
    s, _, _ = _session()

    asyncio.run(serve_stdio(s))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runtime diagnostic" in captured.err
