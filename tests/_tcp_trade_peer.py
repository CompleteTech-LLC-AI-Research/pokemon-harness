"""Standalone TCP-trade peer — spawned as a subprocess by
``test_pyboy_link_session_subprocess.py``.

Runs one side of a two-PyBoy Pokémon trade. Takes the role
(``listen`` or ``connect``), a TCP port, and a deadline on the
command line. Connects to or listens for a peer, loads the Yellow
Cable Club state, drives through the trade flow, then prints a
JSON blob to stdout reporting the final hook counters and exit
cleanly.

The parent process uses this script to get **subprocess-level
scheduling isolation** — each PyBoy instance runs in its own
Python interpreter with its own GIL, so OS scheduling gives both
sides real parallelism (unlike two threads in one Python process).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_TRADE_DIAG_SYMBOLS = (
    "CableClubNPC",
    "SaveGameData",
    "Serial_SyncAndExchangeNybble",
    "LinkMenu",
    "CableClubLeftGameboy",
    "CableClubRightGameboy",
    "CableClub_DoBattleOrTrade",
    "CableClub_DoBattleOrTradeAgain",
    "CableClub_DoBattleOrTradeAgain.finishedPatchingPlayerData",
    "CableClub_DoBattleOrTradeAgain.finishedPartyMonsPatchListPart",
    "CableClub_DoBattleOrTradeAgain.finishedEnemyMonsPatchListPart",
    "CableClub_DoBattleOrTradeAgain.trading",
    "ReturnToCableClubRoom",
    "CallCurrentTradeCenterFunction",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_Trade",
    "_AddEnemyMonToPlayerParty",
    "DisplayLinkBattleVersusTextBox",
    "BattleTransition",
    "MainInBattleLoop",
    "MoveSelectionMenu",
    "LinkBattleExchangeData",
    "ExecutePlayerMove",
    "ExecuteEnemyMove",
    "PlayerCalcMoveDamage",
    "EndOfBattle",
)


PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11


def _install_hook(session, symbol, bucket):
    if symbol not in session.symbols:
        return
    bank, addr = session.symbols.bank_addr(symbol)

    def _cb(_ctx):
        bucket[0] += 1

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        pass


def _party_summary(session) -> dict[str, object]:
    """Return the game-owned party arrays for post-trade verification."""
    memory = session._pyboy.memory
    addr_of = session.symbols.addr_of
    count = int(memory[addr_of("wPartyCount")])
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    return {
        "count": count,
        "species": [int(memory[species_addr + i]) for i in range(count + 1)],
        "mon_species": [
            int(memory[mons_addr + i * PARTY_MON_SIZE]) for i in range(count)
        ],
        "mon_records": [
            bytes(
                memory[mons_addr + i * PARTY_MON_SIZE + offset]
                for offset in range(PARTY_MON_SIZE)
            ).hex()
            for i in range(count)
        ],
    }


def _validate_battle_party_fixture(session) -> str:
    """Validate that the saved battle fixture is already Colosseum-legal."""
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count_addr = addr_of("wPartyCount")
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")

    count = pb.memory[count_addr]
    if count < 3:
        raise RuntimeError(f"battle fixture party count is {count}, expected >=3")
    if pb.memory[species_addr + count] != 0xFF:
        raise RuntimeError("battle fixture party species list is not FF-terminated")
    species = []
    for slot in range(count):
        slot_species = pb.memory[species_addr + slot]
        mon_addr = mons_addr + slot * PARTY_MON_SIZE
        mon_species = pb.memory[mon_addr]
        hp = (pb.memory[mon_addr + 1] << 8) | pb.memory[mon_addr + 2]
        if slot_species in (0, 0xFF) or mon_species != slot_species:
            raise RuntimeError(
                "battle fixture has invalid party slot "
                f"{slot}: species={slot_species} mon_species={mon_species}"
            )
        if hp <= 0:
            raise RuntimeError(f"battle fixture party slot {slot} has no HP")
        for move_idx in range(4):
            move_id = pb.memory[mon_addr + 8 + move_idx]
            pp = pb.memory[mon_addr + 29 + move_idx] & 0x3F
            if move_id != 0 and pp <= 0:
                raise RuntimeError(
                    f"battle fixture party slot {slot} move {move_idx} has zero PP"
                )
        species.append(int(slot_species))
    return f"party_count={int(count)} species={species}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", choices=("listen", "connect"), required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--deadline-seconds", type=float, default=240.0)
    ap.add_argument("--goal", choices=("link_menu", "trade", "battle"), default="link_menu")
    ap.add_argument(
        "--version",
        choices=("red_gb", "red_color", "blue_gb", "blue_color", "yellow"),
        default="yellow",
    )
    ap.add_argument("--record-dir", type=Path)
    ap.add_argument("--label", default="")
    ap.add_argument("--repo-root", type=Path, required=True)
    args = ap.parse_args()

    sys.path.insert(0, str(args.repo_root / "src"))

    from pokered_harness.config import load_versions
    from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
    from pokered_harness.session import Session

    rom_root = Path(os.environ.get("POKERED_ROM_ROOT", args.repo_root / "rom"))
    fixture_root = Path(
        os.environ.get(
            "POKERED_FIXTURE_ROOT", args.repo_root / "tests" / "fixtures" / "link"
        )
    )
    assert rom_root.is_dir(), f"ROM root not found: {rom_root}"

    rom_paths = {
        "red_gb": (
            rom_root / "red" / "pokemon-red.gb",
            rom_root / "red" / "pokemon-red.sym",
            "red",
            "cable_club-vanilla.state",
            "cable_club-battle-vanilla.state",
        ),
        "red_color": (
            rom_root / "red" / "pokemon-red-color.gb",
            rom_root / "red" / "pokemon-red.sym",
            "red",
            "cable_club.state",
            "cable_club-battle.state",
        ),
        "blue_gb": (
            rom_root / "blue" / "pokemon-blue.gb",
            rom_root / "blue" / "pokemon-blue.sym",
            "blue",
            "cable_club-vanilla.state",
            "cable_club-battle-vanilla.state",
        ),
        "blue_color": (
            rom_root / "blue" / "pokemon-blue-color.gb",
            rom_root / "blue" / "pokemon-blue.sym",
            "blue",
            "cable_club.state",
            "cable_club-battle.state",
        ),
        "yellow": (
            rom_root / "yellow" / "pokemon-yellow.gbc",
            rom_root / "yellow" / "pokemon-yellow.sym",
            "yellow",
            "cable_club.state",
            "cable_club-battle.state",
        ),
    }
    rom, sym, fixture_version, trade_fixture_name, battle_fixture_name = rom_paths[args.version]
    fixture_name = battle_fixture_name if args.goal == "battle" else trade_fixture_name
    state = (
        fixture_root / fixture_version / fixture_name
    )

    def log(msg):
        print(f"[peer {args.role}] {msg}", file=sys.stderr, flush=True)

    if args.record_dir is not None:
        args.record_dir.mkdir(parents=True, exist_ok=True)

    shots: list[str] = []

    def shot(phase: str) -> None:
        if args.record_dir is None:
            return
        # Render one frame on demand so headless/null-window screenshots
        # reflect the restored/current LCD state.
        try:
            session.step(1, render=True)
            img = session._pyboy.screen.image
        except Exception as exc:  # noqa: BLE001
            log(f"shot {phase} failed before save: {type(exc).__name__}: {exc}")
            return
        pair = f"{args.label}." if args.label else ""
        path = args.record_dir / f"{pair}{phase}.{args.role}.{args.version}.png"
        try:
            img.save(path)
        except Exception as exc:  # noqa: BLE001
            log(f"shot {phase} save failed: {type(exc).__name__}: {exc}")
            return
        shots.append(str(path))
        log(f"shot {phase}: {path}")

    log(f"loading {args.version} ROM + state")
    pins = load_versions(args.repo_root / "VERSIONS.md")
    expected_sha = pins.sha1_for_path(rom)
    if expected_sha is None:
        raise RuntimeError(f"no VERSIONS.md SHA-1 pin for {rom}")
    session = Session.from_files(
        rom,
        sym,
        expected_rom_sha1=expected_sha,
        expected_pyboy_version=pins.pyboy_version,
    )
    session.load_state(state.read_bytes())
    party_before = _party_summary(session)
    if args.goal == "battle":
        log(f"battle fixture validated: {_validate_battle_party_fixture(session)}")
    shot("00_loaded")
    log("state loaded, installing hooks")

    counters = {s: [0] for s in _TRADE_DIAG_SYMBOLS}
    for s in _TRADE_DIAG_SYMBOLS:
        _install_hook(session, s, counters[s])

    log(f"establishing TCP {args.role}")
    if args.role == "listen":
        link = PyBoyLinkSession.listen(
            args.port, host=args.host, local_rom_version=fixture_version
        )
    else:
        last_exc: Exception | None = None
        for attempt in range(60):
            try:
                link = PyBoyLinkSession.connect(
                    args.host, args.port, local_rom_version=fixture_version
                )
                break
            except OSError as exc:
                last_exc = exc
                if attempt == 59:
                    raise
                time.sleep(0.25)
        else:
            raise RuntimeError(f"could not connect to listener: {last_exc}")
    log("TCP established, attaching PyBoy")
    link.attach(session._pyboy)
    peer_version = link._network_backend.wait_for_hello(timeout=30.0)
    log(
        f"versioned handshake complete: local={fixture_version} "
        f"peer={peer_version}"
    )
    log("attached; starting drive loop")

    deadline = time.monotonic() + args.deadline_seconds
    drive_status = "ok"
    drive_error: str | None = None
    deadline_exceeded = False
    select_mon_announced = False
    link_menu_announced = False
    link_menu_quiet_announced = False
    peer_link_menu_ready = False
    peer_link_menu_quiet_ready = False
    damage_announced = False
    peer_damage_ready = False
    party_after_trade: dict[str, object] | None = None

    def state_snapshot() -> dict[str, int]:
        return {
            "map_id": session.read_game_state().overworld.map_id,
            "hSerialConnectionStatus": session._pyboy.memory[
                session.symbols.addr_of("hSerialConnectionStatus")
            ],
            "wLinkState": session._pyboy.memory[
                session.symbols.addr_of("wLinkState")
            ],
        }

    def backend_snapshot() -> dict[str, object]:
        if link._network_backend is None:
            return {}
        return link._network_backend.debug_snapshot()

    def cooperative_sync(sync_id: int, *, timeout: float = 60.0) -> None:
        """Rendezvous without freezing the local emulator thread.

        A blocking barrier is safe only when neither ROM can be servicing
        serial IRQs. At a nominal UI boundary the peer may still have one
        last edge in flight, so continue ticking while waiting for the
        marker. This keeps the slave able to re-arm and makes the boundary
        an orchestration point rather than a scheduler stop.
        """
        link._network_backend.announce_sync(sync_id=sync_id)
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            if link._network_backend.poll_peer_sync(sync_id=sync_id):
                return
            session.step(4)
        raise RuntimeError(
            f"cooperative sync {sync_id} did not converge: "
            f"local={state_snapshot()} backend={backend_snapshot()}"
        )

    def current_menu_item() -> int | None:
        try:
            return session._pyboy.memory[session.symbols.addr_of("wCurrentMenuItem")]
        except (AttributeError, KeyError, TypeError):
            return None

    try:
        # Phase 1: walk UP ×3 + A-mash to reach LinkMenu.
        for _ in range(3):
            session.press("up", duration=6)
            session.step(20)
        # Start the receptionist interaction from a synchronized input
        # boundary so both processes enter the Cable Club dialog at
        # nearly the same game phase.
        link._network_backend.sync_with_peer(sync_id=100, timeout=60.0)
        log("phase 1 start")
        last_progress = time.monotonic()
        serial_phase_ticks = 0
        peer_link_menu_ready = False
        while time.monotonic() < deadline:
            # LinkMenu is a phase boundary, but the ROM that reaches it
            # first may still need a final serial IRQ/re-arm turn before the
            # peer can reach its own LinkMenu. Keep ticking while waiting for
            # the peer's marker, then use a second marker only after this
            # native clock is idle. Both sides can therefore leave the phase
            # without closing an in-flight edge.
            if link_menu_announced:
                if not peer_link_menu_ready:
                    peer_link_menu_ready = (
                        link._network_backend.poll_peer_sync(sync_id=121)
                    )
                if peer_link_menu_ready:
                    serial = getattr(
                        getattr(session._pyboy, "mb", None), "serial", None
                    )
                    local_master_active = bool(
                        getattr(serial, "transfer_enabled", False)
                        and getattr(serial, "internal_clock", False)
                    )
                    if (
                        not link_menu_quiet_announced
                        and not local_master_active
                    ):
                        link._network_backend.announce_sync(sync_id=122)
                        link_menu_quiet_announced = True
                        log("phase 1 native serial quiet acknowledgement sent")
                    if link_menu_quiet_announced and not peer_link_menu_quiet_ready:
                        peer_link_menu_quiet_ready = (
                            link._network_backend.poll_peer_sync(sync_id=122)
                        )
                if peer_link_menu_quiet_ready:
                    log("phase 1 done: LinkMenu fired on both peers")
                    break
                if not link_menu_quiet_announced:
                    session.step(4)
                else:
                    # After advertising native serial quiescence, do not
                    # enter another emulator tick while the peer's quiet
                    # marker is in flight. The peer is allowed to close
                    # immediately after observing our marker; only the
                    # network reader needs to remain alive to receive its
                    # matching marker.
                    time.sleep(0.001)
                continue
            in_serial_phase = (
                counters["SaveGameData"][0] > 0
                or counters["Serial_SyncAndExchangeNybble"][0] > 0
            )
            if in_serial_phase:
                # Do not block on a phase barrier while either emulator is
                # inside a serial transfer. The slave's main thread must be
                # allowed to run its serial IRQ handler and re-arm SC while
                # the peer's NetworkBackend is waiting for this edge. The
                # network edge/response protocol already provides the
                # per-byte synchronization; explicit barriers are reserved
                # for safe UI/phase boundaries below.
                session.step(4)
                serial_phase_ticks += 1
                if counters["LinkMenu"][0] > 0 and not link_menu_announced:
                    link_menu_announced = True
                    log("phase 1 local LinkMenu fired")
                    shot("01_link_menu")
                    link._network_backend.announce_sync(sync_id=121)
                    log("phase 1 LinkMenu readiness sent")
            else:
                if counters["LinkMenu"][0] > 0 and not link_menu_announced:
                    link_menu_announced = True
                    log("phase 1 local LinkMenu fired")
                    shot("01_link_menu")
                    link._network_backend.announce_sync(sync_id=121)
                    log("phase 1 LinkMenu readiness sent")
                session.press("a", duration=4)
                session.step(40)
            if time.monotonic() - last_progress > 10.0:
                log(
                    f"phase 1 progress: "
                    f"CableClubNPC={counters['CableClubNPC'][0]} "
                    f"SaveGameData={counters['SaveGameData'][0]} "
                    f"Serial_SyncAndExchangeNybble={counters['Serial_SyncAndExchangeNybble'][0]} "
                    f"LinkMenu={counters['LinkMenu'][0]}"
                )
                last_progress = time.monotonic()

        if args.goal == "trade":
            # Phase barrier: both sides at LinkMenu before voting Trade
            # Center. Without this the vote-exchange nibble loop has
            # no way to guarantee overlap in Pokemon's polling windows.
            log("sync: link_menu barrier")
            cooperative_sync(sync_id=1, timeout=60.0)
            log("sync: past link_menu barrier")
            # Trade Center warp — A-mash until map becomes 0xEF.
            TRADE_CENTER = 0xEF
            while time.monotonic() < deadline:
                if session.read_game_state().overworld.map_id == TRADE_CENTER:
                    break
                session.press("a", duration=4)
                session.step(20)
            log("trade center warp complete")
            shot("02_trade_center")

            # Settle after warp, but keep advancing the local ROM while
            # the peer finishes its own warp. A blocking barrier here can
            # starve the peer: the listener may reach 0xEF first, stop its
            # game CPU inside sync_with_peer(), and no longer run the ROM
            # instructions that service/re-arm serial IRQs for the
            # connector's final menu exchange.
            session.step(120)
            warp_announced = False
            peer_warp_ready = False
            log("sync: warp announce-and-continue")
            while time.monotonic() < deadline:
                if (
                    session.read_game_state().overworld.map_id == TRADE_CENTER
                    and not warp_announced
                ):
                    link._network_backend.announce_sync(sync_id=2)
                    warp_announced = True
                    log("local trade-center warp announced")
                    shot("03_post_warp_local")
                if warp_announced and link._network_backend.poll_peer_sync(sync_id=2):
                    peer_warp_ready = True
                    break
                session.step(20)
            if not (warp_announced and peer_warp_ready):
                raise RuntimeError(
                    "trade-center warp rendezvous did not converge: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )
            log("sync: past warp announce-and-continue")
            shot("03_post_warp_sync")

            # Walk onto hidden-event trigger tile.
            conn_status = session._pyboy.memory[
                session.symbols.addr_of("hSerialConnectionStatus")
            ]
            walk_dir = "right" if conn_status == 0x02 else "left"
            for _ in range(6):
                if (
                    counters["CableClubLeftGameboy"][0]
                    + counters["CableClubRightGameboy"][0]
                    > 0
                ):
                    break
                session.press(walk_dir, duration=8)
                session.step(30)

            # A-mash to dismiss "JUST A MOMENT!" and start
            # CableClub_DoBattleOrTrade. NO sync barrier here — the
            # big trainer/party block exchange that runs inside
            # CableClub_DoBattleOrTrade needs both sides' CPUs
            # actively ticking to exchange bytes. Blocking on a
            # barrier mid-exchange would stall both ends. Instead
            # we rely on the warp barrier (sync_id=2) aligning us
            # closely enough that the natural parallel tick rates
            # keep the exchange progressing on both sides.
            while time.monotonic() < deadline:
                if counters["CableClub_DoBattleOrTrade"][0] > 0:
                    break
                session.press("a", duration=4)
                session.step(20)
            log(
                "CableClub_DoBattleOrTrade fired; big exchange running "
                f"{state_snapshot()} "
                f"backend={backend_snapshot()}"
            )

            # Wait for the big exchange to complete — detect via
            # TradeCenter_SelectMon firing (runs after the exchange
            # + warp-to-trade-flow).
            last_exchange_log = time.monotonic()
            peer_ready_for_select_mon = False
            while time.monotonic() < deadline:
                if counters["TradeCenter_SelectMon"][0] > 0 and not select_mon_announced:
                    link._network_backend.announce_sync(sync_id=3)
                    select_mon_announced = True
                    log(
                        "announced select_mon ready "
                        f"{state_snapshot()} "
                        f"CallCurrentTradeCenterFunction={counters['CallCurrentTradeCenterFunction'][0]} "
                        f"TradeCenter_SelectMon={counters['TradeCenter_SelectMon'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                    shot("04_select_mon_ready")
                if select_mon_announced and link._network_backend.poll_peer_sync(sync_id=3):
                    peer_ready_for_select_mon = True
                    log(
                        f"peer announced select_mon ready {state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    break
                session.step(20)
                if time.monotonic() - last_exchange_log > 15.0:
                    log(
                        "waiting for select_mon convergence "
                        f"{state_snapshot()} "
                        f"CableClub_DoBattleOrTrade={counters['CableClub_DoBattleOrTrade'][0]} "
                        f"CallCurrentTradeCenterFunction={counters['CallCurrentTradeCenterFunction'][0]} "
                        f"TradeCenter_SelectMon={counters['TradeCenter_SelectMon'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                    last_exchange_log = time.monotonic()
            if select_mon_announced:
                if not peer_ready_for_select_mon:
                    log(
                        "peer never announced select_mon before deadline "
                        f"{state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                else:
                    # One side can reach TradeCenter_SelectMon before the peer,
                    # but blocking immediately on a barrier can starve the
                    # slower side of the CPU progress it still needs to finish
                    # the same exchange. Keep stepping until the peer's own
                    # announcement arrives, then use a real barrier so both
                    # sides start menu navigation from a matched boundary.
                    settle_deadline = min(deadline, time.monotonic() + 10.0)
                    while time.monotonic() < settle_deadline:
                        session.step(20)
                    log(
                        "TradeCenter_SelectMon converged on both peers; "
                        f"big exchange done {state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    shot("05_select_mon_converged")
            else:
                log(
                    "TradeCenter_SelectMon not reached before deadline "
                    f"{state_snapshot()} "
                    f"backend={backend_snapshot()}"
                )
            if select_mon_announced and peer_ready_for_select_mon:
                # Barrier here: both sides are post-exchange, about to
                # drive the menu. Safe to sync (game is in UI-setup phase,
                # no active serial traffic).
                log("sync: select_mon barrier")
                cooperative_sync(sync_id=3, timeout=60.0)
                log("sync: past select_mon barrier")
                shot("06_select_mon_sync")
            else:
                log("skipping select_mon barrier; peers never converged")

            # State-aware menu navigation.
            log(f"entering menu nav; counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} }")
            last_log = time.monotonic()
            stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
            trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
            menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
            tct_key = "TradeCenter_Trade"
            prev = {k: counters[k][0] for k in (stats_key, trade_key, menu_key, tct_key)}
            right_pending = 0
            while (
                time.monotonic() < deadline
                and counters["_AddEnemyMonToPlayerParty"][0] == 0
            ):
                session.step(40)
                if time.monotonic() - last_log > 15.0:
                    snap = {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS}
                    log(f"menu nav progress: {snap}")
                    last_log = time.monotonic()
                now = {k: counters[k][0] for k in (stats_key, trade_key, menu_key, tct_key)}
                if now[trade_key] > prev[trade_key]:
                    session.press("a", duration=4)
                    right_pending = 0
                elif now[stats_key] > prev[stats_key]:
                    right_pending = 5
                    session.press("right", duration=12)
                elif right_pending > 0:
                    session.press("right", duration=12)
                    right_pending -= 1
                elif now[menu_key] > prev[menu_key] or now[tct_key] > prev[tct_key]:
                    session.press("a", duration=4)
                else:
                    session.press("a", duration=4)
                prev = now

            # Post-trade sync + keep-tick. Whichever side's
            # _AddEnemyMonToPlayerParty fired first has finished the
            # trade locally but the peer may still be mid-exchange
            # waiting for a few final bytes. Exiting the drive loop
            # immediately would tear down our SerialCore and leave
            # the peer's on_edge calls timing out. Instead, rendezvous
            # over OP_SYNC and then keep ticking the local core (and
            # honoring peer EDGE_REQs via the NetworkBackend reader)
            # long enough for the peer to complete its own trade.
            if counters["_AddEnemyMonToPlayerParty"][0] > 0:
                # The hook is at function entry. Advance through the
                # copy routine before the rendezvous so the result records
                # the exchanged party record, not the later room-cleanup
                # state after the trade animation.
                session.step(120)
                party_after_trade = _party_summary(session)
                log("sync: post-trade barrier")
                try:
                    cooperative_sync(sync_id=4, timeout=120.0)
                    log("sync: past post-trade barrier")
                    shot("07_post_trade")
                except Exception as exc:  # noqa: BLE001
                    drive_status = "error"
                    drive_error = f"{type(exc).__name__}: {exc}"
                    log(f"post-trade sync raised {type(exc).__name__}: {exc}")
                # After rendezvous both sides have fired
                # _AddEnemyMonToPlayerParty. Keep ticking briefly so
                # the peer's post-trade animation / UI code can still
                # drive any residual serial traffic through us.
                post_deadline = min(deadline, time.monotonic() + 30.0)
                while time.monotonic() < post_deadline:
                    session.step(40)
        elif args.goal == "battle":
            log("sync: link_menu battle barrier")
            cooperative_sync(sync_id=11, timeout=120.0)
            log("sync: past link_menu battle barrier")

            # LinkMenu opens with TRADE selected (item 0); BATTLE is the
            # next item (item 1). Select it with ordinary directional input
            # in case a fixture or a prior menu leaves the cursor elsewhere,
            # then commit the choice with A. No RAM writes or execution hooks
            # may select the battle mode: this is the same user-input path
            # an MCP client would use.
            initial_item = current_menu_item()
            for _ in range(3):
                if initial_item in (None, 1):
                    break
                session.press("down", duration=4)
                session.step(20)
                initial_item = current_menu_item()
            log(f"battle LinkMenu input selection; initial_item={initial_item}")
            session.press("a", duration=4)
            session.step(20)
            shot("02_battle_menu")

            COLOSSEUM = 0xF0
            for _ in range(80):
                if (
                    session.read_game_state().overworld.map_id == COLOSSEUM
                    and counters["CableClub_DoBattleOrTrade"][0] == 0
                ):
                    break
                session.press("a", duration=4)
                session.step(20)
            log("colosseum warp complete")
            shot("03_colosseum")
            session.step(120)

            conn_status = session._pyboy.memory[
                session.symbols.addr_of("hSerialConnectionStatus")
            ]
            walk_dir = "right" if conn_status == 0x02 else "left"
            for _ in range(6):
                if counters["CableClub_DoBattleOrTrade"][0] > 0:
                    break
                session.press(walk_dir, duration=8)
                session.step(30)
            while time.monotonic() < deadline:
                if counters["CableClub_DoBattleOrTrade"][0] > 0:
                    break
                session.press("a", duration=4)
                session.step(20)
            log(
                "battle CableClub_DoBattleOrTrade fired "
                f"count={counters['CableClub_DoBattleOrTrade'][0]} "
                f"{state_snapshot()} backend={backend_snapshot()}"
            )
            shot("04_battle_launch")

            while time.monotonic() < deadline:
                if counters["EndOfBattle"][0] > 0:
                    break
                if counters["PlayerCalcMoveDamage"][0] > 0 and not damage_announced:
                    link._network_backend.announce_sync(sync_id=14)
                    damage_announced = True
                    shot("05_battle_damage")
                    log(
                        "announced battle damage "
                        f"dmg={counters['PlayerCalcMoveDamage'][0]} "
                        f"lbe={counters['LinkBattleExchangeData'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                if damage_announced and link._network_backend.poll_peer_sync(sync_id=14):
                    peer_damage_ready = True
                    break
                session.press("a", duration=4)
                session.step(20)
            if damage_announced:
                try:
                    cooperative_sync(sync_id=15, timeout=120.0)
                    log("sync: past battle damage barrier")
                    shot("06_battle_synced")
                except Exception as exc:  # noqa: BLE001
                    drive_status = "error"
                    drive_error = f"{type(exc).__name__}: {exc}"
                    log(f"battle damage sync raised {type(exc).__name__}: {exc}")
                post_deadline = min(deadline, time.monotonic() + 10.0)
                while time.monotonic() < post_deadline:
                    session.press("a", duration=4)
                    session.step(20)
            if not peer_damage_ready:
                log(
                    "peer battle damage not observed before deadline "
                    f"dmg={counters['PlayerCalcMoveDamage'][0]} "
                    f"backend={backend_snapshot()}"
                )
    except Exception as exc:  # noqa: BLE001
        drive_status = "error"
        drive_error = f"{type(exc).__name__}: {exc}"
        log(f"EXCEPTION in drive loop: {type(exc).__name__}: {exc}")

    finally:
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001
            if drive_status == "ok":
                drive_status = "error"
                drive_error = f"session cleanup {type(exc).__name__}: {exc}"
            log(f"session cleanup raised {type(exc).__name__}: {exc}")
        try:
            if link._network_backend is not None:
                link._network_backend.stop()
        except Exception as exc:  # noqa: BLE001
            if drive_status == "ok":
                drive_status = "error"
                drive_error = f"backend cleanup {type(exc).__name__}: {exc}"
            log(f"backend cleanup raised {type(exc).__name__}: {exc}")

    if drive_status == "ok":
        if args.goal == "link_menu":
            goal_complete = (
                link_menu_announced
                and peer_link_menu_ready
                and link_menu_quiet_announced
                and peer_link_menu_quiet_ready
            )
        elif args.goal == "trade":
            goal_complete = counters["_AddEnemyMonToPlayerParty"][0] > 0
        else:
            required_battle_hooks = (
                "DisplayLinkBattleVersusTextBox",
                "MoveSelectionMenu",
                "LinkBattleExchangeData",
            )
            goal_complete = (
                damage_announced
                and peer_damage_ready
                and all(counters[name][0] > 0 for name in required_battle_hooks)
                and (
                    counters["ExecutePlayerMove"][0]
                    + counters["ExecuteEnemyMove"][0]
                    > 0
                )
            )
        if not goal_complete:
            drive_status = "deadline"
            deadline_exceeded = True
            drive_error = f"{args.goal} did not complete before deadline"
            log(drive_error)

    result = {s: counters[s][0] for s in _TRADE_DIAG_SYMBOLS}
    result["_role"] = args.role
    result["_version"] = args.version
    result["party_before"] = party_before
    result["party_after"] = party_after_trade or _party_summary(session)
    result["_shots"] = shots
    result["_backend_stats"] = backend_snapshot()
    result["_drive_status"] = drive_status
    result["_drive_error"] = drive_error
    result["_deadline_exceeded"] = deadline_exceeded
    log(f"final counters: {result}")
    # Sentinel-delimited JSON line so the parent can grep it out of
    # the ROM-loading warning spam on stdout.
    print(f"__TCP_TRADE_RESULT__ {json.dumps(result)}")
    return 0 if drive_status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
