from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from pokered_harness.mcp_server import (
    LinkState,
    McpHarnessError,
    dispatch_tool,
    read_resource,
)
from tests._mcp_server_support import (
    _endpoint_session,
    _link_state_with_peer,
    _session,
)

# -- link-cable tools ------------------------------------------------------


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
    result = dispatch_tool(s_primary, "link_step", {"count": 4, "render": True}, link=link)

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

    assert dispatch_tool(s_primary, "link_disconnect", {}, link=link) == {"remote_mode": "idle"}
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
