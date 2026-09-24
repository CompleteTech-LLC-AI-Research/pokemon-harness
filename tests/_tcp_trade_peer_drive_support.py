"""Drive-loop helper methods for the TCP trade peer.

The 21 closures of the pre-split 1896-line ``_run_peer`` (#127) become methods on
this mixin.  Names the single function kept in closure cells became ``self.``
attributes; the remaining statements are the moved text.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable

from tests._battle_turn_evidence import choose_supported_battle_move
from tests._tcp_trade_peer_link_menu import (
    _TRADE_DIAG_SYMBOLS,
    _link_menu_has_real_selection_exchange,
    _read_link_menu_fields,
)
from tests._tcp_trade_peer_sync import _deadline_remaining, _party_summary, _peer_shutdown_sync


class _PeerDriveSupportMixin:
    def log(self, msg):
        print(f"[peer {self.args.role}] {msg}", file=sys.stderr, flush=True)

    def close_pre_link_menu_observer(self):
        if self.pre_link_menu_history is None:
            return
        try:
            self.pre_link_menu_history.close()
        except BaseException as exc:  # noqa: BLE001 - Diagnostic cleanup cannot replace gameplay errors.
            self.pre_link_menu_history.available = False
            self.pre_link_menu_history.reason = "cleanup_error:" + type(exc).__name__[:128]
            self.pre_link_menu_history._error("close", "cleanup", exc)

    def remaining(self, phase: str) -> float:
        return _deadline_remaining(self.deadline, phase=phase)

    def setup_error_text(self, exc: BaseException) -> str:
        try:
            message = f"{type(exc).__name__}: {exc}"
        except BaseException:  # noqa: BLE001
            message = type(exc).__name__
        return message[:2048]

    def backend_snapshot(self) -> dict[str, object]:
        backend = getattr(self.link, "_network_backend", None) if self.link is not None else None
        if backend is None:
            return {}
        try:
            return backend.debug_snapshot()
        except BaseException:  # noqa: BLE001
            return {}

    def link_menu_snapshot(self) -> dict[str, object]:
        """Capture ROM-owned LinkMenu selection fields for failed runs.

        These are observations only.  In particular, this helper never
        writes the send/receive buffers or any connection/warp state.  The
        final values may have been reused after selection and cannot alone
        distinguish a malformed exchange from a valid vote followed by a
        failed warp. The bounded history preserves earlier observations.
        """
        if self.session is None:
            return {}
        return _read_link_menu_fields(self.session)

    def emit_result(self, *, setup_failed: bool = False) -> None:
        if setup_failed or self.session is None:
            party_after: dict[str, object] = {}
        elif self.party_after_trade is not None:
            party_after = self.party_after_trade
        else:
            try:
                party_after = _party_summary(self.session)
            except BaseException:  # noqa: BLE001
                party_after = {}
        result = {s: self.counters[s][0] for s in _TRADE_DIAG_SYMBOLS}
        result["_role"] = self.args.role
        result["_version"] = self.args.version
        result["party_before"] = {} if setup_failed else self.party_before
        result["party_after"] = party_after
        result["_final_state"] = self.final_state
        result["_final_cpu"] = self.final_cpu
        result["_link_menu_state"] = {} if setup_failed else self.link_menu_state
        result["_cable_club_confirmation"] = (
            {}
            if setup_failed or self.cable_club_confirmation is None
            else dict(self.cable_club_confirmation)
        )
        result["_link_menu_history"] = self.link_menu_history.snapshot()
        result["battle_turn"] = (
            self.battle_observer.snapshot() if self.battle_observer is not None else {}
        )
        if self.pre_link_menu_history is not None:
            result["_pre_link_menu_history"] = self.pre_link_menu_history.snapshot()
        result["_shots"] = self.shots
        result["_backend_stats"] = self.backend_snapshot()
        result["_drive_status"] = self.drive_status
        result["_drive_error"] = self.drive_error
        result["_deadline_exceeded"] = self.deadline_exceeded
        try:
            encoded = json.dumps(result)
        except (TypeError, ValueError):
            result["party_before"] = {}
            result["party_after"] = {}
            result["_final_state"] = {}
            result["_final_cpu"] = {}
            result["_shots"] = []
            result["_backend_stats"] = {}
            encoded = json.dumps(result)
        self.log(f"final counters: {result}")
        # Sentinel-delimited JSON line so the parent can grep it out of
        # the ROM-loading warning spam on stdout.
        print(f"__TCP_TRADE_RESULT__ {encoded}")

    def shot(self, phase: str) -> None:
        if self.args.record_dir is None:
            return
        # Render one frame on demand so headless/null-window screenshots
        # reflect the restored/current LCD state.
        try:
            self.session.step(1, render=True)
            img = self.session._pyboy.screen.image
        except Exception as exc:  # noqa: BLE001
            self.log(f"shot {phase} failed before save: {type(exc).__name__}: {exc}")
            return
        pair = f"{self.args.label}." if self.args.label else ""
        path = self.args.record_dir / f"{pair}{phase}.{self.args.role}.{self.args.version}.png"
        try:
            img.save(path)
        except Exception as exc:  # noqa: BLE001
            self.log(f"shot {phase} save failed: {type(exc).__name__}: {exc}")
            return
        self.shots.append(str(path))
        self.log(f"shot {phase}: {path}")

    def state_snapshot(self) -> dict[str, int]:
        snapshot = {
            "map_id": self.session.read_game_state().overworld.map_id,
            "hSerialConnectionStatus": self.session._pyboy.memory[
                self.session.symbols.addr_of("hSerialConnectionStatus")
            ],
            "wLinkState": self.session._pyboy.memory[self.session.symbols.addr_of("wLinkState")],
        }
        for name in (
            "wIsInBattle",
            "wBattleType",
            "wActionResultOrTookBattleTurn",
            "wMoveMenuType",
            "wPlayerSelectedMove",
        ):
            try:
                snapshot[name] = int(self.session._pyboy.memory[self.session.symbols.addr_of(name)])
            except (AttributeError, KeyError, TypeError):
                pass
        return snapshot

    def cpu_snapshot(self) -> dict[str, object]:
        """Capture bounded CPU/serial state for a stalled native run."""
        cpu = getattr(getattr(self.session._pyboy, "mb", None), "cpu", None)
        serial = getattr(getattr(self.session._pyboy, "mb", None), "serial", None)
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
        self, sync_id: int, *, timeout: float = 60.0, step_frames: int = 4
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
        self.link._network_backend.announce_sync(sync_id=sync_id)
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            if self.link._network_backend.poll_peer_sync(sync_id=sync_id):
                return
            self.session.step(step_frames)
        raise RuntimeError(
            f"cooperative sync {sync_id} did not converge: "
            f"local={self.state_snapshot()} backend={self.backend_snapshot()}"
        )

    def passive_sync(
        self, *, ready_sync_id: int, release_sync_id: int, timeout: float = 60.0
    ) -> None:
        """Rendezvous without advancing the restored game state.

        This is used only before the first gameplay input. Both peers have
        already attached and negotiated their native roles, so progressing a
        ROM while the other process is still in setup can create a
        direction-dependent first exchange. A two-marker control handshake
        keeps this pre-drive phase transport-only and clamps its deadline to
        the process-wide cutoff.
        """
        backend = self.link._network_backend
        deadline_at = min(self.deadline, time.monotonic() + timeout)

        def wait_for_peer(marker: int, *, phase: str) -> None:
            while not backend.poll_peer_sync(sync_id=marker):
                remaining_at = deadline_at - time.monotonic()
                if remaining_at <= 0:
                    raise RuntimeError(
                        f"passive sync {phase} did not converge: "
                        f"marker={marker} backend={self.backend_snapshot()}"
                    )
                time.sleep(min(0.001, remaining_at))

        backend.announce_sync(sync_id=ready_sync_id)
        wait_for_peer(ready_sync_id, phase="ready")
        backend.announce_sync(sync_id=release_sync_id)
        wait_for_peer(release_sync_id, phase="release")

    def peer_shutdown_sync(self, *, ready_sync_id: int, timeout: float = 120.0) -> None:
        _peer_shutdown_sync(
            self.link._network_backend,
            step=self.session.step,
            backend_snapshot=self.backend_snapshot,
            ready_sync_id=ready_sync_id,
            timeout=timeout,
            release_sync_id=ready_sync_id + 1,
            progress_after_marker=lambda: self.session.step(1),
            frame_leader=(
                self.link._network_is_internal_clock if self.link._network_frame_barrier else None
            ),
        )

    def current_menu_item(self) -> int | None:
        try:
            return self.session._pyboy.memory[self.session.symbols.addr_of("wCurrentMenuItem")]
        except (AttributeError, KeyError, TypeError):
            return None

    def menu_snapshot(self) -> dict[str, int]:
        memory = self.session._pyboy.memory
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
                snapshot[name] = int(memory[self.session.symbols.addr_of(name)])
            except (AttributeError, KeyError, TypeError):
                pass
        return snapshot

    def wait_for_menu_ready(
        self,
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
            snapshot = self.menu_snapshot()
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
            self.session.step(2)
        raise RuntimeError(
            f"{label} did not become input-ready: "
            f"menu={self.menu_snapshot()} state={self.state_snapshot()} "
            f"cpu={self.cpu_snapshot()} backend={self.backend_snapshot()}"
        )

    def menu_fields_ready(
        self,
        *,
        min_item: int,
        max_item: int,
        expected_max: int | None = None,
        required_keys: int = 0x01,
    ) -> bool:
        snapshot = self.menu_snapshot()
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

    def wait_for_link_menu_selection_exchange(
        self,
        *,
        label: str,
        timeout: float = 120.0,
        input_retry: Callable[[], None] | None = None,
    ) -> None:
        """Require both ROMs to observe a real, non-idle LinkMenu exchange.

        Each marker reports one local, post-call directional observation.  A
        peer marker supplies a second independent observation, so the pair
        proves the combined exchange without assuming that either single ROM
        retains both buffer directions. It never selects a menu item or
        infers Game Boy clock ownership from the TCP role. Keep stepping while
        the peer catches up because either ROM may be the active serial clock
        at this point.
        """

        deadline_at = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < deadline_at:
            if not self.link_menu_exchange_announced and _link_menu_has_real_selection_exchange(
                self.link_menu_history
            ):
                self.link._network_backend.announce_sync(sync_id=125)
                self.link_menu_exchange_announced = True
                self.log(f"{label}: local directional LinkMenu selection evidence observed")
            if self.link_menu_exchange_announced and not self.peer_link_menu_exchange_ready:
                self.peer_link_menu_exchange_ready = self.link._network_backend.poll_peer_sync(
                    sync_id=125
                )
            if self.link_menu_exchange_announced and self.peer_link_menu_exchange_ready:
                self.log(f"{label}: peer directional LinkMenu selection evidence observed")
                return
            if input_retry is not None:
                input_retry()
            self.session.step(1)
        raise RuntimeError(
            f"{label} LinkMenu selection exchange did not converge: "
            f"local_directional_evidence={self.link_menu_exchange_announced} "
            f"peer_directional_evidence={self.peer_link_menu_exchange_ready} "
            f"local_directions={sorted(self.link_menu_history.first_decisive)} "
            f"history={self.link_menu_history.snapshot()} "
            f"menu={self.menu_snapshot()} state={self.state_snapshot()} "
            f"backend={self.backend_snapshot()}"
        )

    def move_menu_to_item(
        self,
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
            snapshot = self.wait_for_menu_ready(
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
            self.session.press(button, duration=input_duration)
            self.session.step(settle_frames)
        raise RuntimeError(
            f"{label} cursor did not reach {target}: "
            f"menu={self.menu_snapshot()} state={self.state_snapshot()} "
            f"cpu={self.cpu_snapshot()} backend={self.backend_snapshot()}"
        )

    def read_active_battle_moves(self) -> tuple[tuple[int, int], ...]:
        """Read the ROM-populated active move/PP slots without mutation."""
        memory = self.session._pyboy.memory
        addr_of = self.session.symbols.addr_of
        moves_addr = addr_of("wBattleMonMoves")
        pp_addr = addr_of("wBattleMonPP")
        return tuple(
            (
                int(memory[moves_addr + move_idx]),
                int(memory[pp_addr + move_idx]) & 0x3F,
            )
            for move_idx in range(4)
        )

    def choose_first_usable_battle_move(self) -> int:
        """Navigate to an existing supported damaging move with PP."""
        ready = self.wait_for_menu_ready(
            label="battle move menu",
            min_item=1,
            max_item=4,
            required_keys=0x01,
            timeout=60.0,
        )
        active_moves = self.read_active_battle_moves()
        move_count = ready.get("wMaxMenuItem", 0) - 1
        if not 1 <= move_count <= len(active_moves):
            raise RuntimeError(f"ROM move-menu count is invalid: menu={ready} moves={active_moves}")
        target, selected_move_id = choose_supported_battle_move(
            self.session, active_moves[:move_count]
        )
        # Move-menu cursors are one-based. Let the ROM install the cursor
        # before reading it; no RAM write or test-only selection hook is used.
        self.move_menu_to_item(
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
        prior_move_selection_phase = self.counters["MainInBattleLoop.selectEnemyMove"][0]
        next_move_input_tick = -1
        move_input_attempts = 0
        selection_deadline = min(time.monotonic() + 30.0, self.deadline)
        while time.monotonic() < selection_deadline:
            if self.counters["MainInBattleLoop.selectEnemyMove"][0] > prior_move_selection_phase:
                return selected_move_id
            if (
                self.menu_fields_ready(
                    min_item=1,
                    max_item=move_count,
                    required_keys=0x01,
                )
                and self.session.current_tick() >= next_move_input_tick
            ):
                self.session.press("a")
                move_input_attempts += 1
                next_move_input_tick = self.session.current_tick() + 8
            self.session.step(2)
        raise RuntimeError(
            "ROM move-menu A input was not consumed: "
            f"expected={selected_move_id} "
            f"select_enemy_move={self.counters['MainInBattleLoop.selectEnemyMove'][0]} "
            f"move_input_attempts={move_input_attempts} "
            f"menu={self.menu_snapshot()} counters={self.counters} "
            f"state={self.state_snapshot()} cpu={self.cpu_snapshot()} "
            f"backend={self.backend_snapshot()}"
        )
