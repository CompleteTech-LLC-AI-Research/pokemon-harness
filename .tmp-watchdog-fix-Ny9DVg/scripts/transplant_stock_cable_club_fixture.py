"""Diagnostic-only builder for derived stock Red/Blue Cable Club states.

The color ROM fixtures already contain a fully initialized Cerulean
Pokecenter/Cable Club WRAM layout. Stock Red/Blue use the same WRAM
layout, but the stock emulator state must keep its own CPU/interrupt
state; loading or saving after synthetic PC jumps can leave input masked
or interrupts disabled. This script starts from any valid stock overworld
state, copies the color fixture's WRAM/HRAM game state, mutates CPU/interrupt
state, and writes a derived walkable stock fixture. It is not a clean fixture
capture, human-valid gameplay evidence, or production acceptance proof.
ROM and symbol files must match the configured ``VERSIONS.md`` pins.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from pokered_harness.config import load_versions
from pokered_harness.session import Session

WRAM_START = 0xC000
WRAM_END = 0xE000
CERULEAN_POKECENTER = 0x40

HRAM_TAGS = (
    "hLoadedROMBank",
    "hJoyLast",
    "hJoyReleased",
    "hJoyPressed",
    "hJoyHeld",
    "hJoy5",
    "hJoy6",
    "hJoy7",
    "hVBlankOccurred",
    "hJoyInput",
)


def _validated_pins(
    rom: Path,
    sym: Path,
    supplied_sha1: str | None,
    label: str,
):
    """Resolve a configured ROM/symbol pin pair and reject overrides."""
    pins = load_versions(_REPO / "VERSIONS.md")
    expected_rom_sha1 = pins.sha1_for_path(rom)
    expected_symbol_sha1 = pins.symbol_sha1_for_path(sym)
    if expected_rom_sha1 is None:
        raise ValueError(f"{label} ROM is not pinned in VERSIONS.md: {rom}")
    if expected_symbol_sha1 is None:
        raise ValueError(f"{label} symbol file is not pinned in VERSIONS.md: {sym}")
    if supplied_sha1 is not None and supplied_sha1.lower() != expected_rom_sha1:
        raise ValueError(
            f"provided {label} ROM SHA-1 does not match its VERSIONS.md pin"
        )
    return pins, expected_rom_sha1, expected_symbol_sha1


def _close_sessions(
    *labeled_sessions: tuple[str, Session | None],
    active_error: BaseException | None = None,
) -> None:
    """Close both sessions, then surface all teardown failures."""
    failures: list[Exception] = []
    for label, session in labeled_sessions:
        if session is None:
            continue
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue
            exc.add_note(f"diagnostic cleanup failed for {label} session")
            failures.append(exc)
            print(
                f"[diagnostic cleanup] failed to close {label} session: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    if failures:
        cleanup_error = ExceptionGroup("diagnostic cleanup failed", failures)
        if active_error is not None:
            raise cleanup_error from active_error
        raise cleanup_error


def _copy_wram_and_safe_hram(stock: Session, color: Session) -> None:
    stock_mem = stock._pyboy.memory  # type: ignore[attr-defined]
    color_mem = color._pyboy.memory  # type: ignore[attr-defined]
    symbols = stock.symbols

    for addr in range(WRAM_START, WRAM_END):
        stock_mem[addr] = int(color_mem[addr]) & 0xFF
    for tag in HRAM_TAGS:
        if tag in symbols:
            addr = symbols.addr_of(tag)
            stock_mem[addr] = int(color_mem[addr]) & 0xFF

    # The stock CPU state is kept, but it may have been saved mid-delay.
    # Put it at the home-bank overworld loop with normal VBlank interrupts.
    stock_mem[0xFFFF] = int(stock_mem[0xFFFF]) | 0x01
    stock_mem[0xFF0F] = int(stock_mem[0xFF0F]) & 0xFE
    stock._pyboy.register_file.cpu.interrupt_master_enable = 1  # type: ignore[attr-defined]
    stock._pyboy.register_file.PC = symbols.addr_of("OverworldLoop")  # type: ignore[attr-defined]


def _assert_walkable(session: Session) -> None:
    symbols = session.symbols
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    session.step(60, render=False)
    gs = session.read_game_state()
    if gs.overworld.map_id != CERULEAN_POKECENTER:
        raise RuntimeError(f"expected Cerulean Pokecenter, got map=0x{gs.overworld.map_id:02x}")
    if "wJoyIgnore" in symbols and int(mem[symbols.addr_of("wJoyIgnore")]) != 0:
        raise RuntimeError("fixture still masks joypad input")

    before = session.read_game_state().overworld
    session.press("left", duration=12)
    session.step(120, render=False)
    left = session.read_game_state().overworld
    session.press("right", duration=12)
    session.step(120, render=False)
    right = session.read_game_state().overworld
    if (left.x, left.y) == (before.x, before.y):
        raise RuntimeError(
            f"walk validation failed: left did not move from ({before.x},{before.y})"
        )
    if (right.x, right.y) != (before.x, before.y):
        raise RuntimeError(
            "walk validation failed: right did not return to "
            f"({before.x},{before.y}); got ({right.x},{right.y})"
        )


def produce(
    *,
    stock_rom: Path,
    stock_sym: Path,
    stock_sha1: str | None,
    stock_seed_state: Path,
    color_rom: Path,
    color_sym: Path,
    color_sha1: str | None,
    color_fixture: Path,
    out: Path,
) -> None:
    stock_pins, expected_stock_sha1, expected_stock_symbol_sha1 = _validated_pins(
        stock_rom, stock_sym, stock_sha1, "stock"
    )
    color_pins, expected_color_sha1, expected_color_symbol_sha1 = _validated_pins(
        color_rom, color_sym, color_sha1, "color"
    )
    stock: Session | None = None
    color: Session | None = None
    active_error: BaseException | None = None
    try:
        stock = Session.from_files(
            stock_rom,
            stock_sym,
            expected_rom_sha1=expected_stock_sha1,
            expected_symbol_sha1=expected_stock_symbol_sha1,
            expected_pyboy_version=stock_pins.pyboy_version,
            expected_pyboy_revision=stock_pins.pyboy_revision,
        )
        color = Session.from_files(
            color_rom,
            color_sym,
            expected_rom_sha1=expected_color_sha1,
            expected_symbol_sha1=expected_color_symbol_sha1,
            expected_pyboy_version=color_pins.pyboy_version,
            expected_pyboy_revision=color_pins.pyboy_revision,
        )
        stock.load_state(stock_seed_state.read_bytes())
        color.load_state(color_fixture.read_bytes())
        _copy_wram_and_safe_hram(stock, color)
        _assert_walkable(stock)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(stock.save_state())
        gs = stock.read_game_state()
        print(
            f"[diagnostic-only] wrote derived state {out} ({out.stat().st_size} bytes); "
            f"map=0x{gs.overworld.map_id:02x} "
            f"xy=({gs.overworld.x},{gs.overworld.y}) party={gs.party.count}; "
            "not clean fixture provenance",
            flush=True,
        )
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        _close_sessions(
            ("stock", stock),
            ("color", color),
            active_error=active_error,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnostic-only derived fixture builder; mutates emulator "
            "WRAM/HRAM/CPU state and is not clean fixture provenance."
        ),
        epilog="ROM and symbol SHA-1 values must match configured VERSIONS.md pins.",
    )
    parser.add_argument("--stock-rom", type=Path, required=True)
    parser.add_argument("--stock-sym", type=Path, required=True)
    parser.add_argument(
        "--stock-sha1",
        help="optional assertion that must equal the configured stock ROM pin",
    )
    parser.add_argument("--stock-seed-state", type=Path, required=True)
    parser.add_argument("--color-rom", type=Path, required=True)
    parser.add_argument("--color-sym", type=Path, required=True)
    parser.add_argument(
        "--color-sha1",
        help="optional assertion that must equal the configured color ROM pin",
    )
    parser.add_argument("--color-fixture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    produce(
        stock_rom=args.stock_rom,
        stock_sym=args.stock_sym,
        stock_sha1=args.stock_sha1,
        stock_seed_state=args.stock_seed_state,
        color_rom=args.color_rom,
        color_sym=args.color_sym,
        color_sha1=args.color_sha1,
        color_fixture=args.color_fixture,
        out=args.out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
