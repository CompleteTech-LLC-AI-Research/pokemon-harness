"""Explicit Session routing contracts with authored PyBoy and real local wire.

The symmetric execution case proves complete public frame calls, not asymmetric
phase rendezvous. Fault-injected endpoint methods test admission and cleanup;
they are not CPU or wire evidence. No emulator counters or RAM are fabricated.
"""

import importlib.machinery
import queue
import socket
import threading
import time
from contextlib import ExitStack, contextmanager

import pytest

from pokered_harness.link.timed_remote import TimedRemoteEndpoint
from pokered_harness.link.timed_wire import Cancelled, ChannelClosed, Progress, ProtocolError
from pokered_harness.session import Session, SessionError, SessionLockTimeout
from pokered_harness.symbols.loader import load_sym_text

BOUND = 3.0
TIMING = {"rearm_budget": 32, "rearm_instruction_cap": 16, "max_edge_lateness": 32}


class Job:
    """A bounded worker whose exceptions reach the asserting thread."""

    def __init__(self, call):
        self.results = queue.Queue()

        def run():
            try:
                self.results.put((True, call()))
            except BaseException as exc:  # noqa: BLE001 - propagate worker failures
                self.results.put((False, exc))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(BOUND + 1)
        assert not self.thread.is_alive(), "Session routing worker exceeded its bound"
        ok, value = self.results.get_nowait()
        if not ok:
            raise value
        return value


def owner_results(workers):
    """Collect both sides before raising so peer-close cannot hide its cause."""
    results, failures = [], []
    for index, worker in enumerate(workers):
        try:
            results.append(worker.result())
        except BaseException as exc:  # noqa: BLE001 - preserve both owner tracebacks
            exc.add_note(f"authored owner index={index}")
            failures.append(exc)
    if failures:
        raise BaseExceptionGroup("authored owner failures", failures)
    return results


@contextmanager
def authored_session(tmp_path, name="routing", *, view=False):
    """Boot an authored cartridge normally, with no Python memory/register edits."""
    from pyboy import PyBoy
    from pyboy.core import cpu, mb, serial

    boot = bytearray(256)
    boot[:6] = bytes([0x31, 0x00, 0xD0, 0xC3, 0xFC, 0x00])  # SP; JP $00fc
    boot[252:] = bytes([0x3E, 0x01, 0xE0, 0x50])  # Unmap boot at PC=$0100.
    cartridge = bytearray(0x8000)
    cartridge[0x100:0x103] = bytes([0xC3, 0x50, 0x01])  # JP $0150
    cartridge[0x134:0x13B] = b"ROUTING"
    # DI; LCD on; loop XOR A / LDH ($02),A / JR. The authored CPU program
    # disables bootstrap SC before the first 512-cycle edge. This exercises
    # public-frame routing, not serial rearm or transfer timing acceptance.
    cartridge[0x150:0x15A] = bytes([0xF3, 0x3E, 0x91, 0xE0, 0x40, 0xAF, 0xE0, 0x02, 0x18, 0xFB])
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 0xFF
    rom_path, boot_path = tmp_path / f"{name}.gb", tmp_path / f"{name}.boot"
    rom_path.write_bytes(cartridge)
    boot_path.write_bytes(boot)
    game = PyBoy(str(rom_path), bootrom=str(boot_path), window="null", sound_emulated=False)
    session = Session(pyboy=game, symbols=load_sym_text(""), view=view)
    try:
        assert type(game.mb) is mb.Motherboard
        assert type(game.mb.cpu) is cpu.CPU
        assert type(game.mb.serial) is serial.Serial
        assert game.mb.cpu.retired_instructions == 0
        game.set_emulation_speed(0)
        session.step(1, render=False)
        assert game.frame_count == session.current_tick() == 1
        assert game.register_file.PC in (0x155, 0x156, 0x158)
        yield session, game
    finally:
        session.close(save=False, timeout_s=BOUND)


def start_endpoint(sock, side):
    return TimedRemoteEndpoint.from_connected_socket(
        sock,
        side=side,
        rom_version="red" if side == "listener" else "blue",
        deadline=time.monotonic() + BOUND,
        **TIMING,
    )


@contextmanager
def endpoint_pair():
    left, right = socket.socketpair()
    worker = Job(lambda: start_endpoint(left, "listener"))
    a = b = None
    try:
        b = start_endpoint(right, "connector")
        a = worker.result()
        yield a, b
    finally:
        for endpoint in (a, b):
            if endpoint is not None:
                endpoint.close()
        left.close()
        right.close()
        worker.thread.join(BOUND + 1)
        assert not worker.thread.is_alive()


@pytest.fixture
def attached_pair(tmp_path):
    with ExitStack() as stack:
        session, game = stack.enter_context(authored_session(tmp_path, "local"))
        _, peer_game = stack.enter_context(authored_session(tmp_path, "peer"))
        fixture = session.save_state()
        session.load_state(fixture)  # Restore before establishing the timed epoch.
        endpoint, peer = stack.enter_context(endpoint_pair())
        endpoint.attach(game, deadline=time.monotonic() + BOUND)
        peer.attach(peer_game, deadline=time.monotonic() + BOUND)
        yield session, game, endpoint, peer, fixture


def observed(session, game):
    return (
        session.current_tick(),
        game.frame_count,
        game.mb.cpu.cycles,
        game.mb.cpu.retired_instructions,
        tuple(int(event) for event in game.events),
        game.mb.execution_before,
        game.mb.execution_after,
        game.mb.serial.backend,
    )


@pytest.mark.parametrize("action", ["bind", "step", "run", "press", "hold", "release", "unbind"])
def test_foreign_owner_rejected_before_mutation(attached_pair, action):
    session, game, endpoint, _, _ = attached_pair
    if action != "bind":
        session.bind_timed_execution(endpoint)
    before = observed(session, game)
    operations = {
        "bind": lambda: session.bind_timed_execution(endpoint),
        "step": lambda: session.step(2),
        "run": lambda: session.run_until_event("absent", max_ticks=2),
        "press": lambda: session.press("a", duration=4),
        "hold": lambda: session.hold("up"),
        "release": lambda: session.release("up"),
        "unbind": lambda: session.unbind_timed_execution(endpoint),
    }
    with pytest.raises(SessionError):
        Job(operations[action]).result()
    assert observed(session, game) == before
    assert not session.closed
    if action != "bind":
        session.unbind_timed_execution(endpoint)


def test_double_binding_and_mismatched_unbind_leave_original_owner(attached_pair):
    session, game, endpoint, peer, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    for candidate in (endpoint, peer):
        with pytest.raises(SessionError):
            session.bind_timed_execution(candidate)
    with pytest.raises(SessionError):
        session.unbind_timed_execution(peer)
    assert observed(session, game) == before
    session.unbind_timed_execution(endpoint)


def test_bind_rejects_wrong_emulator_and_nonendpoint(attached_pair):
    session, game, _, peer, _ = attached_pair
    before = observed(session, game)
    for candidate in (peer, object()):
        with pytest.raises(SessionError):
            session.bind_timed_execution(candidate)
    assert observed(session, game) == before


@pytest.mark.parametrize("value", [-1, float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("method", ["bind_timed_execution", "unbind_timed_execution"])
def test_binding_timeout_requires_finite_nonnegative_value(attached_pair, value, method):
    session, game, endpoint, _, _ = attached_pair
    if method == "unbind_timed_execution":
        session.bind_timed_execution(endpoint)
    before = observed(session, game)
    with pytest.raises(ValueError):
        getattr(session, method)(endpoint, timeout_s=value)
    assert observed(session, game) == before
    if method == "unbind_timed_execution":
        session.unbind_timed_execution(endpoint)


@pytest.mark.parametrize("bound", [False, True])
def test_binding_lock_wait_is_bounded(attached_pair, bound):
    session, game, endpoint, _, _ = attached_pair
    if bound:
        session.bind_timed_execution(endpoint)
    acquired, release = threading.Event(), threading.Event()

    def lock_holder():
        with session.locked(timeout_s=BOUND):
            acquired.set()
            assert release.wait(BOUND)

    worker = Job(lock_holder)
    try:
        assert acquired.wait(BOUND)
        started = time.monotonic()
        method = session.unbind_timed_execution if bound else session.bind_timed_execution
        with pytest.raises(SessionLockTimeout):
            method(endpoint, timeout_s=0.01)
        assert time.monotonic() - started < 0.5
        assert endpoint.session._pyboy is game
    finally:
        release.set()
        worker.result()
    if bound:
        session.unbind_timed_execution(endpoint)


def test_load_and_reset_rejected_until_detach_then_default_execution_restored(attached_pair):
    session, game, endpoint, _, fixture = attached_pair
    raw_tick = game.tick
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    with pytest.raises(SessionError):
        session.load_state(fixture)
    with pytest.raises(SessionError):
        session.reset_tick(99)
    assert observed(session, game) == before
    session.unbind_timed_execution(endpoint)
    assert endpoint.session._pyboy is endpoint.session._board is endpoint.session._core is None
    assert game.mb.execution_before is game.mb.execution_after is None
    assert game.tick == raw_tick
    session.load_state(fixture)
    session.reset_tick(0)
    frames, retirements = game.frame_count, game.mb.cpu.retired_instructions
    assert session.step(2, render=True) is None
    assert session.current_tick() == 2
    assert game.frame_count == frames + 2
    assert game.mb.cpu.retired_instructions > retirements
    assert game.mb.lcd.disable_renderer is False


def test_tick_failure_propagates_identity_and_rolls_back_only_session_bookkeeping(
    attached_pair, monkeypatch
):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    failure = RuntimeError("injected executor failure")
    calls = []

    def reject(count=1, render=True, sound=True):
        calls.append((count, render, sound, session.current_tick()))
        raise failure

    monkeypatch.setattr(endpoint, "tick", reject)
    with pytest.raises(RuntimeError) as raised:
        session.step(4, render=True)
    assert raised.value is failure
    assert calls == [(4, True, True, before[0] + 4)]
    assert observed(session, game) == before
    with pytest.raises(SessionError):
        session.bind_timed_execution(endpoint)
    session.unbind_timed_execution(endpoint)


@pytest.mark.parametrize("action", ["step", "press", "hold", "release", "unbind", "bind"])
def test_reentrant_route_operations_rejected_before_mutation(attached_pair, monkeypatch, action):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    operations = {
        "step": lambda: session.step(),
        "press": lambda: session.press("a"),
        "hold": lambda: session.hold("up"),
        "release": lambda: session.release("up"),
        "unbind": lambda: session.unbind_timed_execution(endpoint),
        "bind": lambda: session.bind_timed_execution(endpoint),
    }
    failure = RuntimeError("stop forwarding-only probe without claiming CPU work")

    def probe(*args, **kwargs):
        before = observed(session, game)
        with pytest.raises(SessionError):
            operations[action]()
        assert observed(session, game) == before
        raise failure

    monkeypatch.setattr(endpoint, "tick", probe)
    with pytest.raises(RuntimeError) as raised:
        session.step()
    assert raised.value is failure
    session.unbind_timed_execution(endpoint)


@pytest.mark.parametrize("registration", ["backend", "execution_before"])
def test_unbind_retains_route_on_replaced_registration_until_repaired(attached_pair, registration):
    session, game, endpoint, _, fixture = attached_pair
    session.bind_timed_execution(endpoint)
    target = game.mb.serial if registration == "backend" else game.mb
    original = getattr(target, registration)
    replacement = object() if registration == "backend" else lambda *args: None
    original_after = game.mb.execution_after
    if registration == "backend":
        target.backend = replacement
    else:
        game.mb.set_execution_governor(None, None)
        game.mb.set_execution_governor(replacement, original_after)
    try:
        with pytest.raises(RuntimeError, match="foreign"):
            session.unbind_timed_execution(endpoint)
        assert getattr(target, registration) is replacement
        with pytest.raises(SessionError):
            session.load_state(fixture)
        with pytest.raises(SessionError):
            session.bind_timed_execution(endpoint)
    finally:
        # Repair only our injected replacement through native public APIs.
        if registration == "backend":
            target.backend = original
        else:
            game.mb.set_execution_governor(None, None)
            game.mb.set_execution_governor(original, original_after)
    session.unbind_timed_execution(endpoint)
    assert endpoint.session._pyboy is None


def test_unbind_retains_route_if_endpoint_close_raises(attached_pair, monkeypatch):
    session, _, endpoint, _, fixture = attached_pair
    session.bind_timed_execution(endpoint)
    failure = RuntimeError("injected owner cleanup failure")

    def reject():
        raise failure

    with monkeypatch.context() as context:
        context.setattr(endpoint, "close", reject)
        with pytest.raises(RuntimeError) as raised:
            session.unbind_timed_execution(endpoint)
        assert raised.value is failure
        with pytest.raises(SessionError):
            session.load_state(fixture)
    session.unbind_timed_execution(endpoint)


def test_foreign_close_cancels_without_starting_stop_worker_and_owner_can_cleanup(attached_pair):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    with pytest.raises(SessionError):
        Job(lambda: session.close(save=False, timeout_s=0.01)).result()
    assert not session.closed
    assert not session._stopped
    assert session._stop_thread is None
    assert observed(session, game) == before
    with pytest.raises(Cancelled):
        session.press("a")
    assert observed(session, game) == before
    session.unbind_timed_execution(endpoint)
    session.close(save=False, timeout_s=BOUND)
    assert session.closed
    frames = game.frame_count
    assert not game.tick(1, render=False)
    assert game.frame_count == frames


def test_owner_close_detaches_before_stop_worker(attached_pair, monkeypatch):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    original_close = endpoint.close
    calls = []

    def close():
        calls.append(threading.get_ident())
        assert not session._stopped
        assert session._stop_thread is None
        original_close()

    monkeypatch.setattr(endpoint, "close", close)
    owner = threading.get_ident()
    session.close(save=False, timeout_s=BOUND)
    assert calls == [owner]
    assert session.closed
    frames = game.frame_count
    assert not game.tick(1, render=False)
    assert game.frame_count == frames
    assert endpoint.session._pyboy is None
    # Fixture cleanup may close this already-detached endpoint again.
    monkeypatch.setattr(endpoint, "close", original_close)


def test_cancel_reaches_active_real_credit_wait_without_session_lock(attached_pair, monkeypatch):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    receiving = threading.Event()
    cancelled = threading.Event()
    original_receive = type(endpoint.channel).receive
    original_wait = endpoint.session._adapter._wait
    owner = threading.get_ident()
    credit_wait = False

    def observe_credit_wait(remaining):
        nonlocal credit_wait
        assert threading.get_ident() == owner
        credit_wait = True
        try:
            return original_wait(remaining)
        finally:
            credit_wait = False

    def observe_receive(channel, *, deadline, cancel_event=None):
        if channel is endpoint.channel:
            assert threading.get_ident() == owner
            # Startup Progress(0) can require a receive before any instruction.
            # Let real controls establish credit before selecting a partial wait.
            if game.mb.cpu.cycles > before[2] and game.mb.cpu.retired_instructions > before[3]:
                assert credit_wait
                assert session._timed_executing and endpoint.session._active
                assert not endpoint.session._in_edge
                receiving.set()
                assert cancelled.wait(BOUND)
        return original_receive(channel, deadline=deadline, cancel_event=cancel_event)

    monkeypatch.setattr(endpoint.session._adapter, "_wait", observe_credit_wait)
    monkeypatch.setattr(type(endpoint.channel), "receive", observe_receive)

    def cancel_during_receive():
        assert receiving.wait(BOUND)
        session.cancel_timed_execution()
        cancelled.set()

    worker = Job(cancel_during_receive)
    try:
        # Cancellation must remain Cancelled even when it closes the channel.
        with pytest.raises(Cancelled):
            session.step(2)
        worker.result()
        assert session.current_tick() == before[0]
        assert game.frame_count == before[1]
        assert game.mb.cpu.cycles > before[2]
        assert game.mb.cpu.retired_instructions > before[3]
        assert endpoint.snapshot().cancelled
        with pytest.raises(SessionError):
            session.reset_tick()
    finally:
        session.cancel_timed_execution()
        session.unbind_timed_execution(endpoint)
        worker.thread.join(BOUND + 1)
        assert not worker.thread.is_alive()


def test_cancel_during_real_progress_send_preserves_cancelled(attached_pair, monkeypatch):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    sending, cancelled = threading.Event(), threading.Event()
    original_send = type(endpoint.channel).send_complete_progress
    owner = threading.get_ident()

    def observe_send(channel, complete, message, *, deadline, cancel_event=None):
        if (
            channel is endpoint.channel
            and isinstance(message, Progress)
            and message.settled_half_cycles > 0
        ):
            assert threading.get_ident() == owner
            assert complete.through_half_cycle == message.settled_half_cycles
            assert game.mb.cpu.cycles > before[2]
            assert game.mb.cpu.retired_instructions > before[3]
            assert session._timed_executing and endpoint.session._active
            sending.set()
            assert cancelled.wait(BOUND)
        return original_send(
            channel, complete, message, deadline=deadline, cancel_event=cancel_event
        )

    monkeypatch.setattr(type(endpoint.channel), "send_complete_progress", observe_send)

    def cancel_during_send():
        assert sending.wait(BOUND)
        session.cancel_timed_execution()
        cancelled.set()

    worker = Job(cancel_during_send)
    try:
        with pytest.raises(Cancelled):
            session.step(2)
        worker.result()
        assert endpoint.snapshot().cancelled
        assert session.current_tick() == before[0]
        assert game.frame_count == before[1]
        assert game.mb.cpu.retired_instructions > before[3]
        with pytest.raises(SessionError):
            session.reset_tick()
    finally:
        session.cancel_timed_execution()
        session.unbind_timed_execution(endpoint)
        worker.thread.join(BOUND + 1)
        assert not worker.thread.is_alive()


@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("cancel", [False, True])
def test_original_tick_caller_error_identity_survives_cancellation(
    attached_pair, monkeypatch, error_type, cancel
):
    """Fault injection at the captured callable; no invented CPU/frame work."""
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    failure = error_type("original caller failure")

    def original_tick_failure(count, render, sound):
        assert (count, render, sound) == (2, False, True)
        assert session.current_tick() == before[0] + 2
        if cancel:
            session.cancel_timed_execution()
        raise failure

    monkeypatch.setattr(endpoint.session, "_original_tick", original_tick_failure)
    with pytest.raises(error_type) as raised:
        session.step(2, render=False)
    assert raised.value is failure
    assert observed(session, game)[:4] == before[:4]
    assert endpoint.snapshot().cancelled is cancel
    with pytest.raises(SessionError):
        session.reset_tick()
    session.unbind_timed_execution(endpoint)


def test_stored_protocol_failure_identity_wins_cancellation_close(attached_pair, monkeypatch):
    """Inject a first wire error; test precedence, not malformed-frame parsing."""
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    failure = ProtocolError("first stored wire protocol failure")

    def original_tick_failure(count, render, sound):
        assert endpoint.channel._terminate(failure) is failure
        session.cancel_timed_execution()
        raise ChannelClosed("later cancellation close")

    monkeypatch.setattr(endpoint.session, "_original_tick", original_tick_failure)
    with pytest.raises(ProtocolError) as raised:
        session.step(2)
    assert raised.value is failure
    assert endpoint.channel.error is failure
    assert observed(session, game)[:4] == before[:4]
    with pytest.raises(SessionError):
        session.reset_tick()
    session.unbind_timed_execution(endpoint)


@pytest.mark.parametrize("action", ["step", "press", "hold", "release"])
def test_cancelled_binding_rejects_before_input_or_execution(attached_pair, action):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    Job(session.cancel_timed_execution).result()
    operation = {
        "step": lambda: session.step(),
        "press": lambda: session.press("a", duration=4),
        "hold": lambda: session.hold("up"),
        "release": lambda: session.release("up"),
    }[action]
    with pytest.raises(Cancelled):
        operation()
    assert observed(session, game) == before
    session.unbind_timed_execution(endpoint)
    assert session.cancel_timed_execution() is None


def test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events(
    tmp_path, record_property
):
    """Symmetric owners only: this does not qualify unequal-phase rendezvous."""
    from pyboy import pyboy as public
    from pyboy.core import cpu, mb, serial
    from pyboy.utils import WindowEvent

    paths = [str(module.__file__) for module in (public, cpu, mb, serial)]
    native = [
        any(path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)
        for path in paths
    ]
    assert all(native) or all(path.endswith(".py") for path in paths), paths
    record_property("runtime", "native" if all(native) else "source")
    record_property("runtime_modules", repr(paths))
    calls_per_owner, owner_count = 3, 2
    paired_capacity_s = owner_count * calls_per_owner * (BOUND + 1)
    # This only aligns owners before paired CPU work. Socket setup and timed
    # routing retain their own protocol deadlines; the capacity bound covers
    # the known two-owner, six-call workload without measuring GIL throughput.
    ready = threading.Barrier(3, timeout=paired_capacity_s)
    left, right = socket.socketpair()

    def owner(sock, index):
        endpoint = None
        with authored_session(tmp_path, f"paired-{index}", view=True) as (session, game):
            try:
                endpoint = start_endpoint(sock, "listener" if index == 0 else "connector")
                endpoint.attach(game, deadline=time.monotonic() + BOUND)
                session.bind_timed_execution(endpoint)
                original_tick = endpoint.tick
                raw_tick = game.tick
                calls = []
                completed_frames = []
                pending_events = []

                def recording_tick(count=1, render=True, sound=True):
                    calls.append((count, render, sound, session.current_tick()))
                    start_frame = game.frame_count
                    result = original_tick(count, render=render, sound=sound)
                    completed_frames.append(game.frame_count - start_frame)
                    pending_events.append([int(event) for event in game.events])
                    return result

                endpoint.tick = recording_tick  # Observe real calls without changing execution.
                before = observed(session, game)
                session.press("a", duration=4)
                session.hold("up")
                session.release("up")
                assert observed(session, game)[:4] == before[:4]
                assert [int(event) for event in game.events] == [
                    WindowEvent.PRESS_BUTTON_A,
                    WindowEvent.PRESS_ARROW_UP,
                    WindowEvent.RELEASE_ARROW_UP,
                ]
                ready.wait()
                assert session.step(2) is None
                result = session.run_until_event("absent", max_ticks=3, chunk=2, render=False)
                assert result.event is None and result.ticks_spent == 3
                assert calls == [(2, True, True, 3), (2, False, True, 5), (1, False, True, 6)]
                assert completed_frames == [2, 2, 1]
                assert session.current_tick() == game.frame_count == before[0] + 5
                assert game.mb.cpu.cycles > before[2]
                assert game.mb.cpu.retired_instructions > before[3]
                assert pending_events == [[], [WindowEvent.RELEASE_BUTTON_A], []]
                assert game.mb.lcd.disable_renderer is True
                assert game.tick == raw_tick  # No PyBoy monkeypatch, including native mode.
                session.unbind_timed_execution(endpoint)
                frames = game.frame_count
                session.step(1, render=True)
                assert game.frame_count == frames + 1
                assert len(calls) == 3
                return game.mb.cpu.retired_instructions
            finally:
                if endpoint is not None:
                    endpoint.close()

    workers = [Job(lambda: owner(left, 0)), Job(lambda: owner(right, 1))]
    try:
        ready.wait()
        capacity_deadline = time.monotonic() + paired_capacity_s
        for worker in workers:
            worker.thread.join(max(0, capacity_deadline - time.monotonic()))
        assert all(not worker.thread.is_alive() for worker in workers), (
            f"paired timed route exceeded {paired_capacity_s:g}s six-call capacity"
        )
        assert all(result > 0 for result in owner_results(workers))
    finally:
        left.close()
        right.close()
        for worker in workers:
            worker.thread.join(BOUND + 1)
            assert not worker.thread.is_alive()


@pytest.mark.parametrize("cancel_owner", ["session", "endpoint"])
def test_real_partial_public_tick_failure_counts_only_completed_frames(tmp_path, cancel_owner):
    """A real hook cancels frame two of tick(3); no completed tick/fence claim."""
    ready = threading.Barrier(2, timeout=BOUND)
    finished = threading.Barrier(2, timeout=BOUND)
    left, right = socket.socketpair()

    def owner(sock, index):
        endpoint = None
        with authored_session(tmp_path, f"partial-{index}") as (session, game):
            try:
                endpoint = start_endpoint(sock, "listener" if index == 0 else "connector")
                endpoint.attach(game, deadline=time.monotonic() + BOUND)
                session.bind_timed_execution(endpoint)
                start_tick, start_frame = session.current_tick(), game.frame_count
                start_retired = game.mb.cpu.retired_instructions
                anticipated = []
                if index == 0:

                    def interrupt_second_frame(_context):
                        if game.frame_count == start_frame + 1 and not anticipated:
                            anticipated.append(session.current_tick())
                            if cancel_owner == "session":
                                session.cancel_timed_execution()
                            else:
                                endpoint.cancel()

                    session.register_hook_at_address(0, 0x155, interrupt_second_frame)
                ready.wait()
                try:
                    session.step(3, render=False)
                except BaseException as exc:  # noqa: BLE001 - test exact propagated failure
                    if index == 0:
                        assert isinstance(exc, (Cancelled, ChannelClosed))
                        assert game.frame_count - start_frame == 1
                        assert session.current_tick() == start_tick + 1
                        assert anticipated == [start_tick + 3]
                        assert game.mb.cpu.retired_instructions > start_retired
                    else:
                        assert isinstance(exc, (Cancelled, ChannelClosed, OSError))
                        assert session.current_tick() - start_tick == game.frame_count - start_frame
                else:
                    pytest.fail("both public calls must unwind after owner cancellation")
                with pytest.raises(SessionError):
                    session.reset_tick()
                finished.wait()
                session.unbind_timed_execution(endpoint)
            finally:
                if endpoint is not None:
                    endpoint.close()

    workers = [Job(lambda: owner(left, 0)), Job(lambda: owner(right, 1))]
    try:
        owner_results(workers)
    finally:
        left.close()
        right.close()
        for worker in workers:
            worker.thread.join(BOUND + 1)
            assert not worker.thread.is_alive()


def test_owner_close_accepts_huge_finite_timeout_without_overflow(attached_pair):
    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint, timeout_s=1e100)
    session.close(save=False, timeout_s=1e100)
    assert session.closed
    frames = game.frame_count
    assert not game.tick(1, render=False)
    assert game.frame_count == frames
    assert endpoint.session._pyboy is None


def test_raw_inflight_bind_rejected_by_admission_contract(attached_pair):
    """A labeled call-boundary spy; no CPU execution or attach inside a hook."""
    session, game, endpoint, _, _ = attached_pair
    before = observed(session, game)
    failure = RuntimeError("end raw admission probe before CPU work")

    class RawCallSpy:
        def __getattr__(self, name):
            return getattr(game, name)

        def tick(self, count=1, render=True):
            assert (count, render) == (2, False)
            assert session.current_tick() == before[0] + 2
            with pytest.raises(SessionError, match="idle owner boundary"):
                session.bind_timed_execution(endpoint)
            raise failure

    session._pyboy = RawCallSpy()
    try:
        with pytest.raises(RuntimeError) as raised:
            session.step(2, render=False)
        assert raised.value is failure
    finally:
        session._pyboy = game
    assert observed(session, game) == before
    session.bind_timed_execution(endpoint)
    session.unbind_timed_execution(endpoint)


@pytest.mark.parametrize("quitting", [False, True])
def test_real_no_frame_return_does_not_fabricate_session_progress(
    attached_pair, monkeypatch, quitting
):
    from pyboy.utils import WindowEvent

    session, game, endpoint, _, _ = attached_pair
    session.bind_timed_execution(endpoint)
    before = observed(session, game)
    game.send_input(WindowEvent.PAUSE)
    if quitting:
        game.send_input(WindowEvent.QUIT)
    original_tick = endpoint.tick
    calls = []

    def observe(count=1, render=True, sound=True):
        anticipated = session.current_tick()
        result = original_tick(count, render=render, sound=sound)
        calls.append((count, anticipated, result))
        return result

    monkeypatch.setattr(endpoint, "tick", observe)
    assert session.step(3) is None
    assert calls == [(3, before[0] + 3, not quitting)]
    assert observed(session, game)[:4] == before[:4]
    result = session.run_until_event("absent", max_ticks=4, chunk=2)
    assert result.event is None and result.ticks_spent == 0
    assert len(calls) == 2
    assert observed(session, game)[:4] == before[:4]
    session.unbind_timed_execution(endpoint)
