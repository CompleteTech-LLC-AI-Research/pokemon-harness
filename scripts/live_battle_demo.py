"""Diagnostic-only live Cable Club BATTLE demonstration.

Pair Red + Blue (or any R/B/Y pair) and drive a Colosseum link battle from
the receptionist through at least one completed turn. This driver deliberately
writes party RAM (it clones the lead Pokemon) and is therefore a controlled
emulator diagnostic, not human-valid gameplay, a production acceptance run,
or clean fixture provenance.

Differences from ``scripts/live_trade_demo.py``:

- At the LinkMenu the cursor is moved DOWN to select **COLOSSEUM**
  (map 0xF0) instead of TRADE_CENTER (0xEF).
- Parties are padded to 3 mons in RAM before the battle (Gen 1
  Colosseum requires >=3). Zero-PP moves on the lead are repaired so
  'press A on first move' doesn't beep.
- The battle driver follows the proven pattern in
  ``tests/test_pyboy_link_session_roms.py::_drive_complete_battle_turn``:
  sub-frame-interleaved serial ticks plus A-mash until
  ``PlayerCalcMoveDamage`` has fired on both peers.

Usage
-----

::

    python -u scripts/live_battle_demo.py --view --versions red,blue

Requires the same BYO ROMs + cable_club fixture as the trade demo.
ROM SHA-1 and symbol SHA-1 pins are loaded from ``VERSIONS.md``; an unpinned
or mismatched ROM is rejected. The result is diagnostic evidence only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# Cable Club Colosseum map id (pokered/pokeyellow constants name this
# 0xF0; adjacent to TRADE_CENTER at 0xEF).
COLOSSEUM_MAP_ID = 0xF0

# Party-record sizes for the _pad_party_to_3 helper.
PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11


def _close_sessions(
    *labeled_sessions: tuple[str, object | None],
    active_error: BaseException | None = None,
) -> None:
    """Close every session, then surface all teardown failures."""
    failures: list[Exception] = []
    for label, session in labeled_sessions:
        if session is None:
            continue
        try:
            session.close()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - cleanup must continue
            exc.add_note(f"diagnostic cleanup failed for session {label}")
            failures.append(exc)
            print(
                f"[diagnostic cleanup] failed to close session {label}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    if failures:
        cleanup_error = ExceptionGroup("diagnostic cleanup failed", failures)
        if active_error is not None:
            raise cleanup_error from active_error
        raise cleanup_error


def _open_validated_session(
    version: str,
    *,
    view: bool = False,
    window_pos: tuple[int, int] | None = None,
):
    """Open one diagnostic session after validating configured asset pins."""
    from live_trade_demo import _ROM_PATHS, _state_path
    from pyboy import PyBoy

    from pokered_harness.config import load_versions
    from pokered_harness.session import Session

    rom, sym = _ROM_PATHS[version]
    pins = load_versions(_REPO / "VERSIONS.md")
    expected_rom_sha1 = pins.sha1_for_path(rom)
    expected_symbol_sha1 = pins.symbol_sha1_for_path(sym)
    if expected_rom_sha1 is None:
        raise ValueError(f"ROM is not pinned in VERSIONS.md: {rom}")
    if expected_symbol_sha1 is None:
        raise ValueError(f"symbol file is not pinned in VERSIONS.md: {sym}")

    if view and window_pos is not None:
        os.environ["SDL_VIDEO_WINDOW_POS"] = f"{window_pos[0]},{window_pos[1]}"

    def _factory(path: str):
        return PyBoy(
            path,
            window="SDL2" if view else "null",
            cgb=True,
            sound_emulated=False,
        )

    session = None
    try:
        session = Session.from_files(
            rom,
            sym,
            expected_rom_sha1=expected_rom_sha1,
            expected_symbol_sha1=expected_symbol_sha1,
            expected_pyboy_version=pins.pyboy_version,
            expected_pyboy_revision=pins.pyboy_revision,
            view=view,
            pyboy_factory=_factory,
        )
        state_path = _state_path(version)
        session.load_state(state_path.read_bytes())
        session.step(12, render=True)
        return session
    except BaseException as exc:
        _close_sessions((version, session), active_error=exc)
        raise


def _open_battle_sessions(
    version_a: str,
    version_b: str,
    *,
    view: bool,
):
    """Open and prepare both peers, closing partial setup on any failure."""
    from live_trade_demo import _find_pyboy_hwnds, _force_move_pyboy_windows

    a = b = None
    try:
        a = _open_validated_session(version_a, view=view, window_pos=(80, 120))
        b = _open_validated_session(version_b, view=view, window_pos=(640, 120))

        for label, sess in (("A", a), ("B", b)):
            _padded, reason = _pad_party_to_3(sess)
            print(f"[diagnostic party pad {label}] {reason}")

        if view:
            if _force_move_pyboy_windows([(200, 300), (1200, 300)]):
                print("[diagnostic] moved PyBoy windows to (200,300) and (1200,300)")
            _pyboy_hwnds = _find_pyboy_hwnds()
            print(f"[diagnostic] hwnds: {_pyboy_hwnds}")
        return a, b
    except BaseException as exc:
        _close_sessions(("A", a), ("B", b), active_error=exc)
        raise


def _pad_party_to_3(session) -> tuple[bool, str]:
    """Gen 1 Cable Club Colosseum requires >=3 Pokemon per team. The
    cable_club fixture was produced by a grind-only walkthrough that
    trained the starter but never caught additional mons. This helper
    fills slots 1 and 2 with clones of the lead so the team is
    regulation-legal and the cable-club battle engine accepts it.

    This IS a memory write into wPartyMons/OT/Nicks - the one
    remaining place the demo touches party state directly. A fully
    player-accurate alternative is to regenerate the cable_club
    fixture from a walkthrough that catches two additional mons
    (a task for scripts/produce_cable_club_fixture.py); done once,
    this helper can be removed.

    Also restores zero-PP moves on the lead to 10 PP so "press A to
    pick first move" doesn't beep on a depleted slot.
    """
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count_addr = addr_of("wPartyCount")
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    ot_addr = addr_of("wPartyMonOT")
    nick_addr = addr_of("wPartyMonNicks")

    count = pb.memory[count_addr]
    if count >= 3:
        return (False, f"party already has {count} mons, no padding needed")

    # Restore 0-PP moves on the lead (grinds can leave VINE WHIP at 0).
    repaired_pp = []
    for i in range(4):
        pp_addr = mons_addr + 29 + i
        pp_byte = pb.memory[pp_addr]
        if (pp_byte & 0x3F) == 0 and pb.memory[mons_addr + 8 + i] != 0:
            pb.memory[pp_addr] = (pp_byte & 0xC0) | 0x0A
            repaired_pp.append(i)

    lead_species = pb.memory[species_addr]
    lead_mon = [pb.memory[mons_addr + i] for i in range(PARTY_MON_SIZE)]
    lead_ot = [pb.memory[ot_addr + i] for i in range(PARTY_OT_SIZE)]
    lead_nick = [pb.memory[nick_addr + i] for i in range(PARTY_NICK_SIZE)]

    for slot in (1, 2):
        pb.memory[species_addr + slot] = lead_species
        for i, byte in enumerate(lead_mon):
            pb.memory[mons_addr + slot * PARTY_MON_SIZE + i] = byte
        for i, byte in enumerate(lead_ot):
            pb.memory[ot_addr + slot * PARTY_OT_SIZE + i] = byte
        for i, byte in enumerate(lead_nick):
            pb.memory[nick_addr + slot * PARTY_NICK_SIZE + i] = byte
    pb.memory[species_addr + 3] = 0xFF
    pb.memory[count_addr] = 3
    msg = "cloned lead into slots 1,2 to satisfy Colosseum 3-mon rule"
    if repaired_pp:
        msg += f" (repaired 0-PP on move slots {repaired_pp})"
    return (True, msg)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnostic-only Cable Club BATTLE demo (Red<->Blue); it "
            "mutates emulator RAM and is not human-valid acceptance evidence."
        ),
        epilog=(
            "ROM and symbol hashes must match configured VERSIONS.md pins. "
            "Success proves only this diagnostic run, not clean fixture "
            "provenance or production gameplay."
        ),
    )
    parser.add_argument("--versions", default="red,blue")
    parser.add_argument("--outdir", default="walkthrough_link_battle")
    parser.add_argument("--view", action="store_true",
                        help="Open SDL2 windows for both peers.")
    parser.add_argument("--hold-after-s", type=int, default=60,
                        help="Seconds to idle both emulators after the "
                             "battle phase so you can watch the windows.")
    parser.add_argument("--battle-turns", type=int, default=1,
                        help="How many move-damage resolutions to "
                             "require on both peers before declaring "
                             "the demo successful. Default 1 — the "
                             "existing test suite also caps at 1 turn "
                             "because multi-turn link battles desync "
                             "in this emulator setup.")
    parser.add_argument("--battle-budget-frames", type=int, default=3600,
                        help="Safety cap on emulated frames per turn. "
                             "Default 3600 (~60s).")
    args = parser.parse_args()

    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    if len(versions) != 2:
        print(f"[error] --versions must be 'a,b'; got {args.versions!r}")
        return 2
    version_a, version_b = versions

    sys.path.insert(0, str(_REPO / "src"))
    sys.path.insert(0, str(_REPO / "scripts"))

    from live_trade_demo import (
        _ROM_PATHS,
        Shooter,
        _assert_fixtures_available,
        _install_hook_counter,
    )

    from pokered_harness.link.pyboy_link_session import PyBoyLinkSession

    for v in (version_a, version_b):
        if v not in _ROM_PATHS:
            print(f"[error] unknown version {v!r}; choose from "
                  f"{sorted(_ROM_PATHS)}")
            return 2
        _assert_fixtures_available(v)

    outdir = (_REPO / args.outdir) if not Path(args.outdir).is_absolute() \
        else Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    shoot = Shooter(outdir)

    print(f"[diagnostic-only] versions: A={version_a}, B={version_b}")
    print(f"[diagnostic-only] outdir:   {outdir}")

    t_start = time.perf_counter()

    a = b = None
    active_error: BaseException | None = None
    try:
        a, b = _open_battle_sessions(version_a, version_b, view=args.view)

        link = PyBoyLinkSession.local(view=args.view)
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        ow_a = a.read_game_state().overworld
        ow_b = b.read_game_state().overworld
        print(f"[diagnostic-only start] A coord=({ow_a.x},{ow_a.y}) map=0x{ow_a.map_id:02x} | "
              f"B coord=({ow_b.x},{ow_b.y}) map=0x{ow_b.map_id:02x}")
        shoot.shoot_pair(a, b, "bphase_00_start")

        # --- Phase A: receptionist → save → LinkMenu ---
        counters = {
            "CableClubNPC": [0, 0],
            "SaveGameData": [0, 0],
            "LinkMenu": [0, 0],
            "CableClub_DoBattleOrTrade": [0, 0],
            "DisplayLinkBattleVersusTextBox": [0, 0],
            "MoveSelectionMenu": [0, 0],
            "LinkBattleExchangeData": [0, 0],
            "PlayerCalcMoveDamage": [0, 0],
            "MainInBattleLoop": [0, 0],
            "DisplayPokemonFaintedText": [0, 0],
            "EndOfBattle": [0, 0],
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

        for _ in range(3):
            a.press("up", duration=6)
            b.press("up", duration=6)
            tick_both_coarse(20)

        for _ in range(120):
            if counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0:
                break
            a.press("a", duration=4)
            b.press("a", duration=4)
            in_serial = (counters["SaveGameData"][0] > 0
                         or counters["SaveGameData"][1] > 0)
            if in_serial:
                tick_both_fine(20)
            else:
                tick_both_coarse(20)

        assert counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0, (
            "LinkMenu never fired on both sides"
        )
        shoot.shoot_pair(a, b, "bphase_01_linkmenu_reached")

        print("[diagnostic-only] moving LinkMenu cursor down to COLOSSEUM...")
        tick_both_fine(30)
        a.press("down", duration=8)
        b.press("down", duration=8)
        tick_both_fine(15)
        shoot.shoot_pair(a, b, "bphase_02_cursor_on_colosseum")
        a.press("a", duration=4)
        b.press("a", duration=4)

        # --- Phase B: warp to COLOSSEUM ---
        for _ in range(80):
            map_a = a.read_game_state().overworld.map_id
            map_b = b.read_game_state().overworld.map_id
            if map_a == COLOSSEUM_MAP_ID and map_b == COLOSSEUM_MAP_ID:
                break
            a.press("a", duration=4)
            b.press("a", duration=4)
            tick_both_fine(20)

        map_a = a.read_game_state().overworld.map_id
        map_b = b.read_game_state().overworld.map_id
        print(f"[diagnostic-only warp] A map=0x{map_a:02x}, B map=0x{map_b:02x}")
        assert map_a == COLOSSEUM_MAP_ID and map_b == COLOSSEUM_MAP_ID, (
            f"expected COLOSSEUM 0x{COLOSSEUM_MAP_ID:02x}, "
            f"got A=0x{map_a:02x} B=0x{map_b:02x}"
        )
        shoot.shoot_pair(a, b, "bphase_03_colosseum_entry")

        t_warp = time.perf_counter() - t_start
        print(f"[diagnostic-only phase A+B: receptionist -> COLOSSEUM] {t_warp:.2f}s")

        # --- Phase C: approach partner, launch battle ---
        conn_a = a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")]
        conn_b = b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")]
        INTERNAL = 0x02
        dir_a = "right" if conn_a == INTERNAL else "left"
        dir_b = "right" if conn_b == INTERNAL else "left"
        print(f"[diagnostic-only colosseum] A walks {dir_a}, B walks {dir_b}")

        cct = counters["CableClub_DoBattleOrTrade"]

        # Walk onto the hidden-event trigger tile.
        for _ in range(4):
            if cct[0] > 0 and cct[1] > 0:
                break
            a.press(dir_a, duration=8)
            b.press(dir_b, duration=8)
            tick_both_coarse(20)

        # A-mash past "JUST A MOMENT!" to kick off the big party+trainer
        # data exchange.
        settle = 0
        while settle < 1800 and not (cct[0] > 0 and cct[1] > 0):
            a.press("a", duration=4)
            b.press("a", duration=4)
            if cct[0] > 0 or cct[1] > 0:
                tick_both_fine(20)
            else:
                tick_both_coarse(20)
            settle += 20

        shoot.shoot_pair(a, b, "bphase_04_approach_partner")

        assert cct[0] > 0 and cct[1] > 0, (
            "CableClub_DoBattleOrTrade never fired on both sides; "
            "battle didn't launch"
        )
        print(
            f"[diagnostic-only battle launched] CableClub_DoBattleOrTrade: "
            f"A={cct[0]} B={cct[1]}"
        )

        # --- Phase D: drive turns until EndOfBattle fires on both sides ---
        #
        # Move selection strategy: the fixture's lead Venusaur has
        # VINE WHIP with PP=2 on slot 0 (grind-depleted). Using slot 0
        # via pure A-mash works for 1-2 turns then fails. Instead we
        # pick TACKLE (slot 1, PP=32) by pressing Down once after
        # FIGHT, then A to confirm. The move-select menu opens each
        # turn, so we use the MoveSelectionMenu hook delta to detect
        # "a new move menu appeared" and respond with Down+A.
        dmg = counters["PlayerCalcMoveDamage"]
        lbe = counters["LinkBattleExchangeData"]
        vs = counters["DisplayLinkBattleVersusTextBox"]
        mm = counters["MoveSelectionMenu"]
        eob = counters["EndOfBattle"]
        faints = counters["DisplayPokemonFaintedText"]

        target_turns = args.battle_turns
        t_battle_start = time.perf_counter()
        extra_frames = 0
        shot_vs = False
        shot_first_move = False
        last_dmg_captured = 0

        # A-mash strategy: both sides' cursors default to FIGHT and move
        # slot 0. Pressing A twice per turn picks FIGHT + slot 0 (VINE
        # WHIP — has PP=2 on the fixture, just enough for the single
        # turn we target). Multi-turn attempts with Down+A to select
        # TACKLE (slot 1, PP=32) desync in this emulator setup, so we
        # stick with the test-proven 1-turn approach here.
        while extra_frames < args.battle_budget_frames * target_turns:
            if eob[0] > 0 and eob[1] > 0:
                break
            if dmg[0] >= target_turns and dmg[1] >= target_turns:
                break

            tick_both_fine(20)
            extra_frames += 20

            a.press("a", duration=4)
            b.press("a", duration=4)

            if not shot_vs and vs[0] > 0 and vs[1] > 0:
                shoot.shoot_pair(a, b, "bphase_05_vs_splash")
                shot_vs = True
            if not shot_first_move and mm[0] > 0 and mm[1] > 0:
                shoot.shoot_pair(a, b, "bphase_06_first_move_menu")
                shot_first_move = True

            cur_dmg = min(dmg[0], dmg[1])
            if cur_dmg > last_dmg_captured:
                shoot.shoot_pair(a, b, f"bphase_07_turn_{cur_dmg:02d}_resolved")
                last_dmg_captured = cur_dmg
                try:
                    hp_a = (a._pyboy.memory[a.symbols.addr_of("wPartyMon1HP")] << 8) \
                        | a._pyboy.memory[a.symbols.addr_of("wPartyMon1HP") + 1]
                    hp_b = (b._pyboy.memory[b.symbols.addr_of("wPartyMon1HP")] << 8) \
                        | b._pyboy.memory[b.symbols.addr_of("wPartyMon1HP") + 1]
                except Exception:  # noqa: BLE001 - snapshotting is best effort
                    hp_a = hp_b = -1
                print(
                    f"  [turn {cur_dmg}] t+{extra_frames / 60:.0f}s: "
                    f"dmg A={dmg[0]} B={dmg[1]}, lbe A={lbe[0]} B={lbe[1]}, "
                    f"faints A={faints[0]} B={faints[1]}, "
                    f"lead HP A={hp_a} B={hp_b}",
                    flush=True,
                )

        t_battle_secs = time.perf_counter() - t_battle_start
        print(
            f"[diagnostic-only battle] turns(dmg) A={dmg[0]} B={dmg[1]}; "
            f"VSbox A={vs[0]} B={vs[1]}; "
            f"MoveSelect A={mm[0]} B={mm[1]}; "
            f"LBE A={lbe[0]} B={lbe[1]}; "
            f"faints A={counters['DisplayPokemonFaintedText'][0]} "
            f"B={counters['DisplayPokemonFaintedText'][1]}; "
            f"EndOfBattle A={eob[0]} B={eob[1]}  "
            f"({t_battle_secs:.2f}s, {extra_frames} frames)"
        )
        shoot.shoot_pair(a, b, "bphase_08_battle_final")

        hit_target = dmg[0] >= target_turns and dmg[1] >= target_turns
        battle_concluded = eob[0] > 0 and eob[1] > 0
        if battle_concluded:
            print(
                f"[diagnostic-only OK] Battle concluded via EndOfBattle on both sides after "
                f"{dmg[0]}/{dmg[1]} move damage resolutions, "
                f"{faints[0]}/{faints[1]} faints per side."
            )
        elif hit_target:
            print(
                f"[diagnostic-only OK] Reached target {target_turns} move-damage "
                f"resolutions on both sides. dmg A={dmg[0]} B={dmg[1]}, "
                f"faints A={faints[0]} B={faints[1]}. "
                f"Link protocol confirmed end-to-end."
            )
        else:
            print(
                f"[diagnostic-only warning] Did not reach target turns. "
                f"dmg A={dmg[0]} B={dmg[1]}, faints A={faints[0]} B={faints[1]}."
            )

        # Post-battle idle hold.
        if args.view:
            hold_s = args.hold_after_s
            print(f"[diagnostic-only] post-battle hold for {hold_s}s...")
            for _ in range(hold_s * 60):
                a.step(1)
                b.step(1)

        t_total = time.perf_counter() - t_start
        print(f"[diagnostic-only total wall-clock] {t_total:.2f}s")
        return 0 if (battle_concluded or hit_target) else 1
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        _close_sessions(("A", a), ("B", b), active_error=active_error)


if __name__ == "__main__":
    sys.exit(main())
