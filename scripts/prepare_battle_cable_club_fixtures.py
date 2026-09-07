"""Create battle-ready Cable Club fixtures with legal parties.

The trade Cable Club fixtures intentionally carry a one-Pokemon party.
Gen 1 Colosseum requires at least three party Pokemon, and the link data
exchange serializes all six party slots. This script prepares separate
battle fixtures by cloning the lead into all six slots and repairing any
zero-PP lead moves before saving the state. The TCP proof runner then
loads these saved states directly without mutating party data at runtime.
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
        "rom": ("red", "pokemon-red.gb"),
        "sym": ("red", "pokemon-red.sym"),
        "fixture_dir": "red",
        "source": "cable_club-vanilla.state",
        "out": "cable_club-battle-vanilla.state",
    },
    "red_color": {
        "rom": ("red", "pokemon-red-color.gb"),
        "sym": ("red", "pokemon-red.sym"),
        "fixture_dir": "red",
        "source": "cable_club.state",
        "out": "cable_club-battle.state",
    },
    "blue_gb": {
        "rom": ("blue", "pokemon-blue.gb"),
        "sym": ("blue", "pokemon-blue.sym"),
        "fixture_dir": "blue",
        "source": "cable_club-vanilla.state",
        "out": "cable_club-battle-vanilla.state",
    },
    "blue_color": {
        "rom": ("blue", "pokemon-blue-color.gb"),
        "sym": ("blue", "pokemon-blue.sym"),
        "fixture_dir": "blue",
        "source": "cable_club.state",
        "out": "cable_club-battle.state",
    },
    "yellow": {
        "rom": ("yellow", "pokemon-yellow.gbc"),
        "sym": ("yellow", "pokemon-yellow.sym"),
        "fixture_dir": "yellow",
        "source": "cable_club.state",
        "out": "cable_club-battle.state",
    },
}


def _prepare_party(session) -> str:
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count_addr = addr_of("wPartyCount")
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    ot_addr = addr_of("wPartyMonOT")
    nick_addr = addr_of("wPartyMonNicks")

    initial_count = int(pb.memory[count_addr])
    if initial_count <= 0:
        raise RuntimeError("source fixture has an empty party")

    repaired_pp: list[int] = []
    for i in range(4):
        pp_addr = mons_addr + 29 + i
        move_id = int(pb.memory[mons_addr + 8 + i])
        pp_byte = int(pb.memory[pp_addr])
        if move_id != 0 and (pp_byte & 0x3F) == 0:
            pb.memory[pp_addr] = (pp_byte & 0xC0) | 0x0A
            repaired_pp.append(i)

    lead_species = int(pb.memory[species_addr])
    lead_mon = [int(pb.memory[mons_addr + i]) & 0xFF for i in range(PARTY_MON_SIZE)]
    lead_ot = [int(pb.memory[ot_addr + i]) & 0xFF for i in range(PARTY_OT_SIZE)]
    lead_nick = [int(pb.memory[nick_addr + i]) & 0xFF for i in range(PARTY_NICK_SIZE)]

    for slot in range(6):
        pb.memory[species_addr + slot] = lead_species
        for i, byte in enumerate(lead_mon):
            pb.memory[mons_addr + slot * PARTY_MON_SIZE + i] = byte
        for i, byte in enumerate(lead_ot):
            pb.memory[ot_addr + slot * PARTY_OT_SIZE + i] = byte
        for i, byte in enumerate(lead_nick):
            pb.memory[nick_addr + slot * PARTY_NICK_SIZE + i] = byte
    pb.memory[species_addr + 6] = 0xFF
    pb.memory[count_addr] = 6
    return (
        f"initial_count={initial_count} final_count=6 "
        f"lead_species={lead_species} repaired_pp={repaired_pp}"
    )


def _validate_party(session) -> None:
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count_addr = addr_of("wPartyCount")
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    count = int(pb.memory[count_addr])
    if count < 3:
        raise RuntimeError(f"battle fixture party count is {count}, expected >=3")
    if int(pb.memory[species_addr + count]) != 0xFF:
        raise RuntimeError("party species list is not FF-terminated")
    for slot in range(count):
        species = int(pb.memory[species_addr + slot])
        mon_species = int(pb.memory[mons_addr + slot * PARTY_MON_SIZE])
        hp = int(pb.memory[mons_addr + slot * PARTY_MON_SIZE + 1]) << 8
        hp |= int(pb.memory[mons_addr + slot * PARTY_MON_SIZE + 2])
        if species == 0 or species == 0xFF or mon_species != species:
            raise RuntimeError(
                f"invalid party slot {slot}: species={species} mon_species={mon_species}"
            )
        if hp <= 0:
            raise RuntimeError(f"party slot {slot} has no HP")
        for move_idx in range(4):
            move_id = int(pb.memory[mons_addr + slot * PARTY_MON_SIZE + 8 + move_idx])
            pp = int(pb.memory[mons_addr + slot * PARTY_MON_SIZE + 29 + move_idx]) & 0x3F
            if move_id != 0 and pp <= 0:
                raise RuntimeError(f"party slot {slot} move {move_idx} has zero PP")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    args = parser.parse_args()

    repo = args.repo_root.resolve()
    os.environ.setdefault("POKERED_SKIP_SHA1", "1")

    from pokered_harness.session import Session

    for variant in args.variants:
        cfg = VARIANTS[variant]
        rom = repo / "rom" / cfg["rom"][0] / cfg["rom"][1]
        sym = repo / "rom" / cfg["sym"][0] / cfg["sym"][1]
        source = repo / "tests" / "fixtures" / "link" / cfg["fixture_dir"] / cfg["source"]
        out = repo / "tests" / "fixtures" / "link" / cfg["fixture_dir"] / cfg["out"]
        session = Session.from_files(rom, sym)
        try:
            session.load_state(source.read_bytes())
            prep = _prepare_party(session)
            _validate_party(session)
            out.write_bytes(session.save_state())
            print(f"{variant}: wrote {out.relative_to(repo)}; {prep}", flush=True)
        finally:
            session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
