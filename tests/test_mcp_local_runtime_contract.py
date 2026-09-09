"""Local MCP pairing must not implicitly select the semantic bridge.

These asset-free tests use real source/native serial cores where supported,
but fake emulator shells. They prove API selection, not ROM gameplay.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import mcp.types as mcp_types
import pyboy
import pytest

from pokered_harness import mcp_server
from pokered_harness.link.pair import LinkPair
from pokered_harness.link.serial_coordinator import CoordinatedBackend
from pokered_harness.link.serial_core import SerialCore
from pokered_harness.mcp_server import LinkState, McpHarnessError, build_server, dispatch_tool
from tests.test_link_pair import FakeFactory, _make_session
from tests.test_pyboy_link_session import _FakePyBoy


def _sessions():
    primary, primary_pb = _make_session()
    peer, peer_pb = _make_session()
    for pyboy in (primary_pb, peer_pb):
        # Preserve the Session fake's memory/hooks and network tick behavior,
        # while supplying the local provider's instruction-clock lifecycle.
        clock_shell = _FakePyBoy(serial=SerialCore())
        pyboy.mb = clock_shell.mb
        pyboy.events = clock_shell.events
        pyboy.frame_count = 0
        pyboy._handle_events = clock_shell._handle_events
        pyboy._post_handle_events = clock_shell._post_handle_events
        pyboy._handle_hooks = clock_shell._handle_hooks
    return primary, peer, LinkState(peer_session=peer, peer_version="blue")


@pytest.fixture
def actual_pyboy(tmp_path):
    """Boot an authored synthetic ROM through the installed PyBoy type."""
    data = bytearray(32768)
    data[0x100:0x103] = b"\xc3\x50\x01"
    data[0x143] = 0x80
    data[0x14D] = (-sum(data[0x134:0x14D]) - 25) & 0xFF
    path = tmp_path / "mcp-native-contract.gb"
    path.write_bytes(data)
    emulator = pyboy.PyBoy(str(path), window="null", sound_emulated=False)
    emulator.set_emulation_speed(0)
    emulator.memory[0xFF50] = 1
    emulator.register_file.PC = 0x150
    try:
        yield emulator
    finally:
        emulator.stop(save=False)


def _break_contract(session, defect):
    serial = SimpleNamespace(
        backend=object(), apply_external_edge=lambda _bit: False,
        peek_out_bit=lambda: 1,
    )
    session._pyboy.mb = SimpleNamespace(serial=serial)
    if defect == "motherboard":
        del session._pyboy.mb
    elif defect == "serial":
        session._pyboy.mb.serial = None
    elif defect.startswith("noncallable_"):
        setattr(serial, defect.removeprefix("noncallable_"), None)
    else:
        delattr(serial, defect)


def _forbid_pair_mutation(monkeypatch, primary, peer):
    forbidden = Mock(side_effect=AssertionError("pairing mutated an unsupported runtime"))
    monkeypatch.setattr(mcp_server, "LinkPair", forbidden)
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", forbidden)
    for session in (primary, peer):
        monkeypatch.setattr(session, "serial_hook", forbidden)
        monkeypatch.setattr(session, "register_hook", forbidden)
        monkeypatch.setattr(session._pyboy, "hook_register", forbidden)
        # Catch even a write that happens to preserve the existing value.
        monkeypatch.setattr(type(session._pyboy.memory), "__setitem__", forbidden)
    return forbidden


@pytest.mark.parametrize("side", ["primary", "peer"])
@pytest.mark.parametrize("defect", [
    "motherboard", "serial", "backend", "apply_external_edge", "peek_out_bit",
    "noncallable_apply_external_edge", "noncallable_peek_out_bit",
])
def test_implicit_pair_rejects_missing_runtime_without_mutation(monkeypatch, side, defect):
    primary, peer, link = _sessions()
    _break_contract(primary if side == "primary" else peer, defect)
    forbidden = _forbid_pair_mutation(monkeypatch, primary, peer)
    before = vars(link).copy()
    memories = [dict(s._pyboy.memory._m) for s in (primary, peer)]

    with pytest.raises(McpHarnessError) as raised:
        dispatch_tool(primary, "link_pair", {}, link=link)

    assert raised.value.code == "unsupported_runtime"
    forbidden.assert_not_called()
    assert vars(link) == before
    assert [dict(s._pyboy.memory._m) for s in (primary, peer)] == memories
    assert [s.current_tick() for s in (primary, peer)] == [0, 0]
    assert all(not s._pyboy._hooks and not s._serial_hooks for s in (primary, peer))


@pytest.mark.parametrize("side", ["primary", "peer"])
def test_mcp_handler_returns_structured_unsupported_runtime(monkeypatch, side):
    primary, peer, link = _sessions()
    _break_contract(primary if side == "primary" else peer, "serial")
    forbidden = _forbid_pair_mutation(monkeypatch, primary, peer)
    server = build_server(primary, link=link)
    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(name="link_pair", arguments={}),
    )

    result = asyncio.run(handler(request)).root

    assert result.isError is True
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["error"]["code"] == "unsupported_runtime"
    assert json.loads(result.content[0].text) == result.structuredContent
    assert link.pair is None and link.local_link_session is None
    forbidden.assert_not_called()


def test_explicit_injected_fake_pair_remains_supported(monkeypatch):
    primary, _ = _make_session()
    peer, _ = _make_session()
    factory = FakeFactory()
    pair = LinkPair(
        primary, peer, version_primary="red", version_peer="blue",
        bridge_factory=factory,
    )
    link = LinkState(peer_session=peer, peer_version="blue")
    link.pair = pair
    forbidden = Mock(side_effect=AssertionError("explicit pair was replaced"))
    monkeypatch.setattr(mcp_server, "LinkPair", forbidden)
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", forbidden)

    assert dispatch_tool(primary, "link_pair", {}, link=link)["paired"] is True
    assert link.pair is pair and pair.paired
    assert len(factory.calls) == 1
    assert dispatch_tool(primary, "link_status", {}, link=link)["link_backend"] == "semantic_compat"
    assert dispatch_tool(primary, "link_unpair", {}, link=link) == {"paired": False}
    assert link.pair is None and not pair.paired
    forbidden.assert_not_called()


@pytest.mark.parametrize("native_side", ["primary", "peer"])
def test_injected_pair_rejects_native_capable_endpoint_before_semantic_mutation(
    monkeypatch, native_side
):
    primary, primary_pb = _make_session()
    peer, peer_pb = _make_session()
    native_pb = _FakePyBoy(serial=SerialCore())
    target = primary_pb if native_side == "primary" else peer_pb
    target.mb = native_pb.mb
    factory = FakeFactory()
    pair = LinkPair(
        primary, peer, version_primary="red", version_peer="blue",
        bridge_factory=factory,
    )
    link = LinkState(peer_session=peer, peer_version="blue")
    link.pair = pair
    pair_call = Mock(side_effect=AssertionError("semantic pair was reached"))
    monkeypatch.setattr(pair, "pair", pair_call)
    before = vars(link).copy()

    with pytest.raises(McpHarnessError) as raised:
        dispatch_tool(primary, "link_pair", {}, link=link)

    assert raised.value.code == "unsupported_runtime"
    pair_call.assert_not_called()
    assert vars(link) == before
    assert not pair.paired
    assert factory.calls == []


@pytest.mark.parametrize("real_side", ["primary", "peer"])
def test_injected_pair_rejects_real_pyboy_with_stale_serial_contract(
    monkeypatch, real_side, actual_pyboy
):
    primary, _ = _make_session()
    peer, _ = _make_session()
    # Simulate the old/stale capability probe while retaining an initialized,
    # normally constructed instance of the installed PyBoy type.
    monkeypatch.setattr(
        mcp_server, "_supports_bit_accurate_network", lambda _session: False
    )
    target_session = primary if real_side == "primary" else peer
    target_session._pyboy = actual_pyboy
    factory = FakeFactory()
    pair = LinkPair(
        primary, peer, version_primary="red", version_peer="blue",
        bridge_factory=factory,
    )
    link = LinkState(peer_session=peer, peer_version="blue")
    link.pair = pair
    pair_call = Mock(side_effect=AssertionError("semantic pair was reached"))
    monkeypatch.setattr(pair, "pair", pair_call)

    with pytest.raises(McpHarnessError) as raised:
        dispatch_tool(primary, "link_pair", {}, link=link)

    assert raised.value.code == "unsupported_runtime"
    pair_call.assert_not_called()
    assert not pair.paired
    assert factory.calls == []


def test_injected_pair_rejects_native_endpoint_not_in_dispatch_sessions(monkeypatch):
    primary, _ = _make_session()
    peer, _ = _make_session()
    pair_primary, pair_primary_pb = _make_session()
    pair_peer, _ = _make_session()
    pair_primary_pb.mb = _FakePyBoy(serial=SerialCore()).mb
    factory = FakeFactory()
    pair = LinkPair(
        pair_primary, pair_peer, version_primary="red", version_peer="blue",
        bridge_factory=factory,
    )
    link = LinkState(peer_session=peer, peer_version="blue")
    link.pair = pair
    pair_call = Mock(side_effect=AssertionError("semantic pair was reached"))
    monkeypatch.setattr(pair, "pair", pair_call)

    with pytest.raises(McpHarnessError) as raised:
        dispatch_tool(primary, "link_pair", {}, link=link)

    assert raised.value.code == "unsupported_runtime"
    pair_call.assert_not_called()
    assert not pair.paired
    assert factory.calls == []


def test_real_serial_cores_select_bit_accurate_provider_and_restore_backends(monkeypatch):
    primary, peer, link = _sessions()
    cores = [s._pyboy.mb.serial for s in (primary, peer)]
    backends = [core.backend for core in cores]
    forbidden = Mock(side_effect=AssertionError("real serial core selected semantic bridge"))
    monkeypatch.setattr(mcp_server, "LinkPair", forbidden)
    try:
        assert dispatch_tool(primary, "link_pair", {}, link=link)["paired"] is True
        assert link.pair is None
        assert link.local_link_session is not None and link.local_link_session.paired
        assert all(isinstance(core.backend, CoordinatedBackend) for core in cores)
        status = dispatch_tool(primary, "link_status", {}, link=link)
        assert status["paired"] and status["link_backend"] == "bit_accurate"
        assert all(not s._pyboy._hooks and not s._serial_hooks for s in (primary, peer))
    finally:
        assert dispatch_tool(primary, "link_unpair", {}, link=link) == {"paired": False}
    assert link.local_link_session is None and link.pair is None
    assert all(core.backend is previous for core, previous in zip(cores, backends, strict=True))
    assert dispatch_tool(primary, "link_unpair", {}, link=link) == {"paired": False}
    forbidden.assert_not_called()
