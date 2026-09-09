"""Raw and managed APIs share one emulator owner without native attributes."""

from __future__ import annotations

import gc
from contextlib import nullcontext
import threading
import weakref

import pytest

from pokered_harness import mcp_server
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.session import Session, SessionClosedError, SessionCleanupTimeoutError
from tests.conftest import DictMemory
from tests.test_local_scheduler_candidate import Endpoint
from tests.test_session import _session

pytestmark = pytest.mark.timing_sensitive

# These tests use the tiny asset-free Endpoint model below: four serial
# cycles per instruction and an LCD marker every four instructions.  Keep the
# production scheduler's normal 140,448-unit physical quantum unchanged, but
# use four fake instructions (4 serial cycles each, 2 physical units per cycle)
# per logical frame. Ownership assertions then exercise the concurrency
# contract with the original fixture workload, rather than 17,556 instructions.
_FAKE_PHYSICAL_QUANTUM = 4 * 4 * 2


def _fake_local_provider():
    provider = PyBoyLinkSession.local()
    provider.PHYSICAL_QUANTUM = _FAKE_PHYSICAL_QUANTUM
    return provider


def test_owner_held_dispatch_rejected_before_outer_operation_lock_wait():
    from pokered_harness.ownership import EmulatorOwnershipError

    provider, primary, peer, a, b = _pair()
    link = mcp_server.LinkState(peer_session=peer)
    link.local_link_session = provider
    entered, attempted = threading.Event(), threading.Event()
    primary._lock = AttemptLock(primary._lock, attempted)
    peer._lock = AttemptLock(peer._lock, attempted)

    def owner_call():
        with primary.locked():
            entered.set()
            assert attempted.wait(2)
            with pytest.raises(EmulatorOwnershipError, match="outside an emulator ownership scope"):
                mcp_server.dispatch_tool(primary, "link_step", {"count": 1}, link)

    owner, owner_errors = _worker(owner_call, "owner")
    assert entered.wait(2)
    managed, managed_errors = _worker(
        lambda: mcp_server.dispatch_tool(primary, "link_step", {"count": 1}, link), "managed"
    )
    _join(owner)
    _join(managed)
    assert owner_errors == managed_errors == []
    assert a.frame_count == b.frame_count == 1
    provider.detach_all()
    primary.close()
    peer.close()


def test_serial_hook_mutating_dispatch_rejected_without_outer_lock_reentry():
    from pokered_harness.ownership import EmulatorOwnershipError

    provider, primary, peer, a, b = _pair()
    link = mcp_server.LinkState(peer_session=peer)
    link.local_link_session = provider
    callbacks, rejections = [], []
    a.hook_register = lambda bank, addr, callback, context: callbacks.append((callback, context))

    def callback(_):
        with pytest.raises(EmulatorOwnershipError, match="outside an emulator ownership scope"):
            mcp_server.dispatch_tool(primary, "link_step", {"count": 1}, link)
        rejections.append(True)

    primary.serial_hook("DisplayTextID", callback)
    a.on_instruction = lambda: callbacks[0][0](callbacks[0][1])
    worker, errors = _worker(
        lambda: mcp_server.dispatch_tool(primary, "link_step", {"count": 1}, link), "serial-hook"
    )
    _join(worker)
    assert errors == [] and rejections
    assert a.frame_count == b.frame_count == 1
    provider.detach_all()
    primary.close()
    peer.close()


@pytest.mark.parametrize("name", ["link_status", "link_disconnect"])
def test_owner_held_status_and_disconnect_bypass_outer_operation_admission(name):
    from pokered_harness.session import locked_sessions

    provider, primary, peer, _, _ = _pair()
    link = mcp_server.LinkState(peer_session=peer)
    link.local_link_session = provider
    entered, release = threading.Event(), threading.Event()

    def hold_outer():
        with link.operation():
            entered.set()
            assert release.wait(3)

    worker, errors = _worker(hold_outer, "outer-holder")
    assert entered.wait(2)
    try:
        with locked_sessions(primary, peer):
            result = mcp_server.dispatch_tool(primary, name, {}, link)
            assert isinstance(result, dict)
    finally:
        release.set()
        _join(worker)
    assert errors == []
    provider.detach_all()
    primary.close()
    peer.close()


class AttemptLock:
    def __init__(self, lock, attempted):
        self.lock, self.attempted = lock, attempted

    def acquire(self, *args, **kwargs):
        if threading.current_thread().name == "managed":
            self.attempted.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


def _worker(call, name):
    errors = []

    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True, name=name)
    thread.start()
    return thread, errors


def _join(thread):
    thread.join(2)
    assert not thread.is_alive(), f"{thread.name} did not finish"


def _endpoint(name):
    endpoint = Endpoint(name, [])
    endpoint.memory = DictMemory()
    endpoint.stopped = False
    endpoint.stop_calls = []

    def stop(save=False):
        endpoint.stop_calls.append(save)
        endpoint.stopped = True

    endpoint.stop = stop
    endpoint.save_state = lambda stream: stream.write(b"snapshot")
    return endpoint


def _pair():
    symbols = _session()[0].symbols
    a, b = _endpoint("a"), _endpoint("b")
    primary, peer = Session(pyboy=a, symbols=symbols), Session(pyboy=b, symbols=symbols)
    provider = _fake_local_provider()
    provider.attach(a)
    provider.attach(b)
    return provider, primary, peer, a, b


def test_raw_and_managed_step_share_owner_without_reverse_lock_deadlock():
    provider, primary, peer, a, b = _pair()
    entered, release, attempted = (threading.Event() for _ in range(3))
    primary._lock = AttemptLock(primary._lock, attempted)
    peer._lock = AttemptLock(peer._lock, attempted)
    callbacks, raw_depth = [], []
    a.hook_register = lambda bank, addr, callback, context: callbacks.append((callback, context))
    a.mb.breakpoint_reached = lambda: (2, 0x4A12, 0)
    a.mb.breakpoint_remove = lambda *_args: None
    primary.register_hook("DisplayTextID", "text")

    def handle_hooks():
        if not entered.is_set():
            raw_depth.append(getattr(primary._execution, "depth", 0))
            entered.set()
            assert release.wait(3)
        for callback, context in callbacks:
            callback(context)

    a._handle_hooks = handle_hooks
    link = mcp_server.LinkState(peer_session=peer)
    link.local_link_session = provider
    raw, raw_errors = _worker(provider.step, "raw")
    assert entered.wait(2)
    managed, managed_errors = _worker(
        lambda: mcp_server.dispatch_tool(primary, "link_step", {"count": 1}, link), "managed"
    )
    try:
        # Signal comes BEFORE attempted owner acquisition, not after it.
        assert attempted.wait(2)
        assert raw_depth[0] > 0, "raw provider did not enter the managed owner scope"
        for session in (primary, peer):
            acquired = session._lock.acquire(blocking=False)
            if acquired:
                session._lock.release()
            assert not acquired, "raw provider must retain both owners while its hook runs"
    finally:
        release.set()
        _join(raw)
        _join(managed)
    assert raw_errors == managed_errors == []
    assert a.frame_count == b.frame_count == 2
    provider.detach_all()
    primary.close()
    peer.close()


def test_raw_step_close_times_out_without_stop_then_retry_and_reject_future_steps():
    provider, primary, peer, a, _ = _pair()
    entered, release = threading.Event(), threading.Event()

    def pause():
        entered.set()
        assert release.wait(3)

    a.on_instruction = pause
    thread, errors = _worker(provider.step, "raw")
    assert entered.wait(2)
    try:
        with pytest.raises(SessionCleanupTimeoutError, match="cleanup deadline"):
            primary.close(timeout_s=.02)
        assert primary.closed
        assert a.stop_calls == []
    finally:
        release.set()
        _join(thread)
    assert errors == []
    primary.close()
    primary.close()
    assert a.stop_calls == [False]
    frames = a.frame_count
    with pytest.raises(SessionClosedError):
        provider.step()
    assert a.frame_count == frames
    provider.detach_all()
    peer.close()


def test_closed_session_can_still_unpair_through_mcp_cleanup():
    provider, primary, peer, _, _ = _pair()
    link = mcp_server.LinkState(peer_session=peer)
    link.local_link_session = provider
    primary.close()
    assert mcp_server.dispatch_tool(primary, "link_unpair", {}, link) == {"paired": False}
    assert provider.attached == ()
    assert link.local_link_session is None
    peer.close()


@pytest.mark.parametrize("operation", ["read", "save", "input"])
def test_raw_provider_excludes_managed_read_save_and_input(operation):
    provider, primary, peer, a, _ = _pair()
    entered, release, attempted, completed = (threading.Event() for _ in range(4))
    primary._lock = AttemptLock(primary._lock, attempted)
    calls = []
    a.button = lambda *args: calls.append(args)

    def pause():
        entered.set()
        assert release.wait(3)

    def managed_operation():
        if operation == "read":
            assert primary.read_game_state() is not None
        elif operation == "save":
            assert primary.save_state() == b"snapshot"
        else:
            primary.press("a")
        completed.set()

    a.on_instruction = pause
    raw, raw_errors = _worker(provider.step, "raw")
    assert entered.wait(2)
    managed, managed_errors = _worker(managed_operation, "managed")
    try:
        assert attempted.wait(2)
        acquired = primary._lock.acquire(blocking=False)
        if acquired:
            primary._lock.release()
        assert not acquired
        assert not completed.is_set()
    finally:
        release.set()
        _join(raw)
        _join(managed)
    assert completed.is_set()
    assert raw_errors == managed_errors == []
    provider.detach_all()
    primary.close()
    peer.close()


def test_raw_callback_close_is_deferred_by_shared_execution_depth():
    provider, primary, peer, a, _ = _pair()
    called = []

    def callback():
        if called:
            return
        with pytest.raises(SessionCleanupTimeoutError, match="current emulator operation"):
            primary.close()
        called.append(True)
        assert a.stop_calls == []

    a.on_instruction = callback
    provider.step()
    assert called == [True]
    primary.close()
    assert a.stop_calls == [False]
    provider.detach_all()
    peer.close()


def test_reversed_group_order_is_canonical_and_reverse_manual_order_fails_closed():
    from pokered_harness.ownership import EmulatorOwnershipError
    from pokered_harness.session import locked_sessions

    provider, primary, peer, _, _ = _pair()
    with locked_sessions(peer, primary):
        provider.step()
    lower, higher = sorted((primary, peer), key=lambda session: session._owner.order)
    with higher.locked():
        with pytest.raises(EmulatorOwnershipError, match="canonical order"):
            with lower.locked():
                pytest.fail("reverse order acquired")
        with pytest.raises(EmulatorOwnershipError, match="canonical order"):
            provider.step()
    provider.step()
    provider.detach_all()
    primary.close()
    peer.close()


def test_duplicate_session_rejected_without_native_weakrefs_attributes_or_hashing():
    from pokered_harness.ownership import EmulatorOwnershipError

    class OpaqueEmulator:
        __slots__ = ()
        __hash__ = None

        def stop(self, save=False):
            pass

    emulator = OpaqueEmulator()
    symbols = _session()[0].symbols
    with pytest.raises(TypeError):
        weakref.ref(emulator)
    with pytest.raises(AttributeError):
        emulator.owner = None
    primary = Session(pyboy=emulator, symbols=symbols)
    with pytest.raises(EmulatorOwnershipError, match="live Session"):
        Session(pyboy=emulator, symbols=symbols)
    assert not primary.closed
    primary.close()


def test_owner_registry_does_not_keep_session_or_emulator_alive():
    from pokered_harness.ownership import owner_for

    session, emulator, _ = _session()
    owner = owner_for(emulator)
    owner_ref, session_ref, emulator_ref = weakref.ref(owner), weakref.ref(session), weakref.ref(emulator)
    del owner, session, emulator
    gc.collect()
    assert owner_ref() is None
    assert session_ref() is None
    assert emulator_ref() is None


def test_provider_retains_owner_until_detached_without_a_registry_leak():
    from pokered_harness.ownership import owner_for

    provider = _fake_local_provider()
    emulator = _endpoint("a")
    provider.attach(emulator)
    owner_ref = weakref.ref(owner_for(emulator))
    gc.collect()
    assert owner_ref() is not None
    provider.detach_all()
    gc.collect()
    assert owner_ref() is None


def test_concurrent_session_registration_accepts_exactly_one_wrapper():
    from pokered_harness.ownership import EmulatorOwnershipError

    emulator = _endpoint("unmanaged")
    symbols = _session()[0].symbols
    barrier = threading.Barrier(3)
    sessions = []

    def register():
        barrier.wait(timeout=2)
        sessions.append(Session(pyboy=emulator, symbols=symbols))

    first, errors_first = _worker(register, "register-first")
    second, errors_second = _worker(register, "register-second")
    barrier.wait(timeout=2)
    _join(first)
    _join(second)
    errors = errors_first + errors_second
    assert len(sessions) == len(errors) == 1
    assert isinstance(errors[0], EmulatorOwnershipError)
    sessions[0].close()


def test_session_registration_inside_raw_callback_is_rejected_without_state_change():
    from pokered_harness.ownership import EmulatorOwnershipError

    provider = _fake_local_provider()
    a, b = _endpoint("a"), _endpoint("b")
    symbols = _session()[0].symbols
    provider.attach(a)
    provider.attach(b)
    rejected = []

    def callback():
        with pytest.raises(EmulatorOwnershipError, match="during emulator execution"):
            Session(pyboy=a, symbols=symbols)
        rejected.append(True)

    a.on_instruction = callback
    provider.step()
    assert rejected and a.frame_count == b.frame_count == 1
    assert not a.stopped
    provider.detach_all()


def test_session_created_after_raw_attachment_adopts_the_existing_owner():
    from pokered_harness.ownership import owner_for

    provider = _fake_local_provider()
    a, b = _endpoint("a"), _endpoint("b")
    provider.attach(a)
    provider.attach(b)
    existing = owner_for(a)
    session = Session(pyboy=a, symbols=_session()[0].symbols)
    assert session._owner is existing
    provider.step()
    session.close()
    with pytest.raises(SessionClosedError):
        provider.step()
    provider.detach_all()


def test_competing_provider_attachment_has_one_owner_and_detach_releases_claim():
    from pokered_harness.ownership import EmulatorOwnershipError

    a = _endpoint("a")
    providers = [_fake_local_provider(), _fake_local_provider()]
    barrier = threading.Barrier(3)

    def attach(provider):
        barrier.wait(timeout=2)
        provider.attach(a)

    workers = [_worker(lambda provider=provider: attach(provider), f"attach-{index}")
               for index, provider in enumerate(providers)]
    barrier.wait(timeout=2)
    for thread, _ in workers:
        _join(thread)
    errors = [error for _, failures in workers for error in failures]
    assert len(errors) == 1 and isinstance(errors[0], EmulatorOwnershipError)
    winner = next(provider for provider in providers if provider.attached)
    loser = next(provider for provider in providers if not provider.attached)
    assert winner.attached == (a,)
    winner.detach_all()
    loser.attach(a)
    loser.detach_all()


def test_cross_provider_pair_attempts_fail_without_lock_cycles_or_claim_loss():
    from pokered_harness.ownership import EmulatorOwnershipError

    first, second = _fake_local_provider(), _fake_local_provider()
    a, b = _endpoint("a"), _endpoint("b")
    first.attach(a)
    second.attach(b)
    barrier = threading.Barrier(3)

    def attach(provider, endpoint):
        barrier.wait(timeout=2)
        provider.attach(endpoint)

    one, errors_one = _worker(lambda: attach(first, b), "attach-first")
    two, errors_two = _worker(lambda: attach(second, a), "attach-second")
    barrier.wait(timeout=2)
    _join(one)
    _join(two)
    assert len(errors_one) == len(errors_two) == 1
    assert all(isinstance(error, EmulatorOwnershipError) for error in errors_one + errors_two)
    assert first.attached == (a,) and second.attached == (b,)
    first.detach_all()
    second.detach_all()


def test_rejected_provider_claim_does_not_write_backend_or_stop_transport():
    from types import SimpleNamespace
    from pokered_harness.ownership import EmulatorOwnershipError

    class Core:
        def __init__(self):
            self.value = None
            self.writes = []

        @property
        def backend(self):
            return self.value

        @backend.setter
        def backend(self, value):
            self.writes.append(value)
            self.value = value

    endpoint = _endpoint("a")
    core = Core()
    endpoint.mb.serial = core
    first = _fake_local_provider()
    stopped = []
    network = SimpleNamespace(start_receiver=lambda **kwargs: None, stop=lambda: stopped.append(True))
    second = PyBoyLinkSession(network_backend=network)
    first.attach(endpoint)
    with pytest.raises(EmulatorOwnershipError, match="another provider"):
        second.attach(endpoint)
    assert core.writes == []
    assert stopped == []
    assert first.attached == (endpoint,) and second.attached == ()
    first.detach_all()


def test_close_rejects_reverse_owner_order_without_stopping_then_can_retry():
    provider, primary, peer, _, _ = _pair()
    lower, higher = sorted((primary, peer), key=lambda session: session._owner.order)
    with higher.locked():
        with pytest.raises(SessionCleanupTimeoutError, match="other emulator ownership"):
            lower.close()
        assert lower._pyboy.stop_calls == []
    lower.close()
    assert lower._pyboy.stop_calls == [False]
    provider.detach_all()
    higher.close()


def test_network_detach_stops_transport_without_waiting_for_emulator_owner():
    from types import SimpleNamespace

    entered, stopped = threading.Event(), threading.Event()
    backend = SimpleNamespace(
        start_receiver=lambda **kwargs: None, stop=stopped.set,
        owner_scope=nullcontext,
    )
    provider = PyBoyLinkSession(network_backend=backend)
    emulator = _endpoint("network")
    session = Session(pyboy=emulator, symbols=_session()[0].symbols)
    provider.attach(emulator)

    def network_wait():
        with session.locked():
            entered.set()
            assert stopped.wait(3), "network cancel waited behind emulator ownership"

    worker, errors = _worker(network_wait, "network-owner")
    assert entered.wait(2)
    provider.detach_all()
    _join(worker)
    assert stopped.is_set() and errors == []
    session.close()


@pytest.mark.parametrize("operation", ["read", "close", "detach"])
def test_raw_network_step_owns_emulator_but_allows_cancellation(operation):
    from types import SimpleNamespace
    from pokered_harness.ownership import EmulatorOwnershipError

    entered, release, attempted = (threading.Event() for _ in range(3))
    backend = SimpleNamespace(
        start_receiver=lambda **kwargs: None, stop=release.set,
        owner_scope=nullcontext,
    )
    provider = PyBoyLinkSession(network_backend=backend)
    emulator = _endpoint("network")
    session = Session(pyboy=emulator, symbols=_session()[0].symbols)
    session._lock = AttemptLock(session._lock, attempted)
    provider.attach(emulator)
    ticks = []

    def tick(*args):
        ticks.append(args)
        entered.set()
        assert release.wait(3), "network detach could not cancel raw tick"

    emulator.tick = tick
    raw, errors = _worker(lambda: provider.step(2), "network-raw")
    assert entered.wait(2)
    managed = None
    try:
        if operation == "close":
            with pytest.raises(SessionCleanupTimeoutError):
                session.close(timeout_s=.02)
            assert emulator.stop_calls == []
        elif operation == "read":
            managed, managed_errors = _worker(session.read_game_state, "managed")
            assert attempted.wait(2)
            assert managed.is_alive()
        else:
            provider.detach_all()
    finally:
        release.set()
        _join(raw)
        if managed is not None:
            _join(managed)
            assert managed_errors == []
    if operation == "close":
        assert len(errors) == 1 and isinstance(errors[0], SessionClosedError)
    elif operation == "detach":
        assert len(errors) == 1 and isinstance(errors[0], EmulatorOwnershipError)
    else:
        assert errors == []
    assert len(ticks) == (2 if operation == "read" else 1)
    provider.detach_all()
    session.close()
