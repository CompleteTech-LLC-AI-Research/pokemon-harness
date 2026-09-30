"""Battle-phase driver for the TCP trade peer.

The pre-split ``_run_peer`` battle branch (#127) becomes ``_battle``.
"""

from __future__ import annotations

import time

from tests._tcp_trade_peer_link_menu import _TRADE_DIAG_SYMBOLS
from tests._tcp_trade_peer_sync import _hold_at_sync_boundary


class _PeerDriveBattleMixin:
    def _battle(self) -> None:
        self.log("sync: link_menu battle barrier")
        self.cooperative_sync(sync_id=11, timeout=120.0)
        self.log("sync: past link_menu battle barrier")

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
        link_menu_before = self.wait_for_menu_ready(
            label="battle LinkMenu",
            min_item=0,
            max_item=self.link_menu_max,
            expected_max=self.link_menu_max,
            required_keys=0x01,
            timeout=60.0,
        )
        initial_item = link_menu_before["wCurrentMenuItem"]
        # LinkMenu's hook and menu-field initialization occur before its
        # first stable joypad polling window. Give the ROM the same
        # post-entry settling interval as the in-process acceptance
        # driver before issuing the directional event.
        self.session.step(60)
        self.move_menu_to_item(
            target=1,
            min_item=0,
            max_item=self.link_menu_max,
            label="battle LinkMenu",
            input_duration=12,
            settle_frames=40,
        )
        selected_item = self.current_menu_item()
        self.log(
            "battle LinkMenu input selection; "
            f"initial_item={initial_item} selected_item={selected_item}"
        )
        if selected_item != 1:
            raise RuntimeError(
                "ordinary LinkMenu input did not select COLOSSEUM: "
                f"initial_item={initial_item} selected_item={selected_item} "
                f"state={self.state_snapshot()}"
            )
        # Both peers have now observed the ROM-owned BATTLE cursor. Keep
        # each owner live while rendezvousing at this boundary: a visible
        # menu does not prove that the final LinkMenu serial edge has
        # drained, and a no-tick hold could strand an EDGE_RESP. Once both
        # processes announce readiness, they commit A from the same menu
        # phase without starving the owner pump.
        self.cooperative_sync(sync_id=117, timeout=120.0, step_frames=1)
        self.session.press("a", duration=4)
        # Do not advance based on the input event alone.  Both ROMs must
        # return with their own native directional LinkMenu evidence.
        next_battle_input_tick = self.session.current_tick() + 12

        def retry_battle_selection_input() -> None:
            nonlocal next_battle_input_tick
            if self.session.current_tick() < next_battle_input_tick:
                return
            if not self.menu_fields_ready(
                min_item=1,
                max_item=1,
                expected_max=self.link_menu_max,
                required_keys=0x01,
            ):
                return
            self.session.press("a", duration=4)
            next_battle_input_tick = self.session.current_tick() + 12

        self.wait_for_link_menu_selection_exchange(
            label="battle",
            input_retry=retry_battle_selection_input,
        )
        self.shot("02_battle_menu")

        COLOSSEUM = 0xF0
        # Do not treat the selection A press as proof of a warp.  A
        # cross-version peer can leave LinkMenu first and continue to
        # tick through Cable Club while this ROM is still waiting for its
        # own ROM-owned selection exchange.  Keep the local emulator
        # active, retry only through the public A-input path, and fail
        # closed if the observable map never changes.
        battle_warp_deadline = min(self.deadline, time.monotonic() + 120.0)
        while time.monotonic() < battle_warp_deadline:
            if self.session.read_game_state().overworld.map_id == COLOSSEUM:
                break
            self.session.press("a", duration=4)
            self.session.step(20)
        if self.session.read_game_state().overworld.map_id != COLOSSEUM:
            raise RuntimeError(
                "battle LinkMenu selection did not reach Colosseum: "
                f"state={self.state_snapshot()} menu={self.menu_snapshot()} "
                f"backend={self.backend_snapshot()}"
            )

        # Hold the first peer at the verified map boundary while the
        # other peer completes its own ordinary menu selection.  The
        # cooperative wait continues stepping the ROM, so a native
        # serial IRQ cannot be starved by the phase rendezvous.
        battle_warp_announced = False
        peer_battle_warp_ready = False
        battle_warp_sync_deadline = min(self.deadline, time.monotonic() + 120.0)
        while time.monotonic() < battle_warp_sync_deadline:
            if not battle_warp_announced:
                self.link._network_backend.announce_sync(sync_id=112)
                battle_warp_announced = True
                self.log(f"battle Colosseum warp verified; readiness sent {self.state_snapshot()}")
            if self.link._network_backend.poll_peer_sync(sync_id=112):
                peer_battle_warp_ready = True
                break
            self.session.step(20)
        if not peer_battle_warp_ready:
            raise RuntimeError(
                "battle Colosseum warp rendezvous did not converge: "
                f"local={self.state_snapshot()} backend={self.backend_snapshot()}"
            )
        self.log("battle Colosseum warp complete on both peers")
        self.shot("03_colosseum")
        # Startup negotiation leaves Yellow's input-sensitive preamble
        # unpaced. Both peers have now observed the same ROM-owned map
        # boundary, so use the existing frame turns for every battle
        # pair. Otherwise a fast external-clock ROM can exhaust its
        # receive counter while a peer byte is still arriving and store
        # stale party data. Owner progress still services native edges
        # while either endpoint waits for the frame handshake.
        self.link.set_network_frame_barrier(True)
        self.log("enabled frame barrier at Colosseum boundary")
        self.session.step(120)

        self.conn_status = self.session._pyboy.memory[
            self.session.symbols.addr_of("hSerialConnectionStatus")
        ]
        self.walk_dir = "right" if self.conn_status == 0x02 else "left"
        for _ in range(6):
            if self.counters["CableClub_DoBattleOrTrade"][0] > 0:
                break
            self.session.press(self.walk_dir, duration=8)
            self.session.step(30)
        while time.monotonic() < self.deadline:
            if self.counters["CableClub_DoBattleOrTrade"][0] > 0:
                break
            self.session.press("a", duration=4)
            self.session.step(20)
        self.log(
            "battle CableClub_DoBattleOrTrade fired "
            f"count={self.counters['CableClub_DoBattleOrTrade'][0]} "
            f"{self.state_snapshot()} backend={self.backend_snapshot()}"
        )
        self.shot("04_battle_launch")

        # The party-patch counters can fire before the final ROM-owned
        # serial work has returned to the battle path. Wait for the later
        # VS-text hook, which is the first observed boundary shared by
        # both variants after that exchange. Do not send input after the
        # hook fires; the following transition is timed ROM work.
        intro_deadline = min(self.deadline, time.monotonic() + 300.0)
        last_prebattle_log = time.monotonic()
        while (
            time.monotonic() < intro_deadline
            and self.counters["DisplayLinkBattleVersusTextBox"][0] == 0
        ):
            self.session.press("a", duration=4)
            # Keep the real-ROM serial exchange moving at the same
            # throughput as the established battle driver. The owner
            # tick wrapper still services queued edges at every frame;
            # one-frame calls add enough Python scheduling overhead to
            # exhaust the bounded prebattle window before the VS hook.
            self.session.step(20)
            if time.monotonic() - last_prebattle_log > 15.0:
                self.log(
                    "battle prebattle progress: "
                    f"counters={ {k: self.counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                    f"state={self.state_snapshot()} cpu={self.cpu_snapshot()} "
                    f"backend={self.backend_snapshot()}"
                )
                last_prebattle_log = time.monotonic()
        if self.counters["DisplayLinkBattleVersusTextBox"][0] == 0:
            raise RuntimeError(
                "battle VS-text milestone did not complete: "
                f"counters={self.counters} state={self.state_snapshot()} "
                f"backend={self.backend_snapshot()}"
            )
        self.log(
            "battle VS-text milestone reached; waiting for wire quiet "
            f"counters={ {k: self.counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
            f"state={self.state_snapshot()} backend={self.backend_snapshot()}"
        )
        # A later edge can still be admitted immediately after the hook.
        # Let the owner continue for a bounded stable quiet window before
        # entering the no-tick ready/release barrier.
        self.link._network_backend.wait_for_wire_idle(
            timeout=120.0,
            progress_callback=lambda: self.session.step(1),
            stable_checks=4,
        )
        self.log("battle VS-text wire quiet; entering hold barrier")
        _hold_at_sync_boundary(
            self.link._network_backend,
            ready_sync_id=113,
            release_sync_id=114,
            timeout=120.0,
            service_pending_edges=lambda: self.link._network_backend.service_pending_edges(
                max_edges=1
            ),
            progress_callback=lambda: self.session.step(1),
        )
        self.log("battle VS-text hold barrier complete; entering transition")

        # The VS splash and transition are timed ROM work. Do not mash A
        # through them: an input consumed in the transition can leave the
        # two independent ROMs in different battle menu states. Wait for
        # the ROM's own battle-menu hooks before selecting FIGHT.
        transition_deadline = min(self.deadline, time.monotonic() + 180.0)
        while time.monotonic() < transition_deadline:
            if self.counters["BattleTransition"][0] > 0:
                break
            self.session.step(1)
        if self.counters["BattleTransition"][0] == 0:
            raise RuntimeError(
                "battle transition did not start after VS-text barrier: "
                f"counters={self.counters} state={self.state_snapshot()} "
                f"cpu={self.cpu_snapshot()} backend={self.backend_snapshot()}"
            )
        _hold_at_sync_boundary(
            self.link._network_backend,
            ready_sync_id=115,
            release_sync_id=116,
            timeout=120.0,
            service_pending_edges=lambda: self.link._network_backend.service_pending_edges(
                max_edges=1
            ),
            progress_callback=lambda: self.session.step(1),
        )
        self.log("battle transition hold barrier complete; entering menu")
        menu_deadline = min(self.deadline, time.monotonic() + 180.0)
        while time.monotonic() < menu_deadline:
            if (
                self.counters["MainInBattleLoop"][0] > 0
                and self.counters["DisplayBattleMenu"][0] > 0
            ):
                break
            self.session.step(1)
        if not (
            self.counters["MainInBattleLoop"][0] > 0 and self.counters["DisplayBattleMenu"][0] > 0
        ):
            raise RuntimeError(
                "battle menu did not open after intro: "
                f"counters={self.counters} state={self.state_snapshot()} "
                f"cpu={self.cpu_snapshot()} backend={self.backend_snapshot()}"
            )
        self.log(
            "battle menu hook reached; settling ROM menu fields "
            f"menu={self.menu_snapshot()} state={self.state_snapshot()} "
            f"backend={self.backend_snapshot()}"
        )
        # The DisplayBattleMenu hook fires before it installs
        # wMaxMenuItem/wMenuWatchedKeys. Wait for those ROM-owned fields
        # on this side before announcing the cross-process boundary.
        battle_menu_ready_deadline = min(self.deadline, time.monotonic() + 60.0)
        while time.monotonic() < battle_menu_ready_deadline:
            if (
                self.counters["DisplayBattleMenu.leftColumn_WaitForInput"][0] > 0
                or self.counters["DisplayBattleMenu.rightColumn_WaitForInput"][0] > 0
            ) and self.menu_fields_ready(
                min_item=0,
                max_item=1,
                expected_max=1,
                required_keys=0x01,
            ):
                break
            self.session.step(2)
        if not (
            (
                self.counters["DisplayBattleMenu.leftColumn_WaitForInput"][0] > 0
                or self.counters["DisplayBattleMenu.rightColumn_WaitForInput"][0] > 0
            )
            and self.menu_fields_ready(
                min_item=0,
                max_item=1,
                expected_max=1,
                required_keys=0x01,
            )
        ):
            raise RuntimeError(
                "battle menu fields did not become input-ready: "
                f"counters={self.counters} menu={self.menu_snapshot()} "
                f"state={self.state_snapshot()} cpu={self.cpu_snapshot()} "
                f"backend={self.backend_snapshot()}"
            )
        self.log(
            "battle menu fields ready; entering sync "
            f"menu={self.menu_snapshot()} state={self.state_snapshot()}"
        )
        # Both ROMs now own a live battle menu. Rendezvous before either
        # side commits FIGHT so a subprocess cannot consume A while its
        # peer is still finishing DisplayTextBoxID/menu setup.
        _hold_at_sync_boundary(
            self.link._network_backend,
            ready_sync_id=12,
            release_sync_id=16,
            timeout=120.0,
            service_pending_edges=lambda: self.link._network_backend.service_pending_edges(
                max_edges=1
            ),
            progress_callback=lambda: self.session.step(1),
        )
        # Match the in-process acceptance driver: after the rendezvous,
        # give both ROMs a short input-free window to finish entering
        # HandleMenuInput before sending the single ordinary A event.
        self.session.step(4)
        self.move_menu_to_item(
            target=0,
            min_item=0,
            max_item=1,
            label="battle command menu",
        )
        self.log(
            "battle menu ready; selecting FIGHT through ordinary input "
            f"state={self.state_snapshot()} cpu={self.cpu_snapshot()}"
        )
        move_menu_deadline = min(self.deadline, time.monotonic() + 120.0)
        next_fight_input_tick = -1
        fight_input_attempts = 0
        while time.monotonic() < move_menu_deadline and (
            self.counters["MoveSelectionMenu"][0] == 0
            or self.counters["MoveSelectionMenu.menuset"][0] == 0
            or not self.menu_fields_ready(min_item=1, max_item=4)
        ):
            # A peer can still be returning from the rendezvous while
            # this ROM is waiting in HandleMenuInput.  If the first
            # one-frame event was sampled before that wait became active,
            # retry it at a bounded frame interval, but only while the
            # ROM still describes the command menu.  This remains the
            # public directional/A path and stops as soon as the ROM's
            # move-menu hook proves that FIGHT was consumed.
            if (
                self.menu_fields_ready(
                    min_item=0,
                    max_item=1,
                    expected_max=1,
                    required_keys=0x01,
                )
                and self.session.current_tick() >= next_fight_input_tick
            ):
                self.session.press("a")
                fight_input_attempts += 1
                next_fight_input_tick = self.session.current_tick() + 8
            self.session.step(2)
        if (
            self.counters["MoveSelectionMenu"][0] == 0
            or self.counters["MoveSelectionMenu.menuset"][0] == 0
            or not self.menu_fields_ready(min_item=1, max_item=4)
        ):
            raise RuntimeError(
                "move menu did not open after FIGHT: "
                f"counters={self.counters} menu={self.menu_snapshot()} "
                f"state={self.state_snapshot()} "
                f"cpu={self.cpu_snapshot()} backend={self.backend_snapshot()} "
                f"fight_input_attempts={fight_input_attempts}"
            )
        self.log(
            "battle move menu fields ready; entering sync "
            f"menu={self.menu_snapshot()} state={self.state_snapshot()}"
        )
        # The move menu is another ROM-owned input boundary. Match the
        # peers before reading its cursor or sending the legal move.
        _hold_at_sync_boundary(
            self.link._network_backend,
            ready_sync_id=13,
            release_sync_id=17,
            timeout=120.0,
            service_pending_edges=lambda: self.link._network_backend.service_pending_edges(
                max_edges=1
            ),
            progress_callback=lambda: self.session.step(1),
        )
        self.session.step(4)
        selected_move_id = self.choose_first_usable_battle_move()
        # Give the ROM a bounded opportunity to consume the ordinary A
        # input and leave MoveSelectionMenu. Without this handoff one
        # subprocess can log the selection before its input is actually
        # processed, while the other begins LinkBattleExchangeData.
        self.log(
            "battle move selected through ROM menu; native exchange begins "
            f"move_id={selected_move_id} state={self.state_snapshot()} "
            f"cpu={self.cpu_snapshot()}"
        )

        last_battle_log = time.monotonic()
        while time.monotonic() < self.deadline:
            if self.counters["EndOfBattle"][0] > 0:
                break
            battle_turn_complete = (
                self.battle_observer is not None
                and self.battle_observer.snapshot()["settled"] is True
            )
            if battle_turn_complete and not self.battle_turn_announced:
                self.link._network_backend.announce_sync(sync_id=14)
                self.battle_turn_announced = True
                self.shot("05_battle_turn")
                self.log(
                    "announced settled battle turn evidence "
                    f"lbe={self.counters['LinkBattleExchangeData'][0]} "
                    f"execute_player={self.counters['ExecutePlayerMove'][0]} "
                    f"execute_enemy={self.counters['ExecuteEnemyMove'][0]} "
                    f"backend={self.backend_snapshot()}"
                )
            if self.battle_turn_announced and self.link._network_backend.poll_peer_sync(sync_id=14):
                self.peer_battle_turn_ready = True
                break
            self.session.step(20)
            if time.monotonic() - last_battle_log > 15.0:
                self.log(
                    "battle progress: "
                    f"counters={ {k: self.counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                    f"state={self.state_snapshot()} cpu={self.cpu_snapshot()} "
                    f"backend={self.backend_snapshot()}"
                )
                last_battle_log = time.monotonic()
        if self.battle_turn_announced:
            try:
                self.cooperative_sync(sync_id=15, timeout=120.0)
                self.log("sync: past battle turn barrier")
                self.shot("06_battle_synced")
            except Exception as exc:  # noqa: BLE001
                self.drive_status = "error"
                self.drive_error = f"{type(exc).__name__}: {exc}"
                self.log(f"battle turn sync raised {type(exc).__name__}: {exc}")
            post_deadline = min(self.deadline, time.monotonic() + 10.0)
            while time.monotonic() < post_deadline:
                self.session.press("a", duration=4)
                self.session.step(20)
            self.log("sync: post-battle shutdown drain")
            self.peer_shutdown_sync(ready_sync_id=21)
            self.log("sync: post-battle shutdown barrier complete")
        if not self.peer_battle_turn_ready:
            self.log(
                "peer battle turn completion not observed before deadline "
                f"lbe={self.counters['LinkBattleExchangeData'][0]} "
                f"execute_player={self.counters['ExecutePlayerMove'][0]} "
                f"execute_enemy={self.counters['ExecuteEnemyMove'][0]} "
                f"backend={self.backend_snapshot()}"
            )
