"""ROM-free checks of the subprocess battle input safety boundary."""

from types import SimpleNamespace

import pytest

from tests._tcp_trade_peer import _BattleMenuInput


class _ReadOnlyMemory:
    cursor = 0

    def __getitem__(self, address):
        assert address == 0xCC26
        return self.cursor

    def __setitem__(self, address, value):
        raise AssertionError("menu driver must never write game memory")


def _controller():
    memory = _ReadOnlyMemory()
    events = []
    session = SimpleNamespace(
        _pyboy=SimpleNamespace(memory=memory),
        symbols=SimpleNamespace(addr_of=lambda name: 0xCC26),
        hold=lambda button: events.append(("hold", button)),
        release=lambda button: events.append(("release", button)),
        press=lambda button, duration: events.append(("press", button, duration)),
    )
    return _BattleMenuInput(session), memory, events


def test_battle_waits_for_input_loop_and_rom_cursor_before_a():
    driver, memory, events = _controller()
    assert not driver.update(input_ready=False)
    assert events == []
    assert not driver.update(input_ready=True)
    assert events == [("hold", "down")]
    for _ in range(5):
        assert not driver.update(input_ready=True)
    assert events == [("hold", "down")]
    memory.cursor = 1  # The fake ROM, never the driver, changes its cursor.
    assert not driver.update(input_ready=True)
    assert events == [("hold", "down"), ("release", "down")]
    assert driver.update(input_ready=True)
    assert events[-1] == ("press", "a", 24)
    assert driver.update(input_ready=True)
    assert events.count(("press", "a", 24)) == 1


def test_battle_existing_colosseum_cursor_needs_no_direction():
    driver, memory, events = _controller()
    memory.cursor = 1
    assert not driver.update(input_ready=True)
    assert driver.update(input_ready=True)
    assert events == [("press", "a", 24)]


@pytest.mark.parametrize("cursor", [2, 255])
def test_battle_rejects_unexpected_cursor_and_releases_down(cursor):
    driver, memory, events = _controller()
    driver.update(input_ready=True)
    memory.cursor = cursor
    with pytest.raises(RuntimeError, match="unexpected battle LinkMenu cursor"):
        driver.update(input_ready=True)
    assert events == [("hold", "down"), ("release", "down")]


def test_battle_rechecks_cursor_after_direction_release():
    driver, memory, events = _controller()
    driver.update(input_ready=True)
    memory.cursor = 1
    driver.update(input_ready=True)
    memory.cursor = 0
    with pytest.raises(RuntimeError, match="cursor changed before A"):
        driver.update(input_ready=True)
    assert all(event[0] != "press" for event in events)


def test_battle_cleanup_releases_pending_direction_once():
    driver, _, events = _controller()
    driver.update(input_ready=True)
    driver.close()
    driver.close()
    assert events == [("hold", "down"), ("release", "down")]
