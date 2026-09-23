from __future__ import annotations

"""Real-ROM serial smoke tests for :class:`PyBoyLinkSession`.

Split from ``tests/test_pyboy_link_session_roms.py`` (#132) with no behavior
change: the two Yellow serial-milestone tests moved here verbatim.
"""

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_core import SerialCore
from tests._pyboy_link_session_roms_support import (
    _LINK_CHUNK_CYCLES,
    _close_linked_pair,
    _CountingBackend,
    _open_session_pair,
    _open_yellow_session,
    _require_real_rom_link_runtime,
)

__all__ = [
    "_require_real_rom_link_runtime",
]

def test_yellow_pair_installs_serial_core():
    """Attach installs :class:`SerialCore` on both motherboards and
    wires the coordinator once both sides are in."""
    a, b = _open_session_pair(_open_yellow_session, _open_yellow_session)
    try:
        link = PyBoyLinkSession.local()

        core_a = link.attach(a._pyboy)
        assert isinstance(core_a, SerialCore)
        assert a._pyboy.mb.serial is core_a
        assert link.paired is False

        core_b = link.attach(b._pyboy)
        assert link.paired is True
        assert link.coordinator is not None
        # Each core's backend is the coordinator's CoordinatedBackend.
        from pokered_harness.link.serial_coordinator import CoordinatedBackend
        assert isinstance(core_a.backend, CoordinatedBackend)
        assert isinstance(core_b.backend, CoordinatedBackend)
    finally:
        _close_linked_pair(locals().get("link"), a, b)


def test_yellow_pair_exchanges_bytes_after_receptionist_A_press():
    """End-to-end smoke: press A on the receptionist on both sides and
    step enough frames for Pokémon's serial code to emit at least one
    edge through our :class:`SerialCore`.

    The Cable Club receptionist dialog stalls until the game sees
    ``SERIAL_CONNECTED`` on both sides, which requires the preamble
    exchange. In practice the receptionist code sets up and tears
    down the serial connection several times as it probes, so even
    a few seconds of game-time produces many edges.
    """
    a, b = _open_session_pair(_open_yellow_session, _open_yellow_session)
    try:
        link = PyBoyLinkSession.local()
        core_a = link.attach(a._pyboy)
        core_b = link.attach(b._pyboy)

        # Install counters on top of the coordinator's backends so we
        # can observe real serial activity.
        counter_a = _CountingBackend(core_a.backend)
        counter_b = _CountingBackend(core_b.backend)
        counter_a.peer_counter = counter_b
        counter_b.peer_counter = counter_a
        core_a.backend = counter_a
        core_b.backend = counter_b

        # Press A on both sides simultaneously to advance past any
        # dialogue prompt and into the receptionist flow.
        a.press("a", duration=6)
        b.press("a", duration=6)

        # Step both in lockstep. ~10 seconds of game-time is plenty
        # for Pokémon's Cable Club code to attempt its preamble
        # handshake; reduce if the test proves too slow.
        total_frames = 600
        step_chunk = 4
        for _ in range(total_frames // step_chunk):
            link.step_interleaved(
                step_chunk, chunk_cycles=_LINK_CHUNK_CYCLES
            )

        # The meaningful assertion: at least *some* serial activity
        # happened. Pokémon's Cable Club state includes the master
        # probe; we should see many edges on both sides.
        assert counter_a.total_edges > 0, (
            "A-side SerialCore saw zero activity after 600 frames — "
            "the ROM isn't driving our serial path"
        )
        assert counter_b.total_edges > 0, (
            "B-side SerialCore saw zero activity after 600 frames — "
            "the ROM isn't driving our serial path"
        )
        # And at least one full byte should have completed somewhere on
        # the link. The side acting as slave may receive bytes without
        # ever becoming master during this short receptionist phase.
        assert (
            counter_a.bytes_sent_complete
            + counter_a.bytes_received_complete
            + counter_b.bytes_sent_complete
            + counter_b.bytes_received_complete
        ) >= 1
    finally:
        _close_linked_pair(locals().get("link"), a, b)
