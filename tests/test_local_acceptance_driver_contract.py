"""ROM-free contracts for the local acceptance-driver helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

import tests.test_pyboy_link_session_roms as rom_tests
from tests.test_pyboy_link_session_roms import (
    _close_linked_pair,
    _drive_two_sessions_to_link_menu,
    _move_link_menu_cursors_to_colosseum,
    _open_session_pair,
)


class _FakeLink:
    def __init__(
        self,
        events: list[str],
        *,
        detach_error: BaseException | None = None,
        clear_on_detach: bool = True,
    ) -> None:
        self._events = events
        self._detach_error = detach_error
        self._clear_on_detach = clear_on_detach
        self.attached = ("left", "right")

    def detach_all(self) -> None:
        self._events.append("detach")
        if self._detach_error is not None:
            raise self._detach_error
        if self._clear_on_detach:
            self.attached = ()


class _FakeSession:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self._name = name
        self._events = events
        self._close_error = close_error

    def close(self) -> None:
        self._events.append(f"close:{self._name}")
        if self._close_error is not None:
            raise self._close_error


def _sessions(events: list[str], **kwargs):
    return (
        _FakeSession("a", events, **kwargs.get("a", {})),
        _FakeSession("b", events, **kwargs.get("b", {})),
    )


def test_close_linked_pair_detaches_before_closing_both_sessions() -> None:
    events: list[str] = []
    link = _FakeLink(events)
    a, b = _sessions(events)

    _close_linked_pair(link, a, b)

    assert events == ["detach", "close:a", "close:b"]
    assert link.attached == ()


def test_close_linked_pair_closes_both_when_detach_fails() -> None:
    events: list[str] = []
    link = _FakeLink(events, detach_error=RuntimeError("detach failed"))
    a, b = _sessions(events)

    with pytest.raises(RuntimeError, match="detach failed"):
        _close_linked_pair(link, a, b)

    assert events == ["detach", "close:a", "close:b"]
    assert link.attached == ("left", "right")


def test_close_linked_pair_reports_retained_attachment_after_detach() -> None:
    events: list[str] = []
    link = _FakeLink(events, clear_on_detach=False)
    a, b = _sessions(events)

    with pytest.raises(RuntimeError, match="remained attached"):
        _close_linked_pair(link, a, b)

    assert events == ["detach", "close:a", "close:b"]
    assert link.attached == ("left", "right")


def test_close_linked_pair_closes_second_when_first_close_fails() -> None:
    events: list[str] = []
    link = _FakeLink(events)
    a, b = _sessions(
        events,
        a={"close_error": RuntimeError("first close failed")},
    )

    with pytest.raises(RuntimeError, match="first close failed"):
        _close_linked_pair(link, a, b)

    assert events == ["detach", "close:a", "close:b"]


def test_close_linked_pair_without_link_closes_both_sessions() -> None:
    events: list[str] = []
    a, b = _sessions(events)

    _close_linked_pair(None, a, b)

    assert events == ["close:a", "close:b"]


def test_close_linked_pair_adds_additional_cleanup_errors_as_notes() -> None:
    events: list[str] = []
    link = _FakeLink(events, detach_error=RuntimeError("detach failed"))
    a, b = _sessions(
        events,
        a={"close_error": RuntimeError("first close failed")},
        b={"close_error": RuntimeError("second close failed")},
    )

    with pytest.raises(RuntimeError, match="detach failed") as raised:
        _close_linked_pair(link, a, b)

    assert events == ["detach", "close:a", "close:b"]
    notes = getattr(raised.value, "__notes__", [])
    assert any("first close failed" in note for note in notes)
    assert any("second close failed" in note for note in notes)


def test_open_session_pair_closes_first_and_preserves_second_open_error() -> None:
    events: list[str] = []
    first = _FakeSession("first", events)
    second_error = RuntimeError("second session construction failed")

    def open_first():
        events.append("open:first")
        return first

    def open_second():
        events.append("open:second")
        raise second_error

    with pytest.raises(RuntimeError) as raised:
        _open_session_pair(open_first, open_second)

    assert raised.value is second_error
    assert events == ["open:first", "open:second", "close:first"]


class _DriverSession:
    """Small endpoint fake that rejects direct stepping by construction."""

    def __init__(self, name: str, events: list[tuple]) -> None:
        self.name = name
        self.events = events

    def press(self, button: str, *, duration: int) -> None:
        self.events.append(("press", self.name, button, duration))

    def step(self, *_args, **_kwargs):  # pragma: no cover - assertion path
        raise AssertionError("receptionist driver stepped an endpoint directly")


class _DriverLink:
    """Pair-owner fake recording both public stepping APIs and their budget."""

    def __init__(
        self,
        events: list[tuple],
        *,
        link_menu_after_frames: int | None = None,
        save_after_frames: int | None = None,
    ) -> None:
        self.events = events
        self.owner_frames = 0
        self.link_menu_after_frames = link_menu_after_frames
        self.save_after_frames = save_after_frames
        self.link_menu_buckets: dict[int, list[int]] = {}
        self.save_buckets: dict[int, list[int]] = {}

    def _advance(self, kind: str, frames: int) -> None:
        self.events.append((kind, frames))
        self.owner_frames += frames
        if (
            self.save_after_frames is not None
            and self.owner_frames >= self.save_after_frames
        ):
            for bucket in self.save_buckets.values():
                bucket[0] = 1
                bucket[1] = 1
        if (
            self.link_menu_after_frames is not None
            and self.owner_frames >= self.link_menu_after_frames
        ):
            for bucket in self.link_menu_buckets.values():
                bucket[0] = 1
                bucket[1] = 1

    def step(self, frames: int) -> None:
        self._advance("step", frames)

    def step_interleaved(self, frames: int, *, chunk_cycles: int) -> None:
        self.events.append(("chunk_cycles", chunk_cycles))
        self._advance("step_interleaved", frames)


class _CursorSymbols:
    addresses: ClassVar[dict[str, int]] = {
        "wCurrentMenuItem": 0xC100,
        "wMaxMenuItem": 0xC101,
        "wMenuWatchedKeys": 0xC102,
    }

    def addr_of(self, name: str) -> int:
        return self.addresses[name]


class _CursorMemory:
    """ROM-free memory view that rejects all writes."""

    def __init__(self, session: _CursorSession) -> None:
        self.session = session
        self.reads: list[int] = []

    def __getitem__(self, address: int) -> int:
        self.reads.append(address)
        values = {
            _CursorSymbols.addresses["wCurrentMenuItem"]: self.session.cursor,
            _CursorSymbols.addresses["wMaxMenuItem"]: 2,
            _CursorSymbols.addresses["wMenuWatchedKeys"]: self.session.watched_keys,
        }
        return values[address]

    def __setitem__(self, _address: int, _value: int) -> None:
        raise AssertionError("battle cursor driver must not write emulator memory")


class _CursorSession:
    """Endpoint fake with delayed ROM-owned menu readiness."""

    def __init__(
        self,
        name: str,
        events: list[tuple],
        *,
        initial_cursor: int = 0,
        ready_after_frames: int = 0,
    ) -> None:
        self.name = name
        self.events = events
        self.cursor = initial_cursor
        self.ready_after_frames = ready_after_frames
        self.owner_frames = 0
        self.watched_keys = 1 if ready_after_frames <= 0 else 0
        self._pending_button: str | None = None
        self.symbols = _CursorSymbols()
        self._pyboy = SimpleNamespace(memory=_CursorMemory(self))

    def press(self, button: str, *, duration: int) -> None:
        self.events.append(("press", self.name, button, duration))
        self._pending_button = button

    def step(self, *_args, **_kwargs):  # pragma: no cover - assertion path
        raise AssertionError("battle cursor driver stepped an endpoint directly")

    def advance(self, frames: int) -> None:
        self.owner_frames += frames
        if self.owner_frames >= self.ready_after_frames:
            self.watched_keys = 1
        if self.watched_keys and self._pending_button is not None:
            if self._pending_button == "down":
                self.cursor += 1
            elif self._pending_button == "up":
                self.cursor -= 1
            self._pending_button = None

    def read_game_state(self):
        return SimpleNamespace(overworld=SimpleNamespace(map_id=0))


class _CursorLink:
    """Pair owner that advances both cursor fakes without endpoint stepping."""

    def __init__(self, events: list[tuple], *sessions: _CursorSession) -> None:
        self.events = events
        self.sessions = sessions
        self.owner_frames = 0

    def step_interleaved(self, frames: int, **kwargs) -> None:
        self.events.append(("step_interleaved", frames, kwargs))
        self.owner_frames += frames
        for session in self.sessions:
            session.advance(frames)


def _run_receptionist_driver(
    monkeypatch,
    *,
    total_frames: int,
    frames_per_attempt: int = 20,
    serial_phase: bool = False,
    link_menu_after_frames: int | None = None,
    save_after_frames: int | None = None,
):
    events: list[tuple] = []
    a = _DriverSession("a", events)
    b = _DriverSession("b", events)
    link = _DriverLink(
        events,
        link_menu_after_frames=link_menu_after_frames,
        save_after_frames=save_after_frames,
    )

    def install_counter(_session, symbol: str, bucket: list[int], slot: int) -> None:
        if symbol == "SaveGameData" and serial_phase:
            bucket[slot] = 1
        if symbol == "SaveGameData":
            link.save_buckets[slot] = bucket
        if symbol == "LinkMenu":
            link.link_menu_buckets[slot] = bucket

    monkeypatch.setattr(rom_tests, "_install_hook_counter", install_counter)
    diagnostics = _drive_two_sessions_to_link_menu(
        a,
        b,
        link,
        total_frames=total_frames,
        frames_per_attempt=frames_per_attempt,
    )
    return events, link, diagnostics


def test_receptionist_driver_staggers_A_by_four_then_sixteen_owner_frames(
    monkeypatch,
) -> None:
    events, link, diagnostics = _run_receptionist_driver(
        monkeypatch,
        total_frames=100,
    )

    assert diagnostics["frames_used"] == 100
    assert link.owner_frames == 100
    assert events == [
        ("press", "a", "up", 6),
        ("press", "b", "up", 6),
        ("step", 20),
        ("press", "a", "up", 6),
        ("press", "b", "up", 6),
        ("step", 20),
        ("press", "a", "up", 6),
        ("press", "b", "up", 6),
        ("step", 20),
        ("press", "a", "a", 4),
        ("step", 4),
        ("press", "b", "a", 4),
        ("step", 16),
        ("press", "a", "a", 4),
        ("step", 4),
        ("press", "b", "a", 4),
        ("step", 16),
    ]


def test_receptionist_driver_reuses_saved_serial_phase_for_both_halves(
    monkeypatch,
) -> None:
    events, link, diagnostics = _run_receptionist_driver(
        monkeypatch,
        total_frames=80,
        serial_phase=True,
    )

    assert diagnostics["frames_used"] == 80
    assert link.owner_frames == 80
    assert events[-6:] == [
        ("press", "a", "a", 4),
        ("chunk_cycles", rom_tests._LINK_CHUNK_CYCLES),
        ("step_interleaved", 4),
        ("press", "b", "a", 4),
        ("chunk_cycles", rom_tests._LINK_CHUNK_CYCLES),
        ("step_interleaved", 16),
    ]
    assert events[-1] == ("step_interleaved", 16)


def test_receptionist_driver_freezes_phase_choice_before_first_A_advance(
    monkeypatch,
) -> None:
    # The SaveGameData milestone appears during A's four-frame lead. The
    # attempt must retain the coarse choice made before that first advance;
    # recomputing it before B's sixteen frames would change the schedule.
    events, link, diagnostics = _run_receptionist_driver(
        monkeypatch,
        total_frames=80,
        save_after_frames=64,
    )

    assert diagnostics["frames_used"] == 80
    assert link.owner_frames == 80
    assert events[-4:] == [
        ("press", "a", "a", 4),
        ("step", 4),
        ("press", "b", "a", 4),
        ("step", 16),
    ]


def test_receptionist_driver_consumes_no_more_than_the_2400_frame_budget(
    monkeypatch,
) -> None:
    events, link, diagnostics = _run_receptionist_driver(
        monkeypatch,
        total_frames=2400,
    )

    owner_advances = [event for event in events if event[0] == "step"]
    assert diagnostics["frames_used"] == 2400
    assert link.owner_frames == 2400
    assert sum(event[1] for event in owner_advances) == 2400
    assert all(event[1] > 0 for event in owner_advances)


def test_receptionist_driver_uses_pair_owner_for_every_advance(monkeypatch) -> None:
    events, link, diagnostics = _run_receptionist_driver(
        monkeypatch,
        total_frames=100,
        serial_phase=True,
    )

    assert diagnostics["frames_used"] == 100
    assert link.owner_frames == 100
    assert all(event[0] != "session_step" for event in events)
    assert {event[0] for event in events if event[0].startswith("step")} == {
        "step",
        "step_interleaved",
    }


def test_receptionist_driver_stops_before_next_attempt_after_early_link_menu(
    monkeypatch,
) -> None:
    events, link, diagnostics = _run_receptionist_driver(
        monkeypatch,
        total_frames=2400,
        link_menu_after_frames=80,
    )

    assert diagnostics["frames_used"] == 80
    assert link.owner_frames == 80
    assert [event for event in events if event[:3] == ("press", "a", "a")] == [
        ("press", "a", "a", 4),
    ]
    assert [event for event in events if event[:3] == ("press", "b", "a")] == [
        ("press", "b", "a", 4),
    ]


@pytest.mark.parametrize("frames_per_attempt", [0, 1, 2, 3, 4])
def test_receptionist_driver_rejects_attempt_without_post_B_frame(
    monkeypatch,
    frames_per_attempt: int,
) -> None:
    with pytest.raises(ValueError, match="post-B frame"):
        _run_receptionist_driver(
            monkeypatch,
            total_frames=80,
            frames_per_attempt=frames_per_attempt,
        )


def _run_cursor_driver(
    *,
    initial_a: int = 0,
    initial_b: int = 0,
    ready_a: int = 0,
    ready_b: int = 0,
    budget_frames: int = 40,
    frames_per_attempt: int = 20,
):
    events: list[tuple] = []
    a = _CursorSession(
        "a",
        events,
        initial_cursor=initial_a,
        ready_after_frames=ready_a,
    )
    b = _CursorSession(
        "b",
        events,
        initial_cursor=initial_b,
        ready_after_frames=ready_b,
    )
    link = _CursorLink(events, a, b)
    diagnostics = _move_link_menu_cursors_to_colosseum(
        a,
        b,
        link,
        budget_frames=budget_frames,
        frames_per_attempt=frames_per_attempt,
    )
    return events, a, b, link, diagnostics


def test_battle_cursor_driver_waits_for_delayed_endpoint_readiness() -> None:
    events, a, b, link, diagnostics = _run_cursor_driver(ready_b=20)

    assert diagnostics == {"frames_used": 40, "cursor_a": 1, "cursor_b": 1}
    assert link.owner_frames == 40
    assert events == [
        ("press", "a", "down", 12),
        ("step_interleaved", 20, {}),
        ("press", "b", "down", 12),
        ("step_interleaved", 20, {}),
    ]
    assert a._pyboy.memory.reads
    assert b._pyboy.memory.reads


def test_battle_cursor_driver_does_not_press_endpoint_already_at_target() -> None:
    events, _a, _b, link, diagnostics = _run_cursor_driver(initial_a=1)

    assert diagnostics["cursor_a"] == 1
    assert diagnostics["cursor_b"] == 1
    assert link.owner_frames == 20
    assert events == [
        ("press", "b", "down", 12),
        ("step_interleaved", 20, {}),
    ]


def test_battle_cursor_driver_exhausts_budget_without_overshoot() -> None:
    events, a, b, link, diagnostics = _run_cursor_driver(
        ready_a=100,
        ready_b=100,
        budget_frames=35,
        frames_per_attempt=20,
    )

    assert diagnostics == {"frames_used": 35, "cursor_a": None, "cursor_b": None}
    assert link.owner_frames == 35
    assert [event for event in events if event[0] == "step_interleaved"] == [
        ("step_interleaved", 20, {}),
        ("step_interleaved", 15, {}),
    ]
    assert a.cursor == 0 and b.cursor == 0


def test_battle_driver_keeps_settle_budget_and_cursor_assertion_boundary(
    monkeypatch,
) -> None:
    events: list[tuple] = []
    a = _CursorSession("a", events)
    b = _CursorSession("b", events)
    link = _CursorLink(events, a, b)
    monkeypatch.setattr(
        rom_tests,
        "_drive_two_sessions_to_link_menu",
        lambda *_args, **_kwargs: {
            "counters": {"LinkMenu": [1, 1]},
            "frames_used": 123,
        },
    )

    diagnostics = rom_tests._drive_past_link_menu_to_colosseum(
        a,
        b,
        link,
        post_link_menu_frames=0,
    )

    assert diagnostics["cursor_frames"] == 80
    assert diagnostics["frames_to_link_menu"] == 123
    assert link.owner_frames == 80
    assert events == [
        ("step_interleaved", 60, {}),
        ("press", "a", "down", 12),
        ("press", "b", "down", 12),
        ("step_interleaved", 20, {}),
    ]


def test_battle_driver_keeps_strict_cursor_assertion_after_budget_exhaustion(
    monkeypatch,
) -> None:
    events: list[tuple] = []
    a = _CursorSession("a", events, ready_after_frames=1000)
    b = _CursorSession("b", events, ready_after_frames=1000)
    link = _CursorLink(events, a, b)
    monkeypatch.setattr(
        rom_tests,
        "_drive_two_sessions_to_link_menu",
        lambda *_args, **_kwargs: {
            "counters": {"LinkMenu": [1, 1]},
            "frames_used": 123,
        },
    )

    with pytest.raises(AssertionError, match="cursor didn't land"):
        rom_tests._drive_past_link_menu_to_colosseum(
            a,
            b,
            link,
            post_link_menu_frames=0,
        )

    assert link.owner_frames == 100
    assert [event for event in events if event[0] == "step_interleaved"] == [
        ("step_interleaved", 60, {}),
        ("step_interleaved", 20, {}),
        ("step_interleaved", 20, {}),
    ]


@pytest.mark.parametrize("budget_frames", [0, 5, 19])
def test_battle_cursor_driver_respects_short_budget(budget_frames: int) -> None:
    events, _a, _b, link, diagnostics = _run_cursor_driver(
        ready_a=100,
        ready_b=100,
        budget_frames=budget_frames,
    )

    assert diagnostics["frames_used"] == budget_frames
    assert link.owner_frames == budget_frames
    assert sum(
        event[1] for event in events if event[0] == "step_interleaved"
    ) == budget_frames
