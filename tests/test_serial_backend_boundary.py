"""Asset-free fault-boundary checks; synthetic program is authored here."""
import io
import pytest

from pyboy import PyBoy
from pyboy.core.serial import CYCLES_PER_EDGE_DMG, NullBackend, Serial, SerialBackendError


class BrokenBackend:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result
        self.error = error

    def on_edge(self, bit, role):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class BrokenIndex:
    def __index__(self):
        raise ValueError("broken bit conversion")


def serial_state(serial):
    return (serial.SB, serial.SC, serial._shift_register,
            serial._bits_remaining, serial.transfer_enabled,
            serial.clock, serial.last_cycles, serial.clock_target)


@pytest.mark.parametrize("backend, cause_type", [
    (BrokenBackend(error=RuntimeError("callback failure")), RuntimeError),
    (BrokenBackend(error=KeyboardInterrupt("cancel edge")), KeyboardInterrupt),
    (BrokenBackend(result=None), TypeError),
    (BrokenBackend(result=BrokenIndex()), ValueError),
])
def test_latched_error_is_first_cause_and_never_retries(backend, cause_type):
    serial = Serial(backend=backend)
    serial.set_SB(0xA5)
    serial.set_SC(0x81)
    before = serial_state(serial)
    assert serial.tick(serial.clock_target) is False
    assert serial.backend_failed
    assert serial_state(serial)[:5] == before[:5]
    with pytest.raises(SerialBackendError) as first:
        serial.check_error()
    assert isinstance(first.value.__cause__, cause_type)
    if backend.error is not None:
        assert first.value.__cause__ is backend.error
    failed = serial_state(serial)
    serial.set_SB(0)
    serial.set_SC(0)
    assert serial.tick(serial.last_cycles + 4096) is False
    assert serial_state(serial) == failed
    assert backend.calls == 1
    serial.backend = NullBackend()  # detach/replacement is not recovery
    assert serial.tick(serial.last_cycles + 8192) is False
    with pytest.raises(SerialBackendError) as repeated:
        serial.check_error()
    assert repeated.value.__cause__ is first.value.__cause__
    for operation in (lambda: serial.apply_external_edge(1),
                      lambda: serial.save_state(None),
                      lambda: serial.load_state(None, 99)):
        with pytest.raises(SerialBackendError):
            operation()
    fresh = Serial()
    fresh.set_SB(0)
    fresh.set_SC(0x81)
    assert fresh.tick(fresh.clock_target * 8)
    assert fresh.SB == 0xFF
    assert not fresh.backend_failed


@pytest.fixture
def emulator(tmp_path):
    # 32 KiB original ROM-only program, no Nintendo/Pokemon data.
    data = bytearray(32768)
    data[0x100:0x103] = b"\xc3\x50\x01"  # JP $0150
    data[0x150:0x153] = b"\xc3\x50\x01"  # JP $0150
    data[0x14D] = (-sum(data[0x134:0x14D]) - 25) & 255
    path = tmp_path / "original-fault-test.gb"
    path.write_bytes(data)
    pyboy = PyBoy(str(path), window="null", sound_emulated=False)
    pyboy.set_emulation_speed(0)
    pyboy.memory[0xFF50] = 1
    pyboy.register_file.PC = 0x150
    yield pyboy
    pyboy.stop(save=False)


@pytest.mark.parametrize("owner", ["tick", "motherboard", "memory_read", "memory_write", "slice_read", "slice_write"])
def test_owner_raises_and_quarantines(emulator, owner):
    pyboy = emulator
    if owner not in ("tick", "motherboard"):
        pyboy.tick(1, False, False)
    serial = pyboy.mb.serial
    backend = BrokenBackend(error=ValueError("native owner callback"))
    serial.backend = backend
    serial.set_SB(0xA5)
    serial.set_SC(0x81)
    if owner not in ("tick", "motherboard"):
        # Force the callback due at the current CPU timestamp, without
        # emulation or private/native CPU field writes.
        serial.clock_target = 0
        serial.last_cycles = 0
    actions = {
        "tick": lambda: pyboy.tick(3, False, False),
        "motherboard": pyboy.mb.tick,
        "memory_read": lambda: pyboy.memory[0xFF01],
        "memory_write": lambda: pyboy.memory.__setitem__(0xFF01, 3),
        "slice_read": lambda: pyboy.memory[0xFF01:0xFF03],
        "slice_write": lambda: pyboy.memory.__setitem__(slice(0xFF01, 0xFF03), [3, 0]),
    }
    frames_before = pyboy.frame_count
    with pytest.raises(SerialBackendError) as error:
        actions[owner]()
    assert pyboy.frame_count == frames_before
    assert error.value.__cause__ is backend.error
    assert backend.calls == 1
    assert serial._bits_remaining == 8
    assert serial.SB == 0xA5
    assert serial.SC & 0x80
    pc = pyboy.register_file.PC
    state = serial_state(serial)
    serial.backend = NullBackend()
    for action in (lambda: pyboy.tick(2, False, False), pyboy.mb.tick,
                   lambda: pyboy.tick(0, False, False),
                   lambda: pyboy.memory[0xC000],
                   lambda: pyboy.memory.__setitem__(0xC000, 42)):
        with pytest.raises(SerialBackendError):
            action()
        assert pyboy.register_file.PC == pc
        assert serial_state(serial) == state

    output = io.BytesIO()
    with pytest.raises(SerialBackendError):
        pyboy.save_state(output)
    assert output.getvalue() == b""
    input_state = io.BytesIO(b"invalid but never consumed")
    with pytest.raises(SerialBackendError):
        pyboy.load_state(input_state)
    assert input_state.tell() == 0
    assert pyboy.register_file.PC == pc
    assert serial_state(serial) == state
    pyboy.paused = True
    with pytest.raises(SerialBackendError):
        pyboy.tick(1, False, False)
    assert pyboy.frame_count == frames_before


def test_normal_transfer_still_sets_irq_after_byte_latch(emulator):
    pyboy = emulator
    pyboy.memory[0xFF0F] = 0
    pyboy.memory[0xFF01] = 0xA5
    pyboy.memory[0xFF02] = 0x81
    pyboy.tick(1, False, False)
    assert pyboy.memory[0xFF01] == 0xFF
    assert not pyboy.memory[0xFF02] & 0x80
    assert pyboy.memory[0xFF0F] & 8


@pytest.mark.parametrize("opcode", [0xF0, 0xE0])
def test_cpu_io_failure_bails_before_next_instruction(emulator, opcode):
    pyboy = emulator
    pyboy.tick(1, False, False)
    # LDH A,[$FF01] / LDH [$FF01],A followed by INC B.
    pyboy.memory[0, 0x150:0x153] = [opcode, 0x01, 0x04]
    pyboy.register_file.PC = 0x150
    pyboy.register_file.B = 23
    pyboy.register_file.A = 42
    serial = pyboy.mb.serial
    backend = BrokenBackend(error=ValueError("CPU I/O callback"))
    serial.backend = backend
    serial.set_SB(0xA5)
    serial.set_SC(0x81)
    serial.last_cycles = 0
    serial.clock_target = 0
    pyboy.mb.lcd.frame_done = False
    with pytest.raises(SerialBackendError):
        pyboy.mb.tick()
    assert backend.calls == 1
    assert pyboy.register_file.PC == 0x152
    assert pyboy.register_file.B == 23
    assert serial.SB == 0xA5
    assert serial._bits_remaining == 8


def test_failed_eighth_edge_does_not_latch_byte():
    class LastEdgeFailure:
        calls = 0

        def on_edge(self, bit, role):
            self.calls += 1
            if self.calls == 8:
                raise OSError("last edge failed")
            return 1

    backend = LastEdgeFailure()
    serial = Serial(backend=backend)
    serial.set_SB(0)
    serial.set_SC(0x81)
    assert serial.tick(serial.clock_target * 7) is False
    assert serial._bits_remaining == 1
    assert serial._shift_register == 0x7F
    assert serial.tick(serial.clock_target) is False
    with pytest.raises(SerialBackendError):
        serial.check_error()
    assert serial.SB == 0
    assert serial.SC & 0x80
    assert serial._bits_remaining == 1
    assert serial._shift_register == 0x7F
    assert backend.calls == 8


@pytest.mark.parametrize("raise_outer", [False, True])
def test_nested_fault_preserves_first_error_without_outer_shift(raise_outer):
    first = ValueError("first nested failure")

    class NestedBackend:
        calls = 0

        def on_edge(self, bit, role):
            self.calls += 1
            if self.calls == 2:
                raise first
            serial.tick(serial.last_cycles + CYCLES_PER_EDGE_DMG)
            if raise_outer:
                raise OSError("secondary outer failure")
            return 1

    backend = NestedBackend()
    serial = Serial(backend=backend)
    serial.set_SB(0xA5)
    serial.set_SC(0x81)
    assert serial.tick(serial.clock_target) is False
    with pytest.raises(SerialBackendError) as error:
        serial.check_error()
    assert error.value.__cause__ is first
    assert serial._bits_remaining == 8
    assert serial._shift_register == 0xA5
    assert backend.calls == 2


def fault_emulator(pyboy):
    serial = pyboy.mb.serial
    serial.backend = BrokenBackend(error=RuntimeError("fatal test backend"))
    serial.set_SC(0x81)
    with pytest.raises(SerialBackendError):
        pyboy.tick(1, False, False)
    assert serial.backend_failed


def test_faulted_public_stop_remains_usable_and_idempotent(emulator):
    fault_emulator(emulator)
    emulator.stop(save=False)
    emulator.stop(save=False)
    assert emulator.mb.serial.backend_failed  # teardown never clears the error


def test_faulted_session_close_releases_owner_without_memory_reads(emulator):
    from pokered_harness.session import Session
    from pokered_harness.symbols.loader import SymbolTable

    fault_emulator(emulator)
    session = Session(pyboy=emulator, symbols=SymbolTable([]))
    session.close(save=False)
    assert session.closed
    assert session._stop_complete
    assert emulator.mb.serial.backend_failed
    session.close(save=False)


def test_faulted_session_close_does_not_claim_success_if_stop_raises(emulator):
    from pokered_harness.session import Session
    from pokered_harness.symbols.loader import SymbolTable

    fault_emulator(emulator)

    class StopOnceFails:
        calls = 0

        @property
        def memory(self):
            raise AssertionError("cleanup must not read faulted memory")

        def stop(self, *, save):
            assert save is False
            self.calls += 1
            if self.calls == 1:
                raise OSError("stop did not complete")
            emulator.stop(save=False)

    proxy = StopOnceFails()
    session = Session(pyboy=proxy, symbols=SymbolTable([]))
    with pytest.raises(OSError, match="stop did not complete"):
        session.close(save=False)
    assert session.closed
    assert not session._stop_complete
    session.close(save=False)
    assert session._stop_complete
    assert proxy.calls == 2
    session.close(save=False)
    assert proxy.calls == 2
