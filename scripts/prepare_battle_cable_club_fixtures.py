#!/usr/bin/env python3
"""Prepare immutable, battle-ready Cable Club save-state fixtures.

The ordinary Cable Club fixtures intentionally contain the small party used by
the trade walkthrough.  Gen I Colosseum requires at least three party
Pokémon, so this release-time utility creates separate battle fixtures by
copying the lead record into six valid party slots and repairing zero-PP lead
moves before saving.  The acceptance runner only loads the resulting state; it
never performs this preparation in emulator RAM.

ROMs, symbols, and state files are operator-managed inputs and stay outside
version control.  The command validates the selected ROM against VERSIONS.md
before writing output, so a battle fixture cannot be silently prepared against
the wrong ROM.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11

VARIANTS = {
    "red_gb": {
        "rom": Path("red/pokemon-red.gb"),
        "symbols": Path("red/pokemon-red.sym"),
        "fixture_version": "red",
        "source": "cable_club-vanilla.state",
        "output": "cable_club-battle-vanilla.state",
    },
    "red_color": {
        "rom": Path("red/pokemon-red-color.gb"),
        "symbols": Path("red/pokemon-red.sym"),
        "fixture_version": "red",
        "source": "cable_club.state",
        "output": "cable_club-battle.state",
    },
    "blue_gb": {
        "rom": Path("blue/pokemon-blue.gb"),
        "symbols": Path("blue/pokemon-blue.sym"),
        "fixture_version": "blue",
        "source": "cable_club-vanilla.state",
        "output": "cable_club-battle-vanilla.state",
    },
    "blue_color": {
        "rom": Path("blue/pokemon-blue-color.gb"),
        "symbols": Path("blue/pokemon-blue.sym"),
        "fixture_version": "blue",
        "source": "cable_club.state",
        "output": "cable_club-battle.state",
    },
    "yellow": {
        "rom": Path("yellow/pokemon-yellow.gbc"),
        "symbols": Path("yellow/pokemon-yellow.sym"),
        "fixture_version": "yellow",
        "source": "cable_club.state",
        "output": "cable_club-battle.state",
    },
}


def _root(value: str | Path, repo_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo_root / path


def _copy_lead_party(session) -> str:
    memory = session._pyboy.memory
    symbols = session.symbols
    count_addr = symbols.addr_of("wPartyCount")
    species_addr = symbols.addr_of("wPartySpecies")
    mons_addr = symbols.addr_of("wPartyMons")
    ot_addr = symbols.addr_of("wPartyMonOT")
    nick_addr = symbols.addr_of("wPartyMonNicks")

    initial_count = int(memory[count_addr])
    if initial_count < 1:
        raise RuntimeError("source fixture has an empty party")

    repaired_pp: list[int] = []
    for move_index in range(4):
        move_id = int(memory[mons_addr + 8 + move_index])
        pp_addr = mons_addr + 29 + move_index
        pp_byte = int(memory[pp_addr])
        if move_id and not (pp_byte & 0x3F):
            memory[pp_addr] = (pp_byte & 0xC0) | 0x0A
            repaired_pp.append(move_index)

    lead_species = int(memory[species_addr])
    lead_mon = [int(memory[mons_addr + i]) & 0xFF for i in range(PARTY_MON_SIZE)]
    lead_ot = [int(memory[ot_addr + i]) & 0xFF for i in range(PARTY_OT_SIZE)]
    lead_nick = [int(memory[nick_addr + i]) & 0xFF for i in range(PARTY_NICK_SIZE)]

    for slot in range(6):
        memory[species_addr + slot] = lead_species
        for offset, byte in enumerate(lead_mon):
            memory[mons_addr + slot * PARTY_MON_SIZE + offset] = byte
        for offset, byte in enumerate(lead_ot):
            memory[ot_addr + slot * PARTY_OT_SIZE + offset] = byte
        for offset, byte in enumerate(lead_nick):
            memory[nick_addr + slot * PARTY_NICK_SIZE + offset] = byte
    memory[species_addr + 6] = 0xFF
    memory[count_addr] = 6
    return (
        f"initial_count={initial_count} final_count=6 "
        f"lead_species={lead_species} repaired_pp={repaired_pp}"
    )


def _validate_party(session) -> None:
    memory = session._pyboy.memory
    symbols = session.symbols
    count_addr = symbols.addr_of("wPartyCount")
    species_addr = symbols.addr_of("wPartySpecies")
    mons_addr = symbols.addr_of("wPartyMons")
    count = int(memory[count_addr])
    if count < 3 or count > 6:
        raise RuntimeError(f"battle fixture party count is {count}, expected 3..6")
    if int(memory[species_addr + count]) != 0xFF:
        raise RuntimeError("party species list is not FF-terminated")
    for slot in range(count):
        species = int(memory[species_addr + slot])
        mon_addr = mons_addr + slot * PARTY_MON_SIZE
        mon_species = int(memory[mon_addr])
        hp = (int(memory[mon_addr + 1]) << 8) | int(memory[mon_addr + 2])
        if species in (0, 0xFF) or mon_species != species:
            raise RuntimeError(
                f"invalid party slot {slot}: species={species} mon_species={mon_species}"
            )
        if hp <= 0:
            raise RuntimeError(f"party slot {slot} has no HP")
        for move_index in range(4):
            move_id = int(memory[mon_addr + 8 + move_index])
            pp = int(memory[mon_addr + 29 + move_index]) & 0x3F
            if move_id and pp <= 0:
                raise RuntimeError(f"party slot {slot} move {move_index} has zero PP")


def prepare_variant(
    *,
    repo_root: Path,
    rom_root: Path,
    fixture_root: Path,
    output_root: Path,
    variant: str,
) -> Path:
    # Import after argument validation so schema/help checks do not require
    # loading SDL or the emulator runtime.
    from pokered_harness.config import load_versions
    from pokered_harness.session import Session

    config = VARIANTS[variant]
    rom = rom_root / config["rom"]
    symbols = rom_root / config["symbols"]
    source = fixture_root / config["fixture_version"] / config["source"]
    output = output_root / config["fixture_version"] / config["output"]
    pins = load_versions(repo_root / "VERSIONS.md")
    expected_sha = pins.sha1_for_path(rom)
    if expected_sha is None:
        raise RuntimeError(f"ROM is not pinned in VERSIONS.md: {rom}")
    if not rom.is_file():
        raise FileNotFoundError(f"ROM not found: {rom}")
    if not symbols.is_file():
        raise FileNotFoundError(f"symbol file not found: {symbols}")
    if not source.is_file():
        raise FileNotFoundError(f"ordinary Cable Club fixture not found: {source}")

    session = Session.from_files(
        rom,
        symbols,
        expected_rom_sha1=expected_sha,
        expected_pyboy_version=pins.pyboy_version,
    )
    try:
        session.load_state(source.read_bytes())
        details = _copy_lead_party(session)
        _validate_party(session)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(session.save_state())
    finally:
        session.close()
    print(f"{variant}: wrote {output}; {details}", flush=True)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_repo = Path(__file__).resolve().parents[1]
    parser.add_argument("--repo-root", type=Path, default=default_repo)
    parser.add_argument("--rom-root", type=Path, default=None)
    parser.add_argument("--fixture-root", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=list(VARIANTS))
    args = parser.parse_args(argv)

    repo_root = args.repo_root.expanduser().resolve()
    rom_root = _root(
        args.rom_root or os.environ.get("POKERED_ROM_ROOT", "rom"), repo_root
    )
    fixture_root = _root(
        args.fixture_root
        or os.environ.get("POKERED_FIXTURE_ROOT", "tests/fixtures/link"),
        repo_root,
    )
    output_root = _root(args.output_root or fixture_root, repo_root)

    # Keep the source tree import explicit for direct script execution while
    # leaving the target checkout and all input assets untouched by default.
    import sys

    sys.path.insert(0, str(repo_root / "src"))
    for variant in args.variants:
        prepare_variant(
            repo_root=repo_root,
            rom_root=rom_root,
            fixture_root=fixture_root,
            output_root=output_root,
            variant=variant,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
