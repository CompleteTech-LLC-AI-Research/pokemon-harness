"""Frame ownership across grouped public calls on the actual PyBoy runtime."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from pyboy.plugins.base_plugin import PyBoyGameWrapper

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests.test_serial_backend_boundary import emulator as _emulator_fixture  # noqa: F401

pytestmark = pytest.mark.unit


@contextmanager
def _observe_frames(pyboy, monkeypatch, *, fail_at=None):
    """Observe frame boundaries without depending on TCP arrival timing."""
    backend, peer = NetworkBackend.pair()
    link = PyBoyLinkSession(network_backend=backend, network_is_internal_clock=True)
    session = Session(pyboy=pyboy, symbols=load_sym_text(""))
    turns = []
    post_ticks = []
    original_wrapper = pyboy._plugin_manager.generic_game_wrapper

    class RecordingGameWrapper(PyBoyGameWrapper):
        def post_tick(self):
            post_ticks.append(int(pyboy.frame_count))
            super().post_tick()

    def begin_frame(*, leader):
        assert leader is True
        turns.append(("begin", int(pyboy.frame_count)))

    def finish_frame(*, leader, progress_callback):
        assert leader is True
        assert callable(progress_callback)
        turns.append(
            (
                "end",
                int(pyboy.frame_count),
                bool(pyboy.mb.lcd.disable_renderer),
                bool(pyboy.mb.sound.disable_sampling),
            )
        )
        if fail_at is not None and pyboy.frame_count == fail_at:
            raise RuntimeError("frame completion failed")

    monkeypatch.setattr(backend, "begin_frame_turn", begin_frame)
    monkeypatch.setattr(backend, "finish_frame_turn", finish_frame)
    try:
        pyboy._plugin_manager.generic_game_wrapper = RecordingGameWrapper(pyboy, pyboy.mb, {})
        link.attach(pyboy)
        link.set_network_frame_barrier(True)
        yield session, link, backend, turns, post_ticks
    finally:
        pyboy._plugin_manager.generic_game_wrapper = original_wrapper
        link.detach_all()
        peer.stop(timeout_s=1.0)
        session.close()


@pytest.mark.parametrize("count", [1, 20, 120])
@pytest.mark.parametrize("entrypoint", ["session", "raw"])
def test_grouped_calls_keep_frame_turns_and_one_plugin_update(
    _emulator_fixture,  # noqa: F811
    monkeypatch,
    count,
    entrypoint,
):
    pyboy = _emulator_fixture
    start = int(pyboy.frame_count)
    with _observe_frames(pyboy, monkeypatch) as (session, _link, _backend, turns, post_ticks):
        if entrypoint == "session":
            session.step(count, render=True)
            sample_last_frame = True
            assert session.current_tick() == count
        else:
            assert pyboy.tick(count, True, False)
            sample_last_frame = False

        expected = []
        for offset in range(count):
            last = offset == count - 1
            expected.extend(
                [
                    ("begin", start + offset),
                    ("end", start + offset + 1, not last, not (sample_last_frame and last)),
                ]
            )
        assert turns == expected
        assert int(pyboy.frame_count) == start + count
        assert post_ticks == [start + count]


def test_zero_frame_call_updates_plugins_without_a_network_turn(
    _emulator_fixture,  # noqa: F811
    monkeypatch,
):
    pyboy = _emulator_fixture
    start = int(pyboy.frame_count)
    with _observe_frames(pyboy, monkeypatch) as (_session, _link, _backend, turns, post_ticks):
        assert not pyboy.tick(0, True, False)
        assert turns == []
        assert int(pyboy.frame_count) == start
        assert post_ticks == [start]


def test_frame_failure_aborts_the_remaining_batch(
    _emulator_fixture,  # noqa: F811
    monkeypatch,
):
    pyboy = _emulator_fixture
    start = int(pyboy.frame_count)
    with _observe_frames(pyboy, monkeypatch, fail_at=start + 2) as (
        session,
        link,
        backend,
        turns,
        post_ticks,
    ):
        with pytest.raises(RuntimeError, match="frame completion failed"):
            session.step(20)
        assert int(pyboy.frame_count) == start + 2
        assert len(turns) == 4
        assert post_ticks == []
        assert link._network_tick_active is False
        assert backend.connected is False
