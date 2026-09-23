"""Source public-tick lifecycle acceptance without a ROM."""

import threading
import time

import pytest

from pokered_harness.link.timed_wire import (
    ChannelClosed,
    EdgeRequest,
    EdgeResponse,
    EmissionComplete,
    Progress,
)
from tests._timed_link_session_support import (
    ScriptedChannel,
    attached,
)


def test_source_actual_public_lifecycle_without_rom(source_lifecycle_game):
    game, events = source_lifecycle_game
    result = game.tick(1, render=False, sound=False)
    assert result is True
    assert game.frame_count == 1
    # Ungoverned MB batches to the inert devices' 80-cycle boundary; JR
    # instructions are indivisible, so seven real retirements reach 84.
    assert game.mb.cpu.cycles == 84
    assert game.mb.cpu.retired_instructions == 7
    assert events[0:2] == [("events",), ("gameshark",)]
    assert events[-2:] == [("post-events",), ("post-tick",)]


def test_source_successful_after_then_device_error_owner_cleanup(
    session_type, source_lifecycle_game
):
    game, events = source_lifecycle_game
    sentinel = RuntimeError("device failed after actual instruction report")
    original_backend = game.mb.serial.backend
    with attached(session_type, game) as (session, channel):

        def fail(clock):
            snapshot = session.snapshot()
            assert snapshot.raw_cpu_clock == game.mb.cpu.cycles == 12
            assert game.mb.cpu.retired_instructions == 1
            assert not snapshot.pending_permit
            raise sentinel

        game.mb.timer.tick = fail
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is sentinel
        assert session.snapshot().closed
        assert session.snapshot().raw_cpu_clock == 12
        assert game.mb.cpu.retired_instructions == 1
        assert game.frame_count == 0
        assert ("post-tick",) not in events
        assert channel.closed
        assert game.mb.serial.backend is original_backend
        assert game.mb.execution_before is None and game.mb.execution_after is None
        assert not any(
            isinstance(m, Progress) and m.settled_half_cycles >= 24 for m in channel.sent
        )


def test_source_recursive_public_tick_rejected_without_extra_retirement(
    session_type, source_lifecycle_game
):
    game, _ = source_lifecycle_game
    with attached(session_type, game) as (session, _):
        attempts = []

        def recurse(clock):
            attempts.append(game.mb.cpu.retired_instructions)
            session.tick(1, render=False, sound=False)

        game.mb.timer.tick = recurse
        with pytest.raises(RuntimeError, match="recursive"):
            session.tick(1, render=False, sound=False)
        assert attempts == [1]
        assert game.mb.cpu.retired_instructions == 1
        assert session.snapshot().closed
        assert game.mb.execution_before is None and game.mb.execution_after is None


def test_direct_public_tick_fails_closed_before_cpu(session_type, game):
    with attached(session_type, game) as (session, channel):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        with pytest.raises(RuntimeError, match="unowned"):
            game.tick(1, render=False, sound=False)
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before
        assert session.snapshot().closed
        assert channel.closed
        assert game.mb.execution_before is not None
        with pytest.raises(ChannelClosed):
            session.tick(1, render=False, sound=False)
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_source_held_rearm_cpu_keeps_pending_delivery(session_type, source_lifecycle_game):
    game, _ = source_lifecycle_game
    channel = ScriptedChannel([EdgeRequest(1, 0, 0, 1), EmissionComplete(0, 1)])
    with attached(session_type, game, channel) as (session, _):
        reports = []
        original = game.mb.timer.tick

        def observe(clock):
            snapshot = session.snapshot()
            reports.append(
                (
                    game.mb.cpu.retired_instructions,
                    snapshot.pending_delivery,
                    snapshot.active_episode,
                )
            )
            return original(clock)

        game.mb.timer.tick = observe
        session.tick(1, render=False, sound=False)
        assert reports == [(1, True, True)]
        assert session.snapshot().pending_delivery
        assert not any(isinstance(m, EdgeResponse) for m in channel.sent)


@pytest.mark.parametrize("nops", [126, 127], ids=["inside-IO", "outer-dispatch"])
def test_source_stop_edge_mapping_and_no_premature_settlement(
    session_type, source_lifecycle_game, nops
):
    game, _ = source_lifecycle_game
    board, core = game.mb, game.mb.serial
    board.cgb = True
    board.key1 = 1
    code = [0x00] * nops + [0x10, 0x00, 0xE0, 0x01, 0x18, 0xFE]
    game._test_memory[0xC000 : 0xC000 + len(code)] = bytes(code)
    memory_write = board.setitem

    def write(address, value):
        if address in (0xFF01, 0xFF02):
            return board.setitem_io_ports(address, value)
        return memory_write(address, value)

    board.setitem = write
    board.lcd.tick = lambda clock: setattr(board.lcd, "frame_done", clock >= 528) or 0
    channel = ScriptedChannel()
    observations = []
    with attached(session_type, game, channel) as (session, _):
        core.set_SB(0x80)
        core.set_SC(0x81)
        assert core.clock_target - core.clock == 512  # Preserve hardware period.

        def reply(message):
            if isinstance(message, EdgeRequest):
                observations.append(
                    (message, session.snapshot().pending_permit, board.cpu.retired_instructions)
                )
                # All completed emission prefixes precede this just-emitted edge.
                assert all(
                    not isinstance(prior, EmissionComplete)
                    or prior.through_half_cycle < message.scheduled_half_cycle
                    for prior in channel.sent
                )
                channel.messages.append(
                    EdgeResponse(message.edge_id, message.scheduled_half_cycle, 1)
                )
            elif isinstance(message, Progress) and session._in_edge:
                assert message.settled_half_cycles < 512 + nops * 4

        channel.on_send = reply
        session.tick(1, render=False, sound=False)
        assert len(observations) == 1
        message, pending, retired = observations[0]
        assert message.scheduled_half_cycle == 512 + nops * 4
        assert message.observed_half_cycle == message.scheduled_half_cycle
        assert pending is (nops == 126)
        assert retired == nops + 1  # STOP retired; unfinished LDH must not count.
        assert core.clock_target == 1024  # 512-cycle cadence survives STOP.


def test_source_cross_thread_close_does_not_detach_live_owner(session_type, source_lifecycle_game):
    game, _ = source_lifecycle_game
    with attached(session_type, game) as (session, _):
        errors = []

        def foreign_close():
            try:
                session.close()
            except RuntimeError as exc:
                errors.append(exc)

        def close_during_device(clock):
            worker = threading.Thread(target=foreign_close, daemon=True)
            worker.start()
            worker.join(2)
            assert not worker.is_alive()
            assert game.mb.execution_before is not None
            assert game.mb.serial.backend is session
            return 0

        game.mb.timer.tick = close_during_device
        with pytest.raises(ChannelClosed):
            session.tick(1, render=False, sound=False)
        assert all("attaching thread" in str(error) for error in errors)
        assert game.mb.cpu.retired_instructions == 1
        assert game.mb.execution_before is None


def test_source_primary_failure_survives_foreign_registration_cleanup(
    session_type, source_lifecycle_game
):
    game, _ = source_lifecycle_game
    channel = ScriptedChannel()
    session = session_type(channel, rearm_budget=64, rearm_instruction_cap=16, max_edge_lateness=64)
    session.attach(game, deadline=time.monotonic() + 1)
    sentinel = RuntimeError("primary device failure")
    foreign = object()

    def fail(clock):
        game.mb.serial.backend = foreign
        raise sentinel

    game.mb.timer.tick = fail
    try:
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is sentinel
        assert game.mb.serial.backend is foreign
        assert game.mb.execution_before is not None
        assert session.snapshot().closed
    finally:
        # Test owns the injected foreign identity; restore only that injection
        # so owner cleanup can release its original registration afterwards.
        if game.mb.serial.backend is foreign:
            game.mb.serial.backend = session
        session.close()
        channel.close()
