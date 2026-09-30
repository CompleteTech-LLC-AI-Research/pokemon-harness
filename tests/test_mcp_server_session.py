from __future__ import annotations

import base64
import json

import pytest

from pokered_harness.mcp_server import (
    DEFAULT_HOOKS,
    McpHarnessError,
    _error_code,
    _error_reply,
    dispatch_tool,
    read_resource,
    register_default_hooks,
)
from pokered_harness.session import (
    RomHashMismatch,
    RomNotFoundError,
    Session,
    SessionConfigurationError,
    SymbolHashMismatch,
    SymbolNotFoundError,
)
from pokered_harness.symbols.loader import load_sym_text
from tests._mcp_server_support import (
    _session,
)
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RomNotFoundError("missing ROM"), "rom_not_found"),
        (SymbolNotFoundError("missing symbols"), "symbol_not_found"),
        (RomHashMismatch("bad ROM hash"), "rom_hash_mismatch"),
        (SymbolHashMismatch("bad symbol hash"), "symbol_hash_mismatch"),
    ],
)
def test_session_asset_errors_keep_stable_mcp_codes(error, code):
    assert _error_code(error) == code
    reply = _error_reply(error)
    assert reply.isError is True
    assert reply.structuredContent is not None
    assert reply.structuredContent["error"]["code"] == code


def test_session_configuration_rejects_malformed_pin_types(tmp_path):
    rom = tmp_path / "game.gb"
    rom.write_bytes(b"rom")
    sym = tmp_path / "game.sym"
    sym.write_text("00:D35E wCurMap\n", encoding="utf-8")

    with pytest.raises(SessionConfigurationError) as exc_info:
        Session.from_files(
            rom,
            sym,
            expected_rom_sha1=object(),  # type: ignore[arg-type]
            pyboy_factory=lambda _path: FakePyBoy(DictMemory()),
        )

    assert _error_code(exc_info.value) == "invalid_session_configuration"

    with pytest.raises(SessionConfigurationError) as exc_info:
        Session.from_files(
            rom,
            sym,
            expected_pyboy_version=object(),  # type: ignore[arg-type]
            pyboy_factory=lambda _path: FakePyBoy(DictMemory()),
        )

    assert _error_code(exc_info.value) == "invalid_session_configuration"


def test_factory_missing_rom_is_reported_as_rom_not_found(tmp_path):
    rom = tmp_path / "game.gb"
    rom.write_bytes(b"rom")
    sym = tmp_path / "game.sym"
    sym.write_text("00:D35E wCurMap\n", encoding="utf-8")

    def disappearing_factory(_path):
        raise FileNotFoundError("ROM disappeared")

    with pytest.raises(RomNotFoundError) as exc_info:
        Session.from_files(rom, sym, pyboy_factory=disappearing_factory)

    assert _error_code(exc_info.value) == "rom_not_found"


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
    symbol_handle = s.register_hook_at("DisplayTextID", callback_calls.append, context="symbol")
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
    with pytest.raises(McpHarnessError) as exc_info:
        read_resource(s, "pokered://nope")
    assert exc_info.value.code == "invalid_resource"


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
