"""Pre-gameplay rendezvous phase for the TCP trade peer.

The pre-split ``_run_peer`` rendezvous body (#127) becomes ``_rendezvous``.
"""

from __future__ import annotations

import time

from tests._tcp_trade_peer_link_menu import _is_cable_club_save_choice_ready
from tests._tcp_trade_peer_sync import _finish_link_menu_phase


class _PeerDriveRendezvousMixin:
    def _rendezvous(self) -> None:
        # Both independent processes must finish attach/HELLO/clock-role
        # selection before either owner begins driving the restored game
        # state. Keep this rendezvous passive: advancing one ROM while the
        # other is still constructing its PyBoy can produce a
        # direction-dependent first serial exchange.
        self.passive_sync(ready_sync_id=99, release_sync_id=98, timeout=60.0)
        # Phase 1: walk UP ×3 + A-mash to reach LinkMenu.
        for _ in range(3):
            self.session.press("up", duration=6)
            self.session.step(20)
        # The fixtures already face their Cable Club receptionist; these UP
        # inputs do not move either ROM. Both cartridges must enter their
        # ordinary Cable Club receptionist interaction before the native
        # $02/$01 role election can complete; the TCP endpoint role is never
        # a substitute for that cartridge-owned participation.
        self.log("phase 1 start")
        last_progress = time.monotonic()
        serial_phase_ticks = 0
        self.peer_link_menu_ready = False
        save_choice_announced = False
        peer_save_choice_ready = False
        save_choice_released = False
        save_choice_attempts = 0
        save_choice_max_attempts = 32
        save_choice_retry_interval_frames = 8
        next_save_choice_attempt_tick = -1
        close_count_at_save_choice_release = 0
        if self.native_internal_clock is None:
            raise RuntimeError("native network clock role was not negotiated")
        local_is_connection_starter = self.native_internal_clock
        connection_start_released = local_is_connection_starter
        local_role_announced = False
        while time.monotonic() < self.deadline:
            if not connection_start_released:
                # Keep participating in the negotiated frame cadence while
                # the starter reaches its ROM-owned SC_INTERNAL write.  The
                # peer may therefore acknowledge frame turns, but receives
                # no dialogue input that could make both cartridges start
                # their initial external-clock exchange together.
                if self.link._network_backend.poll_peer_sync(sync_id=119):
                    connection_start_released = True
                    self.log("phase 1 native connection-starter marker received")
                else:
                    self.session.step(1)
                    continue
            status = int(
                self.session._pyboy.memory[self.session.symbols.addr_of("hSerialConnectionStatus")]
            )
            if (
                local_is_connection_starter
                and not local_role_announced
                and self.counters["CableClubNPC"][0] > 0
                and status == 0x02
            ):
                self.link._network_backend.announce_sync(sync_id=100)
                local_role_announced = True
                self.log("phase 1 local native internal-clock role observed")
            # TCP listener/connector is not a Game Boy clock-role contract.
            # Cable Club may invert clock ownership while either ROM remains
            # in its polling loop, so a peer which has already entered
            # LinkMenu must continue authentic emulation until the other ROM
            # independently observes that same entry.  Do not wait for wire
            # quietness here: the next legitimate transfer can be the peer's
            # final pre-menu edge.
            if self.link_menu_announced:
                if not self.peer_link_menu_ready:
                    self.peer_link_menu_ready = self.link._network_backend.poll_peer_sync(
                        sync_id=121
                    )
                if self.peer_link_menu_ready:
                    if self.args.goal == "link_menu":
                        # Menu-idle exchanges are the ROM's $D0 keepalive,
                        # not a directional selection. Settle at the native
                        # cursor, synchronize both owners, then submit one
                        # ordinary A press. The post-call predicate below
                        # proves the resulting exchange rather than treating
                        # either the input event or menu entry as success.
                        self.wait_for_menu_ready(
                            label="link menu",
                            min_item=0,
                            max_item=self.link_menu_max,
                            expected_max=self.link_menu_max,
                            required_keys=0x01,
                            timeout=60.0,
                        )
                        self.session.step(60)
                        self.cooperative_sync(sync_id=118, timeout=120.0, step_frames=1)
                        self.session.press("a", duration=4)
                        self.wait_for_link_menu_selection_exchange(label="link menu")
                    _finish_link_menu_phase(
                        self.args.goal,
                        cooperative_sync=self.cooperative_sync,
                        peer_shutdown_sync=self.peer_shutdown_sync,
                    )
                    self.log("phase 1 done: LinkMenu fired on both peers")
                    break
                self.session.step(1)
                continue
            if (
                save_choice_released
                and self.counters["CloseLinkConnection"][0] > close_count_at_save_choice_release
            ):
                nonzero_counters = {
                    name: count[0] for name, count in self.counters.items() if count[0]
                }
                raise RuntimeError(
                    "Cable Club closed before LinkMenu after synchronized save choice: "
                    f"close_count_before={close_count_at_save_choice_release} "
                    f"close_count_after={self.counters['CloseLinkConnection'][0]} "
                    f"counters={nonzero_counters} "
                    f"menu={self.menu_snapshot()} state={self.state_snapshot()} "
                    f"cpu={self.cpu_snapshot()} backend={self.backend_snapshot()} "
                    f"pre_link_menu={self.pre_link_menu_history.snapshot() if self.pre_link_menu_history else {}}"
                )
            if not save_choice_released:
                # Do not let either process send the confirmation A while the
                # other is still consuming the receptionist text. Yellow's
                # post-save sync has a narrower overlap window than the Color
                # pair, so synchronize at the native Yes/No input boundary,
                # not after one ROM has already entered SaveGameData.
                if not save_choice_announced and _is_cable_club_save_choice_ready(
                    self.cable_club_confirmation, self.counters, self.menu_snapshot()
                ):
                    self.link._network_backend.announce_sync(sync_id=122)
                    save_choice_announced = True
                    self.log("phase 1 native Cable Club save choice ready")
                if save_choice_announced and not peer_save_choice_ready:
                    peer_save_choice_ready = self.link._network_backend.poll_peer_sync(sync_id=122)
                if save_choice_announced and peer_save_choice_ready:
                    # This is a control-plane rendezvous only. It advances a
                    # single real frame while waiting so a legitimate final
                    # cable edge can be serviced, but no menu/RAM state is
                    # written and neither TCP role is treated as clock owner.
                    self.cooperative_sync(sync_id=123, timeout=60.0, step_frames=1)
                    close_count_at_save_choice_release = self.counters["CloseLinkConnection"][0]
                    # Yellow's native Yes/No poll can begin several frames
                    # after the shared boundary. Keep the input pulse within
                    # the ordinary public button contract, but long enough
                    # for both Red/Blue and Yellow to observe the same A
                    # event without writing menu state.
                    self.session.press("a", duration=4)
                    self.session.step(2)
                    save_choice_attempts = 1
                    next_save_choice_attempt_tick = (
                        self.session.current_tick() + save_choice_retry_interval_frames
                    )
                    save_choice_released = True
                    self.log("phase 1 synchronized native Cable Club save choice released")
                    continue
                if save_choice_announced:
                    # The verified CableClubNPC YesNoChoice call is active.
                    # Do not keep pulsing A while the peer is still consuming
                    # its Cable Club dialogue: that would select/save on only
                    # one side and begin Serial_SyncAndExchangeNybble before
                    # the peer can exchange its matching $6? nibble. Keep
                    # servicing ordinary owner ticks without injecting input
                    # until the peer publishes the same ROM-owned boundary.
                    self.session.step(1)
                    continue
                # One-frame public input pulses advance the dialogue without
                # retaining A across the newly-created Yes/No menu. The next
                # iteration observes that menu before another input can be
                # queued.
                # Until this ROM's own handler hook fires it is still at the
                # overworld receptionist, regardless of the serial role it
                # observed from its peer. Both independent cartridges need
                # the established initial A×4/settle×8 cadence to enter that
                # native interaction; only after entry return to one-frame
                # dialogue pulses that cannot auto-confirm the save menu.
                entering_cable_club = self.counters["CableClubNPC"][0] == 0
                self.session.press("a", duration=4 if entering_cable_club else 1)
                self.session.step(8 if entering_cable_club else 2)
                continue
            if (
                save_choice_released
                and self.counters["SaveGameData"][0] == 0
                and save_choice_attempts < save_choice_max_attempts
                and _is_cable_club_save_choice_ready(
                    self.cable_club_confirmation, self.counters, self.menu_snapshot()
                )
                and self.session.current_tick() >= next_save_choice_attempt_tick
            ):
                # A cross-family ROM can consume the shared boundary frame
                # without sampling A in its first native poll. Retry only
                # while the verified CableClubNPC YesNoChoice call is still
                # active; once SaveGameData or the call return is observed,
                # no further input is injected into the serial phase.
                self.session.press("a", duration=1)
                save_choice_attempts += 1
                next_save_choice_attempt_tick = (
                    self.session.current_tick() + save_choice_retry_interval_frames
                )
                self.log(
                    "phase 1 retrying native Cable Club save choice; "
                    f"attempt={save_choice_attempts} confirmation={self.cable_club_confirmation}"
                )
            in_serial_phase = (
                self.counters["SaveGameData"][0] > 0
                or self.counters["Serial_SyncAndExchangeNybble"][0] > 0
            )
            if in_serial_phase:
                # Do not block on a phase barrier while either emulator is
                # inside a serial transfer. The slave's main thread must be
                # allowed to run its serial IRQ handler and re-arm SC while
                # the peer's NetworkBackend is waiting for this edge. The
                # network edge/response protocol already provides the
                # per-byte synchronization; explicit barriers are reserved
                # for safe UI/phase boundaries below.
                self.session.step(2)
                serial_phase_ticks += 1
                if (
                    self.counters["LinkMenu.waitForInputLoop"][0] > 0
                    and self.menu_fields_ready(
                        min_item=0,
                        max_item=self.link_menu_max,
                        expected_max=self.link_menu_max,
                        required_keys=0x01,
                    )
                    and not self.link_menu_announced
                ):
                    self.link_menu_announced = True
                    self.log("phase 1 local LinkMenu fired")
                    self.shot("01_link_menu")
                    self.link._network_backend.announce_sync(sync_id=121)
                    self.log("phase 1 LinkMenu readiness sent")
            else:
                if (
                    self.counters["LinkMenu.waitForInputLoop"][0] > 0
                    and self.menu_fields_ready(
                        min_item=0,
                        max_item=self.link_menu_max,
                        expected_max=self.link_menu_max,
                        required_keys=0x01,
                    )
                    and not self.link_menu_announced
                ):
                    self.link_menu_announced = True
                    self.log("phase 1 local LinkMenu fired")
                    self.shot("01_link_menu")
                    self.link._network_backend.announce_sync(sync_id=121)
                    self.log("phase 1 LinkMenu readiness sent")
                # The save confirmation was issued exactly once above. From
                # here the cartridge owns the save/serial phase; keep it
                # moving in short slices and never inject a second A.
                self.session.step(2)
            if time.monotonic() - last_progress > 10.0:
                self.log(
                    f"phase 1 progress: "
                    f"CableClubNPC={self.counters['CableClubNPC'][0]} "
                    f"SaveGameData={self.counters['SaveGameData'][0]} "
                    f"Serial_SyncAndExchangeNybble={self.counters['Serial_SyncAndExchangeNybble'][0]} "
                    f"LinkMenu={self.counters['LinkMenu'][0]} "
                    f"waitForInputLoop={self.counters['LinkMenu.waitForInputLoop'][0]} "
                    f"menu={self.menu_snapshot()}"
                )
                last_progress = time.monotonic()

        if not self.link_menu_announced or not self.peer_link_menu_ready:
            raise RuntimeError(
                "LinkMenu rendezvous did not converge before the gameplay phase: "
                f"local_announced={self.link_menu_announced} "
                f"peer_ready={self.peer_link_menu_ready} "
                f"menu={self.menu_snapshot()} state={self.state_snapshot()} "
                f"backend={self.backend_snapshot()}"
            )
