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
    "CallCurrentTradeCenterFunction",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_Trade",
    "_AddEnemyMonToPlayerParty",
)


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", choices=("listen", "connect"), required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--deadline-seconds", type=float, default=240.0)
    ap.add_argument("--goal", choices=("link_menu", "trade"), default="link_menu")
    ap.add_argument("--repo-root", type=Path, required=True)
    args = ap.parse_args()

    os.environ.setdefault("POKERED_SKIP_SHA1", "1")
    sys.path.insert(0, str(args.repo_root / "src"))

    from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
    from pokered_harness.session import Session

    rom_root = None
    for parent in (args.repo_root, *args.repo_root.parents):
        if (parent / "rom").is_dir():
            rom_root = parent / "rom"
            break
    assert rom_root is not None, f"rom/ not found from {args.repo_root}"

    rom = rom_root / "yellow" / "pokemon-yellow.gbc"
    sym = rom_root / "yellow" / "pokemon-yellow.sym"
    state = (
        args.repo_root / "tests" / "fixtures" / "link" / "yellow" / "cable_club.state"
    )

    def log(msg):
        print(f"[peer {args.role}] {msg}", file=sys.stderr, flush=True)

    log(f"loading Yellow ROM + state")
    session = Session.from_files(rom, sym)
    session.load_state(state.read_bytes())
    log(f"state loaded, installing hooks")

    counters = {s: [0] for s in _TRADE_DIAG_SYMBOLS}
    for s in _TRADE_DIAG_SYMBOLS:
        _install_hook(session, s, counters[s])

    log(f"establishing TCP {args.role}")
    if args.role == "listen":
        link = PyBoyLinkSession.listen(args.port, host=args.host)
    else:
        link = PyBoyLinkSession.connect(args.host, args.port)
    log(f"TCP established, attaching PyBoy")
    link.attach(session._pyboy)
    log(f"attached; starting drive loop")

    deadline = time.time() + args.deadline_seconds
    select_mon_announced = False

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

    try:
        # Phase 1: walk UP ×3 + A-mash to reach LinkMenu.
        for _ in range(3):
            session.press("up", duration=6)
            session.step(20)
        # Start the receptionist interaction from a synchronized input
        # boundary so both processes enter the Cable Club dialog at
        # nearly the same game phase.
        link._network_backend.sync_with_peer(sync_id=100, timeout=60.0)
        log(f"phase 1 start")
        last_progress = time.time()
        post_link_menu_ticks = 0
        serial_phase_ticks = 0
        while time.time() < deadline:
            in_serial_phase = (
                counters["SaveGameData"][0] > 0
                or counters["Serial_SyncAndExchangeNybble"][0] > 0
            )
            if in_serial_phase:
                # Once the serial handshake starts, keep both subprocesses
                # aligned at every small step so the nibble-sync loop stays
                # fresh on both sides. Reusing the same sync_id is fine: the
                # NetworkBackend keeps a FIFO queue per id.
                link._network_backend.sync_with_peer(sync_id=120, timeout=60.0)
                session.step(4)
                serial_phase_ticks += 1
                if args.goal == "trade" and serial_phase_ticks >= 100:
                    log("phase 1 done: LinkMenu fired")
                    break
                if args.goal != "trade" and counters["LinkMenu"][0] > 0:
                    post_link_menu_ticks += 1
                    grace_ticks = 20
                    if post_link_menu_ticks >= grace_ticks:
                        log("phase 1 done: LinkMenu fired")
                        break
            else:
                if counters["LinkMenu"][0] > 0:
                    log("phase 1 done: LinkMenu fired")
                    break
                session.press("a", duration=4)
                session.step(40)
            if time.time() - last_progress > 10.0:
                log(
                    f"phase 1 progress: "
                    f"CableClubNPC={counters['CableClubNPC'][0]} "
                    f"SaveGameData={counters['SaveGameData'][0]} "
                    f"Serial_SyncAndExchangeNybble={counters['Serial_SyncAndExchangeNybble'][0]} "
                    f"LinkMenu={counters['LinkMenu'][0]}"
                )
                last_progress = time.time()

        # For the LinkMenu-only goal we keep ticking briefly so the peer can
        # still settle into the menu before the process exits. The trade path
        # stays in the synchronized serial-phase loop above instead.
        if args.goal != "trade":
            extra_deadline = min(deadline, time.time() + 30.0)
            while time.time() < extra_deadline:
                session.step(40)

        if args.goal == "trade":
            # Phase barrier: both sides at LinkMenu before voting Trade
            # Center. Without this the vote-exchange nibble loop has
            # no way to guarantee overlap in Pokemon's polling windows.
            log("sync: link_menu barrier")
            link._network_backend.sync_with_peer(sync_id=1, timeout=60.0)
            log("sync: past link_menu barrier")
            # Trade Center warp — A-mash until map becomes 0xEF.
            TRADE_CENTER = 0xEF
            while time.time() < deadline:
                if session.read_game_state().overworld.map_id == TRADE_CENTER:
                    break
                session.press("a", duration=4)
                session.step(20)
            log("trade center warp complete")

            # Settle after warp + barrier before walking.
            session.step(120)
            log("sync: warp barrier")
            link._network_backend.sync_with_peer(sync_id=2, timeout=60.0)
            log("sync: past warp barrier")

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
            while time.time() < deadline:
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
            last_exchange_log = time.time()
            peer_ready_for_select_mon = False
            while time.time() < deadline:
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
                if select_mon_announced and link._network_backend.poll_peer_sync(sync_id=3):
                    peer_ready_for_select_mon = True
                    log(
                        f"peer announced select_mon ready {state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    break
                session.step(20)
                if time.time() - last_exchange_log > 15.0:
                    log(
                        "waiting for select_mon convergence "
                        f"{state_snapshot()} "
                        f"CableClub_DoBattleOrTrade={counters['CableClub_DoBattleOrTrade'][0]} "
                        f"CallCurrentTradeCenterFunction={counters['CallCurrentTradeCenterFunction'][0]} "
                        f"TradeCenter_SelectMon={counters['TradeCenter_SelectMon'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                    last_exchange_log = time.time()
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
                    settle_deadline = min(deadline, time.time() + 10.0)
                    while time.time() < settle_deadline:
                        session.step(20)
                    log(
                        "TradeCenter_SelectMon converged on both peers; "
                        f"big exchange done {state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
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
                link._network_backend.sync_with_peer(sync_id=3, timeout=60.0)
                log("sync: past select_mon barrier")
            else:
                log("skipping select_mon barrier; peers never converged")

            # State-aware menu navigation.
            log(f"entering menu nav; counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} }")
            last_log = time.time()
            stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
            trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
            menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
            tct_key = "TradeCenter_Trade"
            prev = {k: counters[k][0] for k in (stats_key, trade_key, menu_key, tct_key)}
            right_pending = 0
            while (
                time.time() < deadline
                and counters["_AddEnemyMonToPlayerParty"][0] == 0
            ):
                session.step(40)
                if time.time() - last_log > 15.0:
                    snap = {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS}
                    log(f"menu nav progress: {snap}")
                    last_log = time.time()
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
                elif now[menu_key] > prev[menu_key]:
                    session.press("a", duration=4)
                elif now[tct_key] > prev[tct_key]:
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
                log("sync: post-trade barrier")
                try:
                    link._network_backend.sync_with_peer(
                        sync_id=4, timeout=120.0
                    )
                    log("sync: past post-trade barrier")
                except Exception as exc:
                    log(f"post-trade sync raised {type(exc).__name__}: {exc}")
                # After rendezvous both sides have fired
                # _AddEnemyMonToPlayerParty. Keep ticking briefly so
                # the peer's post-trade animation / UI code can still
                # drive any residual serial traffic through us.
                post_deadline = min(deadline, time.time() + 30.0)
                while time.time() < post_deadline:
                    session.step(40)
    except Exception as exc:
        log(f"EXCEPTION in drive loop: {type(exc).__name__}: {exc}")
    finally:
        try:
            session.close()
        except Exception:
            pass
        try:
            if link._network_backend is not None:
                link._network_backend.stop()
        except Exception:
            pass

    result = {s: counters[s][0] for s in _TRADE_DIAG_SYMBOLS}
    result["_backend_stats"] = backend_snapshot()
    log(f"final counters: {result}")
    # Sentinel-delimited JSON line so the parent can grep it out of
    # the ROM-loading warning spam on stdout.
    print(f"__TCP_TRADE_RESULT__ {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
