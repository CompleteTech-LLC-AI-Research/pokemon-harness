"""Asset-free CPU/HALT coverage for the network owner-dispatch path.

These tests use a tiny ROM program authored below rather than a Pokémon ROM.
The program arms the native serial port, enters ``HALT`` with serial interrupts
enabled, and records progress from the serial interrupt vector.  The PyBoy
endpoint is attached through :class:`PyBoyLinkSession`; all emulator state
observations happen on the thread that owns ``provider.step``.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest
from pyboy.core.serial import CYCLES_PER_BYTE_DMG, Serial

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession

# Reuse the repository's original 32 KiB, asset-free PyBoy fixture.  The
# program and interrupt vector are replaced in cartridge memory below.
from tests.test_serial_backend_boundary import emulator as _emulator_fixture  # noqa: F401

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


_PROGRAM_START = 0x0150
_SERIAL_VECTOR = 0x0058
_HALT_PC = 0x0167
_HALT_MARKER = 0xFFAB
_IRQ_MARKER = 0xFFAC


class _ObservedEdgeQueue(queue.Queue):
    """Queue probe used only to signal the owner when the reader enqueues."""

    def __init__(self, ready: threading.Event, *, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        self._ready = ready

    def put_nowait(self, item) -> None:
        super().put_nowait(item)
        self._ready.set()


def _program(*, internal_clock: bool) -> list[int]:
    """Return a tiny serial/interrupt program for the synthetic cartridge."""
    incoming = 0xA5 if internal_clock else 0x3C
    clock_control = 0x81 if internal_clock else 0x80
    return [
        0x31,
        0xFE,
        0xFF,  # LD SP,$FFFE
        0x3E,
        0x08,
        0xE0,
        0xFF,  # IE = serial interrupt
        0xAF,
        0xE0,
        0x0F,  # IF = 0
        0x3E,
        incoming,
        0xE0,
        0x01,  # SB = payload
        0x3E,
        clock_control,
        0xE0,
        0x02,  # SC = transfer enable + selected clock
        0x3E,
        0xA1,
        0xE0,
        0xAB,  # marker: armed and about to halt
        0xFB,  # EI
        0x76,  # HALT until the serial IRQ
        0x3E,
        0xA2,
        0xE0,
        0xAB,  # marker: HALT resumed after the IRQ
        0x18,
        0xFE,  # JR $016C
    ]


def _install_program(pyboy, *, internal_clock: bool) -> None:
    """Install the authored program/vector without any ROM-derived input."""
    program = _program(internal_clock=internal_clock)
    pyboy.memory[0, _PROGRAM_START : _PROGRAM_START + len(program)] = program
    # LD A,$B2; LDH [$FFAC],A; RETI; NOP.  The marker proves that the
    # hardware serial interrupt woke the CPU from HALT before the main ROM
    # path wrote its post-HALT marker.
    pyboy.memory[0, _SERIAL_VECTOR : _SERIAL_VECTOR + 6] = [
        0x3E,
        0xB2,
        0xE0,
        0xAC,
        0xD9,
        0x00,
    ]
    pyboy.memory[_HALT_MARKER] = 0
    pyboy.memory[_IRQ_MARKER] = 0
    pyboy.memory[0xFF0F] = 0
    pyboy.memory[0xFFFF] = 0
    pyboy.register_file.SP = 0xFFFE
    pyboy.register_file.PC = _PROGRAM_START


def _snapshot(pyboy) -> dict[str, int | bool]:
    """Read one complete owner-thread-only emulator observation."""
    serial = pyboy.mb.serial
    pc = int(pyboy.register_file.PC)
    return {
        # ``CPU.halted`` is a source-runtime-only Python field; the bundled
        # Cython CPU keeps it cdef-private.  The authored ROM leaves PC at the
        # HALT opcode, so this portable observation has the same meaning on
        # both runtimes without inspecting private native state.
        "pc": pc,
        "halted": pc == _HALT_PC,
        "sb": int(serial.SB),
        "sc": int(serial.SC),
        "transfer_enabled": bool(serial.transfer_enabled),
        "internal_clock": bool(serial.internal_clock),
        "if": int(pyboy.memory[0xFF0F]),
        "halt_marker": int(pyboy.memory[_HALT_MARKER]),
        "irq_marker": int(pyboy.memory[_IRQ_MARKER]),
        "frame": int(pyboy.frame_count),
    }


def _wait_for_edge_request(
    backend: NetworkBackend, ready: threading.Event, *, timeout: float = 3.0
) -> None:
    """Wait for the reader queue's event, without polling or sleeps."""
    deadline = time.monotonic() + timeout
    ready.clear()
    while backend._edge_queue.empty():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("network reader did not queue EDGE_REQ")
        if not ready.wait(timeout=remaining):
            raise AssertionError("network reader did not queue EDGE_REQ")
        ready.clear()
        if backend._closed:
            raise AssertionError("network backend closed before EDGE_REQ")


def _run_cpu_case(pyboy, *, internal_clock: bool) -> dict[str, object]:
    """Run one real PyBoy endpoint against a synthetic native Serial peer."""
    _install_program(pyboy, internal_clock=internal_clock)

    left, right = NetworkBackend.pair()
    if internal_clock:
        # The authored PyBoy is the master.  The peer is an external-clock
        # native Serial driven by the compatibility receiver worker; it never
        # touches the PyBoy endpoint.
        peer_backend = right
        attached_backend = left
        peer = Serial(backend=peer_backend)
        peer.set_SB(0x3C)
        peer.set_SC(0x80)
        peer_backend.start_receiver(local_core=peer)
    else:
        # The authored PyBoy is the external-clock owner.  The peer is the
        # internal-clock native Serial, whose requests must be dispatched to
        # the PyBoyLinkSession owner thread.
        peer_backend = left
        attached_backend = right
        peer_backend.start_receiver(local_core=None)
        peer = Serial(backend=peer_backend)
        peer.set_SB(0xA5)
        peer.set_SC(0x81)

    provider = PyBoyLinkSession(network_backend=attached_backend)
    edge_queued = threading.Event()
    if not internal_clock:
        attached_backend._edge_queue = _ObservedEdgeQueue(
            edge_queued, maxsize=attached_backend._edge_queue.maxsize
        )
    provider.attach(pyboy)
    assert attached_backend._dispatch_to_owner is True
    assert pyboy.mb.serial.owner_dispatch_enabled is True

    owner_id: list[int] = []
    service_threads: list[int] = []
    states: list[dict[str, int | bool]] = []
    owner_errors: list[BaseException] = []
    owner_done = threading.Event()
    ready = threading.Event()
    start_transfer = threading.Event()

    original_service = attached_backend.service_pending_edges

    def observe_service(*args, **kwargs):
        service_threads.append(threading.get_ident())
        return original_service(*args, **kwargs)

    attached_backend.service_pending_edges = observe_service

    irq_threads: list[int] = []
    irq_if_values: list[int] = []
    if not internal_clock:
        original_irq = attached_backend._irq_callback
        assert callable(original_irq)

        def observe_irq() -> None:
            irq_threads.append(threading.get_ident())
            original_irq()
            # The callback runs after the native serial byte latch and before
            # the next owner CPU boundary, so IF must contain the serial bit.
            irq_if_values.append(int(pyboy.memory[0xFF0F]))

        attached_backend._irq_callback = observe_irq

    def owner() -> None:
        owner_id.append(threading.get_ident())
        try:
            if not internal_clock:
                for _ in range(256):
                    provider.step(1)
                    state = _snapshot(pyboy)
                    states.append(state)
                    if (
                        state["halted"]
                        and state["transfer_enabled"]
                        and not state["internal_clock"]
                        and state["halt_marker"] == 0xA1
                    ):
                        ready.set()
                        break
                else:
                    raise AssertionError("synthetic ROM did not reach HALT")

                # The test thread clocks the peer only after the owner has
                # observed the ROM's real HALT state.
                if not start_transfer.wait(timeout=3.0):
                    raise AssertionError("owner transfer handoff timed out")

            for _ in range(256):
                if not internal_clock:
                    _wait_for_edge_request(attached_backend, edge_queued)
                provider.step(1)
                state = _snapshot(pyboy)
                states.append(state)
                if state["halt_marker"] == 0xA2:
                    owner_done.set()
                    return
            raise AssertionError("synthetic serial IRQ did not resume HALT")
        except BaseException as exc:  # noqa: BLE001 - reported by coordinator
            owner_errors.append(exc)
        finally:
            owner_done.set()

    owner_thread = threading.Thread(target=owner, name="test-pyboy-owner")
    owner_thread.start()

    try:
        if not internal_clock:
            assert ready.wait(timeout=5.0), "synthetic ROM did not reach HALT"
            start_transfer.set()
            peer.tick(peer.last_cycles + CYCLES_PER_BYTE_DMG)
        assert owner_done.wait(timeout=8.0), "owner thread did not finish"
        owner_thread.join(timeout=2.0)
        assert not owner_thread.is_alive()
        assert owner_errors == []
        assert owner_id and service_threads
        assert set(service_threads) == {owner_id[0]}

        reader = attached_backend._reader
        edge_worker = attached_backend._edge_worker
        assert reader is not None and edge_worker is not None
        assert owner_id[0] not in {reader.ident, edge_worker.ident}

        final = states[-1]
        assert final["halt_marker"] == 0xA2
        assert final["irq_marker"] == 0xB2
        assert final["halted"] is False
        assert final["transfer_enabled"] is False
        assert final["frame"] > 0
        if not internal_clock:
            ready_state = next(
                state
                for state in states
                if state["halt_marker"] == 0xA1 and state["halted"]
            )
            assert ready_state["halted"] is True
            assert ready_state["transfer_enabled"] is True
            assert ready_state["internal_clock"] is False
            assert final["pc"] != ready_state["pc"]
            assert irq_threads == [owner_id[0]]
            assert irq_if_values and irq_if_values[0] & 0x08

        assert not peer.transfer_enabled
        expected_peer_byte = 0xA5 if internal_clock else 0x3C
        expected_local_byte = 0x3C if internal_clock else 0xA5
        assert peer.SB == expected_peer_byte
        assert final["sb"] == expected_local_byte

        snapshot = attached_backend.debug_snapshot()
        peer_snapshot = peer_backend.debug_snapshot()
        assert snapshot["owner_edge_applied"] == (0 if internal_clock else 8)
        assert snapshot["edge_req_sent"] == (8 if internal_clock else 0)
        assert snapshot["edge_resp_received"] == (8 if internal_clock else 0)
        assert peer_snapshot["edge_req_received"] == (8 if internal_clock else 0)
        assert peer_snapshot["edge_resp_sent"] == (8 if internal_clock else 0)
        assert peer_snapshot["edge_req_sent"] == (0 if internal_clock else 8)
        assert peer_snapshot["edge_resp_received"] == (0 if internal_clock else 8)
    finally:
        # A failed owner assertion must still wake any admitted peer edge
        # before the session restores the native Serial backend.
        start_transfer.set()
        edge_queued.set()
        if not owner_done.is_set():
            attached_backend.stop(timeout_s=1.0)
            peer_backend.stop(timeout_s=1.0)
        if not owner_done.is_set():
            owner_done.wait(timeout=2.0)
        owner_thread.join(timeout=2.0)
        assert not owner_thread.is_alive(), "owner thread survived network teardown"
        try:
            provider.detach_all()
        finally:
            attached_backend.stop(timeout_s=1.0)
            peer_backend.stop(timeout_s=1.0)

    return {
        "owner_id": owner_id,
        "states": states,
        "irq_threads": irq_threads,
    }


def test_network_pyboy_external_clock_dispatch_wakes_halted_cpu(_emulator_fixture):  # noqa: F811
    """Peer-driven edges mutate native Serial only on the PyBoy owner."""
    _run_cpu_case(_emulator_fixture, internal_clock=False)


def test_network_pyboy_internal_clock_dispatch_completes_halted_cpu(_emulator_fixture):  # noqa: F811
    """The owner-thread PyBoy master completes a real HALT serial transfer."""
    _run_cpu_case(_emulator_fixture, internal_clock=True)
