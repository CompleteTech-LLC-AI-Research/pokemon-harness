import asyncio
import threading
import time

import pytest

from pokered_harness.mcp_timed_owner import TimedOwner, TimedOwnerError
from pokered_harness.session import SessionError
from tests._mcp_timed_remote_support import (
    BOUND,
    PAIR_WORK_CAPACITY_S,
    block_owner,
    linked,
    publish_late_accounting,
    result,
    result_until,
)


@pytest.mark.parametrize("operation", ["invalid_button", "load_state"])
def test_invalid_local_input_preserves_connected_epoch(tmp_path, operation):
    # The post-rejection liveness check runs two authored owners. Use the
    # same finite paired-work budget as the queued-cancellation regression;
    # input rejection itself still uses the ordinary result bound.
    with linked(tmp_path, request_timeout=PAIR_WORK_CAPACITY_S) as (
        (left, _, game),
        (right, _, peer),
    ):
        epoch = left.status()["epoch"]
        before = (game.frame_count, game.mb.cpu.retired_instructions, tuple(game.events))
        with pytest.raises((ValueError, SessionError)):
            if operation == "invalid_button":
                result(left.submit("press", "not-a-button"))
            else:
                result(left.submit("load_state", b""))
        assert (game.frame_count, game.mb.cpu.retired_instructions, tuple(game.events)) == before
        assert left.status()["epoch"] == epoch
        assert left.status()["admitting"]
        peer_frame = peer.frame_count
        pair_deadline = time.monotonic() + PAIR_WORK_CAPACITY_S
        a = left.submit("step", 1, render=False, deadline=pair_deadline)
        b = right.submit("step", 1, render=False, deadline=pair_deadline)
        result_until(a, pair_deadline)
        result_until(b, pair_deadline)
        assert game.frame_count == before[0] + 1
        assert peer.frame_count == peer_frame + 1


def test_stored_protocol_error_precedes_active_caller_cancellation(
    tmp_path, monkeypatch, failure_snapshots
):
    """Inject only a first wire terminal reason, never scheduler or CPU state."""
    from pokered_harness.link.timed_remote import TimedRemoteEndpoint
    from pokered_harness.link.timed_wire import ProtocolError

    endpoints = {}
    original_attach = TimedRemoteEndpoint.attach

    def observe_attach(endpoint, game, *, deadline):
        answer = original_attach(endpoint, game, deadline=deadline)
        endpoints[game] = endpoint
        return answer

    monkeypatch.setattr(TimedRemoteEndpoint, "attach", observe_attach)
    with linked(tmp_path) as ((owner, _, game), _):
        cached = publish_late_accounting(owner, game, failure_snapshots)
        historical = cached["last_failure"]
        entered, release = threading.Event(), threading.Event()
        failure = ProtocolError("first stored protocol failure before caller cancellation")

        def fail_on_owner(session):
            endpoint = endpoints[game]
            # Same explicit terminal-reason injection as the Session fixture;
            # this tests propagation identity, not malformed frame parsing.
            assert endpoint.channel._terminate(failure) is failure
            assert endpoint.channel.error is failure
            entered.set()
            assert release.wait(BOUND), "test did not release the terminal owner"
            return endpoint.channel.receive(deadline=time.monotonic() + BOUND)

        active = owner.submit(fail_on_owner)
        try:
            assert entered.wait(BOUND)
            active.cancel()
            with pytest.raises(ProtocolError) as raised:
                result(active)
            assert raised.value is failure
            assert owner.status()["last_failure"] == historical
        finally:
            release.set()
        result(owner.disconnect())
        with pytest.raises(ProtocolError) as raised:
            result(active)
        assert raised.value is failure
        assert owner.status()["last_failure"] == historical
        assert owner.status()["failure"] is None


@pytest.mark.asyncio
async def test_blocked_cancellation_keeps_queue_and_cleanup_deadlines_live(tmp_path, monkeypatch):
    """The raw future check cannot manufacture expiry through result()/wait()."""
    from pokered_harness.link.timed_remote import TimedRemoteEndpoint

    with linked(tmp_path) as ((owner, _, _), _):
        active, release_owner = block_owner(owner)
        entered, release_cancel = threading.Event(), threading.Event()
        original_cancel = TimedRemoteEndpoint.cancel
        executed = threading.Event()
        identities = []

        def blocked_cancel(endpoint):
            identities.append(threading.get_ident())
            entered.set()
            assert release_cancel.wait(BOUND), "test did not release cancellation worker"
            return original_cancel(endpoint)

        with monkeypatch.context() as patch:
            patch.setattr(TimedRemoteEndpoint, "cancel", blocked_cancel)
            queued = owner.submit(lambda session: executed.set(), deadline=time.monotonic() + 0.2)
            awaited = owner.submit(lambda session: executed.set(), deadline=time.monotonic() + 0.4)
            try:
                active.cancel()
                assert entered.wait(BOUND)
                # Use concurrent Future directly: only the supervisor can
                # settle this deadline while the native owner stays blocked.
                with pytest.raises(TimedOwnerError) as failure:
                    queued.future.result(timeout=1)
                assert failure.value.code == "timed_deadline"
                with pytest.raises(TimedOwnerError) as failure:
                    await asyncio.wait_for(awaited.wait(), 1)
                assert failure.value.code == "timed_deadline"
                cleanup = owner.disconnect()
                with pytest.raises(TimedOwnerError) as failure:
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.wrap_future(cleanup.future)),
                        owner.policy.close_timeout + 1,
                    )
                assert failure.value.code == "timed_deadline"
                assert cleanup.future.done() and not cleanup.future.cancelled()
                assert not release_cancel.is_set()
                started = time.monotonic()
                assert owner.close(timeout=0.01) is False
                assert time.monotonic() - started < 0.5
                assert owner.status()["cleanup_pending"]
                assert owner.status()["state"] != "idle"
                assert not executed.is_set()
                assert threading.get_ident() not in identities
                release_owner.set()
                # Even a completed native detach cannot hide the retained
                # cancellation worker from bounded close or cached status.
                assert owner.close(timeout=0.05) is False
                assert owner.status()["cleanup_pending"]
                assert owner.status()["state"] != "idle"
            finally:
                release_owner.set()
                release_cancel.set()
        assert owner.close(timeout=BOUND)


def test_blocked_cancellation_retains_failed_unbind_until_explicit_retry(tmp_path, monkeypatch):
    from pokered_harness.link.timed_remote import TimedRemoteEndpoint

    with linked(tmp_path) as ((owner, session, game), _):
        active, release_owner = block_owner(owner)
        entered, release_cancel = threading.Event(), threading.Event()
        original_cancel = TimedRemoteEndpoint.cancel
        failure = RuntimeError("retained unbind failure while cancellation worker is blocked")
        unbind_called = threading.Event()

        def blocked_cancel(endpoint):
            entered.set()
            assert release_cancel.wait(BOUND)
            return original_cancel(endpoint)

        def failed_unbind(*args, **kwargs):
            unbind_called.set()
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr(TimedRemoteEndpoint, "cancel", blocked_cancel)
            try:
                active.cancel()
                assert entered.wait(BOUND)
                with monkeypatch.context() as detach_patch:
                    detach_patch.setattr(session, "unbind_timed_execution", failed_unbind)
                    cleanup = owner.disconnect()
                    release_owner.set()
                    with pytest.raises(TimedOwnerError) as raised:
                        cleanup.future.result(timeout=BOUND)
                    assert raised.value.code in ("timed_deadline", "timed_cleanup_failed")
                    # The supervisor can settle the deadline before the owner
                    # publishes its failed cleanup after the same Event.wait.
                    with owner._condition:
                        assert owner._condition.wait_for(
                            lambda: owner.status()["state"] == "cleanup_failed", timeout=BOUND
                        ), "native owner did not publish retained cleanup failure"
                    assert owner.status()["state"] == "cleanup_failed"
                    assert owner.status()["cleanup_pending"]
                    assert game.mb.execution_before is not None
                    assert not unbind_called.is_set()
                    with pytest.raises(TimedOwnerError):
                        owner.listen("127.0.0.1", 0, "red")
                    release_cancel.set()
                    with pytest.raises(RuntimeError) as raised:
                        owner.disconnect().future.result(timeout=BOUND)
                    assert raised.value is failure
                    assert unbind_called.is_set()
                    assert owner.status()["state"] == "cleanup_failed"
                    assert game.mb.execution_before is not None
                assert owner.status()["state"] == "cleanup_failed"
                result(owner.disconnect())
                assert not owner.status()["cleanup_pending"]
                assert game.mb.execution_before is None
            finally:
                release_owner.set()
                release_cancel.set()


@pytest.mark.asyncio
async def test_server_outer_deadline_preserves_canonical_protocol_error():
    """A stalled request adapter isolates the server deadline boundary."""
    from concurrent.futures import Future

    from pokered_harness.link.timed_wire import ProtocolError
    from pokered_harness.mcp_server import _await_owner_request

    failure = ProtocolError("canonical protocol failure predates wrapper timeout")
    entered = asyncio.Event()
    cancelled = []

    class StalledRequest:
        def __init__(self):
            self.deadline = time.monotonic() + 0.05
            self.future = Future()
            self.future.set_exception(failure)

        async def wait(self):
            entered.set()
            await asyncio.Event().wait()

        def cancel(self):
            cancelled.append(True)

    request = StalledRequest()
    with pytest.raises(ProtocolError) as raised:
        await asyncio.wait_for(_await_owner_request(request), 1)
    assert entered.is_set()
    assert cancelled == [True]
    assert raised.value is failure
    assert request.future.exception() is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_protocol_error", [False, True])
async def test_request_wait_has_independent_deadline_without_supervisor(
    tmp_path, monkeypatch, stored_protocol_error
):
    """Deliberately disable supervision to isolate the async wait's own bound."""
    from contextlib import ExitStack

    from pokered_harness.link.timed_remote import TimedRemoteEndpoint
    from pokered_harness.link.timed_wire import ProtocolError

    supervisor_entered, release_supervisor = threading.Event(), threading.Event()
    owner_entered, release_owner = threading.Event(), threading.Event()
    original_supervise = TimedOwner._supervise
    original_attach = TimedRemoteEndpoint.attach
    endpoints = {}
    executed = threading.Event()
    failure = ProtocolError("stored wire failure survives independent async deadline")

    def blocked_supervisor(owner):
        supervisor_entered.set()
        assert release_supervisor.wait(BOUND), "test did not release deadline supervisor"
        original_supervise(owner)

    def observe_attach(endpoint, game, *, deadline):
        answer = original_attach(endpoint, game, deadline=deadline)
        endpoints[game] = endpoint
        return answer

    monkeypatch.setattr(TimedOwner, "_supervise", blocked_supervisor)
    monkeypatch.setattr(TimedRemoteEndpoint, "attach", observe_attach)
    with ExitStack() as stack:
        try:
            (owner, _, game), _ = stack.enter_context(linked(tmp_path))
            assert supervisor_entered.is_set()

            def blocked_operation(session):
                if stored_protocol_error:
                    assert endpoints[game].channel._terminate(failure) is failure
                owner_entered.set()
                assert release_owner.wait(BOUND), "test did not release native owner"

            active = owner.submit(
                blocked_operation,
                deadline=time.monotonic() + 0.2 if stored_protocol_error else None,
            )
            assert owner_entered.wait(BOUND)
            request = (
                active
                if stored_protocol_error
                else owner.submit(lambda session: executed.set(), deadline=time.monotonic() + 0.05)
            )
            assert not request.future.done()
            expected = ProtocolError if stored_protocol_error else TimedOwnerError
            with pytest.raises(expected) as raised:
                await asyncio.wait_for(request.wait(), 1)
            if stored_protocol_error:
                assert raised.value is failure
            else:
                assert raised.value.code == "timed_deadline"
                assert not active.future.done()
            assert request.future.done() and not request.future.cancelled()
            assert request.future.exception() is raised.value
            assert not executed.is_set()
            assert not release_supervisor.is_set()
            assert not release_owner.is_set()
        finally:
            release_owner.set()
            release_supervisor.set()
