"""Live end-to-end trade demo: pair Red + Blue (or any R/B/Y pair) and
drive a full Cable Club trade, saving per-phase color screenshots and
printing wall-clock timing.

Re-uses :mod:`pokered_harness.link.pyboy_link_session.PyBoyLinkSession`
plus the same trade-drive helpers exercised by
``tests/test_pyboy_link_session_roms.py``. Those helpers live inside
the test module and aren't a stable public surface, so the small
handful we need is copied inline here (copy-over-import, as called out
in the brief — the tests aren't meant to be imported as a library).

Usage
-----

::

    python -u scripts/live_trade_demo.py
    python -u scripts/live_trade_demo.py --versions red,blue
    python -u scripts/live_trade_demo.py --outdir walkthrough_link_demo
    python -u scripts/live_trade_demo.py --venv noncython

When ``--venv`` is passed and the target venv's ``python.exe`` exists,
the script re-execs itself under that interpreter; otherwise it warns
and continues under the ambient interpreter.

Notes on the TRADE_CENTER map id
--------------------------------

The brief mentions map ``0x36`` ("LINK_CLUB Trade Center"); the proven
working constant used by the passing trade-matrix test in
``tests/test_pyboy_link_session_roms.py`` is ``0xEF`` (from pokeyellow's
``constants/map_constants.asm`` — the same value applies to Red/Blue's
internal TRADE_CENTER map). We use ``0xEF`` and assert against it so
we match the test suite that already passes 9/9.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]

# TRADE_CENTER map id (verified via passing trade-matrix test;
# pokeyellow constants/map_constants.asm names this 0xEF).
TRADE_CENTER_MAP_ID = 0xEF


# ---------------------------------------------------------------------------
# venv re-exec
# ---------------------------------------------------------------------------


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


def _open_session(version: str):
    """Mirror of ``_open_session`` in the test module."""
    os.environ.setdefault("POKERED_SKIP_SHA1", "1")
    sys.path.insert(0, str(_REPO / "src"))
    from pokered_harness.session import Session  # noqa: E402

    rom, sym = _ROM_PATHS[version]
    session = Session.from_files(rom, sym)
    session.load_state(_state_path(version).read_bytes())
    return session


# ---------------------------------------------------------------------------
# Copied trade helpers (from tests/test_pyboy_link_session_roms.py)
# ---------------------------------------------------------------------------


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


def _drive_two_sessions_to_link_menu(
    a, b, link, *, total_frames: int = 2400, frames_per_attempt: int = 20
) -> dict:
    counters = {
        "CableClubNPC": [0, 0],
        "SaveGameData": [0, 0],
        "Serial_SyncAndExchangeNybble": [0, 0],
        "Serial_ExchangeBytes": [0, 0],
        "LinkMenu": [0, 0],
    }
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)

    def tick_both_coarse(frames: int) -> None:
        for _ in range(frames):
            a.step(1)
            b.step(1)

    def tick_both_fine(frames: int) -> None:
        link.step_interleaved(frames)

    frames_used = 0
    for _ in range(3):
        a.press("up", duration=6)
        b.press("up", duration=6)
        tick_both_coarse(20)
        frames_used += 20

    attempts = (total_frames - frames_used) // frames_per_attempt
    for _ in range(attempts):
        if counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0:
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        in_serial_phase = (
            counters["SaveGameData"][0] > 0 or counters["SaveGameData"][1] > 0
        )
        if in_serial_phase:
            tick_both_fine(frames_per_attempt)
        else:
            tick_both_coarse(frames_per_attempt)
        frames_used += frames_per_attempt

    return {"counters": counters, "frames_used": frames_used}


def _drive_past_link_menu_to_trade_center(
    a, b, link, *, post_link_menu_frames: int = 1200, frames_per_attempt: int = 20,
    mid_callback=None,
) -> dict:
    diag = _drive_two_sessions_to_link_menu(a, b, link)
    counters = diag["counters"]
    assert counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0, (
        "precondition: both sides must have reached LinkMenu before "
        "attempting TRADE_CENTER warp"
    )

    extra_frames = 0
    called_mid = False
    attempts = post_link_menu_frames // frames_per_attempt
    for _ in range(attempts):
        map_a = a.read_game_state().overworld.map_id
        map_b = b.read_game_state().overworld.map_id
        if map_a == TRADE_CENTER_MAP_ID and map_b == TRADE_CENTER_MAP_ID:
            break
        if mid_callback is not None and not called_mid and extra_frames >= 120:
            mid_callback()
            called_mid = True
        a.press("a", duration=4)
        b.press("a", duration=4)
        link.step_interleaved(frames_per_attempt)
        extra_frames += frames_per_attempt

    map_a = a.read_game_state().overworld.map_id
    map_b = b.read_game_state().overworld.map_id
    return {
        "counters": counters,
        "frames_to_link_menu": diag["frames_used"],
        "extra_frames": extra_frames,
        "final_map_a": map_a,
        "final_map_b": map_b,
    }


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


def _drive_complete_trade(
    a, b, link, *, counters: dict,
    trade_budget_frames: int = 4000, step_frames: int = 20,
    mid_callback=None,
) -> dict:
    add_mon = counters["_AddEnemyMonToPlayerParty"]
    trade_center_trade = counters["TradeCenter_Trade"]

    def tick_interleaved(frames: int) -> None:
        link.step_interleaved(frames)

    def tick_per_frame(frames: int) -> None:
        for _ in range(frames):
            a.step(1)
            b.step(1)

    conn_a_now = a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")]
    conn_b_now = b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")]
    INTERNAL = 0x02
    dir_a = "right" if conn_a_now == INTERNAL else "left"
    dir_b = "right" if conn_b_now == INTERNAL else "left"

    for _ in range(4):
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        a.press(dir_a, duration=8)
        b.press(dir_b, duration=8)
        tick_per_frame(step_frames)

    settle_frames = 0
    while settle_frames < 1800:
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                or counters["CableClub_DoBattleOrTrade"][1] > 0):
            tick_interleaved(step_frames)
        else:
            tick_per_frame(step_frames)
        settle_frames += step_frames

    stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
    trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
    menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
    tct_key = "TradeCenter_Trade"

    prev = {
        k: list(counters[k])
        for k in (stats_key, trade_key, menu_key, tct_key)
    }
    right_pending = [0, 0]
    RIGHT_PRESS_ITERATIONS = 5

    extra_frames = 0
    called_anim_shot = False
    attempts = trade_budget_frames // step_frames
    for _ in range(attempts):
        if add_mon[0] > 0 and add_mon[1] > 0:
            break
        cct_key = "CableClub_DoBattleOrTrade"
        if counters[cct_key][0] > 0 or counters[cct_key][1] > 0:
            tick_interleaved(step_frames)
        else:
            tick_per_frame(step_frames)
        extra_frames += step_frames

        # Capture a mid-trade-animation frame once TradeCenter_Trade has
        # actually been entered.
        if (mid_callback is not None and not called_anim_shot
                and trade_center_trade[0] > 0 and trade_center_trade[1] > 0):
            mid_callback()
            called_anim_shot = True

        now = {k: list(counters[k]) for k in (stats_key, trade_key, menu_key, tct_key)}
        for idx, sess in enumerate((a, b)):
            def ticked(key, _idx=idx):
                return now[key][_idx] > prev[key][_idx]

            if ticked(trade_key):
                sess.press("a", duration=4)
                right_pending[idx] = 0
            elif ticked(stats_key):
                right_pending[idx] = RIGHT_PRESS_ITERATIONS
                sess.press("right", duration=12)
            elif right_pending[idx] > 0:
                sess.press("right", duration=12)
                right_pending[idx] -= 1
            elif ticked(menu_key):
                sess.press("a", duration=4)
            elif ticked(tct_key):
                sess.press("a", duration=4)
            else:
                sess.press("a", duration=4)
        prev = now

    return {
        "add_mon": add_mon,
        "trade_center_trade": trade_center_trade,
        "trade_phase_frames": extra_frames,
        "counters": counters,
    }


# ---------------------------------------------------------------------------
# Helpers: screenshot + species naming + party state
# ---------------------------------------------------------------------------


# Gen-I *internal* species IDs as used in wPartyMons.Species — this is
# NOT the pokedex number; it's the hardware-ordering internal ID from
# pokered/constants/pokemon_constants.asm. Table below covers every
# canonical ID; unknown IDs render as a hex fallback.
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
    except Exception:
        return b""
    return bytes(pb.memory[addr + i] for i in range(11))


@dataclass
class Shooter:
    outdir: Path

    def shoot(self, session_label: str, session, stem: str) -> Path:
        """Save ``session``'s current framebuffer to ``stem__<label>.png``.
        Returns the absolute path. Uses PyBoy's built-in
        ``pyboy.screen.image`` which returns a PIL image (color).
        """
        path = self.outdir / f"{stem}__{session_label}.png"
        session._pyboy.screen.image.save(path)
        return path

    def shoot_pair(self, a, b, stem: str) -> list[Path]:
        return [self.shoot("red", a, stem), self.shoot("blue", b, stem)]


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


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
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    _maybe_reexec_under_venv(args.venv)

    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    if len(versions) != 2:
        print(f"[error] --versions must be 'a,b'; got {args.versions!r}",
              flush=True)
        return 2
    version_a, version_b = versions
    for v in (version_a, version_b):
        if v not in _ROM_PATHS:
            print(f"[error] unknown version {v!r}; choose from "
                  f"{sorted(_ROM_PATHS)}", flush=True)
            return 2

    _assert_fixtures_available(version_a)
    _assert_fixtures_available(version_b)

    outdir = (_REPO / args.outdir) if not Path(args.outdir).is_absolute() \
        else Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    shoot = Shooter(outdir)

    print(f"[info] versions: A={version_a}, B={version_b}", flush=True)
    print(f"[info] outdir:   {outdir}", flush=True)
    print(f"[info] python:   {sys.executable}", flush=True)

    # Ensure harness on sys.path before we import pokered_harness here.
    sys.path.insert(0, str(_REPO / "src"))
    from pokered_harness.link.pyboy_link_session import PyBoyLinkSession  # noqa

    t_all_start = time.perf_counter()

    a = _open_session(version_a)
    b = _open_session(version_b)
    try:
        # Phase A: pair + start.
        t0 = time.perf_counter()
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)
        t_pair = time.perf_counter() - t0
        print(f"[pair attach] {t_pair:.2f}s", flush=True)

        pre_a_species = _lead_species(a)
        pre_b_species = _lead_species(b)
        pre_a_ot = _lead_ot_fingerprint(a)
        pre_b_ot = _lead_ot_fingerprint(b)

        ow_a = a.read_game_state().overworld
        ow_b = b.read_game_state().overworld
        print(
            f"[start] A coord=({ow_a.x},{ow_a.y}) map=0x{ow_a.map_id:02x} | "
            f"B coord=({ow_b.x},{ow_b.y}) map=0x{ow_b.map_id:02x}",
            flush=True,
        )
        start_png = shoot.shoot_pair(a, b, "phase_00_start")
        for p in start_png:
            print(f"  wrote {p}", flush=True)

        # Install trade-phase diag counters *before* warp so events that
        # fire during CableClub_DoBattleOrTrade are caught.
        diag_counters = _install_trade_diag_counters(a, b)

        # Phase B: drive past LinkMenu to TRADE_CENTER warp.
        t0 = time.perf_counter()

        def _mid_linkmenu_shot():
            # Captures a mid-warp frame (typically while the "Please
            # wait" / menu-exchange is in progress).
            paths = shoot.shoot_pair(a, b, "phase_01_linkmenu")
            for p in paths:
                print(f"  wrote {p}", flush=True)

        warp = _drive_past_link_menu_to_trade_center(
            a, b, link, mid_callback=_mid_linkmenu_shot,
        )
        t_phase_b = time.perf_counter() - t0
        print(f"[phase B: link menu -> trade center] {t_phase_b:.2f}s",
              flush=True)

        if warp.get("extra_frames", 0) < 120:
            # Warp happened too quickly for the mid-callback to trigger;
            # grab a post-warp snapshot under the linkmenu stem so the
            # demo always emits that file.
            paths = shoot.shoot_pair(a, b, "phase_01_linkmenu")
            for p in paths:
                print(f"  wrote {p}", flush=True)

        assert warp["final_map_a"] == TRADE_CENTER_MAP_ID, (
            f"A didn't warp to TRADE_CENTER; got "
            f"0x{warp['final_map_a']:02x}, expected 0x{TRADE_CENTER_MAP_ID:02x}"
        )
        assert warp["final_map_b"] == TRADE_CENTER_MAP_ID, (
            f"B didn't warp to TRADE_CENTER; got "
            f"0x{warp['final_map_b']:02x}, expected 0x{TRADE_CENTER_MAP_ID:02x}"
        )

        trade_center_png = shoot.shoot_pair(a, b, "phase_02_trade_center")
        for p in trade_center_png:
            print(f"  wrote {p}", flush=True)

        # Phase C: drive the complete trade.
        t0 = time.perf_counter()
        trade_start_png = shoot.shoot_pair(a, b, "phase_03_trade_start")
        for p in trade_start_png:
            print(f"  wrote {p}", flush=True)

        def _mid_trade_anim_shot():
            paths = shoot.shoot_pair(a, b, "phase_04_trade_animation")
            for p in paths:
                print(f"  wrote {p}", flush=True)

        trade_diag = _drive_complete_trade(
            a, b, link, counters=diag_counters,
            mid_callback=_mid_trade_anim_shot,
        )
        t_phase_c = time.perf_counter() - t0
        print(f"[phase C: complete trade] {t_phase_c:.2f}s", flush=True)

        add_mon = trade_diag["add_mon"]
        if add_mon[0] == 0 or add_mon[1] == 0:
            print(
                f"[warn] _AddEnemyMonToPlayerParty hooks didn't fire on "
                f"both sides: {add_mon}. Trade may have stalled; "
                f"continuing to post snapshots for diagnostics.",
                flush=True,
            )

        # Ensure we got an animation shot even if mid_callback didn't fire
        # (e.g. TradeCenter_Trade hook resolved too fast to catch between
        # our polling steps).
        anim_shot = outdir / "phase_04_trade_animation__red.png"
        if not anim_shot.exists():
            paths = shoot.shoot_pair(a, b, "phase_04_trade_animation")
            for p in paths:
                print(f"  wrote {p} (late-catch)", flush=True)

        trade_done_png = shoot.shoot_pair(a, b, "phase_05_trade_done")
        for p in trade_done_png:
            print(f"  wrote {p}", flush=True)

        # Let a few extra frames tick so the party reflects the swap.
        link.step_interleaved(30)

        post_a_species = _lead_species(a)
        post_b_species = _lead_species(b)
        post_a_ot = _lead_ot_fingerprint(a)
        post_b_ot = _lead_ot_fingerprint(b)

        post_png = shoot.shoot_pair(a, b, "phase_06_post")
        for p in post_png:
            print(f"  wrote {p}", flush=True)

        t_total = time.perf_counter() - t_all_start
        print(f"[total wall-clock] {t_total:.2f}s", flush=True)

        pre_a_str = _species_name(pre_a_species) if pre_a_species is not None else "<none>"
        pre_b_str = _species_name(pre_b_species) if pre_b_species is not None else "<none>"
        post_a_str = _species_name(post_a_species) if post_a_species is not None else "<none>"
        post_b_str = _species_name(post_b_species) if post_b_species is not None else "<none>"

        print(
            f"pre:  {version_a} lead = {pre_a_str}, "
            f"{version_b} lead = {pre_b_str}",
            flush=True,
        )
        # Primary check: _AddEnemyMonToPlayerParty fires on both sides
        # means the game engine actually installed a peer mon into each
        # side's party. Species-equality is a weak secondary signal —
        # if both sides start with the same species the lead species
        # stays the same after a straight swap. OT-name fingerprint
        # flips in that case, so we use it as an extra signal.
        species_changed = (
            pre_a_species != post_a_species and pre_b_species != post_b_species
        )
        ot_changed = pre_a_ot != post_a_ot and pre_b_ot != post_b_ot
        engine_trade = add_mon[0] > 0 and add_mon[1] > 0
        trade_happened = engine_trade and (species_changed or ot_changed)
        tag = "  <- TRADE SUCCEEDED" if trade_happened else "  <- TRADE DID NOT COMPLETE"
        print(
            f"post: {version_a} lead = {post_a_str}, "
            f"{version_b} lead = {post_b_str}{tag}",
            flush=True,
        )
        print(
            f"       _AddEnemyMonToPlayerParty hooks: A={add_mon[0]}, "
            f"B={add_mon[1]}; species-changed={species_changed}; "
            f"OT-fingerprint-changed={ot_changed}",
            flush=True,
        )

        if not trade_happened:
            print(
                "[error] trade did not complete end-to-end. See diag "
                "counters above and the PNGs under the outdir.",
                flush=True,
            )
            return 1
        return 0
    finally:
        try:
            a.close()
        except Exception:
            pass
        try:
            b.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
