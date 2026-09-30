"""Trade-phase driver for the TCP trade peer.

The pre-split ``_run_peer`` trade branch (#127) becomes ``_trade``.
"""

from __future__ import annotations

import time

from tests._tcp_trade_peer_link_menu import _TRADE_DIAG_SYMBOLS
from tests._tcp_trade_peer_sync import _party_summary


class _PeerDriveTradeMixin:
    def _trade(self) -> None:
        # Phase barrier: both sides at LinkMenu before voting Trade
        # Center. Without this the vote-exchange nibble loop has
        # no way to guarantee overlap in Pokemon's polling windows.
        self.log("sync: link_menu barrier")
        self.cooperative_sync(sync_id=1, timeout=60.0)
        self.log("sync: past link_menu barrier")
        # The LinkMenu hook fires before the ROM has finished installing
        # its final menu fields. Settle those fields and rendezvous at
        # the actual Trade Center cursor before sending A; otherwise a
        # peer can consume the selection while the other is still in the
        # menu's setup loop.
        self.wait_for_menu_ready(
            label="trade LinkMenu",
            min_item=0,
            max_item=self.link_menu_max,
            expected_max=self.link_menu_max,
            required_keys=0x01,
            timeout=60.0,
        )
        self.session.step(60)
        self.move_menu_to_item(
            target=0,
            min_item=0,
            max_item=self.link_menu_max,
            label="trade LinkMenu",
            input_duration=12,
            settle_frames=40,
        )
        # Both peers have now reached the ROM-owned Trade cursor. First
        # rendezvous before the real A event so the two independent
        # processes enter the ROM's selection exchange from the same
        # input boundary. A one-frame cooperative barrier keeps each
        # owner thread live for any final serial edge without allowing
        # one side to consume the choice several host frames ahead.
        self.cooperative_sync(sync_id=19, timeout=120.0, step_frames=1)
        if self.args.reset_serial_transcript_before_link_menu:
            self.link._network_backend.enable_serial_transcript(
                max_entries=self.args.serial_transcript_entries
            )
            self.log(
                "serial transcript reset after sync 19 before LinkMenu A press: "
                f"{self.args.serial_transcript_entries} bounded local records"
            )
        self.session.press("a", duration=4)
        # A queued input is not evidence that Cable Club exchanged a
        # selection.  Advance only after both ROMs independently report
        # a post-call directional vote observation. The paired sync in
        # the wait below requires the complementary peer observation.
        next_trade_input_tick = self.session.current_tick() + 12

        def retry_trade_selection_input() -> None:
            nonlocal next_trade_input_tick
            if self.session.current_tick() < next_trade_input_tick:
                return
            if not self.menu_fields_ready(
                min_item=0,
                max_item=0,
                expected_max=self.link_menu_max,
                required_keys=0x01,
            ):
                return
            self.session.press("a", duration=4)
            next_trade_input_tick = self.session.current_tick() + 12

        self.wait_for_link_menu_selection_exchange(
            label="trade",
            input_retry=retry_trade_selection_input,
        )
        self.log("trade menu selection exchange verified on both peers")
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
        while time.monotonic() < self.deadline:
            if (
                self.session.read_game_state().overworld.map_id == TRADE_CENTER
                and not warp_announced
            ):
                self.link._network_backend.announce_sync(sync_id=2)
                warp_announced = True
                self.log("local trade-center warp announced")
                self.shot("02_trade_center")
            if warp_announced and self.link._network_backend.poll_peer_sync(sync_id=2):
                peer_warp_ready = True
                break
            if not warp_announced:
                self.session.press("a", duration=4)
            self.session.step(20)
        if not (warp_announced and peer_warp_ready):
            raise RuntimeError(
                "trade-center warp rendezvous did not converge: "
                f"local={self.state_snapshot()} backend={self.backend_snapshot()}"
            )
        self.log("trade center warp complete on both peers")

        # Both ROMs have now reached the map, but the peer can still have
        # one final serial edge in flight. Drain it while both owners
        # continue ticking, then use a symmetric release rendezvous. A
        # no-tick hold is unsafe here because an already-armed ROM may
        # still need its next native serial callback to finish the phase.
        self.link._network_backend.wait_for_wire_idle(
            timeout=120.0,
            progress_callback=lambda: self.session.step(1),
            stable_checks=4,
        )
        self.cooperative_sync(sync_id=18, timeout=120.0, step_frames=1)
        self.log("sync: trade-center cooperative barrier complete")
        self.shot("03_post_warp_sync")
        # Cross-family negotiation deliberately leaves Yellow's
        # input-sensitive path on native edge pacing. The reproducible
        # failure topology is the non-Yellow listener with Yellow as
        # connector: use a short frame-paced walk rendezvous there, then
        # return to native edges before the serial-heavy routine. The
        # reverse ordered topology stays on its established native path;
        # enabling a leader barrier there can strand Yellow waiting for a
        # FRAME_TICK from the connector.
        cross_family_walk_barrier = (
            self.args.role == "listen"
            and self.args.version != "yellow"
            and self.peer_rom_version == "yellow"
        ) or (
            self.args.role == "connect"
            and self.args.version == "yellow"
            and self.peer_rom_version != "yellow"
        )
        if cross_family_walk_barrier:
            self.link.set_network_frame_barrier(True)
            self.log("enabled cross-family frame barrier at Trade Center boundary")
        elif self.args.version == "yellow" and self.peer_rom_version == "yellow":
            # Preserve the established Yellow↔Yellow pacing boundary;
            # unlike the cross-family path, both cartridges share the
            # same input/serial polling cadence and use the barrier for
            # the full trade-center exchange.
            self.link.set_network_frame_barrier(True)
            self.log("enabled Yellow frame barrier at Trade Center boundary")

        # Walk onto hidden-event trigger tile.
        self.conn_status = self.session._pyboy.memory[
            self.session.symbols.addr_of("hSerialConnectionStatus")
        ]
        self.walk_dir = "right" if self.conn_status == 0x02 else "left"
        for _ in range(6):
            if (
                self.counters["CableClubLeftGameboy"][0] + self.counters["CableClubRightGameboy"][0]
                > 0
            ):
                break
            self.session.press(self.walk_dir, duration=8)
            self.session.step(30)

        if cross_family_walk_barrier:
            # The walk is still a ROM-owned overworld phase, so rendezvous
            # before the first A press while frame pacing is active. This
            # keeps both cartridges on the same side of the hidden-event
            # transition without holding either emulator during serial
            # work. Complete a second transport-only handshake before
            # changing the pacing mode, so no owner frame is in flight
            # on either side when native edge pacing resumes.
            self.passive_sync(ready_sync_id=20, release_sync_id=21, timeout=120.0)
            self.passive_sync(ready_sync_id=22, release_sync_id=23, timeout=120.0)
            self.link.set_network_frame_barrier(False)
            self.log("sync: hidden-event walk boundary complete")
            self.log("disabled cross-family frame barrier before native data exchange")

        # A-mash to dismiss "JUST A MOMENT!" and start
        # CableClub_DoBattleOrTrade. Keep both CPUs actively ticking
        # through the native edge-level exchange once the public input
        # transition begins.
        while time.monotonic() < self.deadline:
            if self.counters["CableClub_DoBattleOrTrade"][0] > 0:
                break
            self.session.press("a", duration=4)
            self.session.step(20)
        self.log(
            "CableClub_DoBattleOrTrade fired; big exchange running "
            f"{self.state_snapshot()} "
            f"backend={self.backend_snapshot()}"
        )

        # Wait for the big exchange to complete — detect via
        # TradeCenter_SelectMon firing (runs after the exchange
        # + warp-to-trade-flow).
        last_exchange_log = time.monotonic()
        peer_ready_for_select_mon = False
        while time.monotonic() < self.deadline:
            if self.counters["TradeCenter_SelectMon"][0] > 0 and not self.select_mon_announced:
                self.link._network_backend.announce_sync(sync_id=3)
                self.select_mon_announced = True
                self.log(
                    "announced select_mon ready "
                    f"{self.state_snapshot()} "
                    f"CallCurrentTradeCenterFunction={self.counters['CallCurrentTradeCenterFunction'][0]} "
                    f"TradeCenter_SelectMon={self.counters['TradeCenter_SelectMon'][0]} "
                    f"backend={self.backend_snapshot()}"
                )
                self.shot("04_select_mon_ready")
            if self.select_mon_announced and self.link._network_backend.poll_peer_sync(sync_id=3):
                peer_ready_for_select_mon = True
                self.log(
                    f"peer announced select_mon ready {self.state_snapshot()} "
                    f"backend={self.backend_snapshot()}"
                )
                break
            self.session.step(20)
            if time.monotonic() - last_exchange_log > 15.0:
                self.log(
                    "waiting for select_mon convergence "
                    f"{self.state_snapshot()} "
                    f"CableClub_DoBattleOrTrade={self.counters['CableClub_DoBattleOrTrade'][0]} "
                    f"CallCurrentTradeCenterFunction={self.counters['CallCurrentTradeCenterFunction'][0]} "
                    f"TradeCenter_SelectMon={self.counters['TradeCenter_SelectMon'][0]} "
                    f"backend={self.backend_snapshot()}"
                )
                last_exchange_log = time.monotonic()
        if self.select_mon_announced:
            if not peer_ready_for_select_mon:
                self.log(
                    "peer never announced select_mon before deadline "
                    f"{self.state_snapshot()} "
                    f"backend={self.backend_snapshot()}"
                )
            else:
                # One side can reach TradeCenter_SelectMon before the peer,
                # but blocking immediately on a barrier can starve the
                # slower side of the CPU progress it still needs to finish
                # the same exchange. Keep stepping until the peer's own
                # announcement arrives, then use a real barrier so both
                # sides start menu navigation from a matched boundary.
                settle_deadline = min(self.deadline, time.monotonic() + 10.0)
                while time.monotonic() < settle_deadline:
                    self.session.step(20)
                self.log(
                    "TradeCenter_SelectMon converged on both peers; "
                    f"big exchange done {self.state_snapshot()} "
                    f"backend={self.backend_snapshot()}"
                )
                self.shot("05_select_mon_converged")
        else:
            self.log(
                "TradeCenter_SelectMon not reached before deadline "
                f"{self.state_snapshot()} "
                f"backend={self.backend_snapshot()}"
            )
        if self.select_mon_announced and peer_ready_for_select_mon:
            # Barrier here: both sides are post-exchange, about to
            # drive the menu. Safe to sync (game is in UI-setup phase,
            # no active serial traffic).
            self.log("sync: select_mon barrier")
            self.cooperative_sync(sync_id=3, timeout=60.0)
            self.log("sync: past select_mon barrier")
            self.shot("06_select_mon_sync")
        else:
            self.log("skipping select_mon barrier; peers never converged")

        # State-aware menu navigation.
        self.log(
            f"entering menu nav; counters={ {k: self.counters[k][0] for k in _TRADE_DIAG_SYMBOLS} }"
        )
        last_log = time.monotonic()
        stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
        trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
        menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
        tct_key = "TradeCenter_Trade"
        prev = {k: self.counters[k][0] for k in (stats_key, trade_key, menu_key, tct_key)}
        right_pending = 0
        while (
            time.monotonic() < self.deadline and self.counters["_AddEnemyMonToPlayerParty"][0] == 0
        ):
            self.session.step(40)
            if time.monotonic() - last_log > 15.0:
                snap = {k: self.counters[k][0] for k in _TRADE_DIAG_SYMBOLS}
                self.log(f"menu nav progress: {snap}")
                last_log = time.monotonic()
            now = {k: self.counters[k][0] for k in (stats_key, trade_key, menu_key, tct_key)}
            if now[trade_key] > prev[trade_key]:
                self.session.press("a", duration=4)
                right_pending = 0
            elif now[stats_key] > prev[stats_key]:
                right_pending = 5
                self.session.press("right", duration=12)
            elif right_pending > 0:
                self.session.press("right", duration=12)
                right_pending -= 1
            elif now[menu_key] > prev[menu_key] or now[tct_key] > prev[tct_key]:
                self.session.press("a", duration=4)
            else:
                self.session.press("a", duration=4)
            prev = now

        # Post-trade sync. Whichever side's
        # _AddEnemyMonToPlayerParty fired first has finished the
        # trade locally but the peer may still be mid-exchange
        # waiting for a few final bytes. Exiting the drive loop
        # immediately would tear down our SerialCore and leave
        # the peer's on_edge calls timing out. The shutdown
        # handshake announces this milestone while both owners
        # continue servicing authentic serial work, then closes
        # only after both peers acknowledge a quiet transport.
        if self.counters["_AddEnemyMonToPlayerParty"][0] > 0:
            # The hook is at function entry. Advance through the
            # copy routine before the rendezvous so the result records
            # the exchanged party record, not the later room-cleanup
            # state after the trade animation.
            self.session.step(120)
            self.party_after_trade = _party_summary(self.session)
            self.log("sync: post-trade barrier")
            try:
                self.cooperative_sync(sync_id=4, timeout=120.0)
                self.log("sync: past post-trade barrier")
                self.shot("07_post_trade")
            except Exception as exc:  # noqa: BLE001
                self.drive_status = "error"
                self.drive_error = f"{type(exc).__name__}: {exc}"
                self.log(f"post-trade sync raised {type(exc).__name__}: {exc}")
            self.log("sync: post-trade shutdown drain")
            self.peer_shutdown_sync(ready_sync_id=5)
            self.log("sync: post-trade shutdown barrier complete")
