"""Fixture resolution, hook counters, species naming, and CLI parsing
helpers for ``scripts/live_trade_demo.py``.

Split out of the demo entrypoint (issue #140). Every definition below is
relocated byte-for-byte from the original script so runtime behavior and
the demo's contract tests are unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _maybe_reexec_under_venv(venv_tag: str | None) -> None:
    """If ``--venv`` points at an existing ``.venv-<tag>/Scripts/python.exe``,
    re-exec this script under it. Otherwise print a warning and fall
    back to the ambient interpreter. No-op when ``venv_tag`` is None or
    we're already running under that venv.
    """
    if not venv_tag:
        return
    target = _REPO / f".venv-{venv_tag}" / "Scripts" / "python.exe"
    if not target.is_file():
        print(
            f"[warn] --venv {venv_tag} requested but {target} not found; "
            f"falling back to ambient interpreter {sys.executable}",
            flush=True,
        )
        return
    # Already under that interp?
    try:
        if Path(sys.executable).resolve() == target.resolve():
            return
    except OSError:
        pass

    # Preserve all CLI args except drop --venv (we've already resolved it).
    argv = [str(target)]
    skip = False
    for arg in sys.argv:
        if skip:
            skip = False
            continue
        if arg == "--venv":
            skip = True
            continue
        if arg.startswith("--venv="):
            continue
        argv.append(arg)
    print(f"[info] re-exec under {target}", flush=True)
    os.execv(str(target), argv)


# ---------------------------------------------------------------------------
# ROM + fixture resolution
# ---------------------------------------------------------------------------


_ROM_PATHS: dict[str, tuple[Path, Path]] = {
    "red": (
        _REPO / "rom" / "red" / "pokemon-red-color.gb",
        _REPO / "rom" / "red" / "pokemon-red.sym",
    ),
    "blue": (
        _REPO / "rom" / "blue" / "pokemon-blue-color.gb",
        _REPO / "rom" / "blue" / "pokemon-blue.sym",
    ),
    "yellow": (
        _REPO / "rom" / "yellow" / "pokemon-yellow.gbc",
        _REPO / "rom" / "yellow" / "pokemon-yellow.sym",
    ),
}


def _state_path(version: str) -> Path:
    return _REPO / "tests" / "fixtures" / "link" / version / "cable_club.state"


def _assert_fixtures_available(version: str) -> None:
    rom, sym = _ROM_PATHS[version]
    state = _state_path(version)
    missing: list[Path] = []
    for p in (rom, sym, state):
        if not p.is_file():
            missing.append(p)
    if missing:
        print(
            "[error] required BYO-ROM / fixture files are missing:",
            flush=True,
        )
        for p in missing:
            print(f"   - {p}", flush=True)
        print(
            "    Generate the Cable Club fixture with "
            "'scripts/produce_cable_club_fixture.py' and ensure the "
            "Full Color Hack v1.2 ROMs are dropped under rom/.",
            flush=True,
        )
        sys.exit(2)




def _install_hook_counter(session, symbol: str, bucket: list, slot: int) -> None:
    if symbol not in session.symbols:
        return
    bank, addr = session.symbols.bank_addr(symbol)

    def _cb(_ctx: object) -> None:
        bucket[slot] += 1

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        pass


_TRADE_DIAG_SYMBOLS = (
    "CableClub_DoBattleOrTrade",
    "CallCurrentTradeCenterFunction",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_Trade",
    "_AddEnemyMonToPlayerParty",
)


def _install_trade_diag_counters(a, b) -> dict:
    counters = {sym: [0, 0] for sym in _TRADE_DIAG_SYMBOLS}
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)
    return counters


_GEN1_SPECIES: dict[int, str] = {
    0x01: "Rhydon", 0x02: "Kangaskhan", 0x03: "Nidoran-M", 0x04: "Clefairy",
    0x05: "Spearow", 0x06: "Voltorb", 0x07: "Nidoking", 0x08: "Slowbro",
    0x09: "Ivysaur", 0x0A: "Exeggutor", 0x0B: "Lickitung", 0x0C: "Exeggcute",
    0x0D: "Grimer", 0x0E: "Gengar", 0x0F: "Nidoran-F", 0x10: "Nidoqueen",
    0x11: "Cubone", 0x12: "Rhyhorn", 0x13: "Lapras", 0x14: "Arcanine",
    0x15: "Mew", 0x16: "Gyarados", 0x17: "Shellder", 0x18: "Tentacool",
    0x19: "Gastly", 0x1A: "Scyther", 0x1B: "Staryu", 0x1C: "Blastoise",
    0x1D: "Pinsir", 0x1E: "Tangela",
    0x21: "Growlithe", 0x22: "Onix", 0x23: "Fearow", 0x24: "Pidgey",
    0x25: "Slowpoke", 0x26: "Kadabra", 0x27: "Graveler", 0x28: "Chansey",
    0x29: "Machoke", 0x2A: "Mr.Mime", 0x2B: "Hitmonlee", 0x2C: "Hitmonchan",
    0x2D: "Arbok", 0x2E: "Parasect", 0x2F: "Psyduck", 0x30: "Drowzee",
    0x31: "Golem",
    0x33: "Magmar",
    0x35: "Electabuzz", 0x36: "Magneton", 0x37: "Koffing",
    0x39: "Mankey", 0x3A: "Seel", 0x3B: "Diglett", 0x3C: "Tauros",
    0x40: "Farfetch'd", 0x41: "Venonat", 0x42: "Dragonite",
    0x46: "Doduo", 0x47: "Poliwag", 0x48: "Jynx", 0x49: "Moltres",
    0x4A: "Articuno", 0x4B: "Zapdos", 0x4C: "Ditto", 0x4D: "Meowth",
    0x4E: "Krabby",
    0x52: "Vulpix", 0x53: "Ninetales", 0x54: "Pikachu", 0x55: "Raichu",
    0x58: "Dratini", 0x59: "Dragonair", 0x5A: "Kabuto", 0x5B: "Kabutops",
    0x5C: "Horsea", 0x5D: "Seadra",
    0x60: "Sandshrew", 0x61: "Sandslash", 0x62: "Omanyte", 0x63: "Omastar",
    0x64: "Jigglypuff", 0x65: "Wigglytuff", 0x66: "Eevee", 0x67: "Flareon",
    0x68: "Jolteon", 0x69: "Vaporeon", 0x6A: "Machop", 0x6B: "Zubat",
    0x6C: "Ekans", 0x6D: "Paras", 0x6E: "Poliwhirl", 0x6F: "Poliwrath",
    0x70: "Weedle", 0x71: "Kakuna", 0x72: "Beedrill",
    0x74: "Dodrio", 0x75: "Primeape", 0x76: "Dugtrio", 0x77: "Venomoth",
    0x78: "Dewgong",
    0x7B: "Caterpie", 0x7C: "Metapod", 0x7D: "Butterfree", 0x7E: "Machamp",
    0x80: "Golduck", 0x81: "Hypno", 0x82: "Golbat", 0x83: "Mewtwo",
    0x84: "Snorlax", 0x85: "Magikarp",
    0x88: "Muk",
    0x8A: "Kingler", 0x8B: "Cloyster",
    0x8D: "Electrode", 0x8E: "Clefable", 0x8F: "Weezing",
    0x90: "Persian", 0x91: "Marowak",
    0x93: "Haunter", 0x94: "Abra", 0x95: "Alakazam", 0x96: "Pidgeotto",
    0x97: "Pidgeot", 0x98: "Starmie", 0x99: "Bulbasaur", 0x9A: "Venusaur",
    0x9B: "Tentacruel",
    0x9D: "Goldeen", 0x9E: "Seaking",
    0xA3: "Ponyta", 0xA4: "Rapidash", 0xA5: "Rattata", 0xA6: "Raticate",
    0xA7: "Nidorino", 0xA8: "Nidorina", 0xA9: "Geodude", 0xAA: "Porygon",
    0xAB: "Aerodactyl",
    0xAD: "Magnemite",
    0xB0: "Charmander", 0xB1: "Squirtle", 0xB2: "Charmeleon",
    0xB3: "Wartortle", 0xB4: "Charizard",
    0xB9: "Oddish", 0xBA: "Gloom", 0xBB: "Vileplume", 0xBC: "Bellsprout",
    0xBD: "Weepinbell", 0xBE: "Victreebel",
}


def _species_name(species_id: int) -> str:
    name = _GEN1_SPECIES.get(species_id)
    if name is not None:
        return f"{name}({species_id:03d})"
    return f"UNKNOWN(0x{species_id:02x})"


def _lead_species(session) -> int | None:
    party = session.read_game_state().party
    return party.lead.species if party.lead else None


def _lead_ot_fingerprint(session) -> bytes:
    """Read 11 bytes of the lead's wPartyMonOT slot 0 — the Original
    Trainer name, which changes after a successful trade even when the
    species is identical. Used as an additional "the trade actually
    happened" signal when both peers start with the same species.
    """
    pb = session._pyboy
    try:
        addr = session.symbols.addr_of("wPartyMonOT")
    except (AttributeError, LookupError):
        return b""
    return bytes(pb.memory[addr + i] for i in range(11))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live end-to-end trade demo: pair two Pokemon ROMs, "
        "drive a complete Cable Club trade, save per-phase color PNGs."
    )
    p.add_argument(
        "--versions", default="red,blue",
        help="Comma-separated 'a,b' versions. Default: red,blue.",
    )
    p.add_argument(
        "--outdir", default="walkthrough_link_demo",
        help="Output directory for PNGs. Created if missing. "
             "Default: walkthrough_link_demo.",
    )
    p.add_argument(
        "--venv", choices=["cython", "noncython"], default=None,
        help="Re-exec under .venv-<tag>/Scripts/python.exe if it exists; "
             "otherwise warn and use ambient interp.",
    )
    p.add_argument(
        "--view", action="store_true",
        help="Open SDL2 windows for both peers so the trade is visible live.",
    )
    p.add_argument(
        "--sample-every", type=int, default=0, metavar="N",
        help="If > 0, capture a framebuffer snapshot from both peers every "
             "N driver iterations during all phases into "
             "'<outdir>/timeline/<NNNN>__{red,blue}.png'. Used for visually "
             "verifying navigation when the SDL2 window isn't being watched.",
    )
    p.add_argument(
        "--speed", type=float, default=1.0, metavar="FACTOR",
        help="Emulation speed multiplier; 1.0 = real-time, 0.5 = half speed, "
             "2.0 = double speed. Default 1.0. Use <1 with --view to make "
             "menu navigation (LinkMenu BATTLE/TRADE, mon-select, TRADE "
             "confirmation) easier to follow visually.",
    )
    p.add_argument(
        "--hold-after-s", type=int, default=90, metavar="N",
        help="Seconds to idle both emulators after the trade completes "
             "so you can watch the final state on the live windows. "
             "Default 90. Only applies with --view.",
    )
    p.add_argument(
        "--no-cgb", action="store_true",
        help="Force DMG mode (cgb=False). Loses the Full Color Hack palette "
             "but makes Gen 1 menu/dialog overlays render correctly. Use for "
             "diagnosing whether the CGB+ColorHack combo is masking menus.",
    )
    p.add_argument(
        "--natural", action="store_true",
        help="Drive the trade the way a human would: pause on each key "
             "menu (save prompt, LinkMenu BATTLE/TRADE/CANCEL, mon-select "
             "STATS/TRADE, YES/NO confirmation, 'Take good care!'), and "
             "briefly move the LinkMenu cursor down to BATTLE and back up "
             "to TRADE so you can see the choice. Off by default so "
             "tests/matrix runs stay fast.",
    )
    return p.parse_args()
