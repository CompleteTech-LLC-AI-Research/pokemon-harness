"""Shared helpers for the split MCP-server test modules.

Split from ``tests/test_mcp_server.py`` for #135 with no behavior change;
the helper bodies are moved verbatim so the split test modules import a
single definition instead of duplicating them.
"""

from __future__ import annotations

import socket as _socket
import time as _time
from types import SimpleNamespace

from pokered_harness.events import EventBus
from pokered_harness.link.pair import LinkPair
from pokered_harness.link.serial_core import SerialCore
from pokered_harness.mcp_server import (
    LinkState,
    dispatch_tool,
)
from pokered_harness.session import (
    Session,
)
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
        03:749D _AddEnemyMonToPlayerParty
    """


def _session() -> tuple[Session, FakePyBoy, EventBus]:
    mem = DictMemory()
    pb = FakePyBoy(mem)
    sym = load_sym_text(_harness_sym())
    bus = EventBus()
    return Session(pyboy=pb, symbols=sym, event_bus=bus), pb, bus


def _link_state_with_peer() -> tuple[
    Session, FakePyBoy, Session, FakePyBoy, LinkState, FakeFactory
]:
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
    """Synthetic symbols retained for the endpoint-session test fixture."""
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
    # The remote MCP tools deliberately reject semantic-only fakes. Give
    # these endpoint sessions the same native serial shape as the bundled
    # PyBoy runtime while retaining FakePyBoy's deterministic tick/memory
    # behavior for the rest of this ROM-free test module.
    assert SerialCore is not None
    pb.mb = SimpleNamespace(
        serial=SerialCore(False),
        cpu=SimpleNamespace(set_interruptflag=lambda _flag: None),
    )
    sym = load_sym_text(_endpoint_sym())
    return Session(pyboy=pb, symbols=sym, event_bus=EventBus()), pb


def _connected_endpoint_pair():
    """Join two endpoint sessions over a real localhost socket.

    Mirrors ``test_link_listen_then_connect_updates_status``: the listener
    binds, the connector attaches, and both report ``connected`` before the
    caller touches the link.  Each call reserves its own port, so tests stay
    order-independent.
    """
    s_listener, _ = _endpoint_session()
    s_connector, _ = _endpoint_session()
    link_l = LinkState(primary_version="blue")
    link_c = LinkState(primary_version="yellow")
    port = _free_port()
    listening = dispatch_tool(s_listener, "link_listen", {"port": port}, link=link_l)
    assert listening["remote_mode"] == "listening", listening
    connected = dispatch_tool(
        s_connector,
        "link_connect",
        {"host": "127.0.0.1", "port": port, "rom_version": "yellow"},
        link=link_c,
    )
    assert connected["remote_mode"] == "connected", connected
    _wait_remote_mode(link_l, "connected")
    assert link_c.remote_mode == "connected"
    return s_listener, link_l, s_connector, link_c


def _disconnect_endpoint_pair(s_listener, link_l, s_connector, link_c):
    """Tear the pair down connector-first, as the lifecycle contract requires."""
    dispatch_tool(s_connector, "link_disconnect", {}, link=link_c)
    dispatch_tool(s_listener, "link_disconnect", {}, link=link_l)
