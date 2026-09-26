"""A terminal peer marker must fence further frame admission."""

from __future__ import annotations

import threading
import time

import pytest
from pyboy.core.serial import Serial

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._tcp_trade_peer import _peer_shutdown_sync

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("late_marker", ["ready", "release"])
def test_release_marker_fences_new_frames_and_finishes_admitted_turns(monkeypatch, late_marker):
    """Place a ready/release marker between a failed poll and owner progress."""
    leader, follower = NetworkBackend.pair()
    follower_finished = threading.Event()
    follower_released = threading.Event()
    results = {}
    attempts_after_release = []
    follower_finishes_after_release = []
    release_sent = {"leader": False, "follower": False}
    original_leader_poll = leader.poll_peer_sync
    original_follower_poll = follower.poll_peer_sync
    first_delayed_poll = True

    def leader_poll(sync_id=0):
        nonlocal first_delayed_poll
        delayed_marker = 21 if late_marker == "ready" else 22
        if sync_id == delayed_marker and first_delayed_poll:
            first_delayed_poll = False
            boundary = follower_released if late_marker == "ready" else follower_finished
            assert boundary.wait(3.0), "follower never reached the controlled boundary"
            # The real marker is already queued. This models it arriving
            # between a failed poll and the next owner action.
            return False
        return original_leader_poll(sync_id=sync_id)

    def follower_poll(sync_id=0):
        if sync_id == 22 and late_marker == "release" and release_sent["leader"]:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if original_follower_poll(sync_id=sync_id):
                    return True
                time.sleep(0.001)
            raise AssertionError("leader never published release")
        return original_follower_poll(sync_id=sync_id)

    monkeypatch.setattr(leader, "poll_peer_sync", leader_poll)
    monkeypatch.setattr(follower, "poll_peer_sync", follower_poll)
    for name, backend in (("leader", leader), ("follower", follower)):
        original_announce = backend.announce_sync

        def announce(sync_id=0, *, original=original_announce, name=name):
            original(sync_id=sync_id)
            if sync_id == 22:
                release_sent[name] = True
                if name == "follower":
                    follower_released.set()

        monkeypatch.setattr(backend, "announce_sync", announce)
        backend.start_receiver(Serial(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)

    def run(name, backend, is_leader):
        def step(count):
            assert count == 1
            if release_sent[name] and is_leader:
                attempts_after_release.append(name)
            if release_sent[name] and not is_leader:
                follower_finishes_after_release.append(name)
            backend.begin_frame_turn(leader=is_leader)
            backend.finish_frame_turn(
                leader=is_leader,
                progress_callback=lambda: backend.service_pending_edges(max_edges=1),
            )

        try:
            _peer_shutdown_sync(
                backend,
                step=step,
                backend_snapshot=backend.debug_snapshot,
                ready_sync_id=21,
                release_sync_id=22,
                progress_after_marker=lambda: step(1),
                frame_leader=is_leader,
                timeout=3.0,
            )
            results[name] = None
        except BaseException as exc:  # noqa: BLE001 - join and report owner failures
            results[name] = exc
        finally:
            backend.stop(timeout_s=1.0)
            if not is_leader:
                follower_finished.set()

    threads = [
        threading.Thread(target=run, args=("leader", leader, True), daemon=True),
        threading.Thread(target=run, args=("follower", follower, False), daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        assert all(not thread.is_alive() for thread in threads), results
        assert release_sent == {"leader": True, "follower": True}, results
        assert attempts_after_release == [], results
        assert results == {"leader": None, "follower": None}
        if late_marker == "ready":
            assert follower_finishes_after_release
        left, right = leader.debug_snapshot(), follower.debug_snapshot()
        assert left["frame_ticks_sent"] > 0
        assert (
            left["frame_ticks_sent"]
            == left["frame_dones_sent"]
            == left["frame_acks_received"]
            == right["frame_ticks_received"]
            == right["frame_dones_received"]
            == right["frame_acks_sent"]
        )
        for stats in (left, right):
            assert stats["pre_close_snapshot"]["pending_edge_requests"] == 0
            assert stats["pre_close_snapshot"]["reader_error"] is None
    finally:
        leader.stop(timeout_s=1.0)
        follower.stop(timeout_s=1.0)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=1.0)
                assert not thread.is_alive()


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_queued_shutdown_marker_does_not_hide_a_progress_failure(failure_type):
    """A received marker cannot excuse an unrelated owner exception."""
    marker_ready = False
    failure = failure_type("owner progress failed")

    class Backend:
        def wait_for_wire_idle(self, **kwargs):
            pass

        def announce_sync(self, **kwargs):
            pass

        def poll_peer_sync(self, **kwargs):
            return marker_ready

        def service_pending_edges(self, **kwargs):
            return 0

    def fail():
        nonlocal marker_ready
        marker_ready = True
        raise failure

    with pytest.raises(failure_type) as raised:
        _peer_shutdown_sync(
            Backend(),
            step=lambda count: None,
            backend_snapshot=dict,
            ready_sync_id=21,
            progress_after_marker=fail,
            timeout=1.0,
        )
    assert raised.value is failure
