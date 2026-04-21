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

    try:
        # Phase 1: walk UP ×3 + A-mash to reach LinkMenu.
        for _ in range(3):
            session.press("up", duration=6)
            session.step(20)
        log(f"phase 1 start")
        last_progress = time.time()
        while time.time() < deadline:
            if counters["LinkMenu"][0] > 0:
                log(f"phase 1 done: LinkMenu fired")
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

        # Keep ticking briefly so peer can still reach LinkMenu if
        # it's behind. Then sync with peer to enter the next phase.
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
            log("CableClub_DoBattleOrTrade fired; big exchange running")

            # Wait for the big exchange to complete — detect via
            # TradeCenter_SelectMon firing (runs after the exchange
            # + warp-to-trade-flow).
            while time.time() < deadline:
                if counters["TradeCenter_SelectMon"][0] > 0:
                    break
                session.step(40)
            log("TradeCenter_SelectMon fired; big exchange done")

            # Barrier here: both sides are post-exchange, about to
            # drive the menu. Safe to sync (game is in UI-setup phase,
            # no active serial traffic).
            log("sync: select_mon barrier")
            link._network_backend.sync_with_peer(sync_id=3, timeout=60.0)
            log("sync: past select_mon barrier")

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
    log(f"final counters: {result}")
    # Sentinel-delimited JSON line so the parent can grep it out of
    # the ROM-loading warning spam on stdout.
    print(f"__TCP_TRADE_RESULT__ {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
