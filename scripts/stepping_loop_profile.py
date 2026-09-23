#!/usr/bin/env python3
"""Measure the scheduler's Python instruction-stepping loop against compiled frames.

#109 asks whether the per-instruction Python scheduler loop
(``PyBoyLinkSession._step_single_step_chunk``) is a measured cost centre or only a
plausible one.  This probe answers that question with a controlled measurement
that needs no operator asset:

* it authors its own 32 KiB ROM-only cartridge in a temporary directory, so no
  Nintendo file, BYO ROM, symbol table or save state is opened, and nothing is
  written outside that temporary directory;
* it drives that cartridge down five declared paths - the production scheduler
  loop, a hoisting *candidate* model of it, a minimal hand-rolled singlestep loop,
  and the compiled frame loop at one frame per call and at an adaptive whole-frame
  batch per call - reporting emulated cycles and retired instructions per second
  for each;
* every sample is validated against a deterministic oracle derived from the
  cartridge's own declared instruction mix, and reported only when the runtime
  retired exactly the instructions that mix describes, the final PC is inside the
  authored window, **and** the authored body re-read from the runtime's memory seam
  is still byte-identical to the declared mix - so the guard only holds if the loop
  still *is* the declared one, not merely if execution ended where it started.

Wall-clock numbers are host-dependent and are reported as medians of repeated
samples.  The instruction and cycle counts are exact, so the measurement can be
audited on a loaded host.  Run it against a compiled runtime; the module refuses
to report a comparison for a source-only runtime, because the question is about
the compiled scheduler's Python overhead specifically.

    python scripts/stepping_loop_profile.py --cycles 4000000 --json profile.json
"""

from __future__ import annotations

import argparse
import cProfile
import importlib.machinery
import json
import os
import pstats
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession

CARTRIDGE_BYTES = 0x8000
LOOP_BASE = 0xC000
TITLE = b"STEPPRF"
HEADER_TITLE = slice(0x134, 0x13B)
HEADER_CHECKSUM = 0x14D
BOOTROM_MAPPING = 0xFF50
INTERRUPT_ENABLE = 0xFFFF
INTERRUPT_FLAG = 0xFF0F
BOOTROM_CHECKSUM_DELTA = 25
DEFAULT_CYCLES = 4_000_000
DEFAULT_REPEATS = 5

# The two declared mixes.  Every opcode is a register or HRAM-only instruction
# with a fixed length and no control flow except the closing jump back to the
# loop head, so execution can never leave the authored body.
OP_NOP = bytes([0x00])
OP_INC_BC = bytes([0x03])
OP_INC_B = bytes([0x04])
OP_LD_A_B = bytes([0x78])
OP_ADD_A_B = bytes([0x80])
OP_LDH_80_A = bytes([0xE0, 0x80])
OP_JP_LOOP = bytes([0xC3, LOOP_BASE & 0xFF, (LOOP_BASE >> 8) & 0xFF])


@dataclass(frozen=True)
class Instruction:
    """One instruction of a declared mix, with its encoded length in cycles."""

    mnemonic: str
    opcode: bytes
    cycles: int


@dataclass(frozen=True)
class Mix:
    """A closed instruction loop of known length and known cycle cost."""

    name: str
    instructions: tuple[Instruction, ...]
    description: str

    @property
    def body(self) -> bytes:
        return b"".join(item.opcode for item in self.instructions)

    @property
    def loop_instructions(self) -> int:
        return len(self.instructions)

    @property
    def loop_cycles(self) -> int:
        return sum(item.cycles for item in self.instructions)

    @property
    def min_instruction_cycles(self) -> int:
        return min(item.cycles for item in self.instructions)

    @property
    def max_instruction_cycles(self) -> int:
        return max(item.cycles for item in self.instructions)

    def expected_cycles(self, retired: int) -> int:
        """Exact emulated cycles for ``retired`` instructions from the loop head."""

        loops, remainder = divmod(retired, self.loop_instructions)
        prefix = sum(item.cycles for item in self.instructions[:remainder])
        return loops * self.loop_cycles + prefix

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "loop_instructions": self.loop_instructions,
            "loop_cycles": self.loop_cycles,
            "min_instruction_cycles": self.min_instruction_cycles,
            "max_instruction_cycles": self.max_instruction_cycles,
            "instructions": [
                {
                    "mnemonic": item.mnemonic,
                    "opcode": item.opcode.hex(),
                    "cycles": item.cycles,
                }
                for item in self.instructions
            ],
        }


MIXES: tuple[Mix, ...] = (
    Mix(
        name="reg-only",
        instructions=(
            Instruction("NOP", OP_NOP, 4),
            Instruction("INC BC", OP_INC_BC, 8),
            Instruction("LD A,B", OP_LD_A_B, 4),
            Instruction("ADD A,B", OP_ADD_A_B, 4),
            Instruction("INC B", OP_INC_B, 4),
            Instruction("JP loop", OP_JP_LOOP, 16),
        ),
        description="register-only six-instruction loop, 40 cycles per iteration",
    ),
    Mix(
        name="hram-io",
        instructions=(
            Instruction("INC BC", OP_INC_BC, 8),
            Instruction("LD A,B", OP_LD_A_B, 4),
            Instruction("LDH (0x80),A", OP_LDH_80_A, 12),
            Instruction("ADD A,B", OP_ADD_A_B, 4),
            Instruction("JP loop", OP_JP_LOOP, 16),
        ),
        description="five-instruction loop with one HRAM write, 44 cycles per iteration",
    ),
)
MIXES_BY_NAME = {mix.name: mix for mix in MIXES}


class ProfileRefused(RuntimeError):
    """The probe cannot produce a meaningful measurement in this runtime."""


def author_cartridge(directory: Path) -> Path:
    """Write a 32 KiB ROM-only cartridge; the loop body is installed at run time.

    The image carries a valid title and header checksum and nothing else, so it
    contains no Nintendo or operator bytes.  The measured loop is installed into
    WRAM through the runtime's own memory seam, the same way the existing
    compiled-runtime probe authors its test code.
    """

    rom = bytearray(CARTRIDGE_BYTES)
    rom[HEADER_TITLE] = TITLE
    rom[HEADER_CHECKSUM] = (-sum(rom[HEADER_TITLE]) - BOOTROM_CHECKSUM_DELTA) & 0xFF
    if len(rom) != CARTRIDGE_BYTES:
        raise ValueError(f"authored cartridge is {len(rom)} bytes, expected {CARTRIDGE_BYTES}")
    path = directory / "stepping-loop-profile.gb"
    path.write_bytes(rom)
    return path


def module_is_compiled(module: object) -> bool:
    """True when ``module`` resolved to a compiled extension, not Python source."""

    origin = getattr(module, "__file__", None)
    if not origin:
        return False
    return any(str(origin).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)


def compiled_runtime_identity(cpu_module: object, mb_module: object) -> dict[str, object]:
    """Describe the runtime, or refuse when it is not the compiled scheduler.

    Separated from :func:`require_compiled_runtime` so the gate can be exercised
    against the vendored Python sources without selecting another runtime.
    """

    if not (module_is_compiled(cpu_module) and module_is_compiled(mb_module)):
        raise ProfileRefused(
            "the stepping-loop profile requires the compiled CPU and Motherboard; "
            "this runtime resolved to Python sources, where the comparison would "
            "measure the fallback interpreter instead of the compiled scheduler"
        )
    cpu_type = getattr(cpu_module, "CPU", None)
    if cpu_type is None or not hasattr(cpu_type, "retired_instructions"):
        raise ProfileRefused(
            "the compiled CPU does not expose retired_instructions, so the probe "
            "cannot validate a sample against its declared instruction mix"
        )
    import pyboy as pyboy_module

    cpu_origin = Path(str(cpu_module.__file__)).resolve()
    mb_origin = Path(str(mb_module.__file__)).resolve()
    revision = getattr(pyboy_module, "__pokered_harness_revision__", None)
    return {
        "pyboy_version": getattr(pyboy_module, "__version__", None),
        # The fork declares this revision as a 40-character string; keep it
        # readable and comparable with the CI contract assertion.
        "pyboy_harness_revision": revision if isinstance(revision, str) else list(revision or ()),
        "compiled_cpu": True,
        "compiled_motherboard": True,
        "has_retired_instructions": True,
        "interpreter": f"{sys.implementation.name} {sys.version.split()[0]}",
        "cpu_module": cpu_origin.name,
        "motherboard_module": mb_origin.name,
    }


def require_compiled_runtime() -> dict[str, object]:
    """Return the runtime identity, or refuse when the runtime is not compiled."""

    try:
        from pyboy.core import cpu as runtime_cpu
        from pyboy.core import mb as runtime_mb
    except ImportError as missing:
        raise ProfileRefused(
            f"no pyboy runtime is importable ({missing}); the probe needs the "
            "compiled CPU and Motherboard"
        ) from missing
    return compiled_runtime_identity(runtime_cpu, runtime_mb)


def open_loop_game(cartridge: Path, mix: Mix):
    """Open the authored cartridge on the declared loop head."""

    from pyboy import PyBoy

    if len(mix.body) > 0x1000:
        raise ValueError("declared loop body does not fit the authored WRAM window")
    game = PyBoy(str(cartridge), window="null", sound_emulated=False)
    game.memory[BOOTROM_MAPPING] = 1
    game.memory[INTERRUPT_ENABLE] = 0
    game.memory[INTERRUPT_FLAG] = 0
    game.memory[LOOP_BASE : LOOP_BASE + len(mix.body)] = mix.body
    game.register_file.PC = LOOP_BASE
    game.mb.breakpoint_singlestep = False
    game.mb.lcd.frame_done = False
    return game


def production_chunk_loop(game, cycles_target: int) -> int:
    """The production scheduler loop, called exactly as the link session calls it."""

    chunk = PyBoyLinkSession._REAL_SCHEDULER_CHUNK_CYCLES
    start = game.mb.cpu.cycles
    calls = 0
    while game.mb.cpu.cycles - start < cycles_target:
        PyBoyLinkSession._step_single_step_chunk(game, chunk, stop_on_frame=False)
        calls += 1
    return calls


def _optimized_step_chunk(p: object, cycle_budget: int, *, stop_on_frame: bool = True) -> bool:
    """Model of ``_step_single_step_chunk`` with its Python overhead removed.

    This is a *probe-local model*, never a shipped change.  The transformation
    over the shipped method is pure redundancy removal: the same object graph,
    the same call order, the same breakpoint and frame-done handling, and the
    same cycle-budget break, but attribute lookups that cannot change within one
    instruction step are hoisted to the top of the call.  Nothing about the
    emulated work changes, so the retired-instruction and cycle counts must stay
    exactly equal to the shipped path; ``tests/test_stepping_loop_profile.py``
    asserts that parity directly.

    This is **one candidate, not a ceiling.**  Its rate bounds only what *this*
    transformation recovers on *this* workload and estimator; further hoisting
    remains, and a candidate that additionally binds the compiled breakpoint
    methods and reads the LCD directly preserved the same counts in an
    independent review and ran faster still.  A real edit must also keep working
    for callers whose ``mb``/``lcd``/``cpu`` are lightweight test doubles that
    rely on the shipped ``getattr(..., None)`` defaults, so the transformation
    that is *permitted* is not the same as the transformation that is *fastest*.
    Report the measured difference for this candidate as an observation, never
    as a proven maximum recovery.
    """

    mb = p.mb
    cpu = getattr(mb, "cpu", None)
    lcd = getattr(mb, "lcd", None)
    tick = mb.tick
    start_cycles = getattr(cpu, "cycles", None)
    fallback_ticks = max(1, cycle_budget // 7)
    max_ticks = min(
        max(fallback_ticks, cycle_budget * 4),
        PyBoyLinkSession._MAX_SINGLE_STEP_TICKS,
    )
    ticks = 0
    while ticks < max_ticks:
        done = getattr(lcd, "frame_done", False)
        if stop_on_frame and done:
            return True
        if not stop_on_frame and done:
            lcd.frame_done = False
        mb.breakpoint_singlestep = 1
        if tick():
            mb.breakpoint_reinject()
            bp = mb.breakpoint_reached()
            if bp != (-1, -1, -1):
                bank, addr, _ = bp
                mb.breakpoint_remove(bank, addr)
                mb.breakpoint_singlestep_latch = 0
                p._handle_hooks()
        ticks += 1
        if start_cycles is not None:
            if cpu.cycles - start_cycles >= cycle_budget:
                break
        elif ticks >= fallback_ticks:
            break
    return bool(getattr(lcd, "frame_done", False))


def optimized_chunk_loop(game, cycles_target: int) -> int:
    """The production chunk loop, driven by the probe-local optimized chunk step."""

    chunk = PyBoyLinkSession._REAL_SCHEDULER_CHUNK_CYCLES
    start = game.mb.cpu.cycles
    calls = 0
    while game.mb.cpu.cycles - start < cycles_target:
        _optimized_step_chunk(game, chunk, stop_on_frame=False)
        calls += 1
    return calls


def minimal_singlestep_loop(game, cycles_target: int) -> int:
    """One instruction per iteration with nothing but the stepping in the loop."""

    cpu = game.mb.cpu
    lcd = game.mb.lcd
    mb = game.mb
    start = cpu.cycles
    calls = 0
    while cpu.cycles - start < cycles_target:
        if lcd.frame_done:
            lcd.frame_done = False
        mb.breakpoint_singlestep = 1
        mb.tick()
        calls += 1
    return calls


def compiled_frame_loop(game, cycles_target: int) -> int:
    """One Python call per emulated frame; the frame loop itself runs compiled."""

    frames_per_call = 1
    start = game.mb.cpu.cycles
    calls = 0
    while game.mb.cpu.cycles - start < cycles_target:
        if not game.tick(frames_per_call, False, False):
            break
        calls += 1
    return calls


def compiled_batched_loop(game, cycles_target: int) -> int:
    """One Python call for a whole batch of emulated frames.

    The batch is *adaptive*, not a fixed frame count: the declared policy asks for
    enough whole frames to cover the requested cycle budget in as few calls as
    possible, so on the reference runtime the batch is 56 frames per call at a 4M
    cycle target and 113 at 8M.  The frame argument actually used is recorded per
    sample, because the work one call performs is part of what is being compared.
    """

    frames = batched_frames_for(cycles_target, int(game.mb.lcd._cycles_to_frame) or 70224)
    start = game.mb.cpu.cycles
    calls = 0
    while game.mb.cpu.cycles - start < cycles_target:
        if not game.tick(frames, False, False):
            break
        calls += 1
    return calls


def batched_frames_for(cycles_target: int, cycles_per_frame: int) -> int:
    """Whole frames the declared adaptive batch policy asks for in one call."""

    return max(1, cycles_target // max(1, cycles_per_frame))


PATHS: tuple[tuple[str, Callable[[object, int], int]], ...] = (
    ("production_chunk_loop", production_chunk_loop),
    ("optimized_chunk_loop", optimized_chunk_loop),
    ("minimal_singlestep_loop", minimal_singlestep_loop),
    ("compiled_frame_loop", compiled_frame_loop),
    ("compiled_batched_loop", compiled_batched_loop),
)


@dataclass
class Sample:
    path: str
    seconds: float
    cycles: int
    retired: int
    python_calls: int
    end_pc: int
    batch_frames_per_call: int | None = None


def run_sample(mix: Mix, cartridge: Path, path: str, cycles_target: int) -> Sample:
    """Run one timed sample and validate it against the mix oracle."""

    steps = dict(PATHS)
    if path not in steps:
        raise ProfileRefused(
            f"unknown stepping path {path!r}; declared paths are {', '.join(steps)}"
        )
    runner = steps[path]
    game = open_loop_game(cartridge, mix)
    try:
        cpu = game.mb.cpu
        start_cycles = cpu.cycles
        start_retired = cpu.retired_instructions
        started = time.perf_counter()
        calls = runner(game, cycles_target)
        seconds = time.perf_counter() - started
        cycles = cpu.cycles - start_cycles
        retired = cpu.retired_instructions - start_retired
        end_pc = int(game.register_file.PC)
        # Loop integrity, checked outside the timed region so it cannot move the
        # clock.  An endpoint and a cycle count are both satisfiable by a loop
        # that was relocated and re-pointed back into the window; re-reading the
        # authored bytes from the runtime's own memory seam is not.
        installed = bytes(game.memory[LOOP_BASE : LOOP_BASE + len(mix.body)])
        batch_frames = (
            batched_frames_for(cycles_target, int(game.mb.lcd._cycles_to_frame) or 70224)
            if path == "compiled_batched_loop"
            else None
        )
    finally:
        game.stop(save=False)

    if installed != mix.body:
        raise ProfileRefused(
            f"{path} no longer runs the declared {mix.name} body: the bytes at "
            f"{LOOP_BASE:#06x} re-read from the memory seam are {installed.hex()}, "
            f"declared {mix.body.hex()}"
        )
    if cycles_target > 0 and cycles < cycles_target - (
        0x4000 if path.startswith("compiled") else 0
    ):
        raise ProfileRefused(f"{path} advanced only {cycles} of {cycles_target} requested cycles")
    if retired <= 0:
        raise ProfileRefused(f"{path} retired no instruction")
    expected = mix.expected_cycles(retired)
    if cycles != expected:
        raise ProfileRefused(
            f"{path} advanced {cycles} cycles for {retired} instructions; the declared "
            f"{mix.name} mix predicts {expected}"
        )
    if not LOOP_BASE <= end_pc < LOOP_BASE + len(mix.body):
        raise ProfileRefused(
            f"{path} left the authored loop: PC {end_pc:#06x} is outside "
            f"{LOOP_BASE:#06x}..{LOOP_BASE + len(mix.body):#06x}"
        )
    return Sample(
        path=path,
        seconds=seconds,
        cycles=cycles,
        retired=retired,
        python_calls=calls,
        end_pc=end_pc,
        batch_frames_per_call=batch_frames,
    )


def summarize(mix: Mix, path: str, samples: list[Sample]) -> dict[str, object]:
    # A summary binds one sample's counts to another sample's fastest time.  That
    # is only meaningful while every sample retired the same work, so drift is
    # refused here rather than silently collapsed into one number.
    counts = {(sample.cycles, sample.retired, sample.python_calls) for sample in samples}
    if len(counts) != 1:
        raise ProfileRefused(
            f"{path} produced {len(counts)} distinct (cycles, retired, python_calls) triples "
            "across its samples; a summary would mix one sample's work with another's time"
        )
    seconds = statistics.median(sample.seconds for sample in samples)
    best_seconds = min(sample.seconds for sample in samples)
    cycles = samples[0].cycles
    retired = samples[0].retired
    calls = samples[0].python_calls
    return {
        "path": path,
        "samples": len(samples),
        "seconds_median": seconds,
        "seconds_min": best_seconds,
        "seconds_max": max(sample.seconds for sample in samples),
        "cycles": cycles,
        "retired_instructions": retired,
        "cycles_per_instruction": cycles / retired,
        "python_calls": calls,
        "python_calls_per_instruction": calls / retired,
        "emulated_cycles_per_second": cycles / seconds,
        "retired_instructions_per_second": retired / seconds,
        "best_emulated_cycles_per_second": cycles / best_seconds,
        "best_retired_instructions_per_second": retired / best_seconds,
        "deterministic_counts_agree": True,
        "batch_frames_per_call": samples[0].batch_frames_per_call,
    }


def measure_mix(
    mix: Mix,
    cycles_target: int,
    repeats: int,
    paths: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    """Time every declared path on an interleaved schedule.

    Paths are sampled round-robin inside each repeat (and in reverse order on
    alternate repeats) rather than one path after another.  The host is shared
    and its load moves during a run, so a sequential schedule lets drift land on
    whichever path happens to be measured last - in an earlier version of this
    probe a loaded interval made the bare hand-rolled loop look *slower* than the
    production loop, which is arithmetically impossible.  Interleaving spreads
    drift across the paths, and the per-path minimum is reported alongside the
    median as the load-robust estimate.
    """

    results: dict[str, dict[str, object]] = {}
    with TemporaryDirectory() as directory:
        cartridge = author_cartridge(Path(directory))
        collected: dict[str, list[Sample]] = {path: [] for path in paths}
        for repeat in range(repeats):
            order = paths if repeat % 2 == 0 else tuple(reversed(paths))
            for path in order:
                collected[path].append(run_sample(mix, cartridge, path, cycles_target))
        for path in paths:
            samples = collected[path]
            results[path] = summarize(mix, path, samples)
    return results


def profile_production_loop(mix: Mix, cycles_target: int, top: int) -> dict[str, object]:
    """Attribute the production loop's Python time by function and call count."""

    with TemporaryDirectory() as directory:
        cartridge = author_cartridge(Path(directory))
        game = open_loop_game(cartridge, mix)
        try:
            cpu = game.mb.cpu
            start_retired = cpu.retired_instructions
            profiler = cProfile.Profile()
            profiler.enable()
            production_chunk_loop(game, cycles_target)
            profiler.disable()
            retired = cpu.retired_instructions - start_retired
        finally:
            game.stop(save=False)

    stats = pstats.Stats(profiler)
    rows: list[dict[str, object]] = []
    for (filename, lineno, name), (_cc, nc, _tt, _ct, _callers) in stats.stats.items():
        rows.append(
            {
                "function": name,
                "module": Path(filename).name,
                "line": lineno,
                "call_count": nc,
                "primitive_call_count": _cc,
            }
        )
    rows.sort(key=lambda row: (-int(row["call_count"]), str(row["function"])))
    calls_per_instruction = {row["function"]: row["call_count"] / retired for row in rows[:top]}
    return {
        "retired_instructions": retired,
        "top_functions": rows[:top],
        "calls_per_retired_instruction": calls_per_instruction,
    }


def compute_ratios(results: dict[str, dict[str, dict[str, object]]]) -> dict[str, object]:
    ratios: dict[str, object] = {}
    for mix_name, paths in results.items():
        entry: dict[str, float] = {}
        # Ratios are relative to the production baseline, so they only exist when
        # that baseline was measured.  A valid path subset that omits it must still
        # produce a report instead of raising ``KeyError``.
        production = paths.get("production_chunk_loop")
        if production is not None:
            throughput = production["retired_instructions_per_second"]
            throughput_best = production["best_retired_instructions_per_second"]
            for path, summary in paths.items():
                if path == "production_chunk_loop":
                    continue
                entry[f"production_to_{path}"] = (
                    throughput / summary["retired_instructions_per_second"]
                )
                entry[f"production_best_to_{path}_best"] = (
                    throughput_best / summary["best_retired_instructions_per_second"]
                )
        densities = [paths[name]["python_calls_per_instruction"] for name in paths]
        if densities:
            entry["python_calls_per_instruction_median_over_paths"] = float(
                statistics.median(densities)
            )
        ratios[mix_name] = entry
    return ratios


def format_report(payload: dict[str, object]) -> str:
    lines: list[str] = []
    lines.append("stepping-loop profile (compiled runtime, authored ROM-only cartridge)")
    identity = payload["runtime"]
    lines.append(
        f"runtime: pyboy {identity['pyboy_version']} revision "
        f"{identity['pyboy_harness_revision']} {identity['interpreter']}"
    )
    load = payload["host_load_1min"]
    lines.append(f"host load1 at measurement: {load:.2f} on {os.cpu_count()} cpus")
    for mix_name, paths in payload["results"].items():
        mix = MIXES_BY_NAME[mix_name]
        lines.append("")
        lines.append(f"[{mix_name}] {mix.description}")
        lines.append(
            f"  oracle: {mix.loop_instructions} instructions / {mix.loop_cycles} cycles per loop"
        )
        lines.append(
            f"  {'path':26s} {'med s':>8s} {'best s':>8s} {'med instr/s':>11s} "
            f"{'best instr/s':>12s} {'py calls/instr':>15s} {'stable':>7s}"
        )
        for path, summary in paths.items():
            lines.append(
                f"  {path:26s} {summary['seconds_median']:8.3f} "
                f"{summary['seconds_min']:8.3f} "
                f"{summary['retired_instructions_per_second']:11.0f} "
                f"{summary['best_retired_instructions_per_second']:12.0f} "
                f"{summary['python_calls_per_instruction']:15.5f} "
                f"{summary['deterministic_counts_agree']!s:>7s}"
            )
        for path, summary in paths.items():
            if summary.get("batch_frames_per_call"):
                lines.append(
                    f"  {path}: adaptive batch = {summary['batch_frames_per_call']} frames per call"
                )
    lines.append("")
    lines.append("ratios: median-based, then best-of-repeat-based (load-robust). Both are")
    lines.append("throughput ratios - production instructions per second divided by the")
    lines.append("other path's instructions per second. A value below 1.0 means the")
    lines.append("production loop retires fewer instructions per second, i.e. it is slower;")
    lines.append("its time per unit of work is the reciprocal (0.79 is about 1.27x the time).")
    for mix_name, entry in payload["ratios"].items():
        for key, value in entry.items():
            lines.append(f"  [{mix_name}] {key} = {value:.3f}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--mixes",
        default=",".join(mix.name for mix in MIXES),
        help="comma-separated subset of the declared mixes",
    )
    parser.add_argument(
        "--paths",
        default=",".join(name for name, _ in PATHS),
        help="comma-separated subset of the declared stepping paths",
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--profile-mix", default=None, help="also cProfile one mix")
    parser.add_argument("--profile-top", type=int, default=12)
    parser.add_argument("--label", default=None, help="free-form context recorded in the JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cycles <= 0:
        raise SystemExit("--cycles must be positive")
    if args.repeats <= 0:
        raise SystemExit("--repeats must be positive")
    mix_names = [name.strip() for name in args.mixes.split(",") if name.strip()]
    path_names = [name.strip() for name in args.paths.split(",") if name.strip()]
    unknown = [name for name in mix_names if name not in MIXES_BY_NAME]
    if unknown:
        raise SystemExit(f"unknown mix(es): {', '.join(unknown)}")
    unknown = [name for name in path_names if name not in dict(PATHS)]
    if unknown:
        raise SystemExit(f"unknown path(s): {', '.join(unknown)}")
    if args.profile_mix is not None and args.profile_mix not in MIXES_BY_NAME:
        raise SystemExit(f"unknown --profile-mix: {args.profile_mix}")
    if not mix_names:
        raise SystemExit("--mixes must name at least one mix")
    if not path_names:
        raise SystemExit("--paths must name at least one stepping path")

    results: dict[str, dict[str, dict[str, object]]] = {}
    # Every refusal path reports the same bounded outcome: a ``REFUSED`` line on
    # stderr, exit code 2, and no report or JSON payload.  A measurement whose
    # counts disagree with the declared mix, or that escapes the authored loop,
    # is the reason this probe exists, so it must not surface as a traceback.
    try:
        runtime = require_compiled_runtime()
        for name in mix_names:
            results[name] = measure_mix(
                MIXES_BY_NAME[name], args.cycles, args.repeats, tuple(path_names)
            )
        payload: dict[str, object] = {
            "subject": "#109 instruction-stepping loop profile",
            "label": args.label,
            "cycles_requested": args.cycles,
            "repeats": args.repeats,
            "runtime": runtime,
            "host_load_1min": os.getloadavg()[0],
            "cpu_count": os.cpu_count(),
            "mixes": {mix.name: mix.as_dict() for mix in MIXES},
            "paths": [name for name, _ in PATHS],
            "results": results,
            "ratios": compute_ratios(results),
        }
        if args.profile_mix:
            payload["production_loop_profile"] = profile_production_loop(
                MIXES_BY_NAME[args.profile_mix], args.cycles, args.profile_top
            )
    except ProfileRefused as refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2

    print(format_report(payload))
    if args.json is not None:
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
