"""Native-local custody controls. Authored only; synthetic errors are not SIGINT."""

from contextlib import contextmanager
import threading
from unittest.mock import Mock

import pytest

from pokered_harness import mcp_server
from pokered_harness.mcp_server import McpHarnessError
from tests._mcp_native_local_custody_support import (
    CHECKPOINTS,
    ERRORS,
    RecordingLock,
    SyntheticControl,
    assert_terminal,
    branch_fault,
    command,
    outcome,
    owned_actors,
    resources,
)
from tests.test_mcp_local_runtime_contract import _sessions


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
@pytest.mark.parametrize("error_type", ERRORS)
def test_native_unwind_custody(monkeypatch, checkpoint, error_type):
    error = error_type(checkpoint)
    primary, peer, link, provider, before, witnesses = branch_fault(
        monkeypatch, checkpoint, error
    )
    _, raised = outcome(lambda: command(primary, link, "link_pair"))
    assert witnesses == [checkpoint]
    assert raised is error
    assert any(frame.name == "fail" for frame in _traceback_frames(raised))
    # Common baseline/candidate negative oracle: not an import/join failure.
    assert_terminal(link)
    assert link._pending_local_link_session is None
    assert link.local_link_session is None
    assert provider.attached == ()
    assert resources(primary, peer) == before
    assert command(primary, link, "link_pair")["paired"]
    command(primary, link, "link_unpair")
    assert resources(primary, peer) == before


def _traceback_frames(error):
    import traceback

    return traceback.extract_tb(error.__traceback__)


@pytest.mark.parametrize("error_type", ERRORS)
def test_postcommit_failure_preserves_published_pair(monkeypatch, error_type):
    primary, peer, link = _sessions()
    before = resources(primary, peer)
    provider = mcp_server.PyBoyLinkSession.local()
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)
    state = link.state
    error = error_type("committed")
    hit = []

    @contextmanager
    def fail_after_commit():
        with state():
            yield
        attempt = getattr(link, "_local_setup_attempt", None)
        committed = getattr(attempt, "committed", False) or not link._local_pair_in_progress
        if not hit and link.local_link_session is provider and committed:
            hit.append(True)
            raise error

    monkeypatch.setattr(link, "state", fail_after_commit)
    _, raised = outcome(lambda: command(primary, link, "link_pair"))
    assert hit == [True] and raised is error
    assert_terminal(link)
    assert link.local_link_session is provider
    assert link._pending_local_link_session is None
    paired_backends = resources(primary, peer)
    command(primary, link, "link_disconnect")
    assert link.local_link_session is provider
    assert resources(primary, peer) == paired_backends
    command(primary, link, "link_unpair")
    assert resources(primary, peer) == before


@pytest.mark.parametrize("primary_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("failed_action", ["detach", "primary_hooks", "peer_hooks"])
@pytest.mark.parametrize("secondary_type", ERRORS)
@pytest.mark.parametrize("consumer", ["link_unpair", "link_disconnect"])
def test_cleanup_preserves_primary(
    monkeypatch, primary_type, failed_action, secondary_type, consumer
):
    primary_error = primary_type("attach primary")
    primary, peer, link, provider, before, witnesses = branch_fault(
        monkeypatch, "attach2", primary_error
    )
    secondary = secondary_type("cleanup secondary")
    called = []
    armed = True
    detach = provider.detach_all
    hook_methods = [session.deactivate_serial_hooks for session in (primary, peer)]

    def fail_once(label, function, *args, **kwargs):
        nonlocal armed
        called.append(label)
        if label == failed_action and armed:
            armed = False
            raise secondary
        return function(*args, **kwargs)

    monkeypatch.setattr(provider, "detach_all", lambda: fail_once("detach", detach))
    for label, session, method in zip(
        ["primary_hooks", "peer_hooks"], (primary, peer), hook_methods
    ):
        monkeypatch.setattr(
            session, "deactivate_serial_hooks",
            lambda *a, _label=label, _method=method, **k: fail_once(_label, _method, *a, **k),
        )
    _, raised = outcome(lambda: command(primary, link, "link_pair"))
    assert witnesses == ["attach2"]
    assert raised is primary_error  # intended identity oracle for secondary interruption
    assert all(label in called for label in ["detach", "primary_hooks", "peer_hooks"])
    assert_terminal(link)
    attempt = link._local_setup_attempt
    assert attempt is not None and attempt.pending
    assert secondary in attempt.secondary
    assert attempt.primary_error is primary_error
    assert any(frame.name == "fail" for frame in _traceback_frames(raised))
    with pytest.raises(McpHarnessError) as busy:
        command(primary, link, "link_pair")
    assert busy.value.code == "link_busy"
    command(primary, link, consumer)
    assert_terminal(link)
    assert link._local_setup_attempt is None
    assert provider.attached == () and resources(primary, peer) == before
    assert command(primary, link, "link_pair")["paired"]
    command(primary, link, "link_unpair")


@pytest.mark.parametrize("slot", range(10))
@pytest.mark.parametrize("error_type", ERRORS)
def test_hook_cleanup_continues_each_slot(monkeypatch, slot, error_type):
    primary, peer, _link = _sessions()
    calls = []
    error = error_type("slot")

    def invoke(label):
        calls.append(label)
        if len(calls) - 1 == slot:
            raise error

    for side, session in enumerate((primary, peer)):
        monkeypatch.setattr(
            session, "deactivate_serial_hooks",
            lambda *, timeout_s, s=side: invoke((s, "serial")),
        )
        monkeypatch.setattr(
            session, "deactivate_hooks_at",
            lambda symbol, *, timeout_s, s=side: invoke((s, symbol)),
        )
    errors = mcp_server._deactivate_link_hooks(primary, peer, catch_base_exceptions=True)
    assert errors == [error]
    assert calls == [
        (side, action)
        for side in (0, 1)
        for action in ("serial", *mcp_server._DIRECT_LINK_HOOKS)
    ]


def test_default_hook_policy_preserves_process_control(monkeypatch):
    primary, peer, _link = _sessions()
    error = SyntheticControl("default policy")
    monkeypatch.setattr(primary, "deactivate_serial_hooks", Mock(side_effect=error))
    _, raised = outcome(lambda: mcp_server._deactivate_link_hooks(primary, peer))
    assert raised is error


@pytest.mark.parametrize("placement", ["attach1", "attach2", "rollback"])
def test_live_attempt_ownership(monkeypatch, placement):
    primary, peer, link = _sessions()
    provider = mcp_server.PyBoyLinkSession.local()
    before = resources(primary, peer)
    attach = provider.attach
    detach = provider.detach_all
    entered, cancelled = threading.Event(), threading.Event()
    calls = []
    hooks = mcp_server._deactivate_link_hooks
    with owned_actors() as actors:
        release = actors.gate()

        def wrapped_attach(emulator):
            result = attach(emulator)
            calls.append(emulator)
            if placement == f"attach{len(calls)}":
                entered.set()
                assert release.wait(5)
            if placement == "rollback" and emulator is peer._pyboy:
                raise RuntimeError("force rollback")
            return result

        def wrapped_detach():
            if placement == "rollback":
                entered.set()
                assert release.wait(5)
            return detach()

        def observed_hooks(*args, **kwargs):
            if kwargs.get("custody") is not None:
                cancelled.set()
            return hooks(*args, **kwargs)

        monkeypatch.setattr(provider, "attach", wrapped_attach)
        monkeypatch.setattr(provider, "detach_all", wrapped_detach)
        monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)
        monkeypatch.setattr(mcp_server, "_deactivate_link_hooks", observed_hooks)
        actors.start("pairer", lambda: command(primary, link, "link_pair"))
        assert entered.wait(5)
        actors.start("disconnector", lambda: command(primary, link, "link_disconnect"))
        assert cancelled.wait(5)
        assert link._local_pair_in_progress and not link._local_setup_done.is_set()
        assert link._pending_local_link_session is provider
    assert actors.outcomes["pairer"][1] is not None
    assert actors.outcomes["disconnector"][1] is None
    if placement != "rollback":
        assert actors.outcomes["pairer"][1].code == "link_cancelled"
    assert_terminal(link)
    assert resources(primary, peer) == before and provider.attached == ()


@pytest.mark.parametrize("actor_error", [None, RuntimeError, KeyboardInterrupt])
def test_delayed_active_disconnect_resource_fence(monkeypatch, actor_error):
    primary, peer, link = _sessions()
    provider = mcp_server.PyBoyLinkSession.local()
    originals = resources(primary, peer)
    attach = provider.attach
    hooks = mcp_server._deactivate_link_hooks
    attached, delayed = threading.Event(), threading.Event()
    hook_actions = []
    token_snapshot = []
    error = actor_error("actor hook") if actor_error else None
    for side, session in enumerate((primary, peer)):
        method = session.deactivate_serial_hooks

        def track(*a, _side=side, _method=method, **k):
            hook_actions.append(_side)
            return _method(*a, **k)

        monkeypatch.setattr(session, "deactivate_serial_hooks", track)
    with owned_actors() as actors:
        release_attach, release_actor = actors.gate(), actors.gate()

        def pause_attach(emulator):
            result = attach(emulator)
            if emulator is peer._pyboy:
                attached.set()
                assert release_attach.wait(5)
            return result

        def delay_actor(*a, **k):
            custody = k.get("custody")
            if custody is not None:
                token_snapshot.append(custody)
                delayed.set()
                assert release_actor.wait(5)
                # The recursive real consumer must coalesce without invoking us again.
                command(primary, link, "link_disconnect")
                if error is not None:
                    raise error
            return hooks(*a, **k)

        monkeypatch.setattr(provider, "attach", pause_attach)
        monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)
        monkeypatch.setattr(mcp_server, "_deactivate_link_hooks", delay_actor)
        worker = actors.start("pairer", lambda: command(primary, link, "link_pair"))
        assert attached.wait(5)
        actors.start("old-disconnect", lambda: command(primary, link, "link_disconnect"))
        assert delayed.wait(5)
        release_attach.set()
        worker.join(5)
        assert not worker.is_alive()
        assert actors.outcomes["pairer"][1].code == "link_cancelled"
        assert link._local_setup_attempt.worker_finished
        assert not link._local_setup_done.is_set()
        for tool, arguments in [
            ("link_pair", {}),
            ("link_connect", {"host": "127.0.0.1", "port": 12345}),
            ("link_listen", {"port": 12345}),
        ]:
            with pytest.raises(McpHarnessError) as busy:
                command(primary, link, tool, arguments)
            assert busy.value.code in {"link_busy", "pair_active"}
        command(primary, link, "link_disconnect")  # second caller coalesces
        assert len(token_snapshot) == 1
    assert_terminal(link)
    assert resources(primary, peer) == originals
    if error is not None:
        assert actors.outcomes["old-disconnect"][1] is not None
        assert error in link._local_setup_attempt.secondary
        command(primary, link, "link_disconnect")
    else:
        assert actors.outcomes["old-disconnect"][1] is None
    # A real replacement has actual callbacks; an old token must perform NO actions.
    assert command(primary, link, "link_pair")["paired"]
    registrations, callbacks, deliveries = [], [], []
    for side, session in enumerate((primary, peer)):
        session.serial_hook("Serial_ExchangeBytes", lambda *_, s=side: deliveries.append(s))
        registrations.append(session._serial_hooks[-1][0])
        callbacks.extend(session._pyboy._hooks[(0, 0x216F)])
    current_backends = resources(primary, peer)
    actions_before = list(hook_actions)
    assert hooks(primary, peer, catch_base_exceptions=True, custody=token_snapshot[0]) == []
    assert hook_actions == actions_before
    assert resources(primary, peer) == current_backends
    assert link.local_link_session is provider
    assert all(state.active for state in registrations)
    for callback, context in callbacks:
        callback(context)
    assert deliveries == [0, 1]
    command(primary, link, "link_unpair")
    assert all(not state.active for state in registrations)
    for callback, context in callbacks:
        callback(context)
    assert deliveries == [0, 1]
    assert all((0, 0x216F) not in session._pyboy._hooks for session in (primary, peer))
    assert resources(primary, peer) == originals


@pytest.mark.parametrize(
    "boundary", ["after_attach", "before_lock_exit", "after_lock_exit", "before_publication"]
)
def test_cancel_at_boundary(monkeypatch, boundary):
    primary, peer, link = _sessions()
    provider = mcp_server.PyBoyLinkSession.local()
    before = resources(primary, peer)
    hit = []
    attach = provider.attach
    original_state = link.state
    lock = link._pair_lock
    outer_active = False

    def cancel():
        if not hit:
            hit.append(boundary)
            command(primary, link, "link_disconnect")

    def second(emulator):
        value = attach(emulator)
        if emulator is peer._pyboy and boundary == "after_attach":
            cancel()
        return value

    class BoundaryLock:
        def acquire(self, *a, **k):
            return lock.acquire(*a, **k)

        def release(self):
            lock.release()

        def __enter__(self):
            nonlocal outer_active
            lock.acquire()
            outer_active = True
            return self

        def __exit__(self, *_args):
            nonlocal outer_active
            if boundary == "before_lock_exit":
                cancel()
            lock.release()
            outer_active = False
            if boundary == "after_lock_exit":
                cancel()

    @contextmanager
    def state():
        if boundary == "before_publication" and provider.paired and not hit and not outer_active:
            # Both actual attach and pair-lock release have finished; the
            # next real state scope performs the final publication check.
            cancel()
        with original_state():
            yield

    monkeypatch.setattr(provider, "attach", second)
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)
    monkeypatch.setattr(link, "_pair_lock", BoundaryLock())
    monkeypatch.setattr(link, "state", state)
    _, error = outcome(lambda: command(primary, link, "link_pair"))
    assert hit == [boundary]
    assert isinstance(error, McpHarnessError) and error.code == "link_cancelled"
    assert_terminal(link)
    assert provider.attached == () and resources(primary, peer) == before


@pytest.mark.parametrize("consumer", ["link_unpair", "link_disconnect"])
@pytest.mark.parametrize("failure", ["detach", "primary_hooks", "peer_hooks"])
def test_pending_retry(monkeypatch, consumer, failure):
    primary, peer, link = _sessions()
    before = resources(primary, peer)
    provider = mcp_server.PyBoyLinkSession.local()
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)
    command(primary, link, "link_pair")
    target = provider if failure == "detach" else (primary if failure == "primary_hooks" else peer)
    name = "detach_all" if failure == "detach" else "deactivate_serial_hooks"
    actual = getattr(target, name)
    error = RuntimeError("retain")
    monkeypatch.setattr(target, name, Mock(side_effect=error))
    # Explicit unpair owns teardown of a healthy published pair.
    with pytest.raises(McpHarnessError) as raised:
        command(primary, link, "link_unpair")
    assert raised.value.code == "link_teardown_failed"
    assert_terminal(link)
    attempt = link._local_setup_attempt
    assert attempt.pending and (attempt.primary_error is error or error in attempt.secondary)
    monkeypatch.setattr(target, name, actual)
    command(primary, link, consumer)
    assert_terminal(link)
    assert provider.attached == () and resources(primary, peer) == before
    assert link._local_setup_attempt is None


def test_native_success_roundtrip(monkeypatch):
    primary, peer, link = _sessions()
    before = resources(primary, peer)
    provider = mcp_server.PyBoyLinkSession.local()
    attach = Mock(wraps=provider.attach)
    monkeypatch.setattr(provider, "attach", attach)
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)
    assert command(primary, link, "link_pair")["paired"]
    assert attach.call_args_list[0].args == (primary._pyboy,)
    assert attach.call_args_list[1].args == (peer._pyboy,)
    assert attach.call_count == 2
    assert command(primary, link, "link_status")["paired"]
    ticks = [session.current_tick() for session in (primary, peer)]
    result = command(primary, link, "link_step", {"count": 1})
    assert result["primary_tick"] == ticks[0] + 1
    assert result["peer_tick"] == ticks[1] + 1
    backends = resources(primary, peer)
    command(primary, link, "link_disconnect")
    assert resources(primary, peer) == backends and link.local_link_session is provider
    command(primary, link, "link_unpair")
    assert_terminal(link)
    assert resources(primary, peer) == before
    assert command(primary, link, "link_pair")["paired"]
    command(primary, link, "link_unpair")


@pytest.mark.parametrize("prior", ["absent", None, 102.0, 120.0])
@pytest.mark.parametrize("error_type", [None, *ERRORS])
def test_cleanup_deadline_restoration(prior, error_type):
    from pokered_harness import mcp_server_local_custody as custody

    provider = mcp_server.PyBoyLinkSession.local()
    state = provider._cleanup_state
    if prior == "absent":
        if hasattr(state, "deadline"):
            del state.deadline
    else:
        state.deadline = prior
    seen = []
    error = error_type("nested") if error_type else None

    def nested():
        with custody.provider_deadline(provider, 110.0):
            seen.append(state.deadline)
            with custody.provider_deadline(provider, 105.0):
                seen.append(state.deadline)
                if error is not None:
                    raise error
            seen.append(state.deadline)

    _, raised = outcome(nested)
    effective = 110.0 if prior in ("absent", None) else min(110.0, prior)
    assert seen[:2] == [effective, min(effective, 105.0)]
    assert raised is error
    if error is None:
        assert seen[2] == effective
    assert hasattr(state, "deadline") == (prior != "absent")
    if prior != "absent":
        assert state.deadline == prior
    # A separate thread must see no inherited thread-local deadline.
    with owned_actors() as actors:
        actors.start("deadline-reader", lambda: getattr(state, "deadline", "absent"))
    assert actors.outcomes["deadline-reader"] == ("absent", None)


def _clock_modules(monkeypatch, now):
    from types import SimpleNamespace

    from pokered_harness import mcp_server_local_custody as custody
    from pokered_harness import mcp_server_remote as remote
    from pokered_harness import mcp_server_remote_contract as remote_contract
    from pokered_harness.link import _pyboy_link_session_lifecycle_mixin as lifecycle
    from pokered_harness.link import _pyboy_link_session_support as support

    clock = SimpleNamespace(monotonic=lambda: now[0])
    for module in (custody, lifecycle, support, remote, remote_contract):
        monkeypatch.setattr(module, "time", clock)
    return custody


@pytest.mark.parametrize("mode", ["available", "contended", "expired"])
def test_cleanup_pair_lock_deadline(monkeypatch, mode):
    primary, peer, link = _sessions()
    provider = mcp_server.PyBoyLinkSession.local()
    provider.attach(primary._pyboy)
    provider.attach(peer._pyboy)
    now = [100.0 if mode != "expired" else 105.0]
    custody = _clock_modules(monkeypatch, now)
    actual_lock = link._pair_lock
    pair_lock = RecordingLock(actual_lock, refuse=mode == "contended")
    link._pair_lock = pair_lock
    detach = Mock(wraps=provider.detach_all)
    monkeypatch.setattr(provider, "detach_all", detach)
    _, error = outcome(lambda: custody.detach_local(
        link, primary, provider, timeout_s=5, peer=peer, deadline=105.0
    ))
    assert pair_lock.timeouts == [max(0, 105.0 - now[0])]
    if mode == "available":
        assert error is None and detach.call_count == 1
        assert provider.attached == ()
    else:
        assert isinstance(error, TimeoutError)
        detach.assert_not_called()
        assert provider.attached == (primary._pyboy, peer._pyboy)
    assert pair_lock.acquired == pair_lock.released
    monkeypatch.setattr(link, "_pair_lock", actual_lock)
    now[0] = 100.0
    provider.detach_all()


@pytest.mark.parametrize("mode", ["available", "contended", "expired_after_owner_wait"])
def test_cleanup_provider_lock_deadline(monkeypatch, mode):
    primary, peer, link = _sessions()
    provider = mcp_server.PyBoyLinkSession.local()
    provider.attach(primary._pyboy)
    provider.attach(peer._pyboy)
    now = [100.0]
    custody = _clock_modules(monkeypatch, now)
    original_owner_group = custody.locked_sessions
    acquire_snapshots = []

    @contextmanager
    def consume_owner_wait(*a, **k):
        acquire_snapshots.append(k["timeout_s"])
        with original_owner_group(*a, **k):
            now[0] = 105.0 if mode == "expired_after_owner_wait" else 104.0
            yield

    monkeypatch.setattr(custody, "locked_sessions", consume_owner_wait)
    operation_lock = provider._operation_lock
    seen = []
    timed = RecordingLock(operation_lock, refuse=mode == "contended")
    monkeypatch.setattr(provider, "_operation_lock", timed)
    original_detach = provider.detach_all

    def spy():
        seen.append(provider._cleanup_state.deadline)
        return original_detach()

    monkeypatch.setattr(provider, "detach_all", spy)
    _, error = outcome(lambda: custody.detach_local(
        link, primary, provider, timeout_s=5, peer=peer, deadline=105.0
    ))
    assert acquire_snapshots == [5.0]
    if mode == "expired_after_owner_wait":
        assert seen == [] and timed.timeouts == []
        assert isinstance(error, TimeoutError)
    else:
        assert seen == [105.0]  # not a new now+2 window
        assert timed.timeouts and all(value == 1.0 for value in timed.timeouts)
        assert (error is None) == (mode == "available")
    assert timed.acquired == timed.released
    assert getattr(provider._cleanup_state, "deadline", None) is None
    monkeypatch.setattr(provider, "_operation_lock", operation_lock)
    now[0] = 100.0
    original_detach()


def test_legacy_detach_call_shape(monkeypatch):
    from types import SimpleNamespace

    from pokered_harness import mcp_server_local_custody as custody

    primary, peer, link = _sessions()
    adapter = SimpleNamespace(detach_all=Mock())
    custody.detach_local(link, primary, adapter, timeout_s=5, peer=peer)
    adapter.detach_all.assert_called_once_with()
    assert not hasattr(adapter, "_cleanup_state")


def test_expired_cleanup_retains_retryable_provider(monkeypatch):
    primary, peer, link, provider, before, witnesses = branch_fault(
        monkeypatch, "attach2", RuntimeError("primary")
    )
    now = [100.0]
    custody = _clock_modules(monkeypatch, now)
    detach = provider.detach_all

    def expired():
        now[0] = 106.0
        return detach()

    # Consume the reservation budget at the actual second attach.
    attach = provider.attach

    def consume(emulator):
        if emulator is peer._pyboy:
            now[0] = 106.0
        return attach(emulator)

    monkeypatch.setattr(provider, "attach", consume)
    spy = Mock(wraps=expired)
    monkeypatch.setattr(provider, "detach_all", spy)
    _, error = outcome(lambda: command(primary, link, "link_pair"))
    assert witnesses == ["attach2"] and isinstance(error, RuntimeError)
    spy.assert_not_called()  # expiry is checked before provider body
    assert_terminal(link)
    assert link._pending_local_link_session is provider
    assert link._local_setup_attempt.detach_pending
    now[0] = 110.0
    monkeypatch.setattr(provider, "detach_all", detach)
    command(primary, link, "link_disconnect")
    assert link._local_setup_attempt is None
    assert provider.attached == () and resources(primary, peer) == before
    assert custody is not None


@pytest.mark.parametrize("kind", ["pair", "provider"])
def test_actual_cleanup_lock_contention(kind):
    import time

    from pokered_harness import mcp_server_local_custody as custody

    primary, peer, link = _sessions()
    provider = mcp_server.PyBoyLinkSession.local()
    provider.attach(primary._pyboy)
    provider.attach(peer._pyboy)
    held = threading.Event()
    lock = link._pair_lock if kind == "pair" else provider._operation_lock
    with owned_actors() as actors:
        release = actors.gate()

        def contend():
            with lock:
                held.set()
                assert release.wait(5)

        actors.start("lock-owner", contend)
        assert held.wait(5)
        _, error = outcome(lambda: custody.detach_local(
            link, primary, provider, timeout_s=0.02, peer=peer,
            deadline=time.monotonic() + 0.02,
        ))
        assert isinstance(error, TimeoutError)
        assert provider.attached == (primary._pyboy, peer._pyboy)
    assert actors.outcomes["lock-owner"][1] is None
    provider.detach_all()
    assert provider.attached == ()


def test_pending_retry_uses_captured_peer(monkeypatch):
    primary, peer, link, provider, before, _witnesses = branch_fault(
        monkeypatch, "attach2", RuntimeError("attach")
    )
    detach = provider.detach_all
    monkeypatch.setattr(provider, "detach_all", Mock(side_effect=RuntimeError("retain")))
    outcome(lambda: command(primary, link, "link_pair"))
    assert link._local_setup_attempt.peer is peer
    unrelated, _other_peer, _other_link = _sessions()
    link.peer_session = unrelated
    unrelated_hooks = Mock(side_effect=AssertionError("cleaned replacement peer"))
    monkeypatch.setattr(unrelated, "deactivate_serial_hooks", unrelated_hooks)
    monkeypatch.setattr(provider, "detach_all", detach)
    command(primary, link, "link_disconnect")
    unrelated_hooks.assert_not_called()
    assert provider.attached == () and resources(primary, peer) == before
    assert_terminal(link)


def test_pending_retry_actor_coalesces_and_retains_until_release(monkeypatch):
    primary, peer, link, provider, before, _witnesses = branch_fault(
        monkeypatch, "attach2", RuntimeError("attach")
    )
    detach = provider.detach_all
    monkeypatch.setattr(provider, "detach_all", Mock(side_effect=RuntimeError("retain")))
    outcome(lambda: command(primary, link, "link_pair"))
    entered = threading.Event()
    with owned_actors() as actors:
        release = actors.gate()

        def pause():
            entered.set()
            assert release.wait(5)
            return detach()

        spy = Mock(wraps=pause)
        monkeypatch.setattr(provider, "detach_all", spy)
        actors.start("retry", lambda: command(primary, link, "link_disconnect"))
        assert entered.wait(5)
        command(primary, link, "link_disconnect")
        assert spy.call_count == 1
        assert link._local_pair_in_progress and not link._local_setup_done.is_set()
    assert actors.outcomes["retry"][1] is None
    assert_terminal(link)
    assert provider.attached == () and resources(primary, peer) == before


def test_published_unpair_uses_captured_peer(monkeypatch):
    primary, peer, link = _sessions()
    before = resources(primary, peer)
    command(primary, link, "link_pair")
    provider = link.local_link_session
    unrelated, _other_peer, _other_link = _sessions()
    link.peer_session = unrelated
    forbidden = Mock(side_effect=AssertionError("cleaned unrelated peer"))
    monkeypatch.setattr(unrelated, "deactivate_serial_hooks", forbidden)
    command(primary, link, "link_unpair")
    forbidden.assert_not_called()
    assert provider.attached == () and resources(primary, peer) == before
    assert_terminal(link)


@pytest.mark.parametrize("consumer", ["link_unpair", "link_disconnect"])
@pytest.mark.parametrize("release_side", ["primary", "peer", "both"])
def test_detach_primary_survives_actual_owner_unwind(monkeypatch, consumer, release_side):
    from tests._mcp_native_local_custody_support import OwnerReleaseFault

    primary, peer, link = _sessions()
    before = resources(primary, peer)
    command(primary, link, "link_pair")
    provider = link.local_link_session
    detach = provider.detach_all
    primed_hook_state = None
    primed_callback = None
    primed_deliveries = []
    if consumer == "link_disconnect":
        # Leave a genuinely active callback and hook obligation, not only
        # detach debt: both ways of disabling this hook fail during priming.
        primary.serial_hook("Serial_ExchangeBytes", lambda *_: primed_deliveries.append(True))
        primed_hook_state = primary._serial_hooks[-1][0]
        primed_callback = primary._pyboy._hooks[(0, 0x216F)][-1]
        hook_error = RuntimeError("prime hook cleanup")
        direct = primary.deactivate_hooks_at
        hook_faults = []

        def fail_direct(symbol, **kwargs):
            if symbol == "Serial_ExchangeBytes":
                hook_faults.append(symbol)
                raise hook_error
            return direct(symbol, **kwargs)

        with monkeypatch.context() as prime:
            prime.setattr(provider, "detach_all", Mock(side_effect=RuntimeError("prime detach")))
            serial_fault = Mock(side_effect=hook_error)
            prime.setattr(primary, "deactivate_serial_hooks", serial_fault)
            prime.setattr(primary, "deactivate_hooks_at", fail_direct)
            with pytest.raises(McpHarnessError) as primed:
                command(primary, link, "link_unpair")
        assert primed.value.code == "link_teardown_failed"
        assert serial_fault.call_count == 1 and hook_faults == ["Serial_ExchangeBytes"]
        assert link._pending_local_link_session is provider
        assert link._local_setup_attempt.hooks_pending
        assert any(error is hook_error for error in link._local_setup_attempt.secondary)
        assert primed_hook_state.active
        callback, context = primed_callback
        callback(context)
        assert primed_deliveries == [True]
    original = KeyboardInterrupt("provider primary E")
    release_errors, releases, adapters, hook_calls = [], [], [], []
    for side, session in zip(["primary", "peer"], (primary, peer)):
        if release_side in (side, "both"):
            error = RuntimeError(f"{side} owner release R")
            release_errors.append(error)
            adapter = OwnerReleaseFault(session._lock, error, releases)
            adapters.append(adapter)
            monkeypatch.setattr(session, "_lock", adapter)
        serial = session.deactivate_serial_hooks
        at_symbol = session.deactivate_hooks_at

        def serial_hook(*args, _side=side, _method=serial, **kwargs):
            hook_calls.append((_side, "serial"))
            return _method(*args, **kwargs)

        def direct_hook(symbol, *args, _side=side, _method=at_symbol, **kwargs):
            hook_calls.append((_side, symbol))
            return _method(symbol, *args, **kwargs)

        monkeypatch.setattr(session, "deactivate_serial_hooks", serial_hook)
        monkeypatch.setattr(session, "deactivate_hooks_at", direct_hook)
    witnesses = {}

    def fail_before_owner_exit():
        for adapter in adapters:
            adapter.armed = True
        try:
            raise original
        except BaseException as error:
            witnesses["body"] = error
            witnesses["traceback"] = error.__traceback__
            raise

    monkeypatch.setattr(provider, "detach_all", fail_before_owner_exit)
    _, raised = outcome(lambda: command(primary, link, consumer))
    assert witnesses["body"] is original
    assert raised is original  # combined-failure call-phase primary oracle
    traceback = raised.__traceback__
    while traceback is not None and traceback is not witnesses["traceback"]:
        traceback = traceback.tb_next
    assert traceback is witnesses["traceback"]
    assert len(releases) == len(release_errors)
    assert all(any(actual is expected for actual in releases) for expected in release_errors)
    attempt = link._local_setup_attempt
    assert all(any(actual is expected for actual in attempt.secondary) for expected in release_errors)
    assert attempt.primary_error is original or any(error is original for error in attempt.secondary)
    assert hook_calls == [
        (side, action)
        for side in ["primary", "peer"]
        for action in ("serial", *mcp_server._DIRECT_LINK_HOOKS)
    ]
    if primed_hook_state is not None:
        assert not primed_hook_state.active
        assert (0, 0x216F) not in primary._pyboy._hooks
        callback, context = primed_callback
        callback(context)
        assert primed_deliveries == [True]  # actual stale callback is now inert
    assert_terminal(link)
    assert attempt.detach_pending and link._pending_local_link_session is provider
    assert provider.attached == (primary._pyboy, peer._pyboy)
    assert all(adapter.acquired == adapter.released for adapter in adapters)
    # A DIFFERENT owned thread proves actual underlying release, not RLock
    # reentrancy in this test's thread. It never waits on the probe lock.
    def probe():
        results = []
        for adapter in adapters:
            acquired = adapter.lock.acquire(blocking=False)
            results.append(acquired)
            if acquired:
                adapter.lock.release()
        return results

    with owned_actors() as actors:
        actors.start("released-owner-probe", probe)
    assert actors.outcomes["released-owner-probe"] == ([True] * len(adapters), None)
    monkeypatch.setattr(provider, "detach_all", detach)
    command(primary, link, consumer)
    assert_terminal(link)
    assert link._local_setup_attempt is None
    assert link._pending_local_link_session is None
    assert provider.attached == () and resources(primary, peer) == before
    assert all(adapter.acquired == adapter.released for adapter in adapters)
