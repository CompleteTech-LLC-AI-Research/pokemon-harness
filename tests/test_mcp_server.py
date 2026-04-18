from __future__ import annotations

import base64
import json

import pytest

from pokered_harness.events import EventBus
from pokered_harness.mcp_server import (
    DEFAULT_HOOKS,
    dispatch_tool,
    read_resource,
    register_default_hooks,
)
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy


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
