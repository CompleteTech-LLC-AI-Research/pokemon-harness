"""Persistent MCP owner contracts; authored CPU evidence is explicitly scoped.

Barriers control admission and cancellation. The tiny authored cartridge follows
the Session routing fixture and supplies no commercial ROM or fabricated RAM.
"""

import asyncio
import json
import threading
import time
from contextlib import contextmanager

import pytest

from pokered_harness.link.timed_wire import Cancelled, ChannelClosed, DeadlineExceeded
from pokered_harness.mcp_timed_owner import TimedOwner, TimedOwnerError, TimedOwnerPolicy
from pokered_harness.session import Session, SessionError
from pokered_harness.symbols.loader import load_sym_text

BOUND = 5.0
PAIR_OWNER_COUNT, PAIR_CALLS_PER_OWNER = 2, 1
PAIR_WORK_CAPACITY_S = PAIR_OWNER_COUNT * PAIR_CALLS_PER_OWNER * (BOUND + 1)
POLICY = {
    "rearm_budget": 32,
    "rearm_instruction_cap": 16,
    "max_edge_lateness": 32,
    "quantum_cycles": 256,
    "operation_timeout": 3.0,
    "max_wait_attempts": 10000,
    "inbound_capacity": 1024,
    "queue_capacity": 2,
    "request_timeout": 5.0,
    "lock_timeout": 1.0,
    "close_timeout": 2.0,
}


@contextmanager
def authored(tmp_path, name, *, close=True):
    from pyboy import PyBoy
    from pyboy.core import cpu, mb, serial

    boot = bytearray(256)
    boot[:6] = bytes([0x31, 0x00, 0xD0, 0xC3, 0xFC, 0x00])
    boot[252:] = bytes([0x3E, 0x01, 0xE0, 0x50])
    cartridge = bytearray(0x8000)
    cartridge[0x100:0x103] = bytes([0xC3, 0x50, 0x01])
    cartridge[0x134:0x13B] = b"ROUTING"
    cartridge[0x150:0x15A] = bytes([0xF3, 0x3E, 0x91, 0xE0, 0x40, 0xAF, 0xE0, 0x02, 0x18, 0xFB])
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 0xFF
    rom, bootstrap = tmp_path / f"{name}.gb", tmp_path / f"{name}.boot"
    rom.write_bytes(cartridge)
    bootstrap.write_bytes(boot)
    game = PyBoy(str(rom), bootrom=str(bootstrap), window="null", sound_emulated=False)
    session = Session(pyboy=game, symbols=load_sym_text(""))
    try:
        assert type(game.mb) is mb.Motherboard
        assert type(game.mb.cpu) is cpu.CPU
        assert type(game.mb.serial) is serial.Serial
        assert game.mb.cpu.retired_instructions == 0
        game.set_emulation_speed(0)
        session.step(1, render=False)
        assert session.current_tick() == game.frame_count == 1
        yield session, game
    finally:
        if close and not session.closed:
            session.close(save=False, timeout_s=BOUND)


@contextmanager
def owned(tmp_path, name="owner", **overrides):
    with authored(tmp_path, name, close=False) as (session, game):
        owner = TimedOwner(session, TimedOwnerPolicy(**(POLICY | overrides)))
        try:
            yield owner, session, game
        finally:
            assert owner.close(timeout=BOUND), "persistent owner did not settle"


def result(request):
    return request.result(timeout=BOUND)


def result_until(request, deadline):
    """Collect a request against one absolute workload deadline."""
    remaining = deadline - time.monotonic()
    return request.result(timeout=max(1e-6, remaining))


@contextmanager
def linked(tmp_path, **overrides):
    with (
        owned(tmp_path, "listener", **overrides) as left,
        owned(tmp_path, "connector", **overrides) as right,
    ):
        listener = left[0].listen("127.0.0.1", 0, "red")
        address = listener.ready.result(timeout=BOUND)
        assert address["state"] == "listening"
        assert 1 <= address["port"] <= 65535
        connector = right[0].connect("127.0.0.1", address["port"], "blue")
        result(connector)
        result(listener)
        yield left, right


def block_owner(owner):
    entered, release = threading.Event(), threading.Event()

    def operation(session):
        entered.set()
        assert release.wait(BOUND), "test barrier was not released"
        return threading.get_ident()

    request = owner.submit(operation)
    assert entered.wait(BOUND)
    return request, release


@pytest.fixture
def failure_snapshots(monkeypatch):
    """Replace accounting only; real owner, binding and cleanup remain in use."""
    from pokered_harness.link.timed_remote import TimedRemoteEndpoint

    endpoints, snapshots, reads = {}, {}, []
    original_attach = TimedRemoteEndpoint.attach
    original_snapshot = TimedRemoteEndpoint.snapshot

    def attach(endpoint, game, *, deadline):
        answer = original_attach(endpoint, game, deadline=deadline)
        endpoints[game] = endpoint
        return answer

    def snapshot(endpoint):
        identity = threading.get_ident()
        reads.append(identity)
        assert identity == endpoint._owner, "foreign native snapshot read"
        if endpoint in snapshots:
            return snapshots[endpoint]
        return original_snapshot(endpoint)

    monkeypatch.setattr(TimedRemoteEndpoint, "attach", attach)
    monkeypatch.setattr(TimedRemoteEndpoint, "snapshot", snapshot)
    return endpoints, snapshots, reads


def late_accounting(epoch, *, cycles=40):
    """Produce failure via a real guard on a separate, deterministic coordinator."""
    from pokered_harness.link.emulated_time import EmulatedTimeCoordinator, EmulatedTimeError

    coordinator = EmulatedTimeCoordinator(
        epoch=epoch,
        raw_cpu_clock=100,
        rearm_budget=32,
        max_edge_lateness=32,
        quantum_cycles=256,
    )
    coordinator.record_peer_progress(epoch=epoch, sequence=1, committed_half_cycles=0)
    permit = coordinator.reserve(cycles)
    assert permit is not None
    coordinator.commit(permit, raw_cpu_clock=100 + cycles, instructions=1)
    with pytest.raises(EmulatedTimeError, match="^edge lateness exceeds bound$"):
        coordinator.receive_edge(epoch=epoch, sequence=1, at_half_cycle=0, payload=b"x")
    return coordinator.snapshot()


def publish_late_accounting(owner, game, failure_snapshots, *, cycles=40):
    endpoints, snapshots, _ = failure_snapshots
    endpoint = endpoints[game]
    snapshot = late_accounting(endpoint.epoch.hex(), cycles=cycles)

    def publish(session):
        snapshots[endpoint] = snapshot

    result(owner.submit(publish))
    return owner.status()


def assert_lateness_failure(status, *, generation, epoch, cycles=40):
    expected = {
        "reason": "edge lateness exceeds bound",
        "terminal_reason": "edge lateness exceeds bound",
        "phase": "receive_edge.lateness",
        "epoch": epoch,
        "local_half_cycles": cycles * 2,
        "peer_half_cycles": 0,
        "raw_cpu_clock": 100 + cycles,
        "observed_raw_cpu_clock": 100 + cycles,
        "edge_sequence": 1,
        "edge_at_half_cycle": 0,
        "measured_lateness_half_cycles": cycles * 2,
        "allowed_lateness_half_cycles": 64,
        "excess_half_cycles": cycles * 2 - 64,
        "emission_complete_half_cycle": -1,
    }
    assert status["failure"] == expected
    assert status["accounting"]["failure"] == expected
    assert status["last_failure"] == {
        "generation": generation,
        "epoch": epoch,
        "failure": expected,
    }
    with pytest.raises(TypeError):
        status["failure"]["reason"] = "overwritten"
    with pytest.raises(TypeError):
        status["last_failure"]["generation"] = -1
    with pytest.raises(TypeError):
        status["last_failure"]["failure"]["excess_half_cycles"] = 0


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


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_real_partial_progress_active_interrupt_is_terminal(
    tmp_path, monkeypatch, interrupt, record_property
):
    """Real CPU progress; the owner-module clock for both owners is controlled.

    The unchanged two-second logical budget expires after active partial progress.
    Real Event/Future bounds still limit readiness and settlement; wire/native time
    and the separate wall-clock deadline regressions remain real. The cancel
    branch uses real time throughout; the deadline branch is not a wall-clock
    CPU-throughput assertion.
    """
    from types import SimpleNamespace

    from pokered_harness import mcp_timed_owner

    reach_capacity_s = PAIR_WORK_CAPACITY_S
    # This finite capacity only covers the known two-owner, one-call workload
    # reaching the real hook. It is not cancellation-throughput evidence: the
    # hook release, future settlement, cleanup, and logical 2s deadline retain
    # their existing independent bounds. The cancel variant gives its two
    # authored requests the same finite capacity; the deadline variant keeps
    # the explicit fake two-second request deadline below.
    link_overrides = {"request_timeout": reach_capacity_s} if interrupt == "cancel" else {}
    with linked(tmp_path, **link_overrides) as ((left, session, game), (right, _, peer)):
        entered, release = threading.Event(), threading.Event()
        start = game.frame_count
        retired = game.mb.cpu.retired_instructions
        progress = {}

        def step(current, label, current_game):
            progress[label] = (current_game.frame_count, current_game.mb.cpu.retired_instructions)
            try:
                return current.step(3, render=False)
            finally:
                progress[label] = (
                    current_game.frame_count,
                    current_game.mb.cpu.retired_instructions,
                )

        def hook(_context):
            if game.frame_count == start + 1 and not entered.is_set():
                entered.set()
                assert release.wait(BOUND)

        result(left.submit(lambda current: current.register_hook_at_address(0, 0x155, hook)))
        clock = SimpleNamespace(now=time.monotonic())
        if interrupt == "deadline":
            # Replace this module's binding, never the shared time module.
            monkeypatch.setattr(
                mcp_timed_owner, "time", SimpleNamespace(monotonic=lambda: clock.now)
            )
        deadline = clock.now + 2.0 if interrupt == "deadline" else None
        if interrupt == "deadline":
            assert left.policy.request_timeout == right.policy.request_timeout == POLICY[
                "request_timeout"
            ]
        reach_started = time.monotonic()
        reach_deadline = reach_started + reach_capacity_s
        active_deadline = reach_deadline if interrupt == "cancel" else deadline
        peer_deadline = reach_deadline if interrupt == "cancel" else None
        active = left.submit(step, "left", game, deadline=active_deadline)
        peer_work = right.submit(step, "right", peer, deadline=peer_deadline)
        try:
            try:
                reached = entered.wait(max(0.0, reach_deadline - time.monotonic()))
            finally:
                record_property("reach_duration_s", time.monotonic() - reach_started)
            try:
                assert reached, (
                    "authored CPU did not enter its second frame within "
                    f"{reach_capacity_s:g}s workload capacity"
                )
            except AssertionError as error:

                def diagnostic(owner, request, label):
                    return {
                        "phase": request.phase,
                        "done": request.future.done(),
                        "error": repr(request.future.exception())
                        if request.future.done()
                        else None,
                        "owner_progress": progress.get(label),
                        "status": dict(owner.status()),
                    }

                error.add_note(
                    f"start={start}, retired={retired}, "
                    f"left={diagnostic(left, active, 'left')!r}, "
                    f"right={diagnostic(right, peer_work, 'right')!r}"
                )
                raise
            assert game.frame_count == start + 1
            assert game.mb.cpu.retired_instructions > retired
            if interrupt == "cancel":
                assert active.phase == "running" and not active.future.done()
                assert not active.cancel_event.is_set()
                active.cancel()
            else:
                assert active.phase == "running" and not active.future.done()
                assert not active.cancel_event.is_set()
                assert active.deadline == deadline == clock.now + 2.0
                with left._condition:
                    clock.now = deadline
                    left._condition.notify_all()
                with pytest.raises(TimedOwnerError) as failure:
                    # Raw Future cannot initiate expiry via OwnerRequest.result:
                    # the independent supervisor must settle the active request.
                    active.future.result(timeout=BOUND)
                assert failure.value.code == "timed_deadline"
            assert not left.status()["admitting"]
        finally:
            release.set()
        with pytest.raises(TimedOwnerError) as failure:
            result(active)
        assert failure.value.code == (
            "timed_cancelled" if interrupt == "cancel" else "timed_deadline"
        )
        with pytest.raises((TimedOwnerError, Cancelled, ChannelClosed, DeadlineExceeded, OSError)):
            result(peer_work)
        result(left.disconnect())
        assert session.current_tick() == game.frame_count == start + 1
        assert left.status()["epoch"] is None
        assert game.mb.execution_before is None
        assert game.frame_count == start + 1
        assert peer.frame_count >= 1


def test_stale_generation_rejected_before_callable_runs(tmp_path):
    with owned(tmp_path) as (owner, _, _):
        generation = owner.status()["generation"]
        executed = threading.Event()
        with pytest.raises(TimedOwnerError) as failure:
            result(owner.submit(lambda session: executed.set(), generation=generation - 1))
        assert failure.value.code == "timed_stale_generation"
        assert not executed.is_set()


@pytest.mark.parametrize("field", list(POLICY))
def test_timed_environment_requires_every_policy_field(monkeypatch, field):
    from pokered_harness.mcp_server import load_timed_policy_from_env

    monkeypatch.setenv("POKERED_LINK_TRANSPORT", "timed")
    for name, value in POLICY.items():
        monkeypatch.setenv(f"POKERED_TIMED_{name.upper()}", str(value))
    monkeypatch.delenv(f"POKERED_TIMED_{field.upper()}")
    with pytest.raises((ValueError, SessionError)):
        load_timed_policy_from_env()


@pytest.mark.parametrize("transport", ["TIMED", "unknown", "", "timed "])
def test_unknown_transport_never_falls_back(monkeypatch, transport):
    from pokered_harness.mcp_server import load_timed_policy_from_env

    monkeypatch.setenv("POKERED_LINK_TRANSPORT", transport)
    with pytest.raises((ValueError, SessionError)):
        load_timed_policy_from_env()


@pytest.mark.parametrize("field", list(POLICY))
@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -1])
def test_policy_rejects_invalid_explicit_values(field, value):
    with pytest.raises((TypeError, ValueError)):
        TimedOwnerPolicy(**(POLICY | {field: value}))


@pytest.mark.parametrize("field", list(POLICY))
def test_policy_has_no_implicit_field_defaults(field):
    values = POLICY.copy()
    del values[field]
    with pytest.raises(TypeError):
        TimedOwnerPolicy(**values)


def test_valid_explicit_environment_and_legacy_default(monkeypatch):
    from pokered_harness.mcp_server import load_timed_policy_from_env

    monkeypatch.delenv("POKERED_LINK_TRANSPORT", raising=False)
    assert load_timed_policy_from_env() is None
    monkeypatch.setenv("POKERED_LINK_TRANSPORT", "timed")
    for name, value in POLICY.items():
        monkeypatch.setenv(f"POKERED_TIMED_{name.upper()}", str(value))
    policy = load_timed_policy_from_env()
    assert policy == TimedOwnerPolicy(**POLICY)


@pytest.mark.parametrize("operation", ["listen", "connect"])
def test_duplicate_connection_preserves_existing_epoch_and_execution(tmp_path, operation):
    with linked(tmp_path) as ((left, _, game), (right, _, peer)):
        epoch = left.status()["epoch"]
        with pytest.raises(TimedOwnerError) as failure:
            result(getattr(left, operation)("127.0.0.1", 12345, "red"))
        assert failure.value.code == "timed_busy"
        assert left.status()["epoch"] == epoch
        before = game.frame_count, peer.frame_count
        a, b = left.submit("step", 1, render=False), right.submit("step", 1, render=False)
        result(a)
        result(b)
        assert (game.frame_count, peer.frame_count) == (before[0] + 1, before[1] + 1)


@pytest.mark.asyncio
async def test_async_cancel_keeps_event_loop_responsive_during_endpoint_cleanup(
    tmp_path, monkeypatch
):
    from pokered_harness.link.timed_remote import TimedRemoteEndpoint

    with linked(tmp_path) as ((left, _, _), _):
        active, release_operation = block_owner(left)
        loop = asyncio.get_running_loop()
        cleanup_entered = loop.create_future()
        release_cleanup = threading.Event()
        original = TimedRemoteEndpoint.cancel
        identities = []

        def announce_cleanup():
            if not cleanup_entered.done():
                cleanup_entered.set_result(None)

        def observe_cancel(endpoint):
            identities.append(threading.get_ident())
            loop.call_soon_threadsafe(announce_cleanup)
            assert release_cleanup.wait(BOUND), "cancellation blocked the asyncio caller"
            return original(endpoint)

        with monkeypatch.context() as patch:
            patch.setattr(TimedRemoteEndpoint, "cancel", observe_cancel)
            try:
                active.cancel()
                await asyncio.wait_for(cleanup_entered, BOUND)
                assert identities and threading.get_ident() not in identities
                assert not left.status()["admitting"]
            finally:
                release_cleanup.set()
                release_operation.set()
        await left.disconnect().wait()


@pytest.mark.asyncio
async def test_mcp_tools_and_resource_share_persistent_native_owner(tmp_path, monkeypatch):
    from mcp import types

    from pokered_harness import mcp_server

    with linked(tmp_path) as ((left, session, game), (right, _, peer)):
        server = mcp_server.build_server(session, timed_owner=left, timed_policy=left.policy)
        assert server._pokered_timed_owner is left
        identities = []
        original_step, original_press = session.step, session.press
        original_resource = mcp_server.read_resource

        def step(*args, **kwargs):
            identities.append(threading.get_ident())
            return original_step(*args, **kwargs)

        def press(*args, **kwargs):
            identities.append(threading.get_ident())
            return original_press(*args, **kwargs)

        def read(*args, **kwargs):
            identities.append(threading.get_ident())
            return original_resource(*args, **kwargs)

        monkeypatch.setattr(session, "step", step)
        monkeypatch.setattr(session, "press", press)
        monkeypatch.setattr(mcp_server, "read_resource", read)

        async def call(name, arguments):
            response = await server.request_handlers[types.CallToolRequest](
                types.CallToolRequest(
                    params=types.CallToolRequestParams(name=name, arguments=arguments)
                )
            )
            assert not response.root.isError, response
            return response

        await call("press", {"button": "a", "duration": 2})
        before = game.frame_count, peer.frame_count
        await asyncio.gather(call("step", {"count": 1}), right.submit("step", 1).wait())
        assert (game.frame_count, peer.frame_count) == (before[0] + 1, before[1] + 1)
        resource = await server.request_handlers[types.ReadResourceRequest](
            types.ReadResourceRequest(
                params=types.ReadResourceRequestParams(uri="pokered://game-state")
            )
        )
        assert json.loads(resource.root.contents[0].text)
        assert len(identities) == 3 and len(set(identities)) == 1
        assert identities[0] != threading.get_ident()


def test_close_retains_busy_owner_until_retry_and_closes_session_on_owner(tmp_path, monkeypatch):
    with owned(tmp_path) as (owner, session, _):
        active, release = block_owner(owner)
        original_close = session.close
        identities = []

        def close(*args, **kwargs):
            identities.append(threading.get_ident())
            return original_close(*args, **kwargs)

        monkeypatch.setattr(session, "close", close)
        try:
            assert owner.close(timeout=0.01) is False
            assert not session.closed
            assert owner.status()["cleanup_pending"]
            assert not identities
        finally:
            release.set()
        assert owner.close(timeout=BOUND)
        assert session.closed
        assert identities and threading.get_ident() not in identities
        with pytest.raises(TimedOwnerError):
            result(active)


@pytest.mark.asyncio
async def test_mcp_cached_status_tool_and_resource_return_while_owner_blocked(tmp_path):
    from mcp import types

    from pokered_harness.mcp_server import build_server

    with owned(tmp_path) as (owner, session, _):
        server = build_server(session, timed_owner=owner, timed_policy=owner.policy)
        active, release = block_owner(owner)
        try:
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
            status = json.loads(resource.root.contents[0].text)
            assert status["transport"] == "timed" and status["active"]
            assert not active.future.done()
        finally:
            release.set()
        result(active)


@pytest.mark.parametrize("mismatch", ["session", "policy"])
def test_server_rejects_supplied_owner_identity_or_policy_mismatch(tmp_path, mismatch):
    from pokered_harness.mcp_server import McpHarnessError, build_server

    with (
        owned(tmp_path, "owned") as (owner, session, _),
        authored(tmp_path, "foreign") as (foreign, _),
    ):
        policy = TimedOwnerPolicy(**(POLICY | {"queue_capacity": 3}))
        with pytest.raises(McpHarnessError) as failure:
            build_server(
                foreign if mismatch == "session" else session,
                timed_owner=owner,
                timed_policy=policy if mismatch == "policy" else owner.policy,
            )
        assert failure.value.code == "invalid_timed_configuration"
        assert result(owner.submit(lambda current: current is session))


@pytest.mark.parametrize("mode", ["starting", "listening", "connected", "disconnecting"])
def test_server_rejects_prebuilt_active_legacy_link(tmp_path, mode):
    from pokered_harness.mcp_server import LinkState, McpHarnessError, build_server

    with owned(tmp_path) as (owner, session, _):
        link = LinkState()
        link.remote_mode = mode  # Explicit lifecycle fault injection, no transport claim.
        with pytest.raises(McpHarnessError) as failure:
            build_server(session, link=link, timed_owner=owner, timed_policy=owner.policy)
        assert failure.value.code == "invalid_timed_configuration"
        assert result(owner.submit(lambda current: current is session))


@pytest.mark.parametrize("operation", ["invalid_button", "load_state"])
def test_invalid_local_input_preserves_connected_epoch(tmp_path, operation):
    # The post-rejection liveness check runs two authored owners. Use the
    # same finite paired-work budget as the queued-cancellation regression;
    # input rejection itself still uses the ordinary result bound.
    with linked(tmp_path, request_timeout=PAIR_WORK_CAPACITY_S) as (
        (left, _, game), (right, _, peer)
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
