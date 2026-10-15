"""Regression for LCD phase markers crossing the local serial scheduler."""

from types import SimpleNamespace

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_core import SerialCore


class PhaseEndpoint:
    """Small CGB-shaped endpoint with a deterministic LCD reset phase."""

    def __init__(self, name, *, reset_lcd_at=None, physical_step=4):
        self.name = name
        self.memory = {0xFF4D: 0}
        self.events = []
        self.frame_count = 0
        self.rearm_count = 0
        self.edge_attempts = 0
        self.edge_rearm_results = []
        self.serial_edge_results = []
        self.arm_external_on_next_instruction = False
        self.on_instruction = None
        self._instruction_count = 0
        self._after_lcd_marker = False
        self._reset_lcd_at = reset_lcd_at
        self._physical_step = physical_step
        self.physical = 0
        self._lcd_reset_pending = False
        self._next_lcd_boundary = 12
        self._lcd_clock = 0

        serial = SerialCore(True)
        self.mb = SimpleNamespace(
            serial=serial,
            cgb_mode=True,
            lcd=SimpleNamespace(frame_done=False, disable_renderer=True),
            sound=SimpleNamespace(
                disable_sampling=False, clear_buffer=lambda: None
            ),
            breakpoint_singlestep=0,
            breakpoint_singlestep_latch=0,
            tick=self.instruction,
            breakpoint_reinject=lambda: None,
            breakpoint_reached=lambda: (-1, -1, -1),
            get_physical_clock=lambda: (0, self.physical),
        )
        serial.clock = 0

    def set_lcdc(self, enabled):
        """Mirror the relevant PyBoy LCDC off/on reset contract."""
        if not enabled:
            self._lcd_clock = 0
            self._next_lcd_boundary = None
            return
        self._lcd_reset_pending = True
        self._next_lcd_boundary = None

    def instruction(self):
        self._instruction_count += 1
        self.serial_advance()

        if self.arm_external_on_next_instruction:
            self.mb.serial.set_SB(0xA5)
            self.mb.serial.set_SC(0x80)
            self.arm_external_on_next_instruction = False

        if self._reset_lcd_at == self._instruction_count:
            self.set_lcdc(False)
            self.set_lcdc(True)

        if self._lcd_reset_pending:
            self._lcd_reset_pending = False
            self._lcd_clock = 0
            self._next_lcd_boundary = self.physical + 12
            self.mb.lcd.frame_done = True
            self._after_lcd_marker = True
        elif self._next_lcd_boundary is not None and self.physical >= self._next_lcd_boundary:
            self._lcd_clock = 0
            self._next_lcd_boundary += 12
            self.mb.lcd.frame_done = True
            self._after_lcd_marker = True

        if self.on_instruction is not None:
            self.on_instruction()
        return True

    def serial_advance(self):
        self.mb.serial.clock += 4
        self.physical += self._physical_step
        self._lcd_clock += self._physical_step
        if self._after_lcd_marker:
            self.rearm_count += 1
            self._after_lcd_marker = False

    def _handle_events(self, events):
        del events

    def _post_handle_events(self):
        pass

    def _handle_hooks(self):
        pass

    def tick(self, frames, *args):
        assert frames == 0


def test_lcd_reset_marker_does_not_freeze_peer_rearm_or_extend_quantum():
    master = PhaseEndpoint("master")
    peer = PhaseEndpoint("peer", reset_lcd_at=1)
    link = PyBoyLinkSession.local()
    link.attach(master)
    link.attach(peer)
    # Keep the fake's physical horizon small while preserving the production
    # default (140,448 half-normal-speed units).
    link.PHYSICAL_QUANTUM = 24

    progress_peer = link._make_owned_peer_progressor(peer)
    master.mb.serial.set_SB(0x00)

    def master_edge_after_peer_reset():
        if master.physical >= 8 and not master.edge_rearm_results:
            master.edge_attempts += 1
            master.mb.serial.set_SC(0x81)
            peer.arm_external_on_next_instruction = True
            master.edge_rearm_results.append(progress_peer())
            # A repeated callback at the same owner frontier must not run the
            # peer into the future merely because the public quantum remains.
            master.edge_rearm_results.append(progress_peer())
            if master.edge_rearm_results[0]:
                master.serial_edge_results.append(
                    master.mb.serial.backend.on_edge(1, 1)
                )

    master.on_instruction = master_edge_after_peer_reset
    link.step()

    # The old LCD barrier sees peer.frame_done and returns pull-up.  The
    # common physical frontier consumes that marker first, so the bounded
    # rearm instruction runs and the edge is serviced causally.
    assert master.edge_attempts == 1
    assert master.edge_rearm_results == [True, False]
    assert master.serial_edge_results == [1]
    assert bool(peer.mb.serial.transfer_enabled) is True
    assert bool(peer.mb.serial.internal_clock) is False
    assert peer.mb.serial._bits_remaining == 7
    assert master.mb.serial.backend.edge_count == 1
    assert master.mb.serial.backend.peer_unarmed_edges == 0
    # The bounded callback performs one rearm; subsequent owner instructions
    # may legitimately cross another display marker within this quantum.
    assert peer.rearm_count >= 1
    assert peer.mb.lcd.frame_done is True

    # One public request is one scheduler quantum.  LCD markers are reported
    # separately; the reset side may cross more than one native marker while
    # catching the peer, but this never creates a second public frame.
    assert link._scheduler_quantum_count == 1
    assert link._last_lcd_markers[0] >= 1
    assert link._last_lcd_markers[1] >= 1
    assert master.frame_count == peer.frame_count == 1

    # No unbounded callback overrun: the nested rearm is one instruction and
    # the returned physical frontiers remain within one instruction step.
    assert abs(master.physical - peer.physical) <= 4
    assert max(master.physical, peer.physical) <= 24

    # Once the peer is genuinely unarmed at the callback boundary, the
    # coordinator returns the physical pull-up value and does not execute a
    # speculative future instruction to manufacture an answer.
    peer.mb.serial.set_SC(0x00)
    before_unarmed = peer._instruction_count
    link._step_active = True
    try:
        assert master.mb.serial.backend.on_edge(1, 1) == 1
    finally:
        link._step_active = False
    assert peer._instruction_count == before_unarmed
    assert master.mb.serial.backend.peer_unarmed_edges == 1
