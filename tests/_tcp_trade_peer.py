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
from collections.abc import Callable
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
    "MainInBattleLoop.selectEnemyMove",
    "DisplayBattleMenu",
    "DisplayBattleMenu.leftColumn_WaitForInput",
    "DisplayBattleMenu.rightColumn_WaitForInput",
    "MoveSelectionMenu",
    "MoveSelectionMenu.menuset",
    "LinkBattleExchangeData",
    "ExecutePlayerMove",
    "ExecuteEnemyMove",
    "PlayerCalcMoveDamage",
    "EndOfBattle",
)


PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11


def _hold_at_sync_boundary(
    backend: object,
    *,
    ready_sync_id: int,
    release_sync_id: int,
    timeout: float,
    service_pending_edges: Callable[[], int],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Hold a safe ROM boundary until both peers have observed release.

    This helper is reserved for milestones where the ROM has already stopped
    doing serial work. It intentionally does not tick the emulator while
    waiting; callers drain only already-admitted owner-dispatch edges.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a positive number")
    if timeout <= 0:
        raise ValueError("timeout must be a positive number")

    deadline = monotonic() + timeout

    def wait_for_peer(marker: int, *, phase: str) -> None:
        while True:
            if backend.poll_peer_sync(sync_id=marker):
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"sync boundary {phase} did not converge: marker={marker}"
                )
            service_pending_edges()
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(0.001, remaining))

    backend.announce_sync(sync_id=ready_sync_id)
    wait_for_peer(ready_sync_id, phase="ready")
    backend.announce_sync(sync_id=release_sync_id)
    wait_for_peer(release_sync_id, phase="release")


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
    # The color Red/Blue Cable Club menu has three choices (0..2), while
    # Yellow adds a fourth (0..3).  Keep the readiness predicate ROM-aware;
    # a hard-coded bound can otherwise leave a valid peer spinning until the
    # outer gameplay deadline without ever announcing the phase.
    link_menu_max = 3 if fixture_version == "yellow" else 2
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
    selected_internal = link.negotiate_network_clock_role(peer_version)
    log(
        f"versioned handshake complete: local={fixture_version} "
        f"peer={peer_version} native_internal_clock={selected_internal}"
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
    battle_turn_announced = False
    peer_battle_turn_ready = False
    party_after_trade: dict[str, object] | None = None
    final_state: dict[str, int] = {}
    final_cpu: dict[str, object] = {}

    def state_snapshot() -> dict[str, int]:
        snapshot = {
            "map_id": session.read_game_state().overworld.map_id,
            "hSerialConnectionStatus": session._pyboy.memory[
                session.symbols.addr_of("hSerialConnectionStatus")
            ],
            "wLinkState": session._pyboy.memory[
                session.symbols.addr_of("wLinkState")
            ],
        }
        for name in (
            "wIsInBattle",
            "wBattleType",
            "wActionResultOrTookBattleTurn",
            "wMoveMenuType",
            "wPlayerSelectedMove",
        ):
            try:
                snapshot[name] = int(session._pyboy.memory[session.symbols.addr_of(name)])
            except (AttributeError, KeyError, TypeError):
                pass
        return snapshot

    def backend_snapshot() -> dict[str, object]:
        if link._network_backend is None:
            return {}
        return link._network_backend.debug_snapshot()

    def cpu_snapshot() -> dict[str, object]:
        """Capture bounded CPU/serial state for a stalled native run."""
        cpu = getattr(getattr(session._pyboy, "mb", None), "cpu", None)
        serial = getattr(getattr(session._pyboy, "mb", None), "serial", None)
        result: dict[str, object] = {}
        for name in (
            "PC",
            "SP",
            "A",
            "F",
            "B",
            "C",
            "D",
            "E",
            "H",
            "L",
            "cycles",
            "halted",
            "stopped",
            "interrupt_master_enable",
            "interrupts_enabled_register",
            "interrupts_flag_register",
        ):
            if hasattr(cpu, name):
                result[name] = getattr(cpu, name)
        for name in (
            "SB",
            "SC",
            "transfer_enabled",
            "internal_clock",
            "_bits_remaining",
            "clock",
            "clock_target",
        ):
            if hasattr(serial, name):
                result[f"serial.{name}"] = getattr(serial, name)
        return result

    def cooperative_sync(
        sync_id: int, *, timeout: float = 60.0, step_frames: int = 4
    ) -> None:
        """Rendezvous without freezing the local emulator thread.

        A blocking barrier is safe only when neither ROM can be servicing
        serial IRQs. At a nominal UI boundary the peer may still have one
        last edge in flight, so continue ticking while waiting for the
        marker. This keeps the slave able to re-arm and makes the boundary
        an orchestration point rather than a scheduler stop.
        """
        if isinstance(step_frames, bool) or not isinstance(step_frames, int):
            raise TypeError("step_frames must be a positive integer")
        if step_frames <= 0:
            raise ValueError("step_frames must be a positive integer")
        link._network_backend.announce_sync(sync_id=sync_id)
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            if link._network_backend.poll_peer_sync(sync_id=sync_id):
                return
            session.step(step_frames)
        raise RuntimeError(
            f"cooperative sync {sync_id} did not converge: "
            f"local={state_snapshot()} backend={backend_snapshot()}"
        )

    def current_menu_item() -> int | None:
        try:
            return session._pyboy.memory[session.symbols.addr_of("wCurrentMenuItem")]
        except (AttributeError, KeyError, TypeError):
            return None

    def menu_snapshot() -> dict[str, int]:
        memory = session._pyboy.memory
        snapshot: dict[str, int] = {}
        for name in (
            "wCurrentMenuItem",
            "wMaxMenuItem",
            "wMenuWatchedKeys",
            "wMenuJoypadPollCount",
            "wMenuWrappingEnabled",
            "wMenuWatchMovingOutOfBounds",
        ):
            try:
                snapshot[name] = int(memory[session.symbols.addr_of(name)])
            except (AttributeError, KeyError, TypeError):
                pass
        return snapshot

    def wait_for_menu_ready(
        *,
        label: str,
        min_item: int,
        max_item: int,
        expected_max: int | None = None,
        required_keys: int = 0x01,
        timeout: float = 60.0,
    ) -> dict[str, int]:
        """Wait until ROM menu fields describe an input-ready menu."""
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            snapshot = menu_snapshot()
            current = snapshot.get("wCurrentMenuItem")
            configured_max = snapshot.get("wMaxMenuItem")
            watched = snapshot.get("wMenuWatchedKeys", 0)
            if (
                current is not None
                and configured_max is not None
                and min_item <= current <= max_item
                and (expected_max is None or configured_max == expected_max)
                and watched & required_keys == required_keys
            ):
                return snapshot
            session.step(2)
        raise RuntimeError(
            f"{label} did not become input-ready: "
            f"menu={menu_snapshot()} state={state_snapshot()} "
            f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
        )

    def menu_fields_ready(
        *,
        min_item: int,
        max_item: int,
        expected_max: int | None = None,
        required_keys: int = 0x01,
    ) -> bool:
        snapshot = menu_snapshot()
        current = snapshot.get("wCurrentMenuItem")
        configured_max = snapshot.get("wMaxMenuItem")
        watched = snapshot.get("wMenuWatchedKeys", 0)
        return bool(
            current is not None
            and configured_max is not None
            and min_item <= current <= max_item
            and (expected_max is None or configured_max == expected_max)
            and watched & required_keys == required_keys
        )

    def move_menu_to_item(
        *,
        target: int,
        min_item: int,
        max_item: int,
        label: str,
        timeout: float = 30.0,
        input_duration: int = 2,
        settle_frames: int = 4,
    ) -> None:
        """Move a ROM-owned cursor with bounded one-shot directions."""
        if input_duration <= 0 or settle_frames <= 0:
            raise ValueError("menu input and settle durations must be positive")
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            snapshot = wait_for_menu_ready(
                label=label,
                min_item=min_item,
                max_item=max_item,
                timeout=min(5.0, max(0.1, deadline_at - time.monotonic())),
            )
            current = snapshot["wCurrentMenuItem"]
            if current == target:
                return
            if current < target:
                button = "down"
            else:
                button = "up"
            session.press(button, duration=input_duration)
            session.step(settle_frames)
        raise RuntimeError(
            f"{label} cursor did not reach {target}: "
            f"menu={menu_snapshot()} state={state_snapshot()} "
            f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
        )

    def read_active_battle_moves() -> tuple[tuple[int, int], ...]:
        """Read the ROM-populated active move/PP slots without mutation."""
        memory = session._pyboy.memory
        addr_of = session.symbols.addr_of
        moves_addr = addr_of("wBattleMonMoves")
        pp_addr = addr_of("wBattleMonPP")
        return tuple(
            (
                int(memory[moves_addr + move_idx]),
                int(memory[pp_addr + move_idx]) & 0x3F,
            )
            for move_idx in range(4)
        )

    def choose_first_usable_battle_move() -> int:
        """Navigate the real move menu to the first move with PP."""
        ready = wait_for_menu_ready(
            label="battle move menu",
            min_item=1,
            max_item=4,
            required_keys=0x01,
            timeout=60.0,
        )
        active_moves = read_active_battle_moves()
        move_count = ready.get("wMaxMenuItem", 0) - 1
        if not 1 <= move_count <= len(active_moves):
            raise RuntimeError(
                "ROM move-menu count is invalid: "
                f"menu={ready} moves={active_moves}"
            )
        usable_slots = [
            index
            for index, (move_id, pp) in enumerate(active_moves[:move_count])
            if move_id != 0 and pp > 0
        ]
        if not usable_slots:
            raise RuntimeError(
                "active battle mon has no usable move: "
                f"moves={active_moves}"
            )
        # Move-menu cursors are one-based. Let the ROM install the cursor
        # before reading it; no RAM write or test-only selection hook is used.
        target = usable_slots[0]
        move_menu_to_item(
            target=target + 1,
            min_item=1,
            max_item=move_count,
            label="battle move menu",
        )
        # A legal move is committed through the normal input path. After this
        # point the battle loop runs without synthetic input.  As with the
        # command menu, the peer may still be leaving the phase rendezvous;
        # retry only while the ROM continues to expose the move menu and stop
        # as soon as the post-menu control-flow label fires.
        prior_move_selection_phase = counters["MainInBattleLoop.selectEnemyMove"][0]
        next_move_input_tick = -1
        move_input_attempts = 0
        selected_move_id = active_moves[target][0]
        selection_deadline = min(time.monotonic() + 30.0, deadline)
        while time.monotonic() < selection_deadline:
            if counters["MainInBattleLoop.selectEnemyMove"][0] > prior_move_selection_phase:
                return selected_move_id
            if (
                menu_fields_ready(
                    min_item=1,
                    max_item=move_count,
                    required_keys=0x01,
                )
                and session.current_tick() >= next_move_input_tick
            ):
                session.press("a")
                move_input_attempts += 1
                next_move_input_tick = session.current_tick() + 8
            session.step(2)
        raise RuntimeError(
            "ROM move-menu A input was not consumed: "
            f"expected={selected_move_id} "
            f"select_enemy_move={counters['MainInBattleLoop.selectEnemyMove'][0]} "
            f"move_input_attempts={move_input_attempts} "
            f"menu={menu_snapshot()} counters={counters} "
            f"state={state_snapshot()} cpu={cpu_snapshot()} "
            f"backend={backend_snapshot()}"
        )

    try:
        # Phase 1: walk UP ×3 + A-mash to reach LinkMenu.
        for _ in range(3):
            session.press("up", duration=6)
            session.step(20)
        # Start the receptionist interaction from a synchronized input
        # boundary so both processes enter the Cable Club dialog at
        # nearly the same game phase.
        # The initial movement boundary can still overlap the ROM's final
        # connection-role negotiation. Keep ticking while waiting for the
        # peer marker so a slave IRQ can re-arm instead of freezing one
        # process inside a blocking transport barrier.
        cooperative_sync(sync_id=100, timeout=60.0)
        log("phase 1 start")
        last_progress = time.monotonic()
        serial_phase_ticks = 0
        peer_link_menu_ready = False
        while time.monotonic() < deadline:
            # LinkMenu is a phase boundary, but the ROM can leave SC bit 7
            # armed while waiting for the next peer clock. Keep ticking until
            # both peers announce LinkMenu, then stop the emulators and drain
            # only the transport work already admitted by the reader. This
            # avoids closing an in-flight edge without requiring an impossible
            # ROM-level "serial idle" state.
            if link_menu_announced:
                if not peer_link_menu_ready:
                    peer_link_menu_ready = (
                        link._network_backend.poll_peer_sync(sync_id=121)
                    )
                if peer_link_menu_ready:
                    if not link_menu_quiet_announced:
                        link._network_backend.wait_for_wire_idle(
                            timeout=10.0,
                            progress_callback=lambda: session.step(1),
                        )
                        link._network_backend.announce_sync(sync_id=122)
                        link_menu_quiet_announced = True
                        log("phase 1 wire-idle acknowledgement sent")
                    if link_menu_quiet_announced and not peer_link_menu_quiet_ready:
                        peer_link_menu_quiet_ready = (
                            link._network_backend.poll_peer_sync(sync_id=122)
                        )
                if peer_link_menu_quiet_ready:
                    link._network_backend.wait_for_wire_idle(
                        timeout=10.0,
                        allow_peer_close=True,
                        progress_callback=lambda: session.step(1),
                    )
                    log("phase 1 done: LinkMenu fired on both peers")
                    break
                if not link_menu_quiet_announced:
                    session.step(4)
                elif not peer_link_menu_quiet_ready:
                    # The local idle marker only means this peer has drained
                    # its currently admitted edges. The other peer may still
                    # be completing its final edge and needs this owner
                    # thread to keep pumping serial work until marker 122 is
                    # observed on both sides.
                    session.step(1)
                else:
                    # Both quiet markers are visible; avoid another emulator
                    # tick before the close-tolerant final drain.
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
                if (
                    counters["LinkMenu.waitForInputLoop"][0] > 0
                    and menu_fields_ready(
                        min_item=0,
                        max_item=link_menu_max,
                        expected_max=link_menu_max,
                        required_keys=0x01,
                    )
                    and not link_menu_announced
                ):
                    link_menu_announced = True
                    log("phase 1 local LinkMenu fired")
                    shot("01_link_menu")
                    link._network_backend.announce_sync(sync_id=121)
                    log("phase 1 LinkMenu readiness sent")
            else:
                if (
                    counters["LinkMenu.waitForInputLoop"][0] > 0
                    and menu_fields_ready(
                        min_item=0,
                        max_item=link_menu_max,
                        expected_max=link_menu_max,
                        required_keys=0x01,
                    )
                    and not link_menu_announced
                ):
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
                    f"LinkMenu={counters['LinkMenu'][0]} "
                    f"waitForInputLoop={counters['LinkMenu.waitForInputLoop'][0]} "
                    f"menu={menu_snapshot()}"
                )
                last_progress = time.monotonic()

        if not link_menu_announced or not peer_link_menu_quiet_ready:
            raise RuntimeError(
                "LinkMenu rendezvous did not converge before the gameplay phase: "
                f"local_announced={link_menu_announced} "
                f"peer_ready={peer_link_menu_ready} "
                f"local_quiet_announced={link_menu_quiet_announced} "
                f"peer_quiet_ready={peer_link_menu_quiet_ready} "
                f"menu={menu_snapshot()} state={state_snapshot()} "
                f"backend={backend_snapshot()}"
            )

        if args.goal == "trade":
            # Phase barrier: both sides at LinkMenu before voting Trade
            # Center. Without this the vote-exchange nibble loop has
            # no way to guarantee overlap in Pokemon's polling windows.
            log("sync: link_menu barrier")
            cooperative_sync(sync_id=1, timeout=60.0)
            log("sync: past link_menu barrier")
            # The LinkMenu hook fires before the ROM has finished installing
            # its final menu fields. Settle those fields and rendezvous at
            # the actual Trade Center cursor before sending A; otherwise a
            # peer can consume the selection while the other is still in the
            # menu's setup loop.
            wait_for_menu_ready(
                label="trade LinkMenu",
                min_item=0,
                max_item=link_menu_max,
                expected_max=link_menu_max,
                required_keys=0x01,
                timeout=60.0,
            )
            session.step(60)
            move_menu_to_item(
                target=0,
                min_item=0,
                max_item=link_menu_max,
                label="trade LinkMenu",
                input_duration=12,
                settle_frames=40,
            )
            # Both peers have now reached the ROM-owned Trade cursor. Keep
            # ticking while rendezvousing after the real A event: the ROM's
            # selection exchange may begin immediately, so a non-ticking
            # host barrier can hold the internal-clock peer before the
            # external-clock peer has reached its native SC wait. A one-frame
            # cooperative barrier preserves the authentic input and serial
            # paths while keeping both owner threads live at the handoff.
            session.press("a", duration=4)
            cooperative_sync(sync_id=19, timeout=120.0, step_frames=1)
            log("sync: trade menu A events queued on both peers")
            session.step(20)
            # Trade Center warp — A-mash until map becomes 0xEF. Once one
            # peer reaches the map it must keep ticking while the other peer
            # completes its ROM-owned selection exchange; stopping the first
            # owner here can strand the second peer before it can announce
            # the same milestone. Keep the established 20-frame input
            # cadence here: one-frame calls can leave the ROM's joypad pulse
            # and serial handoff split across too many host-thread scheduling
            # boundaries on a cross-family pair.
            TRADE_CENTER = 0xEF
            warp_announced = False
            peer_warp_ready = False
            while time.monotonic() < deadline:
                if (
                    session.read_game_state().overworld.map_id == TRADE_CENTER
                    and not warp_announced
                ):
                    link._network_backend.announce_sync(sync_id=2)
                    warp_announced = True
                    log("local trade-center warp announced")
                    shot("02_trade_center")
                if warp_announced and link._network_backend.poll_peer_sync(sync_id=2):
                    peer_warp_ready = True
                    break
                if not warp_announced:
                    session.press("a", duration=4)
                session.step(20)
            if not (warp_announced and peer_warp_ready):
                raise RuntimeError(
                    "trade-center warp rendezvous did not converge: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )
            log("trade center warp complete on both peers")

            # Both ROMs have now reached the map, but the peer can still have
            # one final serial edge in flight. Drain it while both owners
            # continue ticking, then use a symmetric release rendezvous. A
            # no-tick hold is unsafe here because an already-armed ROM may
            # still need its next native serial callback to finish the phase.
            link._network_backend.wait_for_wire_idle(
                timeout=120.0,
                progress_callback=lambda: session.step(1),
                stable_checks=4,
            )
            cooperative_sync(sync_id=18, timeout=120.0, step_frames=1)
            log("sync: trade-center cooperative barrier complete")
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
            # LinkMenu's entry hook precedes its text rendering and menu
            # initialization. Wait for the ROM-owned menu fields, navigate
            # with bounded one-shot input, and verify the cursor before
            # committing the choice.
            link_menu_before = wait_for_menu_ready(
                label="battle LinkMenu",
                min_item=0,
                max_item=link_menu_max,
                expected_max=link_menu_max,
                required_keys=0x01,
                timeout=60.0,
            )
            initial_item = link_menu_before["wCurrentMenuItem"]
            # LinkMenu's hook and menu-field initialization occur before its
            # first stable joypad polling window. Give the ROM the same
            # post-entry settling interval as the in-process acceptance
            # driver before issuing the directional event.
            session.step(60)
            move_menu_to_item(
                target=1,
                min_item=0,
                max_item=link_menu_max,
                label="battle LinkMenu",
                input_duration=12,
                settle_frames=40,
            )
            selected_item = current_menu_item()
            log(
                "battle LinkMenu input selection; "
                f"initial_item={initial_item} selected_item={selected_item}"
            )
            if selected_item != 1:
                raise RuntimeError(
                    "ordinary LinkMenu input did not select COLOSSEUM: "
                    f"initial_item={initial_item} selected_item={selected_item} "
                    f"state={state_snapshot()}"
                )
            # Both peers have now observed the ROM-owned BATTLE cursor. Keep
            # ticking while rendezvousing at this boundary: a visible menu
            # does not prove that the final LinkMenu serial edge has drained,
            # so a no-tick hold here could strand an EDGE_RESP. Once both
            # processes announce readiness, they commit A from the same menu
            # phase without starving the owner pump.
            cooperative_sync(sync_id=117, timeout=120.0, step_frames=1)
            session.step(4)
            session.press("a", duration=4)
            session.step(20)
            shot("02_battle_menu")

            COLOSSEUM = 0xF0
            # Do not treat the selection A press as proof of a warp.  A
            # cross-version peer can leave LinkMenu first and continue to
            # tick through Cable Club while this ROM is still waiting for its
            # own ROM-owned selection exchange.  Keep the local emulator
            # active, retry only through the public A-input path, and fail
            # closed if the observable map never changes.
            battle_warp_deadline = min(deadline, time.monotonic() + 120.0)
            while time.monotonic() < battle_warp_deadline:
                if session.read_game_state().overworld.map_id == COLOSSEUM:
                    break
                session.press("a", duration=4)
                session.step(20)
            if session.read_game_state().overworld.map_id != COLOSSEUM:
                raise RuntimeError(
                    "battle LinkMenu selection did not reach Colosseum: "
                    f"state={state_snapshot()} menu={menu_snapshot()} "
                    f"backend={backend_snapshot()}"
                )

            # Hold the first peer at the verified map boundary while the
            # other peer completes its own ordinary menu selection.  The
            # cooperative wait continues stepping the ROM, so a native
            # serial IRQ cannot be starved by the phase rendezvous.
            battle_warp_announced = False
            peer_battle_warp_ready = False
            battle_warp_sync_deadline = min(deadline, time.monotonic() + 120.0)
            while time.monotonic() < battle_warp_sync_deadline:
                if not battle_warp_announced:
                    link._network_backend.announce_sync(sync_id=112)
                    battle_warp_announced = True
                    log(
                        "battle Colosseum warp verified; readiness sent "
                        f"{state_snapshot()}"
                    )
                if link._network_backend.poll_peer_sync(sync_id=112):
                    peer_battle_warp_ready = True
                    break
                session.step(20)
            if not peer_battle_warp_ready:
                raise RuntimeError(
                    "battle Colosseum warp rendezvous did not converge: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )
            log("battle Colosseum warp complete on both peers")
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

            # The party-patch counters can fire before the final ROM-owned
            # serial work has returned to the battle path. Wait for the later
            # VS-text hook, which is the first observed boundary shared by
            # both variants after that exchange. Do not send input after the
            # hook fires; the following transition is timed ROM work.
            intro_deadline = min(deadline, time.monotonic() + 300.0)
            last_prebattle_log = time.monotonic()
            while (
                time.monotonic() < intro_deadline
                and counters["DisplayLinkBattleVersusTextBox"][0] == 0
            ):
                session.press("a", duration=4)
                # Keep the real-ROM serial exchange moving at the same
                # throughput as the established battle driver. The owner
                # tick wrapper still services queued edges at every frame;
                # one-frame calls add enough Python scheduling overhead to
                # exhaust the bounded prebattle window before the VS hook.
                session.step(20)
                if time.monotonic() - last_prebattle_log > 15.0:
                    log(
                        "battle prebattle progress: "
                        f"counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                        f"state={state_snapshot()} cpu={cpu_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    last_prebattle_log = time.monotonic()
            log(
                "battle VS-text milestone reached; waiting for wire quiet "
                f"counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                f"state={state_snapshot()} backend={backend_snapshot()}"
            )
            if counters["DisplayLinkBattleVersusTextBox"][0] == 0:
                raise RuntimeError(
                    "battle VS-text milestone did not complete: "
                    f"counters={counters} state={state_snapshot()} "
                    f"backend={backend_snapshot()}"
                )
            # A later edge can still be admitted immediately after the hook.
            # Let the owner continue for a bounded stable quiet window before
            # entering the no-tick ready/release barrier.
            link._network_backend.wait_for_wire_idle(
                timeout=120.0,
                progress_callback=lambda: session.step(1),
                stable_checks=4,
            )
            log("battle VS-text wire quiet; entering hold barrier")
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=113,
                release_sync_id=114,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
            )
            log("battle VS-text hold barrier complete; entering transition")

            # The VS splash and transition are timed ROM work. Do not mash A
            # through them: an input consumed in the transition can leave the
            # two independent ROMs in different battle menu states. Wait for
            # the ROM's own battle-menu hooks before selecting FIGHT.
            transition_deadline = min(deadline, time.monotonic() + 180.0)
            while time.monotonic() < transition_deadline:
                if counters["BattleTransition"][0] > 0:
                    break
                session.step(1)
            if counters["BattleTransition"][0] == 0:
                raise RuntimeError(
                    "battle transition did not start after VS-text barrier: "
                    f"counters={counters} state={state_snapshot()} "
                    f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
                )
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=115,
                release_sync_id=116,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
            )
            log("battle transition hold barrier complete; entering menu")
            menu_deadline = min(deadline, time.monotonic() + 180.0)
            while time.monotonic() < menu_deadline:
                if (
                    counters["MainInBattleLoop"][0] > 0
                    and counters["DisplayBattleMenu"][0] > 0
                ):
                    break
                session.step(1)
            if not (
                counters["MainInBattleLoop"][0] > 0
                and counters["DisplayBattleMenu"][0] > 0
            ):
                raise RuntimeError(
                    "battle menu did not open after intro: "
                    f"counters={counters} state={state_snapshot()} "
                    f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
                )
            log(
                "battle menu hook reached; settling ROM menu fields "
                f"menu={menu_snapshot()} state={state_snapshot()} "
                f"backend={backend_snapshot()}"
            )
            # The DisplayBattleMenu hook fires before it installs
            # wMaxMenuItem/wMenuWatchedKeys. Wait for those ROM-owned fields
            # on this side before announcing the cross-process boundary.
            battle_menu_ready_deadline = min(deadline, time.monotonic() + 60.0)
            while time.monotonic() < battle_menu_ready_deadline:
                if (
                    counters["DisplayBattleMenu.leftColumn_WaitForInput"][0] > 0
                    or counters["DisplayBattleMenu.rightColumn_WaitForInput"][0] > 0
                ) and menu_fields_ready(
                    min_item=0,
                    max_item=1,
                    expected_max=1,
                    required_keys=0x01,
                ):
                    break
                session.step(2)
            if not (
                (
                    counters["DisplayBattleMenu.leftColumn_WaitForInput"][0] > 0
                    or counters["DisplayBattleMenu.rightColumn_WaitForInput"][0] > 0
                )
                and menu_fields_ready(
                    min_item=0,
                    max_item=1,
                    expected_max=1,
                    required_keys=0x01,
                )
            ):
                raise RuntimeError(
                    "battle menu fields did not become input-ready: "
                    f"counters={counters} menu={menu_snapshot()} "
                    f"state={state_snapshot()} cpu={cpu_snapshot()} "
                    f"backend={backend_snapshot()}"
                )
            log(
                "battle menu fields ready; entering sync "
                f"menu={menu_snapshot()} state={state_snapshot()}"
            )
            # Both ROMs now own a live battle menu. Rendezvous before either
            # side commits FIGHT so a subprocess cannot consume A while its
            # peer is still finishing DisplayTextBoxID/menu setup.
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=12,
                release_sync_id=16,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
            )
            # Match the in-process acceptance driver: after the rendezvous,
            # give both ROMs a short input-free window to finish entering
            # HandleMenuInput before sending the single ordinary A event.
            session.step(4)
            move_menu_to_item(
                target=0,
                min_item=0,
                max_item=1,
                label="battle command menu",
            )
            log(
                "battle menu ready; selecting FIGHT through ordinary input "
                f"state={state_snapshot()} cpu={cpu_snapshot()}"
            )
            move_menu_deadline = min(deadline, time.monotonic() + 120.0)
            next_fight_input_tick = -1
            fight_input_attempts = 0
            while (
                time.monotonic() < move_menu_deadline
                and (
                    counters["MoveSelectionMenu"][0] == 0
                    or counters["MoveSelectionMenu.menuset"][0] == 0
                    or not menu_fields_ready(min_item=1, max_item=4)
                )
            ):
                # A peer can still be returning from the rendezvous while
                # this ROM is waiting in HandleMenuInput.  If the first
                # one-frame event was sampled before that wait became active,
                # retry it at a bounded frame interval, but only while the
                # ROM still describes the command menu.  This remains the
                # public directional/A path and stops as soon as the ROM's
                # move-menu hook proves that FIGHT was consumed.
                if (
                    menu_fields_ready(
                        min_item=0,
                        max_item=1,
                        expected_max=1,
                        required_keys=0x01,
                    )
                    and session.current_tick() >= next_fight_input_tick
                ):
                    session.press("a")
                    fight_input_attempts += 1
                    next_fight_input_tick = session.current_tick() + 8
                session.step(2)
            if (
                counters["MoveSelectionMenu"][0] == 0
                or counters["MoveSelectionMenu.menuset"][0] == 0
                or not menu_fields_ready(min_item=1, max_item=4)
            ):
                raise RuntimeError(
                    "move menu did not open after FIGHT: "
                    f"counters={counters} menu={menu_snapshot()} "
                    f"state={state_snapshot()} "
                    f"cpu={cpu_snapshot()} backend={backend_snapshot()} "
                    f"fight_input_attempts={fight_input_attempts}"
                )
            log(
                "battle move menu fields ready; entering sync "
                f"menu={menu_snapshot()} state={state_snapshot()}"
            )
            # The move menu is another ROM-owned input boundary. Match the
            # peers before reading its cursor or sending the legal move.
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=13,
                release_sync_id=17,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
            )
            session.step(4)
            selected_move_id = choose_first_usable_battle_move()
            # Give the ROM a bounded opportunity to consume the ordinary A
            # input and leave MoveSelectionMenu. Without this handoff one
            # subprocess can log the selection before its input is actually
            # processed, while the other begins LinkBattleExchangeData.
            log(
                "battle move selected through ROM menu; native exchange begins "
                f"move_id={selected_move_id} state={state_snapshot()} "
                f"cpu={cpu_snapshot()}"
            )

            last_battle_log = time.monotonic()
            while time.monotonic() < deadline:
                if counters["EndOfBattle"][0] > 0:
                    break
                battle_turn_complete = (
                    counters["LinkBattleExchangeData"][0] > 0
                    and (
                        counters["ExecutePlayerMove"][0]
                        + counters["ExecuteEnemyMove"][0]
                        > 0
                    )
                )
                if battle_turn_complete and not battle_turn_announced:
                    link._network_backend.announce_sync(sync_id=14)
                    battle_turn_announced = True
                    shot("05_battle_turn")
                    log(
                        "announced battle turn completion "
                        f"lbe={counters['LinkBattleExchangeData'][0]} "
                        f"execute_player={counters['ExecutePlayerMove'][0]} "
                        f"execute_enemy={counters['ExecuteEnemyMove'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                if battle_turn_announced and link._network_backend.poll_peer_sync(sync_id=14):
                    peer_battle_turn_ready = True
                    break
                session.step(20)
                if time.monotonic() - last_battle_log > 15.0:
                    log(
                        "battle progress: "
                        f"counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                        f"state={state_snapshot()} cpu={cpu_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    last_battle_log = time.monotonic()
            if battle_turn_announced:
                try:
                    cooperative_sync(sync_id=15, timeout=120.0)
                    log("sync: past battle turn barrier")
                    shot("06_battle_synced")
                except Exception as exc:  # noqa: BLE001
                    drive_status = "error"
                    drive_error = f"{type(exc).__name__}: {exc}"
                    log(f"battle turn sync raised {type(exc).__name__}: {exc}")
                post_deadline = min(deadline, time.monotonic() + 10.0)
                while time.monotonic() < post_deadline:
                    session.press("a", duration=4)
                    session.step(20)
            if not peer_battle_turn_ready:
                log(
                    "peer battle turn completion not observed before deadline "
                    f"lbe={counters['LinkBattleExchangeData'][0]} "
                    f"execute_player={counters['ExecutePlayerMove'][0]} "
                    f"execute_enemy={counters['ExecuteEnemyMove'][0]} "
                    f"backend={backend_snapshot()}"
                )
    except Exception as exc:  # noqa: BLE001
        drive_status = "error"
        drive_error = f"{type(exc).__name__}: {exc}"
        log(f"EXCEPTION in drive loop: {type(exc).__name__}: {exc}")

    finally:
        try:
            final_state = state_snapshot()
            final_cpu = cpu_snapshot()
        except Exception as exc:  # noqa: BLE001
            log(f"final state snapshot raised {type(exc).__name__}: {exc}")
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
                battle_turn_announced
                and peer_battle_turn_ready
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
    result["_final_state"] = final_state
    result["_final_cpu"] = final_cpu
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
