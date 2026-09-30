import asyncio
import json
import threading
import time

import pytest

from pokered_harness.mcp_timed_owner import TimedOwnerError
from pokered_harness.session import SessionError
from tests._mcp_timed_remote_support import (
    BOUND,
    PAIR_WORK_CAPACITY_S,
    assert_lateness_failure,
    block_owner,
    late_accounting,
    linked,
    owned,
    publish_late_accounting,
    result,
    result_until,
)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_cached_failure_survives_cancel_and_cleanup(
    tmp_path, monkeypatch, failure_snapshots, cleanup_fails
):
    with linked(tmp_path) as ((owner, session, game), _):
        before = owner.status()
        assert before["failure"] is None and before["last_failure"] is None
        status = publish_late_accounting(owner, game, failure_snapshots)
        assert_lateness_failure(status, generation=before["generation"], epoch=before["epoch"])
        historical = status["last_failure"]
        with monkeypatch.context() as patch:
            if cleanup_fails:

                def failed_unbind(*args, **kwargs):
                    raise RuntimeError("failure snapshot cleanup injection")

                patch.setattr(session, "unbind_timed_execution", failed_unbind)
                with pytest.raises(RuntimeError, match="failure snapshot cleanup injection"):
                    result(owner.cancel())
                failed = owner.status()
                assert failed["state"] == "cleanup_failed"
                assert failed["cleanup_pending"]
                assert failed["last_failure"] == historical
                assert failed["failure"] == status["failure"]
            else:
                result(owner.cancel())
        if cleanup_fails:
            result(owner.disconnect())
        cleaned = owner.status()
        assert cleaned["state"] == "idle" and not cleaned["cleanup_pending"]
        assert cleaned["generation"] == before["generation"] + 1
        assert cleaned["epoch"] is None and cleaned["accounting"] is None
        assert cleaned["failure"] is None
        assert cleaned["last_failure"] == historical
        # Previously returned immutable observations are never rewritten by cleanup.
        assert status["failure"] == historical["failure"]


def test_cached_failure_first_per_epoch_and_attributed_across_reconnect(
    tmp_path, failure_snapshots
):
    with linked(tmp_path) as ((owner, _, game), (peer, _, _)):
        first = publish_late_accounting(owner, game, failure_snapshots)
        historical = first["last_failure"]
        # Deliberately supply a different coordinator observation in the fake;
        # this isolates the owner's first-record rule, not coordinator behavior.
        second = publish_late_accounting(owner, game, failure_snapshots, cycles=48)
        assert second["accounting"]["failure"]["measured_lateness_half_cycles"] == 96
        assert second["failure"] == historical["failure"]
        assert second["last_failure"] == historical
        result(owner.disconnect())
        result(peer.disconnect())
        # The authored CPU loop clears SC normally before another attachment.
        result(owner.submit("step", 1, render=False))
        result(peer.submit("step", 1, render=False))
        listener = owner.listen("127.0.0.1", 0, "red")
        address = listener.ready.result(timeout=BOUND)
        assert address["last_failure"] == historical
        assert address["failure"] is None
        result(peer.connect("127.0.0.1", address["port"], "blue"))
        result(listener)
        connected = owner.status()
        assert connected["generation"] == first["generation"] + 1
        assert connected["epoch"] != first["epoch"]
        assert connected["failure"] is None
        assert connected["accounting"]["failure"] is None
        assert connected["last_failure"] == historical
        new_failure = publish_late_accounting(owner, game, failure_snapshots, cycles=48)
        assert_lateness_failure(
            new_failure, generation=connected["generation"], epoch=connected["epoch"], cycles=48
        )
        assert first["last_failure"] == historical


def test_cached_failure_first_observed_at_cleanup_before_unbind(
    tmp_path, monkeypatch, failure_snapshots
):
    from dataclasses import asdict

    with linked(tmp_path) as ((owner, session, game), _):
        endpoints, snapshots, reads = failure_snapshots
        endpoint = endpoints[game]
        before = owner.status()
        snapshot = late_accounting(endpoint.epoch.hex())
        expected = {
            "generation": before["generation"],
            "epoch": endpoint.epoch.hex(),
            "failure": asdict(snapshot.failure),
        }
        original_unbind = session.unbind_timed_execution
        unbind_observations = []

        def unbind(*args, **kwargs):
            cached = owner.status()
            unbind_observations.append(cached)
            assert threading.get_ident() == endpoint._owner
            assert cached["last_failure"] == expected
            assert cached["failure"] == expected["failure"]
            # Failure-only cleanup observation preserves the previous accounting.
            assert cached["accounting"] == before["accounting"]
            return original_unbind(*args, **kwargs)

        monkeypatch.setattr(session, "unbind_timed_execution", unbind)
        read_count = len(reads)
        snapshots[endpoint] = snapshot
        assert owner.status()["last_failure"] is None
        assert len(reads) == read_count
        cleaned = result(owner.disconnect())
        assert len(unbind_observations) == 1
        assert cleaned["last_failure"] == expected
        assert cleaned["failure"] is None and cleaned["accounting"] is None
        assert cleaned["generation"] == before["generation"] + 1
        assert cleaned["state"] == "idle"
        assert before["last_failure"] is None
        with pytest.raises(TypeError):
            cleaned["last_failure"]["failure"]["excess_half_cycles"] = 0


def test_cached_failure_attach_finally_preserves_setup_exception(
    tmp_path, monkeypatch, failure_snapshots
):
    from dataclasses import asdict

    from pokered_harness.link.timed_remote import TimedRemoteEndpoint
    from pokered_harness.link.timed_wire import ProtocolError

    with owned(tmp_path, "setup-owner") as (owner, _, game), owned(tmp_path, "setup-peer") as peer:
        endpoints, snapshots, _ = failure_snapshots
        original_attach = TimedRemoteEndpoint.attach
        original_snapshot = TimedRemoteEndpoint.snapshot
        failure = ProtocolError("injected attach failure after accounting becomes available")
        supplied, consumed = [], []
        generation = owner.generation
        listener_attached, release_failure = threading.Event(), threading.Event()
        ordering = []

        def attach(endpoint, current_game, *, deadline):
            answer = original_attach(endpoint, current_game, deadline=deadline)
            if current_game is game:
                listener_attached.set()
                assert release_failure.wait(BOUND), "test did not release setup failure injection"
                ordering.append("listener_failure")
                snapshot = late_accounting(endpoint.epoch.hex())
                supplied.append(snapshot)
                snapshots[endpoint] = snapshot
                raise failure
            return answer

        def snapshot(endpoint):
            observed = original_snapshot(endpoint)
            if endpoint in snapshots:
                # Make evidence available only to the attach-finally observation;
                # a later cleanup read cannot rescue a missing setup capture.
                consumed.append(snapshots.pop(endpoint))
            return observed

        monkeypatch.setattr(TimedRemoteEndpoint, "attach", attach)
        monkeypatch.setattr(TimedRemoteEndpoint, "snapshot", snapshot)
        listener = owner.listen("127.0.0.1", 0, "red")
        try:
            address = listener.ready.result(timeout=BOUND)
            connector = peer[0].connect("127.0.0.1", address["port"], "blue")
            assert listener_attached.wait(BOUND), "listener did not complete real attachment"
            connected = result(connector)
            assert connected["state"] == "connected"
            assert connector.phase == "done" and connector.future.exception() is None
            assert not listener.future.done()
            assert supplied == [] and consumed == []
            ordering.append("connector_setup_complete")
        finally:
            release_failure.set()
        with pytest.raises(ProtocolError) as raised:
            result(listener)
        assert raised.value is failure
        assert ordering == ["connector_setup_complete", "listener_failure"]
        result(owner.disconnect())
        assert len(supplied) == 1 and consumed == supplied
        status = owner.status()
        assert status["last_failure"] == {
            "generation": generation,
            "epoch": endpoints[game].epoch.hex(),
            "failure": asdict(supplied[0].failure),
        }
        assert status["failure"] is None and status["accounting"] is None
        assert status["state"] == "idle"
        assert listener.future.exception() is failure


@pytest.mark.asyncio
async def test_cached_failure_mcp_status_never_reads_native_while_blocked(
    tmp_path, monkeypatch, failure_snapshots
):
    from mcp import types

    from pokered_harness.mcp_server import build_server

    with linked(tmp_path) as ((owner, session, game), _):
        before = owner.status()
        status = publish_late_accounting(owner, game, failure_snapshots)
        assert_lateness_failure(status, generation=before["generation"], epoch=before["epoch"])
        server = build_server(session, timed_owner=owner, timed_policy=owner.policy)
        active, release = block_owner(owner)
        reads = tuple(failure_snapshots[2])
        tick_reads = []
        original_tick = session.current_tick

        def tick():
            tick_reads.append(threading.get_ident())
            return original_tick()

        try:
            with monkeypatch.context() as patch:
                patch.setattr(session, "current_tick", tick)
                tool = await asyncio.wait_for(
                    server.request_handlers[types.CallToolRequest](
                        types.CallToolRequest(
                            params=types.CallToolRequestParams(name="link_status", arguments={})
                        )
                    ),
                    1,
                )
                resource = await asyncio.wait_for(
                    server.request_handlers[types.ReadResourceRequest](
                        types.ReadResourceRequest(
                            params=types.ReadResourceRequestParams(uri="pokered://link-status")
                        )
                    ),
                    1,
                )
                assert not tool.root.isError
                tool_status = json.loads(tool.root.content[0].text)
                resource_status = json.loads(resource.root.contents[0].text)
                for response in (tool_status, resource_status):
                    assert response["failure"] == status["failure"]
                    assert response["last_failure"] == status["last_failure"]
                    assert response["transport"] == "timed" and response["active"]
                    assert response["current_tick"] == response["tick"] == before["tick"]
                assert not active.future.done()
                assert tuple(failure_snapshots[2]) == reads
                assert tick_reads == []
        finally:
            release.set()
        result(active)


def test_cached_status_is_immutable_and_available_while_busy(tmp_path):
    with owned(tmp_path) as (owner, _, _):
        initial = owner.status()
        assert initial["failure"] is None and initial["last_failure"] is None
        assert initial["accounting"] is None and initial["epoch"] is None
        request, release = block_owner(owner)
        try:
            status = owner.status()
            assert status["failure"] is None and status["last_failure"] is None
            assert not request.future.done()
            with pytest.raises(TypeError):
                status["generation"] = -1
        finally:
            release.set()
        assert result(request) != threading.get_ident()


def test_queue_bound_and_cancelled_request_never_runs(tmp_path):
    with owned(tmp_path, queue_capacity=1) as (owner, _, _):
        active, release = block_owner(owner)
        executed = threading.Event()
        try:
            queued = owner.submit(lambda session: executed.set())
            with pytest.raises(TimedOwnerError) as failure:
                result(owner.submit(lambda session: pytest.fail("overflow ran")))
            assert failure.value.code == "timed_queue_full"
            queued.cancel()
            assert not active.future.done()
        finally:
            release.set()
        result(active)
        result(owner.submit(lambda session: None))
        assert not executed.is_set()


def test_expired_queued_request_never_executes_later(tmp_path):
    with owned(tmp_path) as (owner, _, _):
        active, release = block_owner(owner)
        executed = threading.Event()
        try:
            queued = owner.submit(lambda session: executed.set(), deadline=time.monotonic() + 0.05)
            with pytest.raises(TimedOwnerError) as failure:
                result(queued)
            assert failure.value.code == "timed_deadline"
            assert not active.future.done()
        finally:
            release.set()
        result(active)
        result(owner.submit(lambda session: None))
        assert not executed.is_set()


def test_real_binding_rejects_foreign_mutation(tmp_path):
    with linked(tmp_path) as ((owner, session, game), _):
        before = (game.frame_count, game.mb.cpu.retired_instructions, tuple(game.events))
        for operation in (lambda: session.step(1), lambda: session.press("a")):
            with pytest.raises(SessionError):
                operation()
        assert (game.frame_count, game.mb.cpu.retired_instructions, tuple(game.events)) == before
        result(owner.submit("press", "a"))
        assert game.events


def test_queued_cancel_preserves_active_real_epoch(tmp_path):
    # Give only this authored paired workload a finite request capacity derived
    # from its two one-call owners. The ordinary result/cleanup bounds remain
    # BOUND; the pair's normal steps share one absolute request deadline.
    with linked(tmp_path, request_timeout=PAIR_WORK_CAPACITY_S) as (
        (left, _, game),
        (right, _, peer),
    ):
        active, release = block_owner(left)
        executed = threading.Event()
        try:
            queued = left.submit(lambda session: executed.set())
            queued.cancel()
            assert not active.future.done()
        finally:
            release.set()
        result(active)
        before = game.frame_count, peer.frame_count
        pair_deadline = time.monotonic() + PAIR_WORK_CAPACITY_S
        a = left.submit("step", 1, render=False, deadline=pair_deadline)
        b = right.submit("step", 1, render=False, deadline=pair_deadline)
        result_until(a, pair_deadline)
        result_until(b, pair_deadline)
        assert (game.frame_count, peer.frame_count) == (before[0] + 1, before[1] + 1)
        assert not executed.is_set()


def test_cleanup_failure_retains_binding_and_blocks_reconnect_until_retry(tmp_path, monkeypatch):
    with linked(tmp_path) as ((owner, session, game), _):
        original = session.unbind_timed_execution
        identities = []

        def fail(*args, **kwargs):
            identities.append(threading.get_ident())
            raise RuntimeError("injected detach failure")

        with monkeypatch.context() as patch:
            patch.setattr(session, "unbind_timed_execution", fail)
            with pytest.raises(RuntimeError, match="injected detach failure"):
                result(owner.disconnect())
            assert owner.status()["cleanup_pending"]
            assert game.mb.execution_before is not None
            with pytest.raises(TimedOwnerError):
                result(owner.listen("127.0.0.1", 0, "red"))
        result(owner.disconnect())
        assert not owner.status()["cleanup_pending"]
        assert game.mb.execution_before is None
        assert identities and all(identity != threading.get_ident() for identity in identities)
        assert session.unbind_timed_execution == original
