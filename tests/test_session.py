from __future__ import annotations

import hashlib

import pytest

from pokered_harness.events import EventBus
from pokered_harness.input import Button
from pokered_harness.session import (
    Session,
    VersionMismatch,
    sha1_of_file,
)
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy


# -- small helpers ---------------------------------------------------------


def _session() -> tuple[Session, FakePyBoy, EventBus]:
    mem = DictMemory()
    pb = FakePyBoy(mem)
    sym = load_sym_text(
        """
        00:D35E wCurMap
        00:D361 wYCoord
        00:D362 wXCoord
        00:D46A wWalkCounter
        00:CC26 wCurrentMenuItem
        00:CC28 wMaxMenuItem
        00:D057 wIsInBattle
        00:D163 wPartyCount
        00:D16B wPartyMons
        00:D356 wObtainedBadges
        00:D31D wNumBagItems
        00:D31E wBagItems
        02:4A12 DisplayTextID
        """
    )
    bus = EventBus()
    return Session(pyboy=pb, symbols=sym, event_bus=bus), pb, bus


# -- tick / step -----------------------------------------------------------


def test_step_advances_tick_and_forwards_to_pyboy():
    s, pb, _ = _session()
    assert s.current_tick() == 0
    s.step(5)
    assert s.current_tick() == 5
    assert pb.tick_calls == [(5, False)]


def test_step_rejects_non_positive():
    s, _, _ = _session()
    with pytest.raises(ValueError):
        s.step(0)


def test_step_render_flag_passthrough():
    s, pb, _ = _session()
    s.step(1, render=True)
    assert pb.tick_calls == [(1, True)]


# -- input ----------------------------------------------------------------


def test_press_validates_and_forwards():
    s, pb, _ = _session()
    s.press("a", duration=4)
    s.press(Button.START)
    assert pb.button_calls == [("a", 4), ("start", 1)]


def test_press_rejects_unknown_button():
    s, _, _ = _session()
    with pytest.raises(ValueError):
        s.press("turbo")


def test_hold_and_release_forward():
    s, pb, _ = _session()
    s.hold(Button.UP)
    s.release(Button.UP)
    assert pb.button_press_calls == ["up"]
    assert pb.button_release_calls == ["up"]


# -- save / load ---------------------------------------------------------


def test_save_state_roundtrip():
    s, _, _ = _session()
    blob = s.save_state()
    assert blob == b"STATE"
    # load_state accepts the bytes back — the fake re-reads them.
    s.load_state(blob)


def test_reset_tick():
    s, _, _ = _session()
    s.step(10)
    s.reset_tick(0)
    assert s.current_tick() == 0
    with pytest.raises(ValueError):
        s.reset_tick(-1)


# -- observation ---------------------------------------------------------


def test_read_game_state_reads_from_memory():
    s, pb, _ = _session()
    pb.memory[0xD35E] = 0x0B  # map id
    pb.memory[0xD362] = 5
    pb.memory[0xD361] = 10
    gs = s.read_game_state()
    assert gs.overworld.map_id == 0x0B
    assert (gs.overworld.x, gs.overworld.y) == (5, 10)


# -- run_until_event -----------------------------------------------------


def test_run_until_event_fires_before_budget():
    s, pb, bus = _session()
    bus.register(pb, s.symbols, "DisplayTextID", "dialog_open",
                 tick_source=s.current_tick)

    # After the second step chunk, fire the hook — the session polls
    # before the next chunk and returns.
    original_tick = pb.tick

    def ticker(count: int = 1, render: bool = False) -> bool:
        r = original_tick(count, render=render)
        if len(pb.tick_calls) == 2:
            pb.fire(0x02, 0x4A12)
        return r

    pb.tick = ticker  # type: ignore[assignment]

    result = s.run_until_event("dialog_open", max_ticks=100, chunk=8)

    assert result.reached is True
    assert result.event is not None
    assert result.event.name == "dialog_open"
    assert result.ticks_spent <= 100


def test_run_until_event_times_out():
    s, _, bus = _session()
    bus.register(
        FakePyBoy(DictMemory()),  # dummy — just to populate registered_symbols
        s.symbols,
        "DisplayTextID",
        "dialog_open",
        tick_source=s.current_tick,
    )
    result = s.run_until_event("dialog_open", max_ticks=32, chunk=8)
    assert result.reached is False
    assert result.event is None
    assert result.ticks_spent == 32


def test_run_until_event_only_counts_events_after_start_tick():
    s, pb, bus = _session()
    bus.register(pb, s.symbols, "DisplayTextID", "dialog_open",
                 tick_source=s.current_tick)

    # Pre-fire before the call — we should NOT return immediately.
    pb.fire(0x02, 0x4A12)
    assert bus.count("dialog_open") == 1

    # With no further firing, the run should time out.
    result = s.run_until_event("dialog_open", max_ticks=16, chunk=4)
    assert result.reached is False


def test_run_until_event_accepts_multiple_names():
    s, pb, bus = _session()
    bus.register(pb, s.symbols, "DisplayTextID", "dialog_open",
                 tick_source=s.current_tick)
    # Fake a second event by emitting directly.
    bus.emit(tick=100, name="trainer_engaged", bank=0, addr=0)
    result = s.run_until_event(
        ["dialog_open", "trainer_engaged"],
        max_ticks=8,
        chunk=2,
    )
    # trainer_engaged was emitted at tick=100 which is > start_tick=0,
    # so we match on the first poll.
    assert result.reached is True
    assert result.event is not None
    assert result.event.name == "trainer_engaged"


def test_run_until_event_rejects_empty_names():
    s, _, _ = _session()
    with pytest.raises(ValueError):
        s.run_until_event([], max_ticks=10)


def test_run_until_event_rejects_bad_budget():
    s, _, _ = _session()
    with pytest.raises(ValueError):
        s.run_until_event("x", max_ticks=0)
    with pytest.raises(ValueError):
        s.run_until_event("x", max_ticks=10, chunk=0)


# -- version enforcement ------------------------------------------------


def test_sha1_helper_and_mismatch_path(tmp_path):
    rom = tmp_path / "fake.gb"
    rom.write_bytes(b"not a real rom")
    expected = hashlib.sha1(b"not a real rom").hexdigest()
    assert sha1_of_file(rom) == expected

    sym = tmp_path / "fake.sym"
    sym.write_text("00:D35E wCurMap\n", encoding="utf-8")

    # Correct hash + dummy factory that returns a FakePyBoy.
    s = Session.from_files(
        rom,
        sym,
        expected_rom_sha1=expected,
        pyboy_factory=lambda path: FakePyBoy(DictMemory()),
    )
    assert s.symbols["wCurMap"].addr == 0xD35E

    # Wrong hash raises.
    with pytest.raises(VersionMismatch):
        Session.from_files(
            rom,
            sym,
            expected_rom_sha1="0" * 40,
            pyboy_factory=lambda path: FakePyBoy(DictMemory()),
        )


def test_close_stops_pyboy():
    s, pb, _ = _session()
    s.close()
    assert pb.stopped is True


def test_session_context_manager_closes_on_exit():
    mem = DictMemory()
    pb = FakePyBoy(mem)
    sym = load_sym_text("00:D35E wCurMap\n00:D361 wYCoord\n00:D362 wXCoord\n"
                        "00:D46A wWalkCounter\n00:CC26 wCurrentMenuItem\n"
                        "00:CC28 wMaxMenuItem\n00:D057 wIsInBattle\n"
                        "00:D163 wPartyCount\n00:D16B wPartyMons\n"
                        "00:D356 wObtainedBadges\n00:D31D wNumBagItems\n"
                        "00:D31E wBagItems\n")
    with Session(pyboy=pb, symbols=sym):
        assert pb.stopped is False
    assert pb.stopped is True
