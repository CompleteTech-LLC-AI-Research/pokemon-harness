from __future__ import annotations

import asyncio
import base64
import json
import socket as _socket
import threading
import time as _time
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

import pytest

from pokered_harness.events import EventBus
from pokered_harness.link.network_backend import NetworkBackendError
from pokered_harness.link.pair import LinkPair
from pokered_harness.link.serial_link import TcpSerialLink
from pokered_harness.mcp_server import (
    DEFAULT_HOOKS,
    LinkState,
    McpHarnessError,
    _close_serial_link,
    _error_code,
    _wait_for_network_hello,
    build_server,
    dispatch_tool,
    read_resource,
    register_default_hooks,
    serve_stdio,
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


def test_dispatch_step_rejects_unbounded_count():
    s, _, _ = _session()
    with pytest.raises(McpHarnessError, match="count must be <="):
        dispatch_tool(s, "step", {"count": 10_001})


def test_dispatch_press_forwards_button():
    s, pb, _ = _session()
    assert dispatch_tool(s, "press", {"button": "a", "duration": 4}) == {"ok": True}
    assert pb.button_calls == [("a", 4)]


def test_dispatch_press_default_duration_is_one():
    s, pb, _ = _session()
    dispatch_tool(s, "press", {"button": "start"})
    assert pb.button_calls == [("start", 1)]


def test_dispatch_press_rejects_unbounded_duration():
    s, _, _ = _session()
    with pytest.raises(McpHarnessError, match="duration must be <="):
        dispatch_tool(s, "press", {"button": "a", "duration": 10_001})


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


def test_dispatch_load_state_rejects_non_base64_payload():
    s, _, _ = _session()
    with pytest.raises(McpHarnessError, match="not valid base64") as exc_info:
        dispatch_tool(s, "load_state", {"data": "%%%"})
    assert exc_info.value.code == "invalid_state"


def test_dispatch_load_state_rejects_oversized_payload():
    s, _, _ = _session()
    # The dispatcher rejects the encoded length before allocating a decoded
    # state buffer, so this remains a cheap adversarial boundary test.
    oversized = "A" * (22_369_624 + 1)
    with pytest.raises(McpHarnessError, match="byte limit") as exc_info:
        dispatch_tool(s, "load_state", {"data": oversized})
    assert exc_info.value.code == "invalid_state"


def test_dispatch_run_until_event_reports_structured_result():
    s, pb, _bus = _session()
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


def test_session_close_deactivates_all_event_hooks():
    s, pb, bus = _session()
    callback_calls: list[object] = []

    event_handle = s.register_hook("DisplayTextID", "dialog_open")
    symbol_handle = s.register_hook_at(
        "DisplayTextID", callback_calls.append, context="symbol"
    )
    address_handle = s.register_hook_at_address(
        0x00, 0x2920, callback_calls.append, context="address"
    )

    assert pb.fire(0x00, 0x2920) == 1
    assert bus.count("dialog_open") == 1
    assert callback_calls == ["symbol", "address"]

    s.close()

    assert event_handle.active is False
    assert symbol_handle.active is False
    assert address_handle.active is False
    assert pb._hooks == {}
    assert pb.fire(0x00, 0x2920) == 0
    assert bus.count("dialog_open") == 1
    assert callback_calls == ["symbol", "address"]


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


def test_dispatch_run_until_event_rejects_unbounded_budget_and_names():
    s, _, _ = _session()
    with pytest.raises(McpHarnessError, match="max_ticks must be <="):
        dispatch_tool(
            s,
            "run_until_event",
            {"event_names": ["dialog_open"], "max_ticks": 100_001},
        )
    with pytest.raises(McpHarnessError, match="event_names must contain"):
        dispatch_tool(
            s,
            "run_until_event",
            {"event_names": ["dialog_open"] * 65, "max_ticks": 1},
        )


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


def test_real_pyboy_without_native_serial_contract_fails_closed():
    primary, _, _ = _session()
    peer, _, _ = _session()

    RealPyBoy = type(
        "RealPyBoy",
        (),
        {"__module__": "pyboy.pyboy"},
    )
    primary._pyboy = RealPyBoy()
    primary._pyboy.mb = SimpleNamespace(serial=SimpleNamespace())

    link = LinkState(peer_session=peer)
    with pytest.raises(McpHarnessError, match="bit-accurate serial contract") as exc_info:
        dispatch_tool(primary, "link_pair", {}, link=link)
    assert exc_info.value.code == "unsupported_runtime"


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


def test_link_pair_refuses_when_remote_link_is_active():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    link.remote_mode = "connected"
    with pytest.raises(McpHarnessError, match="remote link busy") as exc_info:
        dispatch_tool(s_primary, "link_pair", {}, link=link)
    assert exc_info.value.code == "remote_busy"
    assert link.pair is not None and not link.pair.paired


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


def test_link_step_reports_remote_not_connected_during_transition():
    s, _ = _endpoint_session()
    link = LinkState()
    link.remote_mode = "connecting"
    with pytest.raises(McpHarnessError, match="not ready for stepping") as exc_info:
        dispatch_tool(s, "link_step", {"count": 1}, link=link)
    assert exc_info.value.code == "remote_not_connected"


def test_remote_link_step_routes_through_endpoint():
    s_primary, pb_primary, _, _, link, _ = _link_state_with_peer()
    calls: list[tuple[int, bool]] = []

    class EndpointSpy:
        def step(self, count: int, *, render: bool = False) -> None:
            calls.append((count, render))
            s_primary.step(count, render=render)

    link.remote_endpoint = EndpointSpy()  # type: ignore[assignment]
    result = dispatch_tool(
        s_primary, "link_step", {"count": 4, "render": True}, link=link
    )

    assert calls == [(4, True)]
    assert pb_primary.tick_calls == [(4, True)]
    assert result == {"primary_tick": 4, "peer_tick": None}


def test_remote_link_step_holds_session_lock():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    entered = threading.Event()
    release = threading.Event()
    step_done = threading.Event()

    class BlockingEndpoint:
        def step(self, count, *, render=False):
            assert count == 1
            assert render is False
            entered.set()
            assert release.wait(timeout=2.0)

    link.remote_endpoint = BlockingEndpoint()  # type: ignore[assignment]
    link.remote_mode = "connected"

    link_thread = threading.Thread(
        target=dispatch_tool,
        args=(s_primary, "link_step", {"count": 1}),
        kwargs={"link": link},
    )
    link_thread.start()
    assert entered.wait(timeout=1.0)

    def ordinary_step():
        s_primary.step()
        step_done.set()

    ordinary_thread = threading.Thread(target=ordinary_step)
    ordinary_thread.start()
    assert not step_done.wait(timeout=0.1)

    release.set()
    link_thread.join(timeout=2.0)
    ordinary_thread.join(timeout=2.0)
    assert not link_thread.is_alive()
    assert not ordinary_thread.is_alive()
    assert step_done.is_set()


def test_local_link_step_holds_both_session_locks():
    s_primary, _, _s_peer, _, link, _ = _link_state_with_peer()
    entered = threading.Event()
    release = threading.Event()
    step_done = threading.Event()

    class BlockingLinkSession:
        paired = True

        def step_interleaved(self, count, *, render=False):
            assert count == 1
            assert render is False
            entered.set()
            assert release.wait(timeout=2.0)

    link.local_link_session = BlockingLinkSession()  # type: ignore[assignment]

    link_thread = threading.Thread(
        target=dispatch_tool,
        args=(s_primary, "link_step", {"count": 1}),
        kwargs={"link": link},
    )
    link_thread.start()
    assert entered.wait(timeout=1.0)

    def ordinary_step():
        s_primary.step()
        step_done.set()

    ordinary_thread = threading.Thread(target=ordinary_step)
    ordinary_thread.start()
    assert not step_done.wait(timeout=0.1)

    release.set()
    link_thread.join(timeout=2.0)
    ordinary_thread.join(timeout=2.0)
    assert not link_thread.is_alive()
    assert not ordinary_thread.is_alive()
    assert step_done.is_set()


def test_link_disconnect_does_not_deactivate_an_in_process_pair():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    assert link.pair is not None and link.pair.paired

    assert dispatch_tool(s_primary, "link_disconnect", {}, link=link) == {
        "remote_mode": "idle"
    }
    assert link.pair is not None and link.pair.paired


def test_link_peer_press_routes_to_peer_not_primary():
    s_primary, pb_primary, _s_peer, pb_peer, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_peer_press", {"button": "a", "duration": 2}, link=link)
    assert pb_primary.button_calls == []
    assert pb_peer.button_calls == [("a", 2)]


def test_link_peer_hold_release_routes_to_peer():
    s_primary, pb_primary, _s_peer, pb_peer, link, _ = _link_state_with_peer()
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
    s_primary, _, _s_peer, pb_peer, link, _ = _link_state_with_peer()
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


def test_link_transport_resource_serializes_with_pair_mutation():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    entered = threading.Event()
    result: list[str] = []

    def read_transport() -> None:
        entered.set()
        result.append(read_resource(s_primary, "pokered://link-transport", link=link))

    with link._pair_lock:
        worker = threading.Thread(target=read_transport)
        worker.start()
        assert entered.wait(timeout=1.0)
        # The resource must wait for the same lock used by link mutation.
        assert worker.is_alive()
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result


# -- remote (two-process) link tools ----------------------------------------
#
# These exercise the MCP surface for two-agent trading: link_listen spawns
# a background accept on a local port; link_connect from another "session"
# attaches; link_status reports the transitions; link_disconnect tears down.
#
# We use real localhost sockets via TcpSerialLink, because the whole point
# of the tool is to wire up a socket — stubbing it out would prove
# nothing.


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


def test_listener_rejects_bad_peer_and_accepts_next_peer():
    s_listener, _ = _endpoint_session()
    link = LinkState(primary_version="blue")
    port = _free_port()
    dispatch_tool(s_listener, "link_listen", {"port": port}, link=link)
    bad = _socket.create_connection(("127.0.0.1", port), timeout=1.0)
    bad.close()
    try:
        for _ in range(100):
            if link._listener_error is not None:
                break
            _time.sleep(0.01)
        assert link.remote_mode == "listening"

        good = TcpSerialLink.connect("127.0.0.1", port, "yellow")
        try:
            for _ in range(100):
                if link.remote_mode == "connected":
                    break
                _time.sleep(0.01)
            assert link.remote_mode == "connected"
            assert good.peer_rom_version == "blue"
        finally:
            good.close()
    finally:
        dispatch_tool(s_listener, "link_disconnect", {}, link=link)


def test_listener_cleans_semantic_hooks_when_endpoint_install_fails(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    install_entered = threading.Event()
    deactivated: list[Session] = []

    class FakeTransport:
        def __init__(self, sock, _rom_version):
            self._sock = sock
            self._hello_received = threading.Event()
            self._hello_received.set()
            self._reader_exc = None
            self.peer_rom_version = "yellow"
            self.connected = True

        def close(self):
            self.connected = False
            self._sock.close()

    class FailingEndpoint:
        @classmethod
        def as_listener(cls, _session, _transport):
            return cls()

        def install(self):
            install_entered.set()
            raise RuntimeError("endpoint install failed")

    monkeypatch.setattr("pokered_harness.mcp_server.TcpSerialLink", FakeTransport)
    monkeypatch.setattr(
        "pokered_harness.mcp_server.RemoteLinkEndpoint", FailingEndpoint
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._deactivate_link_hooks",
        lambda session, _peer=None: deactivated.append(session),
    )

    port = _free_port()
    dispatch_tool(s, "link_listen", {"port": port}, link=link)
    peer = _socket.create_connection(("127.0.0.1", port), timeout=1.0)
    try:
        assert install_entered.wait(timeout=2.0)
        deadline = _time.monotonic() + 2.0
        while not deactivated and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert deactivated == [s]
        assert link.remote_mode == "listening"
    finally:
        peer.close()
        dispatch_tool(s, "link_disconnect", {}, link=link)


def test_link_rejects_rom_version_override_for_primary_session():
    s, _ = _endpoint_session()
    link = LinkState(primary_version="blue")
    with pytest.raises(McpHarnessError, match="does not match") as exc_info:
        dispatch_tool(
            s,
            "link_listen",
            {"port": _free_port(), "rom_version": "yellow"},
            link=link,
        )
    assert exc_info.value.code == "rom_version_mismatch"
    assert link.remote_mode == "idle"


def test_error_code_maps_network_backend_failures():
    assert _error_code(NetworkBackendError("backend closed")) == "link_error"
    assert _error_code(NetworkBackendError("no peer response within 1s")) == "timeout"


def test_network_hello_rejects_peer_that_closes_after_hello():
    class ClosedAfterHello:
        _hello_received = threading.Event()
        _reader_exc = None
        connected = False

        @property
        def peer_rom_version(self):
            return "blue"

    backend = ClosedAfterHello()
    backend._hello_received.set()
    with pytest.raises(NetworkBackendError, match="closed during HELLO"):
        _wait_for_network_hello(backend, None, 1.0)  # type: ignore[arg-type]


def test_link_disconnect_cancels_in_progress_connect(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    entered = threading.Event()
    result: list[Exception] = []

    def blocked_connect(*_args, cancel_event=None, **_kwargs):
        entered.set()
        assert cancel_event is not None
        while not cancel_event.is_set():
            _time.sleep(0.01)
        raise NetworkBackendError("connection cancelled")

    monkeypatch.setattr(
        "pokered_harness.mcp_server.TcpSerialLink.connect", blocked_connect
    )

    def connect() -> None:
        try:
            dispatch_tool(
                s,
                "link_connect",
                {"host": "127.0.0.1", "port": _free_port(), "timeout_s": 30},
                link=link,
            )
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    worker = threading.Thread(target=connect, daemon=True)
    worker.start()
    assert entered.wait(timeout=1.0)
    dispatch_tool(s, "link_disconnect", {}, link=link)
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result and isinstance(result[0], McpHarnessError)
    assert result[0].code == "link_cancelled"
    assert link.remote_mode == "idle"


def test_link_connect_cleans_unpublished_resources_on_unexpected_failure(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    transport = type("FakeTransport", (), {})()
    transport.closed = False

    def close_transport():
        transport.closed = True

    transport.close = close_transport
    detached = threading.Event()

    class FakeNetworkSession:
        def __init__(self):
            self.calls = 0
            self.fail = True

        def detach_all(self):
            self.calls += 1
            detached.set()
            if self.fail:
                raise RuntimeError("detach failed")

    network_session = FakeNetworkSession()

    monkeypatch.setattr(
        "pokered_harness.mcp_server._supports_bit_accurate_network",
        lambda _session: True,
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server.NetworkBackend.connect",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._attach_network_backend",
        lambda *_args, **_kwargs: network_session,
    )

    def fail_handshake(*_args, **_kwargs):
        raise RuntimeError("unexpected handshake fault")

    monkeypatch.setattr(
        "pokered_harness.mcp_server._wait_for_network_hello",
        fail_handshake,
    )

    with pytest.raises(RuntimeError, match="unexpected handshake fault"):
        dispatch_tool(
            s,
            "link_connect",
            {"host": "127.0.0.1", "port": _free_port()},
            link=link,
        )

    assert detached.is_set()
    assert transport.closed is True
    assert link.remote_mode == "disconnecting"
    assert link.remote_link is None
    assert link.network_session is None
    assert link._pending_network_session is network_session

    # The failed detach remains owned by LinkState rather than being reported
    # as a clean idle transition. A later disconnect retries the exact handle.
    network_session.fail = False
    assert dispatch_tool(s, "link_disconnect", {}, link=link) == {
        "remote_mode": "idle"
    }
    assert network_session.calls == 2
    assert link._pending_network_session is None


def test_link_listen_rejects_live_previous_listener_worker():
    s, _ = _endpoint_session()
    link = LinkState()
    entered = threading.Event()
    release = threading.Event()

    def stale_worker():
        entered.set()
        release.wait(timeout=2.0)

    worker = threading.Thread(target=stale_worker, daemon=True)
    worker.start()
    assert entered.wait(timeout=1.0)
    link._listener_thread = worker
    try:
        with pytest.raises(
            McpHarnessError, match="cleanup is still in progress"
        ):
            dispatch_tool(
                s,
                "link_listen",
                {"port": _free_port()},
                link=link,
            )
        assert link._listener_thread is worker
    finally:
        release.set()
        worker.join(timeout=1.0)


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


def test_link_listen_reservation_survives_disconnect_race(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState()
    bind_entered = threading.Event()
    release_bind = threading.Event()
    listen_result: list[Exception] = []
    disconnect_result: list[Exception] = []
    reconnect_result: list[Exception] = []
    reconnect_called = threading.Event()

    class FakeListener:
        closed = False

        def close(self):
            self.closed = True

    listener = FakeListener()

    def blocked_bind(_host, _port):
        bind_entered.set()
        assert release_bind.wait(timeout=2.0)
        return listener

    monkeypatch.setattr("pokered_harness.mcp_server._bind_listener", blocked_bind)

    def blocked_reconnect(*_args, **_kwargs):
        reconnect_called.set()
        raise NetworkBackendError("reconnect should wait for listener teardown")

    monkeypatch.setattr(
        "pokered_harness.mcp_server.TcpSerialLink.connect", blocked_reconnect
    )

    def listen() -> None:
        try:
            dispatch_tool(
                s, "link_listen", {"port": _free_port()}, link=link
            )
        except Exception as exc:  # noqa: BLE001
            listen_result.append(exc)

    listen_worker = threading.Thread(target=listen)
    listen_worker.start()
    assert bind_entered.wait(timeout=1.0)

    def disconnect() -> None:
        try:
            dispatch_tool(s, "link_disconnect", {}, link=link)
        except Exception as exc:  # noqa: BLE001
            disconnect_result.append(exc)

    disconnect_worker = threading.Thread(target=disconnect)
    disconnect_worker.start()
    # The listener reservation makes the disconnect observe active work even
    # though the socket has not been returned by _bind_listener yet.
    disconnect_worker.join(timeout=1.0)
    assert not disconnect_worker.is_alive()
    assert disconnect_result == []

    def reconnect() -> None:
        try:
            dispatch_tool(
                s,
                "link_connect",
                {"host": "127.0.0.1", "port": _free_port(), "timeout_s": 0.01},
                link=link,
            )
        except Exception as exc:  # noqa: BLE001
            reconnect_result.append(exc)

    reconnect_worker = threading.Thread(target=reconnect)
    reconnect_worker.start()
    _time.sleep(0.05)
    assert reconnect_worker.is_alive()
    assert not reconnect_called.is_set()

    release_bind.set()
    listen_worker.join(timeout=2.0)
    reconnect_worker.join(timeout=2.0)
    assert not listen_worker.is_alive()
    assert not reconnect_worker.is_alive()
    assert listen_result and isinstance(listen_result[0], McpHarnessError)
    assert listen_result[0].code == "link_cancelled"
    assert listener.closed is True
    assert reconnect_result and isinstance(reconnect_result[0], McpHarnessError)
    assert reconnect_result[0].code == "link_connect_failed"
    assert reconnect_called.is_set()
    assert link.remote_mode == "idle"
    assert link._listener_thread is None
    assert link._listener_start_in_progress is False


def test_link_status_serializes_dead_remote_cleanup_before_reconnect(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    reconnect_attempted = threading.Event()
    reconnect_result: list[Exception] = []

    class DeadRemote:
        connected = False
        _reader_exc = RuntimeError("peer closed")

        def close(self):
            cleanup_entered.set()
            assert release_cleanup.wait(timeout=2.0)

    link.remote_mode = "connected"
    link.remote_link = DeadRemote()  # type: ignore[assignment]

    def reconnect() -> None:
        reconnect_attempted.set()
        try:
            dispatch_tool(
                s,
                "link_connect",
                {"host": "127.0.0.1", "port": _free_port()},
                link=link,
            )
        except Exception as exc:  # noqa: BLE001
            reconnect_result.append(exc)

    status_result: list[dict] = []

    def status() -> None:
        status_result.append(dispatch_tool(s, "link_status", {}, link=link))

    status_worker = threading.Thread(target=status)
    status_worker.start()
    assert cleanup_entered.wait(timeout=1.0)
    reconnect_worker = threading.Thread(target=reconnect)
    reconnect_worker.start()
    assert reconnect_attempted.wait(timeout=1.0)
    assert link.remote_mode == "disconnecting"
    reconnect_worker.join(timeout=1.0)
    assert not reconnect_worker.is_alive()
    assert reconnect_result
    assert isinstance(reconnect_result[0], McpHarnessError)
    assert reconnect_result[0].code == "remote_busy"

    release_cleanup.set()
    status_worker.join(timeout=2.0)
    assert not status_worker.is_alive()
    assert status_result and status_result[0]["remote_mode"] == "idle"
    assert link.remote_mode == "idle"


def test_link_connect_reservation_survives_disconnect_race(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    host_validation_entered = threading.Event()
    release_host_validation = threading.Event()
    connect_result: list[Exception] = []
    disconnect_result: list[Exception] = []

    def blocked_host(value):
        host_validation_entered.set()
        assert release_host_validation.wait(timeout=2.0)
        return value

    monkeypatch.setattr(
        "pokered_harness.mcp_server._validate_remote_host", blocked_host
    )

    def blocked_connect(*_args, cancel_event=None, **_kwargs):
        assert cancel_event is not None
        while not cancel_event.is_set():
            _time.sleep(0.01)
        raise NetworkBackendError("connection cancelled")

    monkeypatch.setattr(
        "pokered_harness.mcp_server.TcpSerialLink.connect", blocked_connect
    )

    def connect() -> None:
        try:
            dispatch_tool(
                s,
                "link_connect",
                {"host": "127.0.0.1", "port": _free_port(), "timeout_s": 30},
                link=link,
            )
        except Exception as exc:  # noqa: BLE001
            connect_result.append(exc)

    connect_worker = threading.Thread(target=connect)
    connect_worker.start()
    assert host_validation_entered.wait(timeout=1.0)

    disconnect_done = threading.Event()

    def disconnect() -> None:
        try:
            dispatch_tool(s, "link_disconnect", {}, link=link)
        except Exception as exc:  # noqa: BLE001
            disconnect_result.append(exc)
        finally:
            disconnect_done.set()

    disconnect_worker = threading.Thread(target=disconnect)
    disconnect_worker.start()
    # The connect reservation is held while validation is in progress, so a
    # disconnect cannot incorrectly return from an idle snapshot.
    assert not disconnect_done.wait(timeout=0.05)

    release_host_validation.set()
    connect_worker.join(timeout=2.0)
    disconnect_worker.join(timeout=2.0)
    assert not connect_worker.is_alive()
    assert not disconnect_worker.is_alive()
    assert disconnect_result == []
    assert connect_result and isinstance(connect_result[0], McpHarnessError)
    assert connect_result[0].code == "link_cancelled"
    assert link.remote_mode == "idle"


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


def test_link_listen_rejects_non_loopback_host():
    """Remote TCP is deliberately constrained to the unauthenticated
    localhost-only contract until an authenticated transport exists."""
    s, _ = _endpoint_session()
    link = LinkState()
    with pytest.raises(McpHarnessError, match="localhost-only"):
        dispatch_tool(
            s,
            "link_listen",
            {"port": _free_port(), "host": "1.2.3.4"},
            link=link,
        )
    assert link.remote_mode == "idle"


def test_link_disconnect_is_idempotent():
    s, _ = _endpoint_session()
    link = LinkState()
    # No link set up → disconnect should be a safe no-op.
    result = dispatch_tool(s, "link_disconnect", {}, link=link)
    assert result == {"remote_mode": "idle"}


def test_second_disconnect_reports_when_first_teardown_is_still_running(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState()
    link._disconnecting = True
    link._disconnect_owner = -1
    link._disconnect_done.clear()
    monkeypatch.setattr(
        "pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.01
    )

    with pytest.raises(McpHarnessError, match="still in progress") as exc_info:
        dispatch_tool(s, "link_disconnect", {}, link=link)
    assert exc_info.value.code == "link_teardown_timeout"

    link._disconnect_done.set()
    link._disconnecting = False


def test_second_disconnect_surfaces_first_teardown_error():
    s, _ = _endpoint_session()
    link = LinkState()
    link._disconnecting = True
    link._disconnect_owner = -1
    link._remote_error = RuntimeError("worker leaked")
    link._disconnect_done.set()

    with pytest.raises(McpHarnessError, match="worker leaked") as exc_info:
        dispatch_tool(s, "link_disconnect", {}, link=link)
    assert exc_info.value.code == "link_teardown_failed"

    link._disconnecting = False
    link._remote_error = None


def test_close_serial_link_uses_one_total_worker_deadline():
    join_timeouts: list[float] = []

    class SpyThread(threading.Thread):
        def join(self, timeout=None):
            assert timeout is not None
            join_timeouts.append(timeout)
            _time.sleep(0.01)

        def is_alive(self):
            return True

    class FakeLink:
        def close(self):
            return None

    fake = FakeLink()
    fake._reader = SpyThread()
    fake._edge_worker = SpyThread()
    assert _close_serial_link(fake, timeout_s=0.05) is False  # type: ignore[arg-type]
    assert len(join_timeouts) == 2
    assert join_timeouts[1] < join_timeouts[0]


def test_link_disconnect_cleans_stale_listener_worker_from_idle_state():
    s, _ = _endpoint_session()
    link = LinkState()
    entered = threading.Event()

    def stale_worker() -> None:
        entered.set()
        link._listener_cancel.wait(timeout=2.0)

    worker = threading.Thread(target=stale_worker)
    worker.start()
    assert entered.wait(timeout=1.0)
    link._listener_thread = worker

    dispatch_tool(s, "link_disconnect", {}, link=link)
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert link.remote_mode == "idle"
    assert link._listener_thread is None


def test_link_status_reports_disconnect_in_progress():
    s, _ = _endpoint_session()
    link = LinkState()
    link.remote_mode = "disconnecting"
    link._disconnecting = True
    status = dispatch_tool(s, "link_status", {}, link=link)
    assert status["remote_mode"] == "disconnecting"


def test_link_disconnect_stops_listener_worker():
    s, _ = _endpoint_session()
    link = LinkState()
    dispatch_tool(s, "link_listen", {"port": _free_port()}, link=link)
    worker = link._listener_thread
    assert worker is not None
    dispatch_tool(s, "link_disconnect", {}, link=link)
    assert not worker.is_alive()
    assert link.remote_mode == "idle"
    assert link._listener_thread is None


def test_link_disconnect_closes_accepted_transport_during_handshake():
    """Cancellation must close a connection before HELLO ownership transfers."""
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    port = _free_port()
    before = {
        id(thread)
        for thread in threading.enumerate()
        if thread.name == "TcpSerialLink.reader" and thread.is_alive()
    }
    peer = None
    accepted_readers = []
    try:
        dispatch_tool(s, "link_listen", {"port": port}, link=link)
        # Hold the accepted socket open without sending HELLO.  This keeps the
        # worker in its cancellable handshake wait instead of letting EOF close
        # the transport and hide a teardown leak.
        peer = _socket.create_connection(("127.0.0.1", port), timeout=1.0)
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline:
            accepted_readers = [
                thread
                for thread in threading.enumerate()
                if (
                    thread.name == "TcpSerialLink.reader"
                    and thread.is_alive()
                    and id(thread) not in before
                )
            ]
            if accepted_readers:
                break
            _time.sleep(0.01)
        assert accepted_readers

        dispatch_tool(s, "link_disconnect", {}, link=link)

        assert link.remote_mode == "idle"
        assert link._listener_thread is None
        assert all(not thread.is_alive() for thread in accepted_readers)
    finally:
        if peer is not None:
            peer.close()
        with suppress(McpHarnessError):
            dispatch_tool(s, "link_disconnect", {}, link=link)


def test_link_disconnect_detaches_native_backend_under_session_lock():
    """Restoring the serial backend must not race an in-flight session step."""
    s, _ = _endpoint_session()
    link = LinkState()
    detached = threading.Event()
    disconnect_done = threading.Event()

    class FakeRemote:
        def close(self):
            return None

    class FakeNetworkSession:
        def detach_all(self):
            detached.set()

    link.remote_mode = "connected"
    link.remote_link = FakeRemote()  # type: ignore[assignment]
    link.network_session = FakeNetworkSession()  # type: ignore[assignment]

    def disconnect() -> None:
        dispatch_tool(s, "link_disconnect", {}, link=link)
        disconnect_done.set()

    with s.locked():
        worker = threading.Thread(target=disconnect)
        worker.start()
        assert not detached.wait(timeout=0.1)
        assert not disconnect_done.is_set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert detached.is_set()
    assert disconnect_done.is_set()


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
        params=mcp_types.CallToolRequestParams(
            name="load_state", arguments={"data": "%%%"}
        )
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
        params=mcp_types.CallToolRequestParams(
            name="step", arguments={"count": 1}
        )
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

    monkeypatch.setattr(
        "pokered_harness.mcp_server.dispatch_tool", blocked_dispatch
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._disconnect_remote", cancel_remote
    )

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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("remote teardown failed")
        ),
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("cleanup failed")
        ),
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
