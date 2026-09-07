"""Produce stock Red/Blue Cable Club fixtures from a post-Brock state.

This is a parameterized version of the earlier color-ROM helper. It uses
the game's own blackout/warp flow to land in Cerulean City, enters the
Pokecenter through the door, walks to the Cable Club receptionist, then
patches the minimum link prerequisites used by the matrix harness.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pokered_harness.session import Session  # noqa: E402
from pokered_harness.state.party import _OFFSET_HP, _OFFSET_STATUS  # noqa: E402
import run_to_brock as rtb  # noqa: E402


CERULEAN_CITY = 0x03
CERULEAN_POKECENTER = 0x40
PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11


def _restore_hp(session: Session) -> None:
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    sym = session.symbols
    mons_base = sym.addr_of("wPartyMons")
    for slot in range(int(mem[sym.addr_of("wPartyCount")])):
        base = mons_base + slot * PARTY_MON_SIZE
        max_hp = (int(mem[base + 34]) << 8) | int(mem[base + 35])
        if max_hp == 0:
            max_hp = 150
            mem[base + 34] = (max_hp >> 8) & 0xFF
            mem[base + 35] = max_hp & 0xFF
        mem[base + 1] = (max_hp >> 8) & 0xFF
        mem[base + 2] = max_hp & 0xFF


def _ensure_second_party_mon(session: Session) -> None:
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    sym = session.symbols
    count_addr = sym.addr_of("wPartyCount")
    species_addr = sym.addr_of("wPartySpecies")
    mons_addr = sym.addr_of("wPartyMons")
    ot_addr = sym.addr_of("wPartyMonOT")
    nick_addr = sym.addr_of("wPartyMonNicks")
    if int(mem[count_addr]) >= 2:
        return
    if int(mem[count_addr]) < 1:
        raise RuntimeError("source state has no party Pokemon")
    species = int(mem[species_addr])
    for i in range(PARTY_MON_SIZE):
        mem[mons_addr + PARTY_MON_SIZE + i] = int(mem[mons_addr + i]) & 0xFF
    for i in range(PARTY_OT_SIZE):
        mem[ot_addr + PARTY_OT_SIZE + i] = int(mem[ot_addr + i]) & 0xFF
    for i in range(PARTY_NICK_SIZE):
        mem[nick_addr + PARTY_NICK_SIZE + i] = int(mem[nick_addr + i]) & 0xFF
    mem[count_addr] = 2
    mem[species_addr + 1] = species
    mem[species_addr + 2] = 0xFF


def _set_pokedex_obtained(session: Session) -> None:
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    sym = session.symbols
    addr = sym.addr_of("wEventFlags") + 4
    mem[addr] = int(mem[addr]) | (1 << 5)


def _clear_audio_fade_state(session: Session) -> None:
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    sym = session.symbols
    for tag in (
        "wAudioFadeOutControl",
        "wAudioFadeOutCounter",
        "wAudioFadeOutCounterReloadValue",
        "wNewSoundID",
        "wSoundID",
        "wLastMusicSoundID",
    ):
        if tag in sym:
            mem[sym.addr_of(tag)] = 0
    if "wChannelSoundIDs" in sym:
        base = sym.addr_of("wChannelSoundIDs")
        for i in range(8):
            mem[base + i] = 0


def _poison_blackout_to_cerulean(session: Session, drv: rtb.Driver) -> bool:
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    sym = session.symbols
    mons_base = sym.addr_of("wPartyMons")

    mem[sym.addr_of("wLastBlackoutMap")] = CERULEAN_CITY
    if "wRepelRemainingSteps" in sym:
        mem[sym.addr_of("wRepelRemainingSteps")] = 0

    # Gen 1 applies overworld poison damage every few steps. At 1 HP,
    # this runs the real blackout path without needing to jump the CPU.
    mem[mons_base + _OFFSET_HP] = 0
    mem[mons_base + _OFFSET_HP + 1] = 1
    mem[mons_base + _OFFSET_STATUS] = 1 << 3  # PSN

    print("poison blackout: destination=Cerulean, lead HP=1 status=PSN", flush=True)
    directions = ("left", "right", "up", "down", "right", "left", "down", "up")
    for i in range(240):
        gs = drv.gs()
        if gs.overworld.map_id == CERULEAN_CITY:
            print(f"arrived Cerulean City by poison blackout after {i} steps", flush=True)
            return True
        if gs.battle.active:
            for _ in range(80):
                if drv.gs().overworld.map_id == CERULEAN_CITY:
                    return True
                drv.press("a")
            continue
        if drv.joy_locked() or gs.text.dest_in_vram_tilemap:
            drv.press("a")
            continue
        drv.press(directions[i % len(directions)])
        if i % 40 == 39:
            gs = drv.gs()
            lead = gs.party.mons[0] if gs.party.mons else None
            hp = f"{lead.hp}/{lead.max_hp}" if lead else "-"
            print(
                f"poison step {i + 1}: map=0x{gs.overworld.map_id:02x} "
                f"xy=({gs.overworld.x},{gs.overworld.y}) hp={hp}",
                flush=True,
            )
    return drv.gs().overworld.map_id == CERULEAN_CITY


def _special_warp_to_cerulean(session: Session, drv: rtb.Driver) -> bool:
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    pb = session._pyboy  # type: ignore[attr-defined]
    sym = session.symbols

    _clear_audio_fade_state(session)
    mem[sym.addr_of("wDestinationMap")] = CERULEAN_CITY
    mem[sym.addr_of("wLastBlackoutMap")] = CERULEAN_CITY
    if "wStatusFlags6" in sym:
        mem[sym.addr_of("wStatusFlags6")] = (
            int(mem[sym.addr_of("wStatusFlags6")]) | (1 << 2) | (1 << 6)
        )
    mem[0x2000] = 1
    if "hLoadedROMBank" in sym:
        mem[sym.addr_of("hLoadedROMBank")] = 1

    prepare = sym.addr_of("PrepareForSpecialWarp")
    enter = sym.addr_of("SpecialEnterMap")
    sp = int(pb.register_file.SP) - 2
    pb.register_file.SP = sp
    mem[sp] = enter & 0xFF
    mem[sp + 1] = (enter >> 8) & 0xFF
    pb.register_file.PC = prepare
    print(
        f"special warp: bank1 PrepareForSpecialWarp=0x{prepare:04x} "
        f"return=SpecialEnterMap=0x{enter:04x}",
        flush=True,
    )

    for phase in range(80):
        session.step(15, render=False)
        _clear_audio_fade_state(session)
        gs = drv.gs()
        if gs.overworld.map_id == CERULEAN_CITY:
            for _ in range(40):
                session.step(15, render=False)
                _clear_audio_fade_state(session)
            if "OverworldLoop" in sym:
                pb.register_file.PC = sym.addr_of("OverworldLoop")
            print(
                f"arrived Cerulean City by special warp after "
                f"{(phase + 1) * 15} frames",
                flush=True,
            )
            return True
        if phase % 8 == 7:
            print(
                f"special phase {phase + 1}: map=0x{gs.overworld.map_id:02x} "
                f"xy=({gs.overworld.x},{gs.overworld.y})",
                flush=True,
            )
    return drv.gs().overworld.map_id == CERULEAN_CITY


def produce(source: Path, rom: Path, sym: Path, out: Path,
            sha1: str | None = None) -> None:
    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    session.load_state(source.read_bytes())
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    pb = session._pyboy  # type: ignore[attr-defined]
    symbols = session.symbols
    drv = rtb.Driver(session)

    for _ in range(80):
        if not drv.joy_locked() and not drv.gs().battle.active:
            break
        drv.press("a")
    session.step(30, render=False)

    if not _special_warp_to_cerulean(session, drv) and not _poison_blackout_to_cerulean(session, drv):
        _clear_audio_fade_state(session)
        mem[symbols.addr_of("wLastBlackoutMap")] = CERULEAN_CITY
        mem[symbols.addr_of("wDestinationMap")] = CERULEAN_CITY
        status_flags = symbols.addr_of("wStatusFlags6")
        mem[status_flags] = int(mem[status_flags]) | (1 << 2) | (1 << 6)
        handle_blackout = symbols.addr_of("HandleBlackOut")
        pc_before = pb.register_file.PC
        pb.register_file.PC = handle_blackout
        print(
            f"PC redirected: 0x{pc_before:04x} -> 0x{handle_blackout:04x}",
            flush=True,
        )

        for phase in range(20):
            session.step(60, render=False)
            _clear_audio_fade_state(session)
            gs = drv.gs()
            if gs.overworld.map_id == CERULEAN_CITY and not drv.joy_locked():
                print(f"arrived Cerulean City after {(phase + 1) * 60} frames", flush=True)
                break
            if phase % 4 == 3:
                print(
                    f"phase {phase + 1}: map=0x{gs.overworld.map_id:02x} "
                    f"xy=({gs.overworld.x},{gs.overworld.y}) joy={drv.joy_locked()}",
                    flush=True,
                )

    gs = drv.gs()
    if gs.overworld.map_id != CERULEAN_CITY:
        raise RuntimeError(
            f"expected Cerulean City, got map=0x{gs.overworld.map_id:02x} "
            f"xy=({gs.overworld.x},{gs.overworld.y})"
        )

    for _ in range(10):
        _clear_audio_fade_state(session)
        session.step(30, render=False)
        gs = drv.gs()
        if gs.overworld.map_id != CERULEAN_CITY or not drv.joy_locked():
            break
        drv.press("a")

    for _ in range(12):
        _clear_audio_fade_state(session)
        if drv.joy_locked() or drv.gs().text.dest_in_vram_tilemap:
            drv.press("a")
            continue
        drv.press("up", step=60)
        _clear_audio_fade_state(session)
        session.step(60, render=False)
        if drv.gs().overworld.map_id == CERULEAN_POKECENTER:
            break
    gs = drv.gs()
    if gs.overworld.map_id != CERULEAN_POKECENTER:
        print(
            f"door warp did not fire; direct EnterMap to Cerulean PC from "
            f"map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})",
            flush=True,
        )
        mem[symbols.addr_of("wLastMap")] = CERULEAN_CITY
        mem[symbols.addr_of("wCurMap")] = CERULEAN_POKECENTER
        mem[symbols.addr_of("wXCoord")] = 3
        mem[symbols.addr_of("wYCoord")] = 7
        pb.register_file.PC = symbols.addr_of("EnterMap")
        for _ in range(80):
            session.step(15, render=False)
            _clear_audio_fade_state(session)
        if "OverworldLoop" in symbols:
            pb.register_file.PC = symbols.addr_of("OverworldLoop")
        gs = drv.gs()
        if gs.overworld.map_id != CERULEAN_POKECENTER:
            raise RuntimeError(
                f"could not enter Cerulean Pokecenter: "
                f"map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})"
            )

    for ch in "uuuudrrurrrrrr":
        gs = drv.gs()
        if (gs.overworld.x, gs.overworld.y) == (11, 3):
            break
        drv.press({"u": "up", "d": "down", "l": "left", "r": "right"}[ch])
    drv.press("up")
    session.step(30, render=False)
    gs = drv.gs()
    if gs.overworld.map_id != CERULEAN_POKECENTER or (gs.overworld.x, gs.overworld.y) != (11, 3):
        print(
            f"walk to Cable Club tile did not move; setting stock PC coords "
            f"directly from map=0x{gs.overworld.map_id:02x} "
            f"xy=({gs.overworld.x},{gs.overworld.y})",
            flush=True,
        )
        mem[symbols.addr_of("wCurMap")] = CERULEAN_POKECENTER
        mem[symbols.addr_of("wXCoord")] = 11
        mem[symbols.addr_of("wYCoord")] = 3
        if "OverworldLoop" in symbols:
            pb.register_file.PC = symbols.addr_of("OverworldLoop")
        session.step(60, render=False)

    _restore_hp(session)
    _ensure_second_party_mon(session)
    _restore_hp(session)
    _set_pokedex_obtained(session)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(session.save_state())
    gs = drv.gs()
    print(
        f"wrote {out} ({out.stat().st_size} bytes); "
        f"map=0x{gs.overworld.map_id:02x} "
        f"xy=({gs.overworld.x},{gs.overworld.y}) party={gs.party.count}",
        flush=True,
    )
    session.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--rom", type=Path, required=True)
    ap.add_argument("--sym", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sha1", default=None)
    args = ap.parse_args()
    produce(args.source, args.rom, args.sym, args.out, args.sha1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
