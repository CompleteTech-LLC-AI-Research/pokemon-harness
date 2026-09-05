"""Actual source opcode retirement; optional compiled CPU/MB integration.

Source cases use a memory seam, never a replacement CPU or retirement count.
Native cases author their own cartridge and require compiled runtime types.
Pytest collects only source cases and opens no cartridge. Run native cases with
``python tests/test_cpu_instruction_counter.py --native-probe`` after a build.
Native illegal opcodes and overflow remain unverified: private fetch/counter
access needs a typed probe, and ticking an illegal opcode can loop forever.
"""

import argparse
import ast
import importlib.machinery
import importlib.util
import io
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

CORE = Path(__file__).resolve().parents[1] / "vendor/pyboy-src/pyboy/core"
UINT64_MAX = (1 << 64) - 1


def _source(name):
    spec = importlib.util.spec_from_file_location(
        f"pyboy.core._counter_{name}", CORE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Compiled Logger's C call signature is not the Python source logging API.
    # Replace only this isolated module's logger, never the runtime's globals.
    module.logger = logging.getLogger(f"counter_source.{name}")
    return module


class Memory:
    bootrom_enabled = True  # Exercise getitem fetch, without a cartridge.
    cgb = False

    def __init__(self):
        self.data = bytearray(65536)

    def getitem(self, address):
        return self.data[address]

    def setitem(self, address, value):
        self.data[address] = value

    setitem_io_ports = setitem


@pytest.fixture
def cpu():
    module = _source("cpu")
    module.opcodes = _source("opcodes")
    memory = Memory()
    instance = module.CPU(memory)
    memory.cpu = instance
    instance.PC = 0xC000
    instance.SP = 0xD000
    return instance


def _program(cpu, code):
    cpu.mb.data[cpu.PC : cpu.PC + len(code)] = bytes(code)


@pytest.mark.parametrize(
    "code,pc_delta,cycles",
    [
        ([0x00], 1, 4),
        ([0xCB, 0x00], 2, 8),
        ([0x18, 0xFE], 0, 12),
        ([0x10, 0x00], 2, 4),
    ],
)
def test_source_success_counts_once(cpu, code, pc_delta, cycles):
    assert cpu.retired_instructions == 0
    _program(cpu, code)
    before = cpu.PC
    cpu.B = 0x80
    assert cpu.fetch_and_execute() is None  # Keep the source dispatcher's return value.
    assert (cpu.retired_instructions, cpu.PC, cpu.cycles) == (1, before + pc_delta, cycles)
    if code[0] == 0xCB:
        assert (cpu.B, cpu.F) == (1, 0x10)


def test_source_halt_opcode_then_idle(cpu):
    _program(cpu, [0x76])
    cpu.tick(4)
    assert (cpu.retired_instructions, cpu.cycles, cpu.halted) == (1, 4, True)
    pc = cpu.PC
    cpu.tick(20)
    assert (cpu.retired_instructions, cpu.cycles, cpu.PC) == (1, 24, pc)


@pytest.mark.parametrize("halted", [False, True])
def test_source_interrupt_only_does_not_retire(cpu, halted):
    cpu.halted = halted
    cpu.interrupt_master_enable = True
    cpu.interrupts_enabled_register = cpu.interrupts_flag_register = 1
    cpu.tick(4)
    assert (cpu.retired_instructions, cpu.cycles, cpu.PC, cpu.SP) == (0, 20, 0x40, 0xCFFE)
    assert not cpu.halted


@pytest.mark.parametrize(
    "opcode", [0xDB, 0xD3, 0xDD, 0xE3, 0xE4, 0xEB, 0xEC, 0xED, 0xF4, 0xFC, 0xFD]
)
def test_source_break_and_illegal_fetch_does_not_retire(cpu, opcode):
    cpu.fetch_and_execute()  # Establish a real prior retirement.
    _program(cpu, [opcode])
    before = (cpu.retired_instructions, cpu.cycles, cpu.PC)
    assert cpu.fetch_and_execute() == 0  # Never tick: illegal opcodes consume no cycles.
    assert (cpu.retired_instructions, cpu.cycles, cpu.PC) == before


@pytest.mark.parametrize("failure", ["fetch", "handler", "register_callback"])
def test_source_failure_preserves_prior_count_and_partial_clock(cpu, monkeypatch, failure):
    cpu.fetch_and_execute()
    _program(cpu, [0xE0, 0x01])  # LDH increments the clock by 4 before its IO write.
    pc, clock = cpu.PC, cpu.cycles

    def fail(*args):
        raise RuntimeError("counter sentinel")

    if failure == "fetch":
        monkeypatch.setattr(cpu.mb, "getitem", fail)
    elif failure == "handler":
        # Real dispatcher and handler; the invalid register fails inside LDH.
        cpu.cycles = None
    else:
        monkeypatch.setattr(cpu.mb, "setitem_io_ports", fail)
    with pytest.raises(TypeError if failure == "handler" else RuntimeError):
        cpu.fetch_and_execute()
    assert (cpu.retired_instructions, cpu.PC) == (1, pc)
    assert cpu.cycles == (
        None if failure == "handler" else clock + (4 if failure == "register_callback" else 0)
    )


def test_source_save_load_leaves_lifetime_counter_unchanged(cpu, monkeypatch):
    from pyboy.utils import STATE_VERSION, IntIOWrapper

    cpu.fetch_and_execute()
    saved = io.BytesIO()
    cpu.save_state(IntIOWrapper(saved))
    assert cpu.retired_instructions == 1
    cpu.fetch_and_execute()
    # dump_state reads unrelated display/audio devices absent from this memory seam.
    monkeypatch.setattr(cpu, "dump_state", lambda labels: "")
    saved.seek(0)
    cpu.load_state(IntIOWrapper(saved), STATE_VERSION)
    assert (cpu.retired_instructions, cpu.cycles, cpu.PC) == (2, 4, 0xC001)
    again = io.BytesIO()
    cpu.save_state(IntIOWrapper(again))
    assert again.getvalue() == saved.getvalue()  # Counter is absent from the wire format.


def test_source_overflow_precedes_fetch_and_any_mutation(cpu, monkeypatch):
    cpu.retired_instructions = UINT64_MAX - 1
    cpu.fetch_and_execute()
    assert cpu.retired_instructions == UINT64_MAX
    before = vars(cpu).copy()
    memory = bytes(cpu.mb.data)

    def forbidden_fetch(address):
        pytest.fail("overflow must fail before even fetching")

    monkeypatch.setattr(cpu.mb, "getitem", forbidden_fetch)
    with pytest.raises(OverflowError, match="retired_instructions counter overflow"):
        cpu.fetch_and_execute()
    assert vars(cpu) == before
    assert bytes(cpu.mb.data) == memory


def test_source_exception_declarations_and_generator_literals():
    cpu_pxd = (CORE / "cpu.pxd").read_text()
    assert "cdef readonly uint64_t retired_instructions" in cpu_pxd
    for filename, names in {
        "cpu.pxd": ("fetch_and_execute", "tick", "check_interrupts", "handle_interrupt"),
        "mb.pxd": (
            "tick",
            "getitem",
            "setitem",
            "getitem_io_ports",
            "setitem_io_ports",
            "transfer_DMA",
            "set_hdma5",
        ),
    }.items():
        declarations = (CORE / filename).read_text().splitlines()
        for name in names:
            matches = [line for line in declarations if re.search(rf"\b{name}\(", line)]
            assert matches, (filename, name)
            assert all("except *" in line and "noexcept" not in line for line in matches), matches

    pxd = (CORE / "opcodes.pxd").read_text()
    declarations = [
        line for line in pxd.splitlines() if line.startswith("cdef ") and "(cpu.CPU" in line
    ]
    assert len(declarations) == 504
    for line in declarations:
        sentinel = re.search(r"\b(BRK|no_opcode)\(", line)
        assert ("noexcept nogil" if sentinel else "except * nogil") in line
    # Parse literals only: importing this generator would fetch external opcode data.
    tree = ast.parse((CORE / "opcodes_gen.py").read_text())
    cimports = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "cimports" for t in node.targets)
    )
    for line in cimports.splitlines():
        if "(cpu.CPU" in line:
            assert line in pxd
    templates = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("cdef uint8_t %s_")
    ]
    assert len(templates) == 2
    assert all("except * nogil" in value for value in templates)


@contextmanager
def native_game(tmp_path):
    from pyboy import PyBoy
    from pyboy.core import cpu as runtime_cpu
    from pyboy.core import mb as runtime_mb

    for module in (runtime_cpu, runtime_mb):
        if not any(
            str(module.__file__).endswith(suffix)
            for suffix in importlib.machinery.EXTENSION_SUFFIXES
        ):
            raise RuntimeError("native probe requires compiled CPU and Motherboard")
    # Authored ROM-only 32 KiB cartridge; no Nintendo/game assets or BYO files.
    rom = bytearray(0x8000)
    rom[0x134:0x13B] = b"COUNTER"
    rom[0x14D] = (-sum(rom[0x134:0x14D]) - 25) & 0xFF
    path = tmp_path / "counter.gb"
    path.write_bytes(rom)
    game = PyBoy(str(path), window="null", sound_emulated=False)
    try:
        assert type(game.mb) is runtime_mb.Motherboard
        assert type(game.mb.cpu) is runtime_cpu.CPU
        assert game.mb.cpu.retired_instructions == 0  # Missing compiled API is a failure.
        game.memory[0xFF50] = 1
        game.memory[0xFFFF] = 0
        game.memory[0xFF0F] = 0
        game.register_file.PC = 0xC000
        game.register_file.SP = 0xD000
        game.mb.breakpoint_singlestep = True
        yield game
    finally:
        game.stop(save=False)


def probe_native_real_opcode_retirement(native_game, code, delta, cycles):
    game = native_game
    game.memory[0xC000 : 0xC000 + len(code)] = code
    clock = game.mb.cpu.cycles
    game.mb.tick()
    assert game.mb.cpu.retired_instructions == 1
    assert game.register_file.PC == 0xC000 + delta
    assert game.mb.cpu.cycles == clock + cycles
    with pytest.raises(AttributeError):
        game.mb.cpu.retired_instructions = 0


def probe_native_interrupt_only_then_isr_retirement(native_game):
    game = native_game
    game.memory[0xC000] = 0xFB  # EI with IE/IF clear; enables the real CPU's IME.
    game.mb.tick()
    assert game.mb.cpu.retired_instructions == 1
    assert game.register_file.PC == 0xC001
    game.memory[0xFFFF] = game.memory[0xFF0F] = 1
    clock = game.mb.cpu.cycles
    game.mb.tick()  # Four-clock target is exhausted by the 20-clock IRQ entry.
    assert (game.mb.cpu.retired_instructions, game.mb.cpu.cycles) == (1, clock + 20)
    assert (game.register_file.PC, game.register_file.SP) == (0x40, 0xCFFE)
    assert game.memory[0xCFFE:0xD000] == [0x01, 0xC0]
    game.mb.tick()  # The authored cartridge contains NOP at the interrupt vector.
    assert (game.mb.cpu.retired_instructions, game.mb.cpu.cycles) == (2, clock + 24)
    assert game.register_file.PC == 0x41


def probe_native_break_does_not_retire(native_game):
    game = native_game
    game.mb.tick()  # Establish a real prior NOP retirement.
    game.memory[0xC001] = 0xDB  # BRK sets bail; unlike illegal opcodes this terminates.
    clock = game.mb.cpu.cycles
    game.mb.tick()
    assert (game.mb.cpu.retired_instructions, game.mb.cpu.cycles) == (1, clock)
    assert game.register_file.PC == 0xC001
    assert game.mb.breakpoint_singlestep


def probe_native_halt_idle_and_save_load(native_game):
    game = native_game
    game.memory[0xC000] = 0x76
    game.mb.tick()
    assert game.mb.cpu.retired_instructions == 1
    clock = game.mb.cpu.cycles
    game.mb.tick()
    assert (game.mb.cpu.retired_instructions, game.mb.cpu.cycles) == (1, clock + 4)
    saved = io.BytesIO()
    game.save_state(saved)
    assert game.mb.cpu.retired_instructions == 1
    # Wake HALT using pending enabled interrupt, with IME still off.
    game.memory[0xFFFF] = game.memory[0xFF0F] = 1
    game.mb.tick()
    count = game.mb.cpu.retired_instructions
    assert count == 2
    saved.seek(0)
    game.load_state(saved)
    assert game.mb.cpu.retired_instructions == count


def probe_native_serial_callback_failure_preserves_partial_clock(native_game):
    game = native_game
    game.mb.tick()  # NOP: a real prior retirement.
    game.memory[0xC001:0xC003] = [0xE0, 0x01]
    serial = game.mb.serial
    calls = []

    class FailingBackend:
        def on_edge(self, bit, clock):
            calls.append((bit, clock))
            raise RuntimeError("serial counter sentinel")

    game.memory[0xFF01] = 0x80
    game.memory[0xFF02] = 0x81
    serial.backend = FailingBackend()
    # Arrange an edge during LDH's IO access, after its first four clocks.
    serial.clock_target = serial.clock + 4
    clock = game.mb.cpu.cycles
    with pytest.raises(RuntimeError, match="serial counter sentinel"):
        game.mb.tick()
    assert calls == [(1, 1)]
    assert game.mb.cpu.retired_instructions == 1
    assert game.mb.cpu.cycles == clock + 4
    assert serial.last_cycles == clock + 4
    assert game.register_file.PC == 0xC001


def _run_native_probe():
    cases = [
        ("NOP", probe_native_real_opcode_retirement, ([0], 1, 4)),
        ("CB", probe_native_real_opcode_retirement, ([0xCB, 0], 2, 8)),
        ("self-jump", probe_native_real_opcode_retirement, ([0x18, 0xFE], 0, 12)),
        ("STOP", probe_native_real_opcode_retirement, ([0x10, 0], 2, 4)),
        ("IRQ", probe_native_interrupt_only_then_isr_retirement, ()),
        ("BRK", probe_native_break_does_not_retire, ()),
        ("HALT/save-load", probe_native_halt_idle_and_save_load, ()),
        ("serial callback", probe_native_serial_callback_failure_preserves_partial_clock, ()),
    ]
    for name, probe, args in cases:
        with (
            TemporaryDirectory(prefix="cpu-counter-probe-") as directory,
            native_game(Path(directory)) as game,
        ):
            probe(game, *args)
        print(f"PASS {name}")
    print(f"Native counter probe: {len(cases)} passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-probe", action="store_true", required=True)
    parser.parse_args()
    _run_native_probe()
