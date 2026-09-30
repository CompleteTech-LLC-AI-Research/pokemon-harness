"""V3 control-session acceptance, including real framed transport."""

import threading
import time

import pytest

from pokered_harness.link.timed_wire import (
    Cancelled,
    DeadlineExceeded,
    EdgeRequest,
    EdgeResponse,
    EmissionComplete,
    Fence,
    FenceAck,
    Progress,
    Sync,
)
from tests._timed_link_session_support import (
    NoFurtherEdgesChannel,
    ScriptedChannel,
    control_session,
    real_v3_channels,
)


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("old_sb", [0x00, 0xA5, 0xFF])
def test_v3_explicit_startup_accepts_generic_fully_armed_external(
    session_type, game, internal, old_sb
):
    core = game.mb.serial
    core.set_SB(old_sb)
    core.set_SC(0x80)
    before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
    with control_session(session_type, game, startup=internal) as (session, _):
        expected_sb = 1 if internal else 2
        assert expected_sb == core.SB
        assert core.transfer_enabled and bool(core.internal_clock) is internal
        assert core._bits_remaining == 8
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before
        writes = session.controls_snapshot()["bootstrap_writes"]
        assert writes == 3
        session.service_controls(deadline=time.monotonic() + 1)
        assert session.controls_snapshot()["bootstrap_writes"] == writes
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


@pytest.mark.parametrize("startup", [0, 1, "internal"])
def test_v3_startup_flag_rejects_non_boolean(session_type, game, startup):
    original = game.mb.serial.backend
    with pytest.raises(TypeError), control_session(session_type, game, startup=startup):
        pytest.fail("non-bool startup admitted")
    assert game.mb.serial.backend is original
    assert game.mb.execution_before is None
    assert game.mb.cpu.retired_instructions == 0


@pytest.mark.parametrize("armed_kind", ["partial-external", "active-internal"])
def test_v3_startup_rejects_inflight_transfer_transactionally(session_type, game, armed_kind):
    core = game.mb.serial
    core.set_SB(0xA5)
    core.set_SC(0x81 if armed_kind == "active-internal" else 0x80)
    if armed_kind == "partial-external":
        core.apply_external_edge(1)
    before = (core.SB, core.SC, core._bits_remaining, core.backend)
    with pytest.raises(RuntimeError), control_session(session_type, game, startup=True):
        pytest.fail("inflight startup admitted")
    assert (core.SB, core.SC, core._bits_remaining, core.backend) == before
    assert game.mb.cpu.retired_instructions == 0


def test_v3_sync_passive_consuming_and_never_bootstraps(session_type, game):
    channel = ScriptedChannel([Sync(7), Sync(9)], revision=3)
    core = game.mb.serial
    before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, core.SB, core.SC)
    with control_session(session_type, game, channel) as (session, _):
        deadline = time.monotonic() + 1
        session.service_controls(deadline=deadline)
        assert session.poll_peer_sync(9) is True
        assert session.poll_peer_sync(9) is False
        assert session.poll_peer_sync(7) is True
        session.announce_sync(3, deadline=deadline)
        assert Sync(3) in channel.sent
        assert session.controls_snapshot()["bootstrap_writes"] == 0
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, core.SB, core.SC) == before


def test_v3_fence_ack_waits_for_contiguous_applied_prefix(session_type, game):
    # IDs are receive order, not timestamp order: completing edge 2 alone
    # must not acknowledge a fence through 2 while edge 1 is still future.
    channel = NoFurtherEdgesChannel(
        [
            EdgeRequest(1, 100, 100, 1),
            EdgeRequest(2, 0, 0, 0),
            EmissionComplete(100, 2),
            Fence(1, 2),
        ],
        revision=3,
    )
    with control_session(session_type, game, channel) as (session, _):
        game.mb.serial.set_SB(0)
        game.mb.serial.set_SC(0x80)
        session.service_controls(deadline=time.monotonic() + 1)
        assert [m.edge_id for m in channel.sent if isinstance(m, EdgeResponse)] == [2]
        assert session.controls_snapshot()["completed_edge_prefix"] == 0
        assert not any(isinstance(m, FenceAck) for m in channel.sent)
        session.tick(1, render=False, sound=False)
        assert [m.edge_id for m in channel.sent if isinstance(m, EdgeResponse)] == [2, 1]
        assert [m for m in channel.sent if isinstance(m, FenceAck)] == [FenceAck(1, 2)]
        assert session.controls_snapshot()["completed_edge_prefix"] == 2


def test_v3_fence_ack_after_eighth_response_and_irq(session_type, game):
    channel = ScriptedChannel(
        [*(EdgeRequest(i, 0, 0, 1) for i in range(1, 9)), EmissionComplete(0, 8), Fence(1, 8)],
        revision=3,
    )
    with control_session(session_type, game, channel) as (session, _):
        game.mb.serial.set_SB(0)
        game.mb.serial.set_SC(0x80)
        observations = []

        def observe(message):
            if isinstance(message, (EdgeResponse, FenceAck)):
                observations.append((message, game.memory[0xFF0F] & 8))
            if isinstance(message, FenceAck):
                assert EdgeResponse(8, 0, 0) in channel.sent
                assert game.memory[0xFF0F] & 8
                assert not session.snapshot().pending_delivery

        channel.on_send = observe
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        session.service_controls(deadline=time.monotonic() + 1)
        assert len(observations) == 9
        assert observations[-2][0].edge_id == 8 and observations[-2][1] == 0
        assert observations[-1] == (FenceAck(1, 8), 8)
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_v3_on_edge_stages_controls_until_safe_owner_boundary(session_type, game):
    # Initial serial arm is explicit local test setup; remote Sync never arms.
    code = [0x00] * 127 + [0xF0, 0x01, 0x18, 0xFE]
    game.memory[0xC000 : 0xC000 + len(code)] = code
    channel = NoFurtherEdgesChannel(revision=3)
    with control_session(session_type, game, channel) as (session, _):
        core = game.mb.serial
        core.set_SB(0xA5)
        core.set_SC(0x81)
        in_edge = []

        def reply(message):
            if isinstance(message, EdgeRequest):
                in_edge.append(session.snapshot().pending_permit)
                if message.edge_id == 1:
                    channel.messages.extend([Sync(5), Fence(1, 0)])
                channel.messages.append(
                    EdgeResponse(message.edge_id, message.scheduled_half_cycle, 1)
                )
            elif isinstance(message, FenceAck):
                assert not session._in_edge
                assert not session.snapshot().pending_permit

        channel.on_send = reply
        session.tick(1, render=False, sound=False)
        assert in_edge[0] is True
        assert session._applied == 0
        assert session.poll_peer_sync(5) is True
        assert [m for m in channel.sent if isinstance(m, FenceAck)] == [FenceAck(1, 0)]


def test_v3_start_fence_forces_emission_and_ack_poll_consumes(session_type, game):
    with control_session(session_type, game) as (session, channel):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        deadline = time.monotonic() + 1
        sent_before = len(channel.send_deadlines)
        fence_id = session.start_fence(deadline=deadline)
        fence = Fence(fence_id, 0)
        assert fence in channel.sent
        position = channel.sent.index(fence)
        assert EmissionComplete(0, 0) in channel.sent[:position]
        assert session.poll_fence(fence_id) is False
        channel.messages.append(FenceAck(fence_id, 0))
        session.service_controls(deadline=deadline)
        assert session.poll_fence(fence_id) is True
        assert session.poll_fence(fence_id) is False
        assert all(value <= deadline for value in channel.send_deadlines[sent_before:])
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


@pytest.mark.parametrize("operation", ["service", "fence", "sync"])
def test_v3_expired_control_deadline_cannot_renew_or_execute(session_type, game, operation):
    original_backend = game.mb.serial.backend
    with control_session(session_type, game) as (session, channel):
        deadline = time.monotonic() - 1
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        sent = list(channel.sent)
        with pytest.raises(DeadlineExceeded):
            if operation == "service":
                session.service_controls(deadline=deadline)
            elif operation == "fence":
                session.start_fence(deadline=deadline)
            else:
                session.announce_sync(1, deadline=deadline)
        assert channel.sent == sent
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before
        assert channel.closed and session.snapshot().closed
        assert game.mb.serial.backend is original_backend
        assert game.mb.execution_before is game.mb.execution_after is None


def test_v3_passive_control_poll_is_bounded_under_continuous_input(session_type, game):
    channel = ScriptedChannel(revision=3)
    with control_session(session_type, game, channel) as (session, _):
        channel.messages.clear()
        channel.on_poll = lambda: channel.messages.append(Sync(3))
        polls = channel.polls
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        session.service_controls(deadline=time.monotonic() + 1)
        assert 0 < channel.polls - polls <= 64
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_v3_control_cancel_after_poll_never_sends_fence_ack(session_type, game):
    channel = ScriptedChannel([Fence(1, 0)], revision=3)
    with control_session(session_type, game, channel) as (session, _):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        channel.on_poll = session.cancel
        with pytest.raises(Cancelled):
            session.service_controls(deadline=time.monotonic() + 1)
        assert not any(isinstance(message, FenceAck) for message in channel.sent)
        assert session.snapshot().cancelled
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_v3_foreign_controls_snapshot_rejects_without_mutation(session_type, game):
    with control_session(session_type, game) as (session, _):
        before = session.controls_snapshot()
        errors = []

        def foreign():
            try:
                session.controls_snapshot()
            except RuntimeError as exc:
                errors.append(exc)

        worker = threading.Thread(target=foreign, daemon=True)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert session.controls_snapshot() == before
        assert not session.snapshot().closed
        assert game.mb.cpu.retired_instructions == 0


def test_v3_close_during_fence_send_detaches_only_after_control_unwinds(session_type, game):
    with control_session(session_type, game) as (session, channel):
        observations = []

        def close_during_send(message):
            if isinstance(message, Fence):
                try:
                    session.close()
                finally:
                    observations.append(
                        (game.mb.serial.backend is session, game.mb.execution_before is not None)
                    )

        channel.on_send = close_during_send
        with pytest.raises(RuntimeError):
            session.start_fence(deadline=time.monotonic() + 1)
        assert observations == [(True, True)]
        assert session.snapshot().closed
        assert game.mb.execution_before is None
        assert game.mb.serial.backend is not session
        assert game.mb.cpu.retired_instructions == 0


@pytest.mark.parametrize("boundary", ["credit-wait", "edge-wait", "zero-work-return"])
def test_external_cancel_at_real_execution_boundary(session_type, game, boundary):
    external = threading.Event()
    entered = threading.Event()
    worker_errors = []
    channel = ScriptedChannel(revision=3)
    backend = game.mb.serial.backend
    if boundary == "edge-wait":
        # SC arms at raw32; the first edge falls inside LDH at raw544.
        code = [0x3E, 0xA5, 0xE0, 0x01, 0x3E, 0x81, 0xE0, 0x02]
        code += [0x00] * 125 + [0xF0, 0x01, 0x18, 0xFE]
        game.memory[0xC000 : 0xC000 + len(code)] = code

    def cancel_from_peer():
        if not entered.wait(3):
            worker_errors.append("owner did not reach the requested boundary")
        external.set()

    worker = threading.Thread(target=cancel_from_peer, daemon=True)
    session = session_type(
        channel,
        rearm_budget=4096,
        rearm_instruction_cap=1024,
        max_edge_lateness=4096,
        quantum_cycles=24 if boundary == "credit-wait" else 100000,
        operation_timeout=5,
        cancel_event=external,
    )
    if boundary == "zero-work-return":
        public_tick = game.tick

        def cancel_after_actual_public_return(*args, **kwargs):
            result = public_tick(*args, **kwargs)
            entered.set()
            assert external.wait(3)
            return result

        game.tick = cancel_after_actual_public_return
    else:
        receive = channel.receive

        def observe_wait(*, deadline, cancel_event=None):
            assert game.mb.cpu.retired_instructions > 0
            assert session.snapshot().pending_permit is (boundary == "edge-wait")
            entered.set()
            return receive(deadline=deadline, cancel_event=cancel_event)

        channel.receive = observe_wait
    try:
        session.attach(game, deadline=time.monotonic() + 1)
        worker.start()
        with pytest.raises(Cancelled):
            session.tick(0 if boundary == "zero-work-return" else 1, render=False, sound=False)
        worker.join(3)
        assert not worker.is_alive() and not worker_errors
        assert session.snapshot().cancelled
        assert game.mb.execution_before is None
        assert game.mb.serial.backend is backend
        assert channel.closed
        if boundary == "zero-work-return":
            assert game.mb.cpu.retired_instructions == game.mb.cpu.cycles == 0
        elif boundary == "edge-wait":
            # The failure is latched inside LDH's MMIO at raw 544. The native
            # fault boundary returns to the instruction boundary before it
            # raises: LDH finishes at raw 552 with 130 retired instructions.
            # Account that executed suffix; never roll CPU progress back to
            # the failed serial edge's earlier deadline.
            assert game.mb.cpu.retired_instructions == 130
            assert game.mb.cpu.cycles == 552
            assert session.snapshot().raw_cpu_clock == 552
        else:
            assert game.mb.cpu.retired_instructions == 1
            assert game.mb.cpu.cycles == 12
    finally:
        external.set()
        entered.set()
        session.close()
        channel.close()
        if worker.ident is not None:
            worker.join(3)


@pytest.mark.parametrize("boundary", ["credit-wait", "edge-wait"])
def test_real_v3_external_cancel_interrupts_active_receive(session_type, game, boundary):
    external = threading.Event()
    entered = threading.Event()
    failures = []
    original_backend = game.mb.serial.backend
    if boundary == "edge-wait":
        # Authored instructions arm SC at raw32 and encounter the edge in LDH.
        code = [0x3E, 0xA5, 0xE0, 0x01, 0x3E, 0x81, 0xE0, 0x02]
        code += [0x00] * 125 + [0xF0, 0x01, 0x18, 0xFE]
        game.memory[0xC000 : 0xC000 + len(code)] = code
    with real_v3_channels() as (channel, peer):
        session = session_type(
            channel,
            rearm_budget=4096,
            rearm_instruction_cap=1024,
            max_edge_lateness=4096,
            quantum_cycles=24 if boundary == "credit-wait" else 100000,
            operation_timeout=1,
            cancel_event=external,
        )
        receive = channel.receive

        def observe_receive(*, deadline, cancel_event=None):
            if game.mb.cpu.retired_instructions:
                assert session._active
                assert session.snapshot().pending_permit is (boundary == "edge-wait")
                entered.set()
            return receive(deadline=deadline, cancel_event=cancel_event)

        channel.receive = observe_receive

        def cancel_wait():
            if not entered.wait(2):
                failures.append("owner never entered active receive")
            external.set()

        worker = threading.Thread(target=cancel_wait, daemon=True)
        try:
            session.attach(game, deadline=time.monotonic() + 2)
            # The peer has executed no CPU work. Zero is its truthful anchor.
            peer.send(Progress(0), deadline=time.monotonic() + 1)
            worker.start()
            with pytest.raises(Cancelled):
                session.tick(1, render=False, sound=False)
            worker.join(2)
            assert not worker.is_alive() and not failures
            assert entered.is_set()
            assert game.mb.cpu.retired_instructions == (1 if boundary == "credit-wait" else 130)
            assert game.mb.cpu.cycles == (12 if boundary == "credit-wait" else 552)
            assert session.snapshot().cancelled
            assert channel.closed
            assert game.mb.serial.backend is original_backend
            assert game.mb.execution_before is game.mb.execution_after is None
        finally:
            external.set()
            entered.set()
            session.close()
            if worker.ident is not None:
                worker.join(2)


@pytest.mark.parametrize("armed,fail_after", [(False, 1), (True, 1), (True, 2)])
def test_real_v3_startup_failure_preserves_native_writes(session_type, game, armed, fail_after):
    """Inject registration failure between real setters, never replace Serial."""
    core = game.mb.serial
    core.set_SB(0xA5)
    if armed:
        core.set_SC(0x80)
    original_backend = core.backend
    sentinel = RuntimeError("registration failure after actual startup write")
    with real_v3_channels() as (channel, _):
        session = session_type(
            channel,
            rearm_budget=4096,
            rearm_instruction_cap=1024,
            max_edge_lateness=4096,
        )
        verify = session._verify_registration
        observations = []

        def fail_between_setters():
            verify()
            observations.append((core.SB, core.SC, len(session._bootstrap_writes)))
            assert game.mb.execution_before is not None
            assert game.mb.execution_after is not None
            assert core.backend is session
            assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0
            if len(session._bootstrap_writes) == fail_after:
                raise sentinel

        session._verify_registration = fail_between_setters
        try:
            with pytest.raises(RuntimeError) as caught:
                session.attach(game, deadline=time.monotonic() + 2, startup_internal_clock=True)
            assert caught.value is sentinel
            assert len(session._bootstrap_writes) == fail_after
            assert observations[-1] == (core.SB, core.SC, fail_after)
            assert core.SB == (0xA5 if armed and fail_after == 1 else 1)
            assert not core.transfer_enabled
            assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0
            assert core.backend is original_backend
            assert game.mb.execution_before is game.mb.execution_after is None
            assert channel.closed and session.snapshot().closed
            with pytest.raises(RuntimeError, match="epoch cannot be reused"):
                session.attach(game, deadline=time.monotonic() + 1)
            assert len(session._bootstrap_writes) == fail_after
        finally:
            session.close()


def test_real_v3_poll_returning_after_deadline_never_applies_edge(session_type, game):
    original_backend = game.mb.serial.backend
    with (
        real_v3_channels() as (channel, peer),
        control_session(session_type, game, channel, startup=False) as (session, _),
    ):
        core = game.mb.serial
        before = (core.SB, core.SC, core._bits_remaining)
        deadline = time.monotonic() + 0.1
        peer.send(Progress(0), deadline=deadline)
        peer.send(EdgeRequest(1, 0, 0, 1), deadline=deadline)
        peer.send(EmissionComplete(0, 1), deadline=deadline)
        poll = channel.poll
        delayed = []

        def delayed_wire_poll():
            message = poll()
            if isinstance(message, EmissionComplete):
                # Hold a genuinely decoded wire result across the absolute
                # deadline. Event.wait uses real wall time, not a fake clock.
                sleeper = threading.Event()
                while time.monotonic() < deadline:
                    sleeper.wait(max(0, deadline - time.monotonic()))
                delayed.append(message)
            return message

        channel.poll = delayed_wire_poll
        with pytest.raises(DeadlineExceeded):
            while time.monotonic() < deadline:
                session.service_controls(deadline=deadline)
            session.service_controls(deadline=deadline)
        assert delayed == [EmissionComplete(0, 1)]
        assert (core.SB, core.SC, core._bits_remaining) == before
        assert session._applied == 0
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0
        assert session.snapshot().closed and channel.closed
        assert core.backend is original_backend
        assert game.mb.execution_before is game.mb.execution_after is None
