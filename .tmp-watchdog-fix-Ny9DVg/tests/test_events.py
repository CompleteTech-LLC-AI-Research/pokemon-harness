from __future__ import annotations

import pytest

from pokered_harness.events import EventBus, GameEvent
from pokered_harness.symbols.loader import load_sym_text
from tests.fakes import FakePyBoy


def test_empty_bus_has_no_events(mem):
    bus = EventBus()
    assert len(bus) == 0
    assert bus.count("anything") == 0
    assert bus.latest("anything") is None
    assert bus.events_since(0) == []


def test_emit_records_event(mem):
    bus = EventBus()
    evt = bus.emit(tick=5, name="dialog_open", bank=0, addr=0x1234)
    assert isinstance(evt, GameEvent)
    assert evt.tick == 5
    assert bus.count("dialog_open") == 1
    assert bus.latest("dialog_open") is evt
    assert list(bus) == [evt]


def test_count_independent_per_name(mem):
    bus = EventBus()
    for t in (1, 2, 3):
        bus.emit(tick=t, name="a", bank=0, addr=0)
    bus.emit(tick=4, name="b", bank=0, addr=0)
    assert bus.count("a") == 3
    assert bus.count("b") == 1


def test_latest_reflects_most_recent_only(mem):
    bus = EventBus()
    bus.emit(tick=1, name="x", bank=0, addr=0)
    bus.emit(tick=9, name="x", bank=0, addr=0)
    latest = bus.latest("x")
    assert latest is not None
    assert latest.tick == 9


def test_events_since_is_strict_greater_than(mem):
    bus = EventBus()
    for t in (1, 2, 3, 4):
        bus.emit(tick=t, name="tick", bank=0, addr=0)
    got = [e.tick for e in bus.events_since(2)]
    assert got == [3, 4]


def test_clear_resets_everything(mem):
    bus = EventBus()
    bus.emit(tick=1, name="x", bank=0, addr=0)
    bus.clear()
    assert len(bus) == 0
    assert bus.count("x") == 0
    assert bus.latest("x") is None


def test_capacity_bounds_log_but_counts_keep_growing(mem):
    bus = EventBus(capacity=2)
    for t in range(5):
        bus.emit(tick=t, name="x", bank=0, addr=0)
    assert len(bus) == 2
    # The log is bounded (FIFO), but the "ever saw it" count is cumulative.
    assert bus.count("x") == 5


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        EventBus(capacity=0)


def test_register_resolves_symbol_and_hooks_pyboy(mem):
    sym = load_sym_text("02:4A12 DisplayTextID\n")
    pb = FakePyBoy(mem)
    bus = EventBus()
    current_tick = [0]

    bus.register(
        pyboy=pb,
        symbols=sym,
        symbol_name="DisplayTextID",
        event_name="dialog_open",
        tick_source=lambda: current_tick[0],
    )

    assert "DisplayTextID" in bus.registered_symbols
    # Firing the hook emits an event tagged with the current tick.
    current_tick[0] = 42
    fired = pb.fire(0x02, 0x4A12)
    assert fired == 1

    evt = bus.latest("dialog_open")
    assert evt is not None
    assert evt.tick == 42
    assert evt.bank == 0x02
    assert evt.addr == 0x4A12
