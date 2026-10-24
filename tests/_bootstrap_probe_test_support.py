"""Minimal serial/emulator doubles used by bootstrap behavior probes."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace


class ProbeSerialError(RuntimeError):
    """Typed failure surfaced by the synthetic serial core."""


class ProbeSerial:
    def __init__(self, _cgb: bool, *, defect: str = "") -> None:
        self.defect = defect
        self.backend = None
        self.backend_failed = False
        self.failure = None
        self.SB = self.SC = 0
        self._bits_remaining = 8
        self._shift_register = 0
        self.callback = None
        self.token = None
        self.owner_thread = None
        self.owner_pump_active = False
        self.owner_poll_enabled = False
        self.sequence = 0

    def set_SB(self, value: int) -> None:
        self.SB = self._shift_register = value

    def set_SC(self, value: int) -> None:
        self.SC = value

    def check_error(self) -> None:
        if self.backend_failed and self.defect != "no_raise":
            raise ProbeSerialError("synthetic serial callback failed") from self.failure

    def _emit(self, kind: int, *, cycles: int = 0, address: int = -1, value: int = -1) -> None:
        if self.callback is None or self.backend_failed:
            return
        self.sequence += 1
        event = (self.sequence, kind, cycles, 512, address, value, 0, self.SB, self.SC, 0, 8, False)
        if kind == 4 and self.defect == "malformed_poll":
            event = event[:6]
        elif kind == 4 and self.defect == "wrong_poll_kind":
            event = (event[0], 1, *event[2:])
        self.owner_pump_active = True
        try:
            self.callback(event)
        except (RuntimeError, SystemExit, AssertionError) as exc:
            self.failure = exc
            self.backend_failed = True
        finally:
            self.owner_pump_active = False

    def tick(self, cycles: int) -> bool:
        if self.backend is not None:
            if cycles < 512 or self.backend_failed:
                return False
            try:
                self.backend.on_edge(self.SB >> 7, 1)
            except RuntimeError as exc:
                self.failure = exc
                self.backend_failed = True
            return False
        self._emit(1, cycles=cycles)
        return False

    def set_owner_pump(self, callback, poll: bool = False) -> None:
        if self.owner_pump_active or self.token is not None:
            raise RuntimeError("owner callback is active or exclusively claimed")
        self.check_error()
        self.callback = callback
        self.owner_thread = threading.get_ident() if callback is not None else None
        self.owner_poll_enabled = callback is not None and poll

    def claim_owner_pump(self, callback, poll: bool = False):
        if self.owner_pump_active or self.callback is not None or self.token is not None:
            raise RuntimeError("owner callback is already installed or claimed")
        self.check_error()
        self.token = object()
        self.callback = callback
        self.owner_thread = threading.get_ident()
        self.owner_poll_enabled = poll
        return self.token

    def release_owner_pump(self, token) -> None:
        if self.owner_pump_active or token is not self.token:
            raise RuntimeError("invalid or active owner token")
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("owner token belongs to another thread")
        self.callback = self.token = self.owner_thread = None
        self.owner_poll_enabled = False


class AuthoredProbeEmulator:
    def __init__(self, path, *, defect: str, window: str, sound_emulated: bool) -> None:
        self.path = Path(path)
        cartridge = self.path.read_bytes()
        assert len(cartridge) == 32768 and cartridge[0x143] == 0x80
        assert cartridge[0x100:0x103] == b"\xc3\x50\x01"
        assert window == "null" and sound_emulated is False
        self.defect = defect
        self.serial = ProbeSerial(False, defect=defect)
        self.register_file = SimpleNamespace(PC=0, A=0)
        self.memory = self
        self.program = []
        self.physical = 0
        self.generation = 0
        self.double_speed = False
        self.key1 = 0
        self.incomplete_load = False
        self.saved_double_speed = False
        self.halted = False
        self.stopped = []
        self.mb = SimpleNamespace(
            serial=self.serial,
            lcd=SimpleNamespace(frame_done=False),
            get_physical_clock=self.clock,
            tick=self.tick,
        )

    def __setitem__(self, key, value) -> None:
        if isinstance(key, tuple):
            self.program = list(value)
        elif key == 0xFF4D:
            self.key1 = value

    def set_emulation_speed(self, speed: int) -> None:
        assert speed == 0

    def save_state(self, stream) -> None:
        self.saved_double_speed = self.double_speed
        stream.write(b"authored probe state")

    def load_state(self, stream) -> None:
        from pyboy.utils import PyBoyAssertException

        if not stream.read():
            self.incomplete_load = True
            self.generation += 1
            raise PyBoyAssertException("truncated authored state")
        self.generation += 1
        self.incomplete_load = False
        self.double_speed = self.saved_double_speed

    def clock(self):
        if self.incomplete_load:
            raise RuntimeError("state load is incomplete")
        if self.defect == "invalid_clock_shape":
            return self.generation, True
        return self.generation, self.physical

    def _advance(self) -> None:
        index = self.register_file.PC - 0x150
        opcode = self.program[index] if 0 <= index < len(self.program) else 0
        if opcode == 0x00:
            self.register_file.PC += 1
        elif opcode == 0x76:
            self.register_file.PC = self.register_file.PC or 0x151
        self.physical += 8

    def tick(self) -> None:
        if self.serial.owner_pump_active:
            raise RuntimeError("recursive CPU execution during owner callback")
        index = self.register_file.PC - 0x150
        opcode = self.program[index] if 0 <= index < len(self.program) else 0
        if opcode in (0xF0, 0xE0):
            self.physical += 8
            if self.serial.callback is not None and self.defect != "no_mmio_pump":
                self.serial._emit(1, address=0xFF01)
                self.serial.check_error()
            if opcode == 0xF0:
                self.register_file.A = self.serial.SB
            else:
                self.serial.SB = self.register_file.A
            self.register_file.PC = 0x152
            self.physical += 16
            return

        if self.serial.owner_poll_enabled:
            already_halted = self.halted
            if self.defect == "late_poll":
                self._advance()
            skip = self.defect == "no_poll" or (already_halted and self.defect == "no_halt_poll")
            if not skip:
                self.serial._emit(4, address=-1, value=-1)
                self.serial.check_error()
            if self.defect != "late_poll":
                if opcode == 0x76 or already_halted:
                    self.halted = True
                    self.physical += 8
                else:
                    self._advance()
            return

        if opcode == 0x10 and self.key1:
            self.double_speed = not self.double_speed
            self.key1 = 0
        self.physical += 4 if self.double_speed else 8

    def stop(self, *, save: bool) -> None:
        self.stopped.append(save)


def run_owner_clock_probe(bootstrap, *, defect: str = "") -> AuthoredProbeEmulator:
    created = []

    def factory(path, **options):
        emulator = AuthoredProbeEmulator(path, defect=defect, **options)
        created.append(emulator)
        return emulator

    try:
        bootstrap._verify_owner_clock_features(
            "source",
            SimpleNamespace(PyBoy=factory),
            SimpleNamespace(SerialBackendError=ProbeSerialError),
        )
    finally:
        assert len(created) == 1
        assert created[0].stopped == [False]
        assert not created[0].path.exists() and not created[0].path.parent.exists()
    return created[0]


def run_owner_poll_probe(
    bootstrap, *, defect: str = "", diagnostics: list[AuthoredProbeEmulator] | None = None
) -> AuthoredProbeEmulator:
    created = []

    def factory(path, **options):
        emulator = AuthoredProbeEmulator(path, defect=defect, **options)
        created.append(emulator)
        return emulator

    serial_module = SimpleNamespace(
        Serial=lambda cgb: ProbeSerial(cgb, defect=defect),
        SerialBackendError=ProbeSerialError,
    )
    try:
        bootstrap._verify_owner_poll_features(
            "source", SimpleNamespace(PyBoy=factory), serial_module
        )
    except Exception as exc:
        raise SystemExit(f"unsupported runtime: {exc}") from exc
    finally:
        for emulator in created:
            assert emulator.stopped == [False]
            assert not emulator.path.exists() and not emulator.path.parent.exists()
        if diagnostics is not None:
            diagnostics.extend(created)
    return created[0]
