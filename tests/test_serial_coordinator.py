"""Tests for :class:`LockstepCoordinator` (milestone 3).

Verifies the coordinator's core promise: two :class:`SerialCore`
instances bound under a coordinator exchange bytes byte-for-byte
through the master's internal clock alone, without any external
scheduler. Covers edge cases called out in the design doc: peer
not armed, simultaneous master-master protocol error, role swap
mid-session, attach/detach idempotency.
"""

from __future__ import annotations

import pytest

from pokered_harness.link.serial_coordinator import (
    CoordinatedBackend,
    LockstepCoordinator,
)
from pokered_harness.link.serial_core import (
    CYCLES_PER_BYTE_DMG,
    CYCLES_PER_EDGE_DMG,
    NullBackend,
    SerialCore,
)


class _FailingBackendAssignmentCore:
    """Minimal core double for attach rollback without emulator state."""

    def __init__(self, backend, *, fail_assignment: bool = False) -> None:
        self._backend = backend
        self.fail_assignment = fail_assignment

    @property
    def backend(self):
        return self._backend

    @backend.setter
    def backend(self, value) -> None:
        if self.fail_assignment:
            raise RuntimeError("injected backend assignment failure")
        self._backend = value

# ---------------------------------------------------------------------------
# Basic pairing
# ---------------------------------------------------------------------------


def test_attach_installs_coordinated_backends_on_both_cores():
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)

    assert coord.attached
    assert isinstance(a.backend, CoordinatedBackend)
    assert isinstance(b.backend, CoordinatedBackend)
    assert a.backend.peer is b
    assert b.backend.peer is a


def test_coordinator_rejects_same_core_both_sides():
    a = SerialCore()
    with pytest.raises(ValueError, match="two distinct cores"):
        LockstepCoordinator(a, a)


def test_detach_restores_previous_backends():
    original_a = NullBackend()
    original_b = NullBackend()
    a = SerialCore(backend=original_a)
    b = SerialCore(backend=original_b)
    coord = LockstepCoordinator(a, b)

    coord.detach()

    assert not coord.attached
    assert a.backend is original_a
    assert b.backend is original_b


def test_detach_does_not_overwrite_a_backend_replaced_by_another_owner():
    original_b = NullBackend()
    a = SerialCore()
    b = SerialCore(backend=original_b)
    coord = LockstepCoordinator(a, b)
    replacement = NullBackend()
    a.backend = replacement

    coord.detach()

    assert a.backend is replacement
    assert b.backend is original_b
    assert not coord.attached


def test_detach_retains_state_for_retry_after_backend_restore_failure():
    """A transient native setter failure must not make cleanup unretryable."""
    original_a = NullBackend()
    original_b = NullBackend()
    a = _FailingBackendAssignmentCore(original_a)
    b = _FailingBackendAssignmentCore(original_b)
    coord = LockstepCoordinator(a, b)
    b.fail_assignment = True

    with pytest.raises(RuntimeError, match="could not be detached"):
        coord.detach()

    assert coord.attached is True
    assert a.backend is original_a
    assert isinstance(b.backend, CoordinatedBackend)

    b.fail_assignment = False
    coord.detach()
    assert coord.attached is False
    assert b.backend is original_b


def test_attach_detach_is_idempotent():
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)
    coord.attach()  # second attach is a no-op
    assert isinstance(a.backend, CoordinatedBackend)

    coord.detach()
    coord.detach()  # second detach is a no-op
    assert not coord.attached


def test_attach_rolls_back_if_the_second_core_rejects_assignment():
    original_a = NullBackend()
    original_b = NullBackend()
    a = _FailingBackendAssignmentCore(original_a)
    b = _FailingBackendAssignmentCore(original_b, fail_assignment=True)

    with pytest.raises(RuntimeError, match="injected backend assignment failure"):
        LockstepCoordinator(a, b)

    assert a.backend is original_a
    assert b.backend is original_b


def test_attach_keeps_backends_inactive_until_both_assignments_succeed():
    observed: list[bool] = []
    original_a = NullBackend()
    original_b = NullBackend()

    class ObservingCore(_FailingBackendAssignmentCore):
        @property
        def backend(self):
            return self._backend

        @backend.setter
        def backend(self, value) -> None:
            observed.append(getattr(value, "active", True))
            _FailingBackendAssignmentCore.backend.fset(self, value)

    a = ObservingCore(original_a)
    b = _FailingBackendAssignmentCore(original_b, fail_assignment=True)

    with pytest.raises(RuntimeError, match="injected backend assignment failure"):
        LockstepCoordinator(a, b)

    assert observed[0] is False
    assert a.backend is original_a


def test_detach_deactivates_stale_coordinated_backends():
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)
    stale_backend = a.backend

    b.set_SB(0x00)
    b.set_SC(0x80)
    coord.detach()

    assert isinstance(stale_backend, CoordinatedBackend)
    assert stale_backend.active is False
    assert stale_backend.on_edge(1, 1) == 1
    assert b._bits_remaining == 8


# ---------------------------------------------------------------------------
# Master/slave exchange through the master's tick loop
# ---------------------------------------------------------------------------


def test_master_tick_drives_full_byte_exchange():
    """Single master-side tick() moves both sides through all 8 edges."""
    a = SerialCore()
    b = SerialCore()
    LockstepCoordinator(a, b)

    a.set_SB(0xAA)
    b.set_SB(0x55)
    a.set_SC(0x81)  # A master
    b.set_SC(0x80)  # B slave

    irq = a.tick(CYCLES_PER_BYTE_DMG)

    assert irq is True
    assert a.SB == 0x55
    assert b.SB == 0xAA
    assert a.transfer_enabled == 0
    assert b.transfer_enabled == 0
    # Pan Docs: SC bit 7 auto-clears on both sides.
    assert a.SC & 0x80 == 0
    assert b.SC & 0x80 == 0


def test_either_side_can_be_master():
    """B-as-master / A-as-slave works symmetrically."""
    a = SerialCore()
    b = SerialCore()
    LockstepCoordinator(a, b)

    a.set_SB(0x12)
    b.set_SB(0x34)
    a.set_SC(0x80)
    b.set_SC(0x81)  # B master

    b.tick(CYCLES_PER_BYTE_DMG)

    assert a.SB == 0x34
    assert b.SB == 0x12


def test_edge_by_edge_progression_under_coordinator():
    """Each 128-cycle quantum advances both sides by exactly one bit."""
    a = SerialCore()
    b = SerialCore()
    LockstepCoordinator(a, b)

    a.set_SB(0xFF)  # MSB-first: 1,1,1,1,1,1,1,1
    b.set_SB(0x00)
    a.set_SC(0x81)
    b.set_SC(0x80)

    for edge in range(1, 8):
        a.tick(edge * CYCLES_PER_EDGE_DMG)
        # After `edge` bits exchanged: A has shifted in `edge` zeros
        # (LSB-aligned), B has shifted in `edge` ones.
        assert a.transfer_enabled == 1
        assert b.transfer_enabled == 1

    a.tick(CYCLES_PER_BYTE_DMG)
    assert a.SB == 0x00  # received all B's zeros
    assert b.SB == 0xFF  # received all A's ones


# ---------------------------------------------------------------------------
# Peer-not-armed: pull-up fallback
# ---------------------------------------------------------------------------


def test_master_reads_0xff_when_peer_not_armed():
    """Pan Docs disconnect semantics: unarmed peer = pulled-up line."""
    a = SerialCore()
    b = SerialCore()
    LockstepCoordinator(a, b)

    a.set_SB(0x42)
    a.set_SC(0x81)  # A arms and transmits
    # B does nothing — not armed.

    a.tick(CYCLES_PER_BYTE_DMG)

    assert a.SB == 0xFF
    # B never shifted anything.
    assert b.SB == 0xFF
    assert b.transfer_enabled == 0


def test_peer_in_master_mode_is_treated_as_disconnected():
    """Protocol error: both sides master. Coordinator falls back to pull-up."""
    a = SerialCore()
    b = SerialCore()
    LockstepCoordinator(a, b)

    a.set_SB(0xAA)
    b.set_SB(0x55)
    a.set_SC(0x81)
    b.set_SC(0x81)  # both master (error)

    # A advances through its own edges. Its backend sees B is also master
    # and returns pull-up (1) every edge. A ends up reading 0xFF.
    a.tick(CYCLES_PER_BYTE_DMG)
    assert a.SB == 0xFF
    # B remains armed — its own tick() hasn't advanced.
    assert b.transfer_enabled == 1


# ---------------------------------------------------------------------------
# Multi-byte sequences and role swaps
# ---------------------------------------------------------------------------


def test_three_consecutive_bytes_exchanged():
    """Each side rearms between bytes; the coordinator stays correct."""
    a = SerialCore()
    b = SerialCore()
    LockstepCoordinator(a, b)

    pairs = [(0x01, 0x02), (0x03, 0x04), (0xAA, 0x55)]
    for byte_a, byte_b in pairs:
        a.set_SB(byte_a)
        b.set_SB(byte_b)
        a.set_SC(0x81)
        b.set_SC(0x80)
        a.tick(a.last_cycles + CYCLES_PER_BYTE_DMG)
        assert a.SB == byte_b
        assert b.SB == byte_a


def test_role_swap_between_bytes():
    """After byte 1 (A=master) completes, B arms as master for byte 2."""
    a = SerialCore()
    b = SerialCore()
    LockstepCoordinator(a, b)

    # Byte 1: A master, B slave.
    a.set_SB(0x11)
    b.set_SB(0x22)
    a.set_SC(0x81)
    b.set_SC(0x80)
    a.tick(CYCLES_PER_BYTE_DMG)
    assert a.SB == 0x22 and b.SB == 0x11

    # Byte 2: swap roles.
    a.set_SB(0x33)
    b.set_SB(0x44)
    a.set_SC(0x80)
    b.set_SC(0x81)
    b.tick(b.last_cycles + CYCLES_PER_BYTE_DMG)
    assert a.SB == 0x44 and b.SB == 0x33


# ---------------------------------------------------------------------------
# advance_master helper
# ---------------------------------------------------------------------------


def test_advance_master_runs_the_current_master():
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)

    a.set_SB(0xAB)
    b.set_SB(0xCD)
    a.set_SC(0x81)
    b.set_SC(0x80)

    completed = coord.advance_master(CYCLES_PER_BYTE_DMG)
    assert completed is True
    assert a.SB == 0xCD and b.SB == 0xAB


def test_advance_master_no_op_when_no_master():
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)
    # Neither side is master.
    assert coord.advance_master(CYCLES_PER_BYTE_DMG) is False


def test_advance_master_no_op_when_both_master():
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)
    a.set_SB(0x00)
    b.set_SB(0x00)
    a.set_SC(0x81)
    b.set_SC(0x81)
    # Both master — protocol error; helper declines to advance.
    assert coord.advance_master(CYCLES_PER_BYTE_DMG) is False


@pytest.mark.parametrize("cycles", [0, -1])
def test_advance_master_rejects_non_positive_cycles(cycles: int):
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)

    with pytest.raises(ValueError, match="positive integer"):
        coord.advance_master(cycles)


def test_advance_master_rejects_boolean_cycles():
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)

    with pytest.raises(TypeError, match="positive integer"):
        coord.advance_master(True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CoordinatedBackend direct unit tests
# ---------------------------------------------------------------------------


def test_coordinated_backend_advances_peer_slave_by_one_edge():
    peer = SerialCore()
    peer.set_SB(0x00)
    peer.set_SC(0x80)  # armed as slave, waiting for clock

    backend = CoordinatedBackend(peer)
    # peek_out_bit = 0 (MSB of 0x00). After applying our bit=1, peer has
    # shifted in 1, advancing its bits_remaining from 8 to 7.
    returned = backend.on_edge(our_bit=1, our_role=1)
    assert returned == 0
    assert peer._bits_remaining == 7


def test_coordinated_backend_returns_pullup_if_peer_unarmed():
    peer = SerialCore()  # not armed
    backend = CoordinatedBackend(peer)
    assert backend.on_edge(our_bit=0, our_role=1) == 1
