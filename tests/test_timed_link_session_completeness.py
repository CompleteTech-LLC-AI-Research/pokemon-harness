"""Emission-completeness gating for the timed link session."""

import time
from collections import deque

import pytest

from pokered_harness.link.timed_wire import (
    Cancelled,
    ChannelClosed,
    DeadlineExceeded,
    EdgeRequest,
    EdgeResponse,
    EmissionComplete,
    Frame,
    Progress,
    encode_frame,
)
from tests._timed_link_session_support import (
    NoFurtherEdgesChannel,
    ScriptedChannel,
    _source_module,
    attached,
    real_v3_channels,
)


@pytest.mark.parametrize("revision", [2, 3])
def test_timed_attach_always_enforces_completeness(session_type, game, revision):
    with attached(session_type, game, ScriptedChannel(revision=revision)) as (session, _):
        assert session._coordinator._enforce_completeness is True
        assert session._coordinator_watermark == -1
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0


def test_source_completeness_progress_due_forces_prefix_below_threshold(
    session_type, source_lifecycle_game
):
    """Synthetic publication bookkeeping at a genuinely retired CPU frontier.

    Only cached send positions are arranged; CPU/serial clocks and coordinator
    time stay untouched. The known no-edge script makes both prefixes valid.
    Actual paired edge/coalescing coverage remains in the real-wire test.
    """
    game, _ = source_lifecycle_game
    board = game.mb
    board.lcd.tick = lambda clock: setattr(board.lcd, "frame_done", clock >= 312) or 0
    channel = NoFurtherEdgesChannel(revision=3)
    # Synthetic peer explicitly reports this settled interval; not a runtime
    # liveness assertion or a future completeness watermark.
    channel.messages.append(Progress(476))
    with attached(session_type, game, channel, quantum_cycles=256) as (session, _):
        session.tick(1, render=False, sound=False)
        assert board.cpu.cycles == 312 and board.cpu.retired_instructions == 26
        assert board.serial.last_cycles == 312
        before = session.snapshot()
        assert before.local_half_cycles == 624
        session._sent_progress = 480
        session._sent_watermark = 512
        assert 624 - 480 >= session._threshold == 128
        assert 0 < 624 - 512 < session._threshold
        sent_before = len(channel.sent)
        calls_before = len(channel.send_calls)
        session._publish()
        assert channel.sent[sent_before:] == [EmissionComplete(624, 0), Progress(624)]
        calls = channel.send_calls[calls_before:]
        assert len(calls) == 1
        assert calls[0][:2] == ("complete_progress", (EmissionComplete(624, 0), Progress(624)))
        assert calls[0][3] is session._cancel_view
        assert session.snapshot() == before
        assert board.cpu.cycles == 312 and board.cpu.retired_instructions == 26


def test_control_completeness_bootstrap_edge_zero_waits_for_prefix(session_type, game):
    """Synthetic edge-zero control script; actual Serial, no CPU clock writes."""
    channel = ScriptedChannel([EdgeRequest(1, 0, 0, 1)], revision=3)
    with attached(session_type, game, channel, max_edge_lateness=32) as (session, _):
        core = game.mb.serial
        core.set_SB(0)
        core.set_SC(0x80)
        before = (core.SB, core.SC, core._bits_remaining)
        session.service_controls(deadline=time.monotonic() + 1)
        assert session._coordinator_watermark == -1
        assert (core.SB, core.SC, core._bits_remaining) == before
        assert not any(isinstance(m, EdgeResponse) for m in channel.sent)
        channel.messages.append(EmissionComplete(0, 1))
        session.service_controls(deadline=time.monotonic() + 1)
        assert [m for m in channel.sent if isinstance(m, EdgeResponse)] == [EdgeResponse(1, 0, 0)]
        assert core._bits_remaining == 7
        assert session.controls_snapshot()["completed_edge_prefix"] == 1
        assert not game.memory[0xFF0F] & 8
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0


def test_control_completeness_watermark_only_pump_wakes_without_receive(session_type, game):
    """Synthetic passive prefix advance must be noticed by the initial pump."""
    channel = ScriptedChannel([Progress(476)], revision=3)
    with attached(session_type, game, channel, max_edge_lateness=32) as (session, _):
        session.service_controls(deadline=time.monotonic() + 1)
        before = session.snapshot()
        assert before.peer_half_cycles == 476
        assert session._coordinator_watermark == -1  # Progress is not completeness.
        channel.messages.append(EmissionComplete(476, 0))

        def unexpected_receive(**kwargs):
            pytest.fail("watermark-only initial pump must return without blocking receive")

        channel.receive = unexpected_receive
        session._wait_for_progress(0.2)
        after = session.snapshot()
        assert after.peer_half_cycles == before.peer_half_cycles
        assert session._coordinator_watermark == 476
        assert after.local_half_cycles == before.local_half_cycles == 0
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0


def test_source_completeness_withheld_edge_then_prefix_keeps_cpu_bound(
    session_type, source_lifecycle_game
):
    """Synthetic P/W476 and edge512 script; real JR and Serial execution.

    This controls receipt ordering, not peer runtime progress. P476 alone
    cannot certify W476, and receiving an edge alone cannot raise W+L540.
    """
    game, _ = source_lifecycle_game
    board, core = game.mb, game.mb.serial
    board.lcd.tick = lambda clock: setattr(board.lcd, "frame_done", clock >= 264) or 0
    channel = ScriptedChannel([Progress(476), EmissionComplete(476, 0)], revision=3)
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        core.set_SB(0)
        core.set_SC(0x80)
        arrivals = deque([EdgeRequest(1, 512, 512, 1), EmissionComplete(512, 1)])
        waits = []

        def receive(*, deadline, cancel_event=None):
            channel._check(cancel_event)
            assert time.monotonic() < deadline
            assert board.cpu.cycles == 252
            assert board.cpu.retired_instructions == 21
            assert session.snapshot().local_half_cycles == 504
            assert session._coordinator_watermark == 476
            assert not session.snapshot().pending_permit
            assert core._bits_remaining == 8
            assert not any(isinstance(m, EdgeResponse) for m in channel.sent)
            assert arrivals, "CPU failed to resume after the complete prefix"
            message = arrivals.popleft()
            waits.append(message)
            return message

        channel.receive = receive
        session.tick(1, render=False, sound=False)
        assert waits == [EdgeRequest(1, 512, 512, 1), EmissionComplete(512, 1)]
        assert board.cpu.cycles == 264 and board.cpu.retired_instructions == 22
        assert session.snapshot().local_half_cycles == 528 <= 512 + 64
        assert session._coordinator_watermark == 512
        assert [m for m in channel.sent if isinstance(m, EdgeResponse)] == [EdgeResponse(1, 528, 0)]
        assert core._bits_remaining == 7
        assert not board.cpu.interrupts_flag_register & 8
        assert session.controls_snapshot()["completed_edge_prefix"] == 1


def test_source_completeness_stop_preserves_actual_speed_interval(
    session_type, source_lifecycle_game
):
    """Authored STOP, real CPU/Serial, synthetic frame boundary and EC0 peer."""
    game, _ = source_lifecycle_game
    board = game.mb
    board.cgb = True
    board.key1 = 1
    game._test_memory[0xC000:0xC004] = bytes([0x10, 0, 0x18, 0xFE])
    channel = ScriptedChannel([EmissionComplete(0, 0)], revision=3)
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        session.tick(1, render=False, sound=False)
        assert board.cpu.retired_instructions == 1
        assert board.cpu.cycles == 4
        assert board.double_speed and board.speed_transition_count == 1
        boundary = board.speed_transition_clock
        assert session.snapshot().local_half_cycles == boundary * 2 + (4 - boundary)
        assert session.snapshot().raw_cpu_clock == 4
        assert session.snapshot().local_half_cycles <= 64
        assert not session.snapshot().pending_permit
        assert not any(isinstance(m, EdgeRequest) for m in channel.sent)


def test_source_completeness_hdma_412_explicit_nonprogress(session_type, source_lifecycle_game):
    """Real source HDMA selection, CPU and Serial; synthetic inert LCD HBlank.

    L64 cannot fit the atomic 206 CPU cycles / 412 half-cycles. A partial
    reservation must never execute DMA, retire CPU work, or advance clocks.
    """
    game, _ = source_lifecycle_game
    board = game.mb
    board.cgb_mode = True
    board_module = _source_module("core/mb.py", "pyboy.core._timed_hdma_mb")
    board.hdma = board_module.HDMA()
    board.hdma.hdma1 = 0xC0
    board.hdma.set_hdma5(0x80, board)
    assert board.hdma.transfer_active and board.lcd._STAT._mode == 0
    channel = ScriptedChannel([Progress(476), EmissionComplete(0, 0)], revision=3)
    original_backend = board.serial.backend
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        reserve = session._coordinator.reserve
        permits = []

        def observe_reservation(required):
            assert required == 206
            permit = reserve(required)
            if permit is not None:
                permits.append((permit.cpu_cycles, permit.half_cycles))
            return permit

        session._coordinator.reserve = observe_reservation
        waits = []

        def cancel_unfittable_dma(*, deadline, cancel_event=None):
            assert time.monotonic() < deadline
            waits.append((board.cpu.cycles, board.cpu.retired_instructions))
            assert not session.snapshot().pending_permit
            session.cancel()
            channel._check(cancel_event)

        channel.receive = cancel_unfittable_dma
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert waits == [(0, 0)]
        assert permits and all(p == (32, 64) for p in permits)
        assert board.cpu.cycles == board.cpu.retired_instructions == 0
        assert session.snapshot().local_half_cycles == session.snapshot().raw_cpu_clock == 0
        assert board.hdma.transfer_active and board.hdma.curr_src == 0xC000
        assert board.hdma.curr_dst == 0x8000
        assert game.frame_count == 0
        assert not any(isinstance(m, Progress) and m.settled_half_cycles for m in channel.sent)
        assert channel.closed and session.snapshot().cancelled
        assert board.serial.backend is original_backend
        assert board.execution_before is board.execution_after is None


def test_control_completeness_cancel_wait_preserves_real_cpu_frontier(session_type, game):
    channel = ScriptedChannel(revision=3)
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        waits = []

        def cancel_wait(*, deadline, cancel_event=None):
            assert time.monotonic() < deadline
            waits.append((game.mb.cpu.cycles, game.mb.cpu.retired_instructions))
            assert session._coordinator_watermark == -1
            session.cancel()
            channel._check(cancel_event)

        channel.receive = cancel_wait
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert waits == [(12, 1)]
        assert game.mb.cpu.cycles == 12 and game.mb.cpu.retired_instructions == 1
        assert session.snapshot().local_half_cycles == 24
        assert session.snapshot().cancelled and channel.closed
        assert game.mb.execution_before is game.mb.execution_after is None


def test_real_v3_complete_progress_equal_watermark_accepts_current_prefix(
    session_type, source_lifecycle_game
):
    """Real wire acceptance; real source CPU, synthetic publication bookkeeping.

    Temporarily suppress credit at an already retired endpoint to send only
    EC through the real standalone path, then publish its matching credit.
    No clocks, CPU counters, wire sequence, or wire prefix state are replaced.
    """
    game, _ = source_lifecycle_game
    with (
        real_v3_channels() as (channel, peer),
        attached(session_type, game, channel) as (session, _),
    ):
        peer.send(Progress(0), deadline=time.monotonic() + 1)
        # Decode the bootstrap anchor before CPU execution; its safe zero EC
        # is explicit, rather than depending on reader-thread arrival timing.
        session.service_controls(deadline=time.monotonic() + 1)
        assert peer.receive(deadline=time.monotonic() + 1) == Progress(0)
        assert peer.receive(deadline=time.monotonic() + 1) == EmissionComplete(0, 0)
        publish = session._publish

        def complete_without_credit(*, force=False):
            local = session.snapshot().local_half_cycles
            if local == 0:
                return publish(force=force)
            assert local == 24 and game.mb.cpu.cycles == 12
            previous_progress = session._sent_progress
            session._sent_progress = local  # Explicit synthetic cache seam.
            try:
                return publish(force=force)
            finally:
                session._sent_progress = previous_progress

        session._publish = complete_without_credit
        session.tick(1, render=False, sound=False)
        session._publish = publish
        assert peer.receive(deadline=time.monotonic() + 1) == EmissionComplete(24, 0)
        assert (session._sent_watermark, session._sent_prefix, session._sent_progress) == (
            24,
            0,
            0,
        )
        before = session.snapshot()
        calls = []
        batch = channel.send_complete_progress

        def observe(complete, progress, *, deadline, cancel_event=None):
            calls.append((complete, progress, deadline, cancel_event))
            assert (session._sent_watermark, session._sent_prefix, session._sent_progress) == (
                24,
                0,
                0,
            )
            return batch(complete, progress, deadline=deadline, cancel_event=cancel_event)

        channel.send_complete_progress = observe
        deadline = time.monotonic() + 1
        session.service_controls(deadline=deadline)
        assert len(calls) == 1
        assert calls[0][:2] == (EmissionComplete(24, 0), Progress(24))
        assert calls[0][2] <= deadline and calls[0][3] is session._cancel_view
        # Both actual reader-side frames must pass the real prefix validator.
        assert peer.receive(deadline=deadline) == EmissionComplete(24, 0)
        assert peer.receive(deadline=deadline) == Progress(24)
        assert (session._sent_watermark, session._sent_prefix, session._sent_progress) == (
            24,
            0,
            24,
        )
        assert session.snapshot() == before
        assert game.mb.cpu.cycles == 12 and game.mb.cpu.retired_instructions == 1
        assert not channel.closed and not peer.closed


@pytest.mark.parametrize("failure_type", [Cancelled, DeadlineExceeded, ChannelClosed])
def test_control_complete_progress_failure_preserves_sent_frontiers(
    session_type, source_lifecycle_game, failure_type
):
    """Synthetic canonical batch failures after real CPU/peripheral settlement."""
    game, _ = source_lifecycle_game
    channel = ScriptedChannel(revision=3)
    backend = game.mb.serial.backend
    with attached(session_type, game, channel) as (session, _):
        sentinel = failure_type("synthetic complete-progress failure")
        attempts = []
        before = (session._sent_watermark, session._sent_prefix, session._sent_progress)

        def fail(complete, progress, *, deadline, cancel_event=None):
            assert time.monotonic() < deadline
            assert cancel_event is session._cancel_view
            assert game.mb.cpu.cycles == game.mb.serial.last_cycles == 12
            assert game.mb.cpu.retired_instructions == 1
            attempts.append((complete, progress))
            raise sentinel

        channel.send_complete_progress = fail
        with pytest.raises(failure_type) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is sentinel
        assert attempts == [(EmissionComplete(24, 0), Progress(24))]
        assert (session._sent_watermark, session._sent_prefix, session._sent_progress) == before
        assert not any(isinstance(m, Progress) and m.settled_half_cycles for m in channel.sent)
        assert game.mb.cpu.cycles == 12 and game.mb.cpu.retired_instructions == 1
        assert session.snapshot().closed and channel.closed
        assert game.mb.serial.backend is backend
        assert game.mb.execution_before is game.mb.execution_after is None


def test_real_v3_partial_complete_progress_write_preserves_sent_frontiers(
    session_type, source_lifecycle_game
):
    """Real EC bytes leave the socket, then a synthetic send fault cuts off P.

    The actual wire admission/write/termination path remains in use. This is
    controlled transport failure evidence, not network throughput acceptance.
    """
    game, _ = source_lifecycle_game
    backend = game.mb.serial.backend
    with (
        real_v3_channels() as (channel, peer),
        attached(session_type, game, channel) as (session, _),
    ):
        peer.send(Progress(0), deadline=time.monotonic() + 1)
        session.service_controls(deadline=time.monotonic() + 1)
        before = (session._sent_watermark, session._sent_prefix, session._sent_progress)
        batch = channel.send_complete_progress
        attempts = []
        written = bytearray()
        expected = []

        class PartialWriteSocket:
            """Delegate the real fd; inject failure only after actual EC bytes."""

            def __init__(self, sock, prefix):
                self.sock = sock
                self.prefix = prefix

            def __getattr__(self, name):
                return getattr(self.sock, name)

            def send(self, data):
                if len(written) == len(self.prefix):
                    raise BrokenPipeError("synthetic failure after complete EC bytes")
                remaining = len(self.prefix) - len(written)
                count = self.sock.send(data[:remaining])
                written.extend(data[:count])
                return count

        def interrupt_batch(complete, progress, *, deadline, cancel_event=None):
            assert game.mb.cpu.cycles == game.mb.serial.last_cycles == 12
            prefix = encode_frame(Frame(channel.epoch, channel._outgoing.sequence + 1, complete, 3))
            expected.append(prefix)
            attempts.append((complete, progress))
            channel._sock = PartialWriteSocket(channel._sock, prefix)
            return batch(complete, progress, deadline=deadline, cancel_event=cancel_event)

        channel.send_complete_progress = interrupt_batch
        with pytest.raises(
            ChannelClosed, match="synthetic failure after complete EC bytes"
        ) as caught:
            session.tick(1, render=False, sound=False)
        assert attempts == [(EmissionComplete(24, 0), Progress(24))]
        assert len(expected) == 1 and bytes(written) == expected[0]
        assert written, "the failure must follow a real partial batch write"
        assert (session._sent_watermark, session._sent_prefix, session._sent_progress) == before
        assert caught.value is channel.error
        assert game.mb.cpu.cycles == 12 and game.mb.cpu.retired_instructions == 1
        assert session.snapshot().local_half_cycles == 24
        assert session.snapshot().closed and channel.closed
        assert game.mb.serial.backend is backend
        assert game.mb.execution_before is game.mb.execution_after is None
        with pytest.raises(ChannelClosed):
            session.tick(0, render=False, sound=False)
        assert len(attempts) == 1
