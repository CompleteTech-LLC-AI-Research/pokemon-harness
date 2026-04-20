from __future__ import annotations

import base64
import json

import pytest

from pokered_harness.events import EventBus
from pokered_harness.link.pair import LinkPair
from pokered_harness.mcp_server import (
    DEFAULT_HOOKS,
    LinkState,
    dispatch_tool,
    read_resource,
    register_default_hooks,
)
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy
from tests.test_link_pair import FakeFactory


def _harness_sym() -> str:
    """A minimal .sym containing only what the MCP tests need.

    Addresses are fake — parsers look things up by name; the MCP tests
    exercise dispatch, not game-state semantics.
    """
    return """
        00:D35E wCurMap
        00:D361 wYCoord
        00:D362 wXCoord
        00:CFC5 wWalkCounter
        00:CC26 wCurrentMenuItem
        00:CC28 wMaxMenuItem
        00:D057 wIsInBattle
        00:D163 wPartyCount
        00:D16B wPartyMons
        00:D356 wObtainedBadges
        00:D31D wNumBagItems
        00:D31E wBagItems
        00:3C49 PrintText
        00:2920 DisplayTextID
        00:35EC YesNoChoice
        0E:6D0E TryEvolvingMon
        01:7078 SetLastBlackoutMap
    """


def _session() -> tuple[Session, FakePyBoy, EventBus]:
    mem = DictMemory()
    pb = FakePyBoy(mem)
    sym = load_sym_text(_harness_sym())
    bus = EventBus()
    return Session(pyboy=pb, symbols=sym, event_bus=bus), pb, bus


# -- tool dispatch ----------------------------------------------------------


def test_dispatch_step_advances_session():
    s, pb, _ = _session()
    result = dispatch_tool(s, "step", {"count": 3})
    assert result == {"tick": 3}
    assert pb.tick_calls == [(3, False)]


def test_dispatch_step_render_flag():
    s, pb, _ = _session()
    dispatch_tool(s, "step", {"count": 1, "render": True})
    assert pb.tick_calls == [(1, True)]


def test_dispatch_press_forwards_button():
    s, pb, _ = _session()
    assert dispatch_tool(s, "press", {"button": "a", "duration": 4}) == {"ok": True}
    assert pb.button_calls == [("a", 4)]


def test_dispatch_press_default_duration_is_one():
    s, pb, _ = _session()
    dispatch_tool(s, "press", {"button": "start"})
    assert pb.button_calls == [("start", 1)]


def test_dispatch_hold_release_roundtrip():
    s, pb, _ = _session()
    dispatch_tool(s, "hold", {"button": "up"})
    dispatch_tool(s, "release", {"button": "up"})
    assert pb.button_press_calls == ["up"]
    assert pb.button_release_calls == ["up"]


def test_dispatch_save_state_returns_base64():
    s, _, _ = _session()
    result = dispatch_tool(s, "save_state", {})
    assert "data" in result
    # Fake PyBoy writes b"STATE" by default.
    assert base64.b64decode(result["data"]) == b"STATE"


def test_dispatch_load_state_accepts_base64():
    s, _, _ = _session()
    payload = base64.b64encode(b"SNAPSHOT").decode("ascii")
    assert dispatch_tool(s, "load_state", {"data": payload}) == {"ok": True}


def test_dispatch_run_until_event_reports_structured_result():
    s, pb, bus = _session()
    s.register_hook("DisplayTextID", "dialog_open")

    # Fire mid-run on the second step chunk.
    original_tick = pb.tick

    def ticker(count=1, render=False):
        r = original_tick(count, render=render)
        if len(pb.tick_calls) == 2:
            pb.fire(0x00, 0x2920)
        return r

    pb.tick = ticker  # type: ignore[assignment]

    result = dispatch_tool(
        s,
        "run_until_event",
        {"event_names": ["dialog_open"], "max_ticks": 64, "chunk": 8},
    )
    assert result["reached"] is True
    assert result["event"] is not None
    assert result["event"]["name"] == "dialog_open"


def test_dispatch_run_until_event_timeout_path():
    s, _, _ = _session()
    s.register_hook("DisplayTextID", "dialog_open")
    result = dispatch_tool(
        s,
        "run_until_event",
        {"event_names": ["dialog_open"], "max_ticks": 16, "chunk": 4},
    )
    assert result["reached"] is False
    assert result["event"] is None
    assert result["ticks_spent"] == 16


def test_dispatch_unknown_tool_raises():
    s, _, _ = _session()
    with pytest.raises(ValueError):
        dispatch_tool(s, "teleport", {})


# -- resources -------------------------------------------------------------


def test_read_resource_game_state_returns_valid_json():
    s, pb, _ = _session()
    pb.memory[0xD35E] = 0x0B
    pb.memory[0xD362] = 5
    pb.memory[0xD361] = 10
    payload = json.loads(read_resource(s, "pokered://game-state"))
    assert payload["overworld"]["map_id"] == 0x0B
    assert payload["overworld"]["x"] == 5
    assert payload["overworld"]["y"] == 10
    # Direction field serialises as the underlying IntEnum value (or None).
    assert "direction" in payload["overworld"]


def test_read_resource_event_log_starts_empty_and_grows():
    s, _, bus = _session()
    assert json.loads(read_resource(s, "pokered://events")) == []
    bus.emit(tick=5, name="dialog_open", bank=0, addr=0x2920)
    payload = json.loads(read_resource(s, "pokered://events"))
    assert len(payload) == 1
    assert payload[0]["name"] == "dialog_open"
    assert payload[0]["tick"] == 5


def test_read_resource_unknown_uri_raises():
    s, _, _ = _session()
    with pytest.raises(ValueError):
        read_resource(s, "pokered://nope")


# -- default hook registration --------------------------------------------


def test_register_default_hooks_wires_everything_when_symbols_present():
    s, _, bus = _session()
    registered = register_default_hooks(s)
    assert set(registered) == {ev for _sym, ev in DEFAULT_HOOKS}
    assert bus.registered_symbols == {sym for sym, _ev in DEFAULT_HOOKS}


def test_register_default_hooks_skips_missing_symbols():
    mem = DictMemory()
    pb = FakePyBoy(mem)
    # Only DisplayTextID — the other three hooks should be skipped cleanly.
    sym = load_sym_text(
        """
        00:D35E wCurMap
        00:D361 wYCoord
        00:D362 wXCoord
        00:CFC5 wWalkCounter
        00:CC26 wCurrentMenuItem
        00:CC28 wMaxMenuItem
        00:D057 wIsInBattle
        00:D163 wPartyCount
        00:D16B wPartyMons
        00:D356 wObtainedBadges
        00:D31D wNumBagItems
        00:D31E wBagItems
        00:2920 DisplayTextID
        """
    )
    s = Session(pyboy=pb, symbols=sym)
    registered = register_default_hooks(s)
    assert registered == ["overworld_dialog"]


# -- link-cable tools ------------------------------------------------------


def _link_state_with_peer() -> tuple[Session, FakePyBoy, Session, FakePyBoy, LinkState, FakeFactory]:
    """Build a primary+peer pair backed by fake PyBoys, with a FakeFactory
    so pair() doesn't try to construct the real SerialBridge."""
    s_primary, pb_primary, _ = _session()
    s_peer, pb_peer, _ = _session()
    factory = FakeFactory()
    link = LinkState(peer_session=s_peer, primary_version="red", peer_version="red")
    # Pre-seed a LinkPair using the fake factory; dispatch code will call
    # .pair() on this rather than constructing a fresh one.
    link.pair = LinkPair(
        s_primary,
        s_peer,
        version_primary="red",
        version_peer="red",
        bridge_factory=factory,
    )
    return s_primary, pb_primary, s_peer, pb_peer, link, factory


def test_link_status_before_pairing_reports_unpaired():
    s, _, _ = _session()
    link = LinkState(peer_session=None)
    result = dispatch_tool(s, "link_status", {}, link=link)
    assert result["paired"] is False
    assert result["peer_tick"] is None
    assert result["primary_tick"] == 0


def test_link_status_with_peer_unpaired_reports_peer_tick():
    s_primary, _, _ = _session()
    s_peer, _, _ = _session()
    link = LinkState(peer_session=s_peer)
    result = dispatch_tool(s_primary, "link_status", {}, link=link)
    assert result["paired"] is False
    assert result["peer_tick"] == 0


def test_link_pair_without_peer_raises():
    s, _, _ = _session()
    link = LinkState(peer_session=None)
    with pytest.raises(ValueError, match="peer session not configured"):
        dispatch_tool(s, "link_pair", {}, link=link)


def test_link_pair_transitions_to_paired():
    s_primary, _, _, _, link, factory = _link_state_with_peer()
    result = dispatch_tool(s_primary, "link_pair", {}, link=link)
    assert result == {"paired": True, "primary_version": "red", "peer_version": "red"}
    assert link.pair is not None
    assert link.pair.paired is True
    assert len(factory.calls) == 1
    status = dispatch_tool(s_primary, "link_status", {}, link=link)
    assert status["paired"] is True


def test_link_pair_already_paired_raises():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    with pytest.raises(ValueError, match="already paired"):
        dispatch_tool(s_primary, "link_pair", {}, link=link)


def test_link_step_errors_when_not_paired():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    # Never called link_pair → pair exists but not paired.
    with pytest.raises(ValueError, match="not paired"):
        dispatch_tool(s_primary, "link_step", {"count": 4}, link=link)


def test_link_step_advances_both_when_paired():
    s_primary, _, s_peer, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    result = dispatch_tool(s_primary, "link_step", {"count": 4}, link=link)
    assert s_primary.current_tick() == 4
    assert s_peer.current_tick() == 4
    assert result == {"primary_tick": 4, "peer_tick": 4}


def test_link_peer_press_routes_to_peer_not_primary():
    s_primary, pb_primary, s_peer, pb_peer, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_peer_press", {"button": "a", "duration": 2}, link=link)
    assert pb_primary.button_calls == []
    assert pb_peer.button_calls == [("a", 2)]


def test_link_peer_hold_release_routes_to_peer():
    s_primary, pb_primary, s_peer, pb_peer, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_peer_hold", {"button": "up"}, link=link)
    dispatch_tool(s_primary, "link_peer_release", {"button": "up"}, link=link)
    assert pb_primary.button_press_calls == []
    assert pb_primary.button_release_calls == []
    assert pb_peer.button_press_calls == ["up"]
    assert pb_peer.button_release_calls == ["up"]


def test_link_unpair_is_idempotent_when_not_paired():
    s, _, _ = _session()
    link = LinkState(peer_session=None)
    result = dispatch_tool(s, "link_unpair", {}, link=link)
    assert result == {"paired": False}


def test_link_unpair_after_pair_drops_pair():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    assert link.pair is not None
    result = dispatch_tool(s_primary, "link_unpair", {}, link=link)
    assert result == {"paired": False}
    assert link.pair is None


def test_peer_game_state_resource_returns_data_when_configured():
    s_primary, _, s_peer, pb_peer, link, _ = _link_state_with_peer()
    pb_peer.memory[0xD35E] = 0x0C
    pb_peer.memory[0xD362] = 7
    pb_peer.memory[0xD361] = 9
    payload = json.loads(read_resource(s_primary, "pokered://peer-game-state", link=link))
    assert payload["overworld"]["map_id"] == 0x0C
    assert payload["overworld"]["x"] == 7
    assert payload["overworld"]["y"] == 9


def test_peer_game_state_resource_errors_when_not_configured():
    s, _, _ = _session()
    link = LinkState(peer_session=None)
    with pytest.raises(ValueError, match="peer session not configured"):
        read_resource(s, "pokered://peer-game-state", link=link)


def test_link_transport_resource_empty_when_not_paired():
    s, _, _ = _session()
    link = LinkState(peer_session=None)
    payload = json.loads(read_resource(s, "pokered://link-transport", link=link))
    assert payload == {"a_to_b": [], "b_to_a": []}


def test_link_transport_resource_reflects_transport_state():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    # Push some bytes through the transport directly and verify the
    # snapshot resource reflects them.
    link.pair.transport.push_a_to_b(0x42)
    link.pair.transport.push_b_to_a(0x17)
    payload = json.loads(read_resource(s_primary, "pokered://link-transport", link=link))
    assert payload == {"a_to_b": [0x42], "b_to_a": [0x17]}


# -- remote (two-process) link tools ----------------------------------------
#
# These exercise the MCP surface for two-agent trading: link_listen spawns
# a background accept on a local port; link_connect from another "session"
# attaches; link_status reports the transitions; link_disconnect tears down.
#
# We use real localhost sockets via TcpSerialLink, because the whole point
# of the tool is to wire up a socket — stubbing it out would prove
# nothing.


import socket as _socket
import time as _time


def _free_port() -> int:
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_remote_mode(link: LinkState, mode: str, timeout: float = 2.0) -> None:
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if link.remote_mode == mode:
            return
        _time.sleep(0.01)
    raise AssertionError(
        f"remote_mode never reached {mode!r} (last={link.remote_mode!r}, "
        f"error={link._listener_error!r})"
    )


def _endpoint_sym() -> str:
    """Symbols required by RemoteLinkEndpoint.install(). Addresses are
    synthetic; the MCP test only needs the install() call to succeed."""
    return """
        00:22FA Serial_TryEstablishingExternallyClockedConnection
        00:216F Serial_ExchangeBytes
        00:22C3 Serial_ExchangeNybble
        00:2247 Serial_ExchangeLinkMenuSelection
        00:FFAA hSerialConnectionStatus
        00:FFAC hSerialSendData
        00:FFAD hSerialReceiveData
        00:CC42 wLinkMenuSelectionSendBuffer
        00:CC3D wLinkMenuSelectionReceiveBuffer
        00:CC42 wSerialExchangeNybbleSendData
        00:CC3E wSerialExchangeNybbleReceiveData
        00:D141 wSerialRandomNumberListBlock
        00:D173 wSerialPlayerDataBlock
    """


def _endpoint_session():
    mem = DictMemory()
    pb = FakePyBoy(mem)
    sym = load_sym_text(_endpoint_sym())
    return Session(pyboy=pb, symbols=sym, event_bus=EventBus()), pb


def test_link_listen_then_connect_updates_status():
    s_listener, _ = _endpoint_session()
    s_connector, _ = _endpoint_session()
    link_l = LinkState(primary_version="blue")
    link_c = LinkState(primary_version="yellow")

    port = _free_port()
    listen_result = dispatch_tool(
        s_listener, "link_listen", {"port": port}, link=link_l
    )
    assert listen_result["remote_mode"] == "listening"
    assert listen_result["remote_role"] == "listener"
    assert listen_result["rom_version"] == "blue"

    # Poll link_status until bind completes (accept() is on a bg thread).
    for _ in range(100):
        status = dispatch_tool(s_listener, "link_status", {}, link=link_l)
        if status["remote_mode"] == "listening":
            break
        _time.sleep(0.01)
    assert status["remote_mode"] == "listening"

    # Connect from the other side.
    connect_result = dispatch_tool(
        s_connector,
        "link_connect",
        {"host": "127.0.0.1", "port": port, "rom_version": "yellow"},
        link=link_c,
    )
    assert connect_result["remote_mode"] == "connected"
    assert connect_result["remote_role"] == "connector"
    assert connect_result["peer_rom_version"] == "blue"

    _wait_remote_mode(link_l, "connected")
    status_l = dispatch_tool(s_listener, "link_status", {}, link=link_l)
    status_c = dispatch_tool(s_connector, "link_status", {}, link=link_c)
    assert status_l["remote_mode"] == "connected"
    assert status_l["remote_peer_rom_version"] == "yellow"
    assert status_c["remote_mode"] == "connected"
    assert status_c["remote_peer_rom_version"] == "blue"

    # Cleanup in the proper order — connector first drops the cable,
    # then listener notices.
    dispatch_tool(s_connector, "link_disconnect", {}, link=link_c)
    assert link_c.remote_mode == "idle"
    # Listener sees its link drop on the next status poll.
    for _ in range(100):
        status_l = dispatch_tool(s_listener, "link_status", {}, link=link_l)
        if status_l["remote_mode"] == "idle":
            break
        _time.sleep(0.01)
    assert status_l["remote_mode"] == "idle"
    dispatch_tool(s_listener, "link_disconnect", {}, link=link_l)


def test_link_listen_rejects_concurrent_call():
    s, _ = _endpoint_session()
    link = LinkState()
    port = _free_port()
    dispatch_tool(s, "link_listen", {"port": port}, link=link)
    try:
        with pytest.raises(ValueError, match="remote link busy"):
            dispatch_tool(s, "link_listen", {"port": _free_port()}, link=link)
    finally:
        dispatch_tool(s, "link_disconnect", {}, link=link)


def test_link_connect_refuses_when_pair_active():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    with pytest.raises(ValueError, match="in-process pair is active"):
        dispatch_tool(
            s_primary,
            "link_connect",
            {"host": "127.0.0.1", "port": _free_port()},
            link=link,
        )


def test_link_listen_surfaces_bind_failure_via_status():
    """Binding to a port we know can't succeed (port 0 with a
    host that rejects outright) ends up with the listener thread
    storing the exception; link_status surfaces it.

    We reach this by passing an invalid host. Using a non-loopback
    address like ``1.2.3.4`` guarantees bind() raises OSError on every
    platform, whereas the "bind same port twice" trick depends on
    SO_REUSEADDR semantics which differ by OS.
    """
    s, _ = _endpoint_session()
    link = LinkState()
    dispatch_tool(
        s,
        "link_listen",
        {"port": _free_port(), "host": "1.2.3.4"},
        link=link,
    )
    for _ in range(200):
        status = dispatch_tool(s, "link_status", {}, link=link)
        if status["remote_error"] is not None:
            break
        _time.sleep(0.01)
    assert status["remote_error"] is not None
    assert status["remote_mode"] == "idle"


def test_link_disconnect_is_idempotent():
    s, _ = _endpoint_session()
    link = LinkState()
    # No link set up → disconnect should be a safe no-op.
    result = dispatch_tool(s, "link_disconnect", {}, link=link)
    assert result == {"remote_mode": "idle"}


def test_link_status_resource_returns_same_shape_as_tool():
    """The `pokered://link-status` resource is intended as a pollable
    snapshot for clients that don't want to round-trip via a tool call.
    Its JSON must match the tool's output exactly."""
    s, _ = _endpoint_session()
    link = LinkState()
    tool_payload = dispatch_tool(s, "link_status", {}, link=link)
    resource_payload = json.loads(
        read_resource(s, "pokered://link-status", link=link)
    )
    assert resource_payload == tool_payload
    assert resource_payload["remote_mode"] == "idle"
