"""Public tick and control-session acceptance for the timed link session."""

import socket
import threading
import time

import pytest
from pyboy.core.serial import SerialBackendError

from pokered_harness.link.timed_wire import (
    Cancelled,
    ChannelClosed,
    EdgeRequest,
    EdgeResponse,
    EmissionComplete,
    Progress,
    ProtocolError,
    TimedWireChannel,
)
from tests._timed_link_session_support import (
    NoFurtherEdgesChannel,
    ScriptedChannel,
    attached,
    authored_game,
)


def test_public_tick_executes_real_instructions_and_preserves_result(session_type, game):
    public_tick = game.tick
    calls = []

    def observe(*args, **kwargs):
        result = public_tick(*args, **kwargs)
        calls.append(result)
        return result

    game.tick = observe
    start = game.mb.cpu.cycles
    retired = game.mb.cpu.retired_instructions
    frame = game.frame_count
    with attached(session_type, game, NoFurtherEdgesChannel()) as (session, channel):
        result = session.tick(1, render=False, sound=False)
        assert calls and type(result) is type(calls[-1]) and result == calls[-1]
        assert game.frame_count == frame + 1
        count = game.mb.cpu.retired_instructions - retired
        assert count > 0
        assert game.mb.cpu.cycles - start == count * 12
        assert any(isinstance(message, Progress) for message in channel.sent)
        assert any(isinstance(message, EmissionComplete) for message in channel.sent)


def test_public_zero_count_does_not_execute(session_type, game):
    before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, game.frame_count)
    with attached(session_type, game) as (session, _):
        result = session.tick(0, render=False, sound=False)
        assert result == 0
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, game.frame_count) == before


def test_control_armed_attach_is_transactional(session_type, game):
    original = game.mb.serial.backend
    game.memory[0xFF02] = 0x80
    channel = ScriptedChannel()
    session = session_type(channel, rearm_budget=64, rearm_instruction_cap=16, max_edge_lateness=64)
    try:
        with pytest.raises(RuntimeError, match="idle"):
            session.attach(game, deadline=time.monotonic() + 1)
        assert game.mb.serial.backend is original
        assert game.mb.execution_before is None
        assert game.mb.execution_after is None
    finally:
        session.close()
        channel.close()


def test_control_close_restores_owned_backend_and_callbacks(session_type, game):
    serial = game.mb.serial
    before = (serial.backend, serial.owner_dispatch_callback, serial.owner_dispatch_enabled)
    with attached(session_type, game) as (session, _):
        session.close()
        assert serial.backend is before[0]
        assert serial.owner_dispatch_callback is before[1]
        assert serial.owner_dispatch_enabled == before[2]
        assert game.mb.execution_before is None
        assert game.mb.execution_after is None
        with pytest.raises(RuntimeError, match="epoch cannot be reused"):
            session.attach(game, deadline=time.monotonic() + 1)


def test_control_eighth_response_precedes_irq_and_latches_real_byte(session_type, game):
    # Transfer armed by real LDH instruction after the idle-only attachment.
    game.memory[0xC000:0xC008] = [0x3E, 0x80, 0xE0, 0x02, 0x00, 0x18, 0xFE, 0x00]
    channel = NoFurtherEdgesChannel()
    owner = threading.get_ident()
    observations = []
    queued = False

    def inject():
        nonlocal queued
        if not queued and game.mb.serial.transfer_enabled:
            queued = True
            channel.messages.extend(
                EdgeRequest(i, 0, 0, (0xA5 >> (8 - i)) & 1) for i in range(1, 9)
            )
            channel.messages.append(EmissionComplete(0, 8))

    def observe(message):
        if isinstance(message, EdgeResponse):
            observations.append(
                (
                    message.edge_id,
                    threading.get_ident(),
                    game.memory[0xFF0F] & 8,
                    game.mb.serial.SB,
                    bool(game.mb.serial.transfer_enabled),
                )
            )

    channel.on_poll = inject
    channel.on_send = observe
    with attached(session_type, game, channel) as (session, _):
        session.tick(1, render=False, sound=False)
        assert [row[0] for row in observations] == list(range(1, 9))
        assert all(row[1] == owner and row[2] == 0 for row in observations)
        assert observations[-1][3:] == (0xA5, False)
        assert game.memory[0xFF0F] & 8


def test_control_failure_after_successful_public_execution_cleans_up(session_type, game):
    original_tick = game.tick
    backend = game.mb.serial.backend
    error = RuntimeError("public post-execution sentinel")

    def fail_after(*args, **kwargs):
        original_tick(*args, **kwargs)
        raise error

    game.tick = fail_after
    with attached(session_type, game, NoFurtherEdgesChannel()) as (session, channel):
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is error
        assert game.mb.cpu.retired_instructions > 0
        assert channel.closed
        assert game.mb.execution_before is None
        assert game.mb.execution_after is None
        assert game.mb.serial.backend is backend


def test_cancel_does_not_mask_unrelated_native_backend_failure(session_type, game):
    external = threading.Event()
    channel = ScriptedChannel(revision=3)
    marker = RuntimeError("unrelated native backend fault")
    with attached(session_type, game, channel, cancel_event=external) as (session, _):
        core = game.mb.serial
        core.latch_backend_error(marker)
        external.set()
        with pytest.raises(SerialBackendError) as caught:
            core.check_error()

        normalized = session._normalize_failure(caught.value)
        assert normalized is caught.value
        assert normalized.__cause__ is marker
        assert core.backend_failed
        with pytest.raises(SerialBackendError) as retained:
            core.check_error()
        assert retained.value.__cause__ is marker


def test_cancelled_native_wrapper_preserves_protocol_error_and_cause(session_type, game):
    external = threading.Event()
    channel = ScriptedChannel(revision=3)
    original = Cancelled("owner cancellation")
    protocol = ProtocolError("malformed peer frame")
    wrapped_error = None
    with attached(session_type, game, channel, cancel_event=external) as (session, _):
        channel.error = protocol
        try:
            raise original
        except Cancelled as cause:
            try:
                raise SerialBackendError("serial backend failed") from cause
            except SerialBackendError as wrapped:
                wrapped_error = wrapped
                external.set()
                normalized = session._normalize_failure(wrapped)

        assert normalized is protocol
        assert wrapped_error is not None
        assert wrapped_error.__cause__ is original
        assert original.__cause__ is None


def test_cancelled_native_wrapper_normalizes_without_cause_cycle(session_type, game):
    external = threading.Event()
    channel = ScriptedChannel(revision=3)
    original = Cancelled("owner cancellation")
    wrapped_error = None
    with attached(session_type, game, channel, cancel_event=external) as (session, _):
        try:
            raise original
        except Cancelled as cause:
            try:
                raise SerialBackendError("serial backend failed") from cause
            except SerialBackendError as wrapped:
                wrapped_error = wrapped
                external.set()
                normalized = session._normalize_failure(wrapped)

        assert isinstance(normalized, Cancelled)
        assert normalized is not original
        assert wrapped_error is not None
        with pytest.raises(Cancelled) as raised:
            raise normalized from wrapped_error
        assert raised.value.__cause__ is wrapped_error
        assert wrapped_error.__cause__ is original
        assert original.__cause__ is None


def test_control_cancel_before_tick_prevents_cpu_execution(session_type, game):
    with attached(session_type, game) as (session, _):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        session.cancel()
        start = time.monotonic()
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert time.monotonic() - start < 1
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_real_channel_handshake_attach_and_cleanup(session_type, game):
    left, right = socket.socketpair()
    channel = TimedWireChannel(left, epoch=b"timed-test-epoch")
    peer = TimedWireChannel(right, epoch=channel.epoch)
    failures = []

    def handshake():
        try:
            peer.handshake(deadline=time.monotonic() + 1)
        except BaseException as exc:  # noqa: BLE001 - Report every worker failure to the main assertion.
            failures.append(exc)

    worker = threading.Thread(target=handshake, daemon=True)
    worker.start()
    try:
        with attached(session_type, game, channel):
            worker.join(1)
            assert not worker.is_alive()
            assert not failures
    finally:
        peer.close()
        channel.close()
        worker.join(1)


@pytest.mark.parametrize(
    "exchange,lateness",
    [(False, 4096), (True, 4096), (False, 32)],
    ids=["idle", "delayed-byte", "asymmetric-L64-half"],
)
def test_real_pair_repeated_public_frames_bounded_wire_volume(
    session_type, tmp_path, exchange, lateness
):
    """Real wire, public PyBoy ticks and CPU counts; no fabricated peer credit."""
    left, right = socket.socketpair()
    channels = [
        TimedWireChannel(left, epoch=b"timed-test-epoch"),
        TimedWireChannel(right, epoch=b"timed-test-epoch"),
    ]
    sessions = []
    failures = []
    results = [None, None]
    counts = [0, 0]
    edge_requests = [[], []]
    edge_responses = [[], []]
    request_permits = []
    progress = [[], []]
    messages = [[], []]
    publications = [[], []]
    send_calls = [[], []]
    native_progress = []
    start = threading.Barrier(2)
    for i, channel in enumerate(channels):

        def record(message, index=i):
            counts[index] += 1
            messages[index].append(message)
            if isinstance(message, Progress):
                progress[index].append(message.settled_half_cycles)
                if sessions[index]._in_edge:
                    assert message.settled_half_cycles == sessions[index]._settled
                    native_progress.append(message)
            if isinstance(message, EdgeRequest):
                edge_requests[index].append(message)
                request_permits.append(sessions[index].snapshot().pending_permit)
            if isinstance(message, EdgeResponse):
                edge_responses[index].append(message)
                assert sessions[index].snapshot().pending_delivery

        original = channel.send

        def observe(message, *, deadline, cancel_event=None, index=i, send=original, log=record):
            send_calls[index].append(("send", (message,)))
            log(message)
            return send(message, deadline=deadline, cancel_event=cancel_event)

        channel.send = observe
        original_batch = channel.send_complete_progress

        def observe_batch(
            complete,
            progress,
            *,
            deadline,
            cancel_event=None,
            index=i,
            send=original_batch,
            log=record,
        ):
            assert not sessions[index]._in_edge
            assert cancel_event is sessions[index]._cancel_view
            send_calls[index].append(("complete_progress", (complete, progress)))
            log(complete)
            log(progress)
            return send(complete, progress, deadline=deadline, cancel_event=cancel_event)

        channel.send_complete_progress = observe_batch
        sessions.append(
            session_type(
                channel,
                rearm_budget=4096,
                rearm_instruction_cap=1024,
                max_edge_lateness=lateness,
                quantum_cycles=256,
                operation_timeout=5,
                max_wait_attempts=16,
                inbound_capacity=64,
            )
        )
        publish = sessions[i]._publish

        def observe_publish(*, force=False, index=i, original=publish):
            begin = len(messages[index])
            call_begin = len(send_calls[index])
            result = original(force=force)
            publications[index].append((messages[index][begin:], send_calls[index][call_begin:]))
            return result

        sessions[i]._publish = observe_publish

    def owner(index):
        try:
            with authored_game(tmp_path, f"pair-{index}") as emulator:
                if lateness == 32 and index == 1:
                    # Real asymmetric 4-cycle NOP / 12-cycle JR retirement.
                    emulator.memory[0xC000:0xC003] = [0x00, 0x18, 0xFD]
                if exchange:
                    # Slave arms after the first master's 512-cycle edge.
                    # NOPs are real retired instructions, not injected clocks.
                    code = [0x00] * 180 if index == 1 else []
                    code += [
                        0x3E,
                        (0xA5, 0x3C)[index],
                        0xE0,
                        0x01,
                        0x3E,
                        (0x81, 0x80)[index],
                        0xE0,
                        0x02,
                    ]
                    if index == 0:
                        # The first internal edge occurs in the unfinished LDH
                        # read at +512 clocks, exercising a pending permit.
                        # SC arms at the LDH write's +4 phase, leaving eight
                        # cycles in that instruction before these 125 NOPs.
                        code += [0x00] * 125 + [0xF0, 0x01]
                    code += [0x18, 0xFE]
                    emulator.memory[0xC000 : 0xC000 + len(code)] = code
                session = sessions[index]
                session.attach(emulator, deadline=time.monotonic() + 10)
                assert session._coordinator._enforce_completeness is True
                if lateness == 32:
                    assert session._lateness == 64 and session._threshold == 128
                start.wait(10)
                raw = emulator.mb.cpu.cycles
                retired = emulator.mb.cpu.retired_instructions
                frame = emulator.frame_count
                try:
                    for _ in range(3):
                        assert session.tick(1, render=False, sound=False) == 1
                    results[index] = (
                        emulator.frame_count - frame,
                        emulator.mb.cpu.cycles - raw,
                        emulator.mb.cpu.retired_instructions - retired,
                        session.snapshot(),
                        emulator.mb.serial.SB,
                        emulator.memory[0xFF0F] & 8,
                        bool(emulator.mb.serial.transfer_enabled),
                    )
                finally:
                    # Keep both owners alive until both have finished frame 3.
                    start.wait(10)
                    session.close()
        except BaseException as exc:  # noqa: BLE001 - Report every worker failure to the main assertion.
            failures.append(exc)
            start.abort()
            for session in sessions:
                session.cancel()

    workers = [threading.Thread(target=owner, args=(i,), daemon=True) for i in range(2)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(30)
        assert not any(worker.is_alive() for worker in workers), "paired public ticks deadlocked"
        assert not failures, failures
        for index, result in enumerate(results):
            frames, cycles, retired, snapshot, sb, irq, armed = result
            assert frames == 3 and retired > 0
            if exchange:
                assert sb == (0x3C, 0xA5)[index]
                assert irq == 8 and not armed
            elif lateness != 32 or index == 0:
                assert cycles == retired * 12
            else:
                assert retired * 4 < cycles < retired * 12
            assert snapshot.local_half_cycles == cycles * 2
            # At least a fourfold reduction from two messages per instruction;
            # permits coalescing policy choices without accepting per-step spam.
            if lateness == 32:
                deltas = [b - a for a, b in zip(progress[index], progress[index][1:])]
                assert any(0 < delta < 128 for delta in deltas)
                assert snapshot.local_half_cycles <= sessions[index]._coordinator_watermark + 64
            else:
                assert 0 < counts[index] <= retired // 2 + 32
            # When a safe publication sends both independently coalesced
            # frontiers, its emission prefix must be sent first.
            both = []
            for batch, calls in publications[index]:
                if any(isinstance(m, EmissionComplete) for m in batch) and any(
                    isinstance(m, Progress) for m in batch
                ):
                    assert len(batch) == 2
                    assert isinstance(batch[0], EmissionComplete)
                    assert isinstance(batch[1], Progress)
                    assert batch[0].through_half_cycle == batch[1].settled_half_cycles
                    assert calls == [("complete_progress", tuple(batch))]
                    both.append(batch)
            assert both
            watermark = -1
            last_edge = 0
            # Attachment sends only the truthful zero CPU anchor; it must not
            # certify edge zero absent before the first safe owner pump.
            assert messages[index][0] == Progress(0)
            for position, message in enumerate(messages[index][1:], start=1):
                if isinstance(message, EdgeRequest):
                    assert message.scheduled_half_cycle > watermark
                    last_edge = message.edge_id
                    assert messages[index][position + 1] == EmissionComplete(
                        message.scheduled_half_cycle, message.edge_id
                    )
                elif isinstance(message, EmissionComplete):
                    assert message.last_edge_id == last_edge
                    watermark = message.through_half_cycle
                elif isinstance(message, Progress):
                    assert message.settled_half_cycles <= watermark
        if exchange:
            assert native_progress
            assert [m.edge_id for m in edge_requests[0]] == list(range(1, 9))
            assert not edge_requests[1]
            assert [m.edge_id for m in edge_responses[1]] == list(range(1, 9))
            assert not edge_responses[0]
            assert request_permits[0] is True
            assert (
                edge_responses[1][0].delivered_half_cycle > edge_requests[0][0].scheduled_half_cycle
            )
    finally:
        for session in sessions:
            session.cancel()
        for channel in channels:
            channel.close()
        for worker in workers:
            worker.join(5)


def test_control_send_failure_after_eighth_apply_is_terminal_no_retry(session_type, game):
    game.memory[0xC000:0xC007] = [0x3E, 0x80, 0xE0, 0x02, 0x00, 0x18, 0xFE]
    channel = ScriptedChannel()
    injected = False
    attempts = []
    sentinel = RuntimeError("eighth response failed")

    def inject():
        nonlocal injected
        if not injected and game.mb.serial.transfer_enabled:
            injected = True
            channel.messages.extend(EdgeRequest(i, 0, 0, 1) for i in range(1, 9))
            channel.messages.append(EmissionComplete(0, 8))

    def fail(message):
        if isinstance(message, EdgeResponse):
            attempts.append(message.edge_id)
            if message.edge_id == 8:
                assert game.mb.serial.SB == 0xFF
                assert not game.mb.serial.transfer_enabled
                assert not game.memory[0xFF0F] & 8
                raise sentinel

    channel.on_poll, channel.on_send = inject, fail
    with attached(session_type, game, channel) as (session, _):
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is sentinel
        assert attempts == list(range(1, 9))
        assert game.mb.serial.SB == 0xFF and not game.mb.serial.transfer_enabled
        assert not game.memory[0xFF0F] & 8
        assert session.snapshot().closed
        with pytest.raises(ChannelClosed):
            session.tick(1, render=False, sound=False)
        assert attempts == list(range(1, 9))


def test_control_held_edge_cancel_keeps_original_deadline(session_type, game):
    channel = ScriptedChannel([EdgeRequest(1, 0, 0, 1), EmissionComplete(0, 1)])
    with attached(session_type, game, channel, quantum_cycles=256) as (session, _):
        deadlines = []
        original = channel.poll

        def observe():
            if session._held_deadline is not None:
                deadlines.append(session._held_deadline)
                if len(deadlines) == 3:
                    session.cancel()
            return original()

        channel.poll = observe
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert len(deadlines) >= 3
        assert len(set(deadlines)) == 1
        assert not any(isinstance(message, EdgeResponse) for message in channel.sent)
        assert session.snapshot().cancelled
        assert game.mb.cpu.retired_instructions <= 1024
