"""Build stock Red/Blue Cable Club fixtures from validated color fixtures.

The color ROM fixtures already contain a fully initialized Cerulean
Pokecenter/Cable Club WRAM layout. Stock Red/Blue use the same WRAM
layout, but the stock emulator state must keep its own CPU/interrupt
state; loading or saving after synthetic PC jumps can leave input masked
or interrupts disabled. This script starts from any valid stock overworld
state, copies the color fixture's WRAM/HRAM game state, and writes a
walkable stock fixture.
"""

from __future__ import annotations

import argparse
from pathlib import Path

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
    stock = Session.from_files(stock_rom, stock_sym, expected_rom_sha1=stock_sha1)
    color = Session.from_files(color_rom, color_sym, expected_rom_sha1=color_sha1)
    try:
        stock.load_state(stock_seed_state.read_bytes())
        color.load_state(color_fixture.read_bytes())
        _copy_wram_and_safe_hram(stock, color)
        _assert_walkable(stock)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(stock.save_state())
        gs = stock.read_game_state()
        print(
            f"wrote {out} ({out.stat().st_size} bytes); "
            f"map=0x{gs.overworld.map_id:02x} "
            f"xy=({gs.overworld.x},{gs.overworld.y}) party={gs.party.count}",
            flush=True,
        )
    finally:
        stock.close()
        color.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-rom", type=Path, required=True)
    parser.add_argument("--stock-sym", type=Path, required=True)
    parser.add_argument("--stock-sha1")
    parser.add_argument("--stock-seed-state", type=Path, required=True)
    parser.add_argument("--color-rom", type=Path, required=True)
    parser.add_argument("--color-sym", type=Path, required=True)
    parser.add_argument("--color-sha1")
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
