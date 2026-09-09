"""Remote MCP admission fails before I/O on unsupported serial runtimes."""

import asyncio
from unittest.mock import Mock

import mcp.types as mcp_types
import pytest

from pokered_harness import mcp_server
from pokered_harness.mcp_server import LinkState, McpHarnessError, build_server, dispatch_tool
from tests.test_mcp_local_runtime_contract import _break_contract
from tests.test_mcp_server import _endpoint_session


def _arguments(command):
    return {"port": 34567, **({"host": "127.0.0.1"} if command == "link_connect" else {})}


def _forbid_mutation(monkeypatch, session):
    forbidden = Mock(side_effect=AssertionError("unsupported runtime mutated state or opened I/O"))
    for name in ("_bind_listener", "NetworkBackend", "TcpSerialLink", "RemoteLinkEndpoint", "PyBoyLinkSession"):
        replacement = Mock(side_effect=forbidden)
        replacement.connect = forbidden
        replacement.as_connector = forbidden
        replacement.as_listener = forbidden
        monkeypatch.setattr(mcp_server, name, replacement)
    monkeypatch.setattr(session, "serial_hook", forbidden)
    monkeypatch.setattr(session, "register_hook", forbidden)
    monkeypatch.setattr(session._pyboy, "hook_register", forbidden)
    monkeypatch.setattr(type(session._pyboy.memory), "__setitem__", forbidden)
    return forbidden


@pytest.mark.parametrize("command", ["link_listen", "link_connect"])
@pytest.mark.parametrize("defect", [
    "motherboard", "serial", "backend", "apply_external_edge", "peek_out_bit",
    "noncallable_apply_external_edge", "noncallable_peek_out_bit",
])
def test_remote_rejects_unsupported_runtime_without_mutation(monkeypatch, command, defect):
    session, pb = _endpoint_session()
    _break_contract(session, defect)
    link = LinkState()
    before = vars(link).copy()
    memory = dict(pb.memory._m)
    forbidden = _forbid_mutation(monkeypatch, session)
    with pytest.raises(McpHarnessError) as raised:
        dispatch_tool(session, command, _arguments(command), link=link)
    assert raised.value.code == "unsupported_runtime"
    forbidden.assert_not_called()
    assert vars(link) == before
    assert dict(pb.memory._m) == memory
    assert not session._serial_hooks and not pb._hooks


@pytest.mark.parametrize("command", ["link_listen", "link_connect"])
def test_remote_handler_returns_structured_runtime_error(monkeypatch, command):
    session, _ = _endpoint_session()
    _break_contract(session, "serial")
    forbidden = _forbid_mutation(monkeypatch, session)
    server = build_server(session, link=LinkState())
    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(params=mcp_types.CallToolRequestParams(
        name=command, arguments=_arguments(command)))
    result = asyncio.run(handler(request)).root
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "unsupported_runtime"
    forbidden.assert_not_called()


@pytest.mark.parametrize("command", ["link_listen", "link_connect"])
@pytest.mark.parametrize("override", [
    {"port": 0}, {"host": "example.com"}, {"rom_version": "green"}, {"timeout_s": -1},
])
def test_argument_validation_precedes_runtime_guard(monkeypatch, command, override):
    session, _ = _endpoint_session()
    _break_contract(session, "serial")
    forbidden = _forbid_mutation(monkeypatch, session)
    with pytest.raises(ValueError) as raised:
        dispatch_tool(session, command, {**_arguments(command), **override}, link=LinkState())
    assert getattr(raised.value, "code", None) != "unsupported_runtime"
    forbidden.assert_not_called()


def test_supported_serial_runtime_never_constructs_semantic_adapter(monkeypatch):
    from tests.test_mcp_server import test_link_listen_then_connect_updates_status

    forbidden = Mock(side_effect=AssertionError("semantic adapter selected"))
    forbidden.connect = forbidden
    forbidden.as_listener = forbidden
    forbidden.as_connector = forbidden
    monkeypatch.setattr(mcp_server, "TcpSerialLink", forbidden)
    monkeypatch.setattr(mcp_server, "RemoteLinkEndpoint", forbidden)
    test_link_listen_then_connect_updates_status()
    forbidden.assert_not_called()
