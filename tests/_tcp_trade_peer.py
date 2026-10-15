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
    "LinkMenu.waitForInputLoop",
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
    "HandlePlayerMonFainted",
    "HandleEnemyMonFainted",
    "EndOfBattle",
)


PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11


def _install_hook(session, symbol, bucket, on_hit=None):
    if symbol not in session.symbols:
        return
    bank, addr = session.symbols.bank_addr(symbol)

    def _cb(_ctx):
        bucket[0] += 1
        if on_hit is not None:
            on_hit(symbol)

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        pass


class _BattleTurnObserver:
    """Read the first post-execution boundary without changing game state.

    MainInBattleLoop can be bypassed when a move faints a combatant or ends
    battle. Faint handlers observe the applied HP before replacement; the
    EndOfBattle snapshot is retained before cleanup can reuse battle RAM.
    """

    BOUNDARIES = frozenset({
        "MainInBattleLoop", "HandlePlayerMonFainted",
        "HandleEnemyMonFainted", "EndOfBattle",
    })

    def __init__(self, session, counters):
        self.session = session
        self.counters = counters
        self.snapshot = None
        self.execution_exchange_count = None

    def _read(self, symbol, width=1):
        addr = self.session.symbols.addr_of(symbol)
        memory = self.session._pyboy.memory
        return int.from_bytes(bytes(memory[addr : addr + width]), "big")

    def observe(self, symbol):
        if self.snapshot is not None:
            return
        exchanges = self.counters["LinkBattleExchangeData"][0]
        player_executions = self.counters["ExecutePlayerMove"][0]
        enemy_executions = self.counters["ExecuteEnemyMove"][0]
        loops = self.counters["MainInBattleLoop"][0]
        if symbol in {"ExecutePlayerMove", "ExecuteEnemyMove"}:
            if loops >= 1 and exchanges > 0:
                self.execution_exchange_count = exchanges
            return
        if symbol not in self.BOUNDARIES:
            return
        if (
            exchanges <= 0 or player_executions + enemy_executions <= 0
            or loops < 1 or (symbol == "MainInBattleLoop" and loops < 2)
            or self.execution_exchange_count != exchanges
        ):
            return

        def combatant(prefix):
            return {
                "species": self._read(prefix + "Species"),
                "hp": self._read(prefix + "HP", 2),
                "max_hp": self._read(prefix + "MaxHP", 2),
                "status": self._read(prefix + "Status"),
            }

        self.snapshot = {
            "schema": 1,
            "boundary": symbol,
            "boundary_count": self.counters[symbol][0],
            "loop_count": loops,
            "exchange_count": exchanges,
            "execution_exchange_count": self.execution_exchange_count,
            "player_execute_count": player_executions,
            "enemy_execute_count": enemy_executions,
            "in_battle": self._read("wIsInBattle"),
            "result": self._read("wBattleResult"),
            "player": combatant("wBattleMon"),
            "enemy": combatant("wEnemyMon"),
            "cleanup_complete": False,
            "post_cleanup_in_battle": None,
        }

    def observe_cleanup(self):
        snapshot = self.snapshot
        if snapshot is not None and snapshot["boundary"] == "EndOfBattle":
            state = self._read("wIsInBattle")
            if state == 0:
                snapshot["post_cleanup_in_battle"] = state
                snapshot["cleanup_complete"] = True

    @property
    def ready(self):
        snapshot = self.snapshot
        return snapshot is not None and (
            snapshot["boundary"] != "EndOfBattle"
            or snapshot["cleanup_complete"]
        )


def _drive_battle_to_settled_turn(session, backend, observer, *, deadline, shot, log):
    """Keep both CPUs running until each has a post-turn observation."""
    announced = False
    while time.monotonic() < deadline:
        observer.observe_cleanup()
        if observer.ready and not announced:
            backend.announce_sync(sync_id=14)
            announced = True
            shot("05_battle_settled")
            log(f"announced settled battle turn: {observer.snapshot}")
        if announced and backend.poll_peer_sync(sync_id=14):
            return
        # A peer already at its boundary continues servicing the serial
        # protocol, but does not select another move while waiting.
        if observer.snapshot is None:
            session.press("a", duration=4)
        session.step(20)
    raise RuntimeError(
        "settled battle turn was not observed on both peers before deadline: "
        f"local={observer.snapshot} announced={announced}"
    )


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


class _BattleMenuInput:
    """Choose Colosseum using buttons and read-only ROM cursor observations.

    The caller advances one frame between updates and supplies an observation
    of LinkMenu.waitForInputLoop. DOWN remains held until the cartridge moves
    its cursor, then gets a release frame before A is queued.
    """

    def __init__(self, session):
        self.session = session
        self.down_held = False
        self.cursor_confirmed = False
        self.vote_queued = False

    def update(self, *, input_ready: bool) -> bool:
        if self.vote_queued or not input_ready:
            return self.vote_queued
        cursor = self.session._pyboy.memory[
            self.session.symbols.addr_of("wCurrentMenuItem")
        ]
        if cursor not in (0, 1):
            self.close()
            raise RuntimeError(f"unexpected battle LinkMenu cursor: {cursor}")
        if self.cursor_confirmed:
            if cursor != 1:
                raise RuntimeError("battle LinkMenu cursor changed before A")
            self.session.press("a", duration=24)
            self.vote_queued = True
        elif cursor == 1:
            self.close()
            self.cursor_confirmed = True
        elif not self.down_held:
            self.session.hold("down")
            self.down_held = True
        return self.vote_queued

    def close(self) -> None:
        if self.down_held:
            self.session.release("down")
            self.down_held = False


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

    from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
    from pokered_harness.config import load_versions
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
    battle_observer = _BattleTurnObserver(session, counters) if args.goal == "battle" else None
    for s in _TRADE_DIAG_SYMBOLS:
        _install_hook(
            session, s, counters[s],
            on_hit=battle_observer.observe if battle_observer is not None else None,
        )

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
    battle_menu = _BattleMenuInput(session) if args.goal == "battle" else None
    select_mon_announced = False
    party_after_trade: dict[str, object] | None = None
    party_final: dict[str, object] | None = None
    drive_error: str | None = None

    def state_snapshot() -> dict[str, int]:
        snap = {
            "map_id": session.read_game_state().overworld.map_id,
            "hSerialConnectionStatus": session._pyboy.memory[
                session.symbols.addr_of("hSerialConnectionStatus")
            ],
            "wLinkState": session._pyboy.memory[
                session.symbols.addr_of("wLinkState")
            ],
        }
        for name in (
            "wCurrentMenuItem",
            "wLinkMenuSelectionSendBuffer",
            "wLinkMenuSelectionReceiveBuffer",
            "wCableClubDestinationMap",
            "wXCoord",
            "wYCoord",
        ):
            if name in session.symbols:
                snap[name] = session._pyboy.memory[session.symbols.addr_of(name)]
        return snap

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
        deadline_at = min(deadline, time.monotonic() + timeout)
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
        except Exception:
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
        link_menu_announced = False
        # LinkMenu's hook fires before the ROM reaches its joypad polling
        # loop. Trade queues A at entry. Battle waits for the input-loop
        # observer and confirms cursor 1 after DOWN before it queues A.
        menu_vote_queued = False
        peer_link_menu_ready = False
        peer_link_menu_ready_grace = 0
        while time.monotonic() < deadline:
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
                session.step(1 if battle_menu is not None else 4)
                serial_phase_ticks += 1
                if counters["LinkMenu"][0] > 0:
                    if not link_menu_announced:
                        link._network_backend.announce_sync(sync_id=121)
                        link_menu_announced = True
                        if battle_menu is None:
                            session.press("a", duration=24)
                            menu_vote_queued = True
                        else:
                            session.release("a")
                        log("phase 1 local LinkMenu fired")
                        shot("01_link_menu")
                    if battle_menu is not None:
                        menu_vote_queued = battle_menu.update(
                            input_ready=counters["LinkMenu.waitForInputLoop"][0] > 0
                        )
                    if link._network_backend.poll_peer_sync(sync_id=121):
                        peer_link_menu_ready = True
                    if peer_link_menu_ready and (battle_menu is None or menu_vote_queued):
                        peer_link_menu_ready_grace += 1
                        if peer_link_menu_ready_grace >= 20:
                            log("phase 1 done: LinkMenu fired on both peers")
                            break
                    elif args.goal == "trade" or (args.goal == "battle" and menu_vote_queued):
                        # Keep the real A input live while the slower peer
                        # reaches LinkMenu.  A single short press can be
                        # consumed by the preceding "Please wait" dialog;
                        # repeated bounded presses ensure the first menu
                        # selection exchange carries the chosen A vote.
                        session.press("a", duration=4)
            else:
                if counters["LinkMenu"][0] > 0:
                    if not link_menu_announced:
                        link._network_backend.announce_sync(sync_id=121)
                        link_menu_announced = True
                        if battle_menu is None:
                            session.press("a", duration=24)
                            menu_vote_queued = True
                        else:
                            session.release("a")
                        log("phase 1 local LinkMenu fired")
                        shot("01_link_menu")
                    if battle_menu is not None:
                        menu_vote_queued = battle_menu.update(
                            input_ready=counters["LinkMenu.waitForInputLoop"][0] > 0
                        )
                    if link._network_backend.poll_peer_sync(sync_id=121):
                        peer_link_menu_ready = True
                    if peer_link_menu_ready and (battle_menu is None or menu_vote_queued):
                        peer_link_menu_ready_grace += 1
                        if peer_link_menu_ready_grace >= 5:
                            log("phase 1 done: LinkMenu fired on both peers")
                            break
                    elif args.goal == "trade" or (args.goal == "battle" and menu_vote_queued):
                        session.press("a", duration=4)
                if battle_menu is None or counters["LinkMenu"][0] == 0:
                    session.press("a", duration=4)
                    session.step(40)
                else:
                    session.step(1)
            if time.monotonic() - last_progress > 10.0:
                log(
                    f"phase 1 progress: "
                    f"CableClubNPC={counters['CableClubNPC'][0]} "
                    f"SaveGameData={counters['SaveGameData'][0]} "
                    f"Serial_SyncAndExchangeNybble={counters['Serial_SyncAndExchangeNybble'][0]} "
                    f"LinkMenu={counters['LinkMenu'][0]}"
                )
                last_progress = time.monotonic()

        if args.goal == "link_menu" and not (
            counters["LinkMenu"][0] > 0 and peer_link_menu_ready
        ):
            raise RuntimeError(
                "LinkMenu did not fire on both peers before deadline: "
                f"local={state_snapshot()} backend={backend_snapshot()}"
            )
        if args.goal in {"trade", "battle"} and not (
            counters["LinkMenu"][0] > 0 and peer_link_menu_ready
        ):
            raise RuntimeError(
                "LinkMenu did not converge on both peers before gameplay: "
                f"local={state_snapshot()} backend={backend_snapshot()}"
            )

        if battle_menu is not None and not menu_vote_queued:
            raise RuntimeError(
                "battle LinkMenu cursor selection did not settle before deadline: "
                f"local={state_snapshot()} backend={backend_snapshot()}"
            )

        if link_menu_announced and args.goal != "link_menu":
            # Do not insert a blocking phase barrier here.  LinkMenu's hook
            # fires before its final menu-selection serial exchange has gone
            # idle; if the master enters a barrier while the peer is waiting,
            # the peer can no longer tick its slave ROM to answer that last
            # edge.  The real A input was queued at hook entry, so continue
            # directly into the selected-game flow and let the ROM pair
            # settle through its natural serial traffic.
            log("phase 1 menu vote queued; continuing without serial barrier")

        if args.goal == "trade":
            # The menu vote was queued as soon as each ROM entered LinkMenu.
            # A blocking barrier here can starve the peer's final menu
            # exchange, so proceed with both CPUs ticking naturally.
            log("trade: past link_menu rendezvous")
            # Trade Center warp — A-mash until map becomes 0xEF.
            TRADE_CENTER = 0xEF
            while time.monotonic() < deadline:
                if session.read_game_state().overworld.map_id == TRADE_CENTER:
                    break
                session.press("a", duration=4)
                session.step(20)
            if session.read_game_state().overworld.map_id != TRADE_CENTER:
                raise RuntimeError(
                    "trade-center warp did not settle before deadline: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )
            log("trade center warp settled")
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
            # Each process must finish its own walk.  Stopping this loop as
            # soon as *one* local hook fires leaves the peer stranded one
            # tile short while the first ROM has already entered the large
            # trade exchange.  Once our trigger fires, keep ticking without
            # further movement so the remote slave can still service edges.
            for _ in range(12):
                local_triggered = (
                    counters["CableClubLeftGameboy"][0]
                    + counters["CableClubRightGameboy"][0]
                    > 0
                )
                if not local_triggered:
                    session.press(walk_dir, duration=8)
                session.step(30)
                log(
                    f"trade-center walk tick: triggered={local_triggered} "
                    f"coords=({state_snapshot().get('wXCoord')},"
                    f"{state_snapshot().get('wYCoord')}) "
                    f"left={counters['CableClubLeftGameboy'][0]} "
                    f"right={counters['CableClubRightGameboy'][0]}"
                )

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
            if counters["CableClub_DoBattleOrTrade"][0] <= 0:
                raise RuntimeError(
                    "CableClub_DoBattleOrTrade did not fire after the trade-center "
                    f"trigger: local={state_snapshot()} backend={backend_snapshot()}"
                )
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
                    raise RuntimeError(
                        "peer never announced select_mon before deadline: "
                        f"local={state_snapshot()} backend={backend_snapshot()}"
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
                raise RuntimeError(
                    "TradeCenter_SelectMon not reached before deadline: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
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
                raise RuntimeError(
                    "select_mon rendezvous did not converge: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )

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
                elif now[menu_key] > prev[menu_key]:
                    session.press("a", duration=4)
                elif now[tct_key] > prev[tct_key]:
                    session.press("a", duration=4)
                else:
                    session.press("a", duration=4)
                prev = now

            if counters["_AddEnemyMonToPlayerParty"][0] <= 0:
                raise RuntimeError(
                    "local _AddEnemyMonToPlayerParty did not fire before deadline: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )

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
                except Exception as exc:
                    log(f"post-trade sync raised {type(exc).__name__}: {exc}")
                # After rendezvous both sides have fired
                # _AddEnemyMonToPlayerParty. Keep ticking briefly so
                # the peer's post-trade animation / UI code can still
                # drive any residual serial traffic through us.
                post_deadline = min(deadline, time.monotonic() + 30.0)
                while time.monotonic() < post_deadline:
                    session.step(40)
        elif args.goal == "battle":
            # As with trade, do not stop either emulator at the LinkMenu
            # boundary while the final serial vote may still be in flight.
            log("battle: past link_menu rendezvous")

            # Colosseum was selected through DOWN, cursor observation, then A
            # during phase 1. Let the cartridge perform its normal vote and warp.
            shot("02_battle_menu")
            COLOSSEUM = 0xF0
            while time.monotonic() < deadline:
                if session.read_game_state().overworld.map_id == COLOSSEUM:
                    break
                session.press("a", duration=4)
                session.step(20)
            if session.read_game_state().overworld.map_id != COLOSSEUM:
                raise RuntimeError(
                    "Colosseum warp did not settle before deadline: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )
            log("colosseum warp settled")
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
            if counters["CableClub_DoBattleOrTrade"][0] <= 0:
                raise RuntimeError(
                    "CableClub_DoBattleOrTrade did not fire after the Colosseum "
                    f"trigger: local={state_snapshot()} backend={backend_snapshot()}"
                )
            shot("04_battle_launch")

            assert battle_observer is not None
            _drive_battle_to_settled_turn(
                session, link._network_backend, battle_observer,
                deadline=deadline, shot=shot, log=log,
            )
            # A failed final rendezvous is a failed row, even when local
            # battle state was observed. Keep this within the peer deadline.
            cooperative_sync(sync_id=15, timeout=120.0)
            log("sync: past settled battle turn barrier")
            shot("06_battle_synced")
            post_deadline = min(deadline, time.monotonic() + 10.0)
            while time.monotonic() < post_deadline:
                session.step(20)
    except Exception as exc:
        drive_error = f"{type(exc).__name__}: {exc}"
        log(f"EXCEPTION in drive loop: {type(exc).__name__}: {exc}")
    finally:
        if battle_menu is not None:
            try:
                battle_menu.close()
            except Exception as exc:
                drive_error = drive_error or f"battle input cleanup failed: {exc}"
        try:
            party_final = party_after_trade or _party_summary(session)
        except Exception:
            party_final = party_after_trade or party_before
        try:
            if link._network_backend is not None:
                link._network_backend.stop()
        except Exception:
            pass
        try:
            session.close()
        except Exception:
            pass

    result = {s: counters[s][0] for s in _TRADE_DIAG_SYMBOLS}
    result["_role"] = args.role
    result["_version"] = args.version
    result["party_before"] = party_before
    result["party_after"] = party_final or party_before
    if battle_observer is not None:
        result["battle_turn"] = battle_observer.snapshot
    if drive_error is not None:
        result["_error"] = drive_error
    result["_shots"] = shots
    result["_backend_stats"] = backend_snapshot()
    log(f"final counters: {result}")
    # Sentinel-delimited JSON line so the parent can grep it out of
    # the ROM-loading warning spam on stdout.
    print(f"__TCP_TRADE_RESULT__ {json.dumps(result)}")
    return 1 if drive_error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
