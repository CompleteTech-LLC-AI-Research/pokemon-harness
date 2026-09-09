"""Inactive SC clock-select bits must not block cooperative CPU progress."""

import pytest

from pokered_harness.link.serial_core import CYCLES_PER_BYTE_DMG, SerialCore
from pokered_harness.link.serial_coordinator import CoordinatedBackend


@pytest.mark.parametrize("completed", [False, True])
def test_inactive_former_master_can_rearm_as_external(completed):
    peer = SerialCore()
    peer.set_SC(0x81 if completed else 0x01)
    if completed:
        assert peer.tick(CYCLES_PER_BYTE_DMG) is True
    assert peer.internal_clock and not peer.transfer_enabled
    calls = []

    def advance_cpu():
        calls.append(None)
        if len(calls) == 2:
            peer.set_SB(0x00)
            peer.set_SC(0x80)
        return True

    backend = CoordinatedBackend(peer, on_peer_unarmed=advance_cpu)
    assert backend.prepare_peer_for_edge() is True
    assert len(calls) == 2
    assert backend.peer_rearm_attempts == 2
    assert backend.peer_rearm_successes == 1
    assert backend.on_edge(1, 1) == 0
    assert peer._bits_remaining == 7


def test_inactive_former_master_progress_stays_bounded():
    peer = SerialCore()
    peer.set_SC(0x01)
    calls = []

    def advance_cpu():
        calls.append(None)
        return True

    backend = CoordinatedBackend(peer, on_peer_unarmed=advance_cpu)
    assert backend.prepare_peer_for_edge() is False
    assert len(calls) == 256
    assert backend.peer_rearm_successes == 0


def test_rearmed_master_is_not_treated_as_external_peer():
    peer = SerialCore()
    peer.set_SC(0x01)
    calls = []

    def advance_cpu():
        calls.append(None)
        peer.set_SC(0x81)
        return True

    backend = CoordinatedBackend(peer, on_peer_unarmed=advance_cpu)
    assert backend.prepare_peer_for_edge() is False
    assert len(calls) == 1
    assert peer._bits_remaining == 8
    assert backend.peer_rearm_successes == 0


def test_inactive_former_master_stops_when_cpu_cannot_progress():
    peer = SerialCore()
    peer.set_SC(0x01)
    calls = []

    def advance_cpu():
        calls.append(None)
        return False

    backend = CoordinatedBackend(peer, on_peer_unarmed=advance_cpu)
    assert backend.prepare_peer_for_edge() is False
    assert len(calls) == 1
