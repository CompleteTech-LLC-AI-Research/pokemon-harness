"""Original synthetic opcodes; no copyrighted ROM or fixture inputs."""
import queue
import threading

import pytest
from pyboy.core.serial import SerialBackendError

from pokered_harness.session import Session
from pokered_harness.symbols.loader import SymbolTable
from tests.test_serial_backend_boundary import emulator as emulator  # noqa: PLC0414

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


@pytest.mark.parametrize("opcode,length,read", [
    (0xE0, 2, False), (0xE2, 1, False), (0xEA, 3, False),
    (0xF0, 2, True), (0xF2, 1, True), (0xFA, 3, True),
])
@pytest.mark.parametrize("address", [0xFF01, 0xFF02])
def test_actual_opcode_stack_pauses_before_commit(emulator, opcode, length, read, address):
    p = emulator
    p.tick(1, False, False)
    program = [opcode]
    if length == 2:
        program += [address & 255]
    if length == 3:
        program += [address & 255, 255]
    program += [0x04]  # INC B must not execute while paused.
    p.memory[0, 0x150:0x150 + len(program)] = program
    p.register_file.PC = 0x150
    p.register_file.A = 0x2A
    p.register_file.B = 23
    p.register_file.C = address & 255
    s = p.mb.serial
    s.set_SB(0xA5)
    s.set_SC(0)
    before = (s.SB, s.SC)
    entered = threading.Event()
    commands = queue.Queue()
    worker_done = threading.Event()
    seen = []
    owner = threading.get_ident()
    session = Session(pyboy=p, symbols=SymbolTable([]))

    def producer():
        if entered.wait(3):
            commands.put(("grant",))  # immutable, no emulator access
        worker_done.set()

    worker = threading.Thread(target=producer)
    worker.start()

    def pump(event):
        assert threading.get_ident() == owner
        assert isinstance(event, tuple)
        assert event[1] == (1 if read else 2)
        assert event[4] == address
        assert event[5] == (-1 if read else 0x2A)
        assert (s.SB, s.SC) == before
        assert p.register_file.PC == 0x150
        assert p.register_file.A == 0x2A
        assert p.register_file.B == 23
        for recursive in (lambda: p.tick(1, False, False), p.mb.tick, session.step):
            with pytest.raises(RuntimeError, match="recursive CPU"):
                recursive()
        assert session.current_tick() == 0  # rejected step bookkeeping rolled back
        entered.set()
        assert commands.get(timeout=3) == ("grant",)
        assert (s.SB, s.SC) == before
        assert p.register_file.PC == 0x150
        seen.append(event)
        p.mb.lcd.frame_done = True

    s.set_owner_pump(pump)
    p.mb.breakpoint_singlestep = True
    p.mb.lcd.frame_done = False
    try:
        p.mb.tick()
    finally:
        worker.join(4)
    assert not worker.is_alive() and worker_done.is_set()
    assert len(seen) == 1
    assert p.register_file.PC == 0x150 + length
    assert p.register_file.B == 23
    if read:
        assert p.register_file.A == before[address - 0xFF01]
    elif address == 0xFF01:
        assert s.SB == 0x2A
    else:
        assert s.SC == 0x7E
    s.set_owner_pump(None)
    p.mb.lcd.frame_done = False
    p.mb.tick()
    assert p.register_file.B == 24  # preserved continuation, not replay


def test_queued_external_edges_are_applied_by_owner_before_read(emulator):
    p = emulator
    p.tick(1, False, False)
    p.memory[0, 0x150:0x153] = [0xF0, 1, 0x04]
    p.register_file.PC = 0x150
    s = p.mb.serial
    s.set_SB(0xA5)
    s.set_SC(0x80)
    events = queue.Queue()
    events.put(tuple((0x3C >> bit) & 1 for bit in range(7, -1, -1)))
    seen = []

    def pump(event):
        seen.append(event)
        assert event[7] == 0xA5
        completed = [s.apply_external_edge(bit) for bit in events.get_nowait()]
        assert completed == [False] * 7 + [True]
        p.mb.cpu.set_interruptflag(8)
        assert s.SB == 0x3C
        assert event[7] == 0xA5  # immutable old observation, not commit input
        p.mb.lcd.frame_done = True

    s.set_owner_pump(pump)
    p.mb.breakpoint_singlestep = True
    p.mb.lcd.frame_done = False
    p.mb.tick()
    assert len(seen) == 1  # external application does not recurse into pump
    assert p.register_file.A == 0x3C
    assert s._bits_remaining == 0
    s.set_owner_pump(None)
    assert p.memory[0xFF0F] & 8


@pytest.mark.parametrize("edge", [False, True])
def test_pump_exception_prevents_write_or_edge_commit(emulator, edge):
    p = emulator
    p.tick(1, False, False)
    s = p.mb.serial
    s.set_SB(0xA5)
    s.set_SC(0x81 if edge else 0)
    if edge:
        s.last_cycles = 0
        s.clock_target = 0
    p.memory[0, 0x150:0x153] = [0xE0, 1, 0x04]
    p.register_file.PC = 0x150
    p.register_file.A = 0x2A
    p.register_file.B = 23
    seen = []

    def pump(event):
        seen.append(event)
        if event[1] == (3 if edge else 2):
            assert s.SB == 0xA5
            assert s._shift_register == 0xA5
            raise ValueError("cancel before commit")

    s.set_owner_pump(pump)
    p.mb.lcd.frame_done = False
    with pytest.raises(SerialBackendError) as failure:
        p.mb.tick()
    assert isinstance(failure.value.__cause__, ValueError)
    assert s.SB == 0xA5 and s._shift_register == 0xA5
    assert s._bits_remaining == (8 if edge else 0)
    assert p.register_file.B == 23
    assert not s.owner_pump_active


def test_internal_edge_pause_precedes_sample_and_shift(emulator):
    p = emulator
    p.tick(1, False, False)
    s = p.mb.serial
    s.set_SB(0xA5)
    s.set_SC(0x81)
    s.last_cycles = 0
    s.clock_target = 0  # deliberately overdue: deadline != observed CPU time
    s.clock = 0
    seen = []

    class Backend:
        def on_edge(self, bit, role):
            assert seen[-1][0] == "pump"
            assert bit == 1 and role == 1
            seen.append(("sample",))
            return 0

    def pump(event):
        assert event[1] == 3
        assert event[2] > event[3]
        assert s._bits_remaining == 8
        assert s._shift_register == 0xA5
        seen.append(("pump", event))
        # Bound this synthetic catch-up to one edge.
        s.clock_target = event[2]
        p.mb.lcd.frame_done = True

    s.backend = Backend()
    s.set_owner_pump(pump)
    p.mb.breakpoint_singlestep = True
    p.mb.lcd.frame_done = False
    p.mb.tick()
    assert [record[0] for record in seen] == ["pump", "sample"]
    assert s._bits_remaining == 7 and s._shift_register == 0x4A
    assert s.clock_target == seen[0][1][2] + 512
