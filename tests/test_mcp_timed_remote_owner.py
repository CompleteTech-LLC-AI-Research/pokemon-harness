import asyncio
import json
import threading
import time

import pytest

from pokered_harness.link.timed_wire import Cancelled, ChannelClosed, DeadlineExceeded
from pokered_harness.mcp_timed_owner import TimedOwnerError, TimedOwnerPolicy
from pokered_harness.session import SessionError
from tests._mcp_timed_remote_support import (
    BOUND,
    PAIR_WORK_CAPACITY_S,
    POLICY,
    authored,
    block_owner,
    linked,
    owned,
    result,
)


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
            assert (
                left.policy.request_timeout
                == right.policy.request_timeout
                == POLICY["request_timeout"]
            )
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


@pytest.mark.asyncio
async def test_timed_discovery_omits_the_legacy_remote_frame_barrier(tmp_path):
    """The timed owner publishes only the operations it can actually perform.

    ``link_frame_barrier`` toggles the ROM-owned network frame barrier of a
    non-timed remote link.  The timed transport replaces that pacing model with
    its own policy and rejects the name unconditionally with
    ``timed_unsupported_tool``, so advertising it in timed ``tools/list`` would
    offer a client an operation this server can never perform.  This control
    reads the server's actual list handler and then calls the same name through
    the actual call handler, so re-advertising the legacy tool fails here
    instead of in the field.
    """
    import mcp.types as mcp_types

    from pokered_harness.mcp_server import build_server

    with owned(tmp_path) as (owner, session, _):
        server = build_server(session, timed_owner=owner, timed_policy=owner.policy)
        listed = await server.request_handlers[mcp_types.ListToolsRequest](
            mcp_types.ListToolsRequest()
        )
        names = {tool.name for tool in listed.root.tools}
        assert "link_frame_barrier" not in names, sorted(names)
        # The advertised timed surface still carries every operation the timed
        # transport does implement, including its own ``link_step``.
        assert {
            "step",
            "link_status",
            "link_listen",
            "link_connect",
            "link_disconnect",
            "link_step",
        } <= names, sorted(names)

        called = await server.request_handlers[mcp_types.CallToolRequest](
            mcp_types.CallToolRequest(
                params=mcp_types.CallToolRequestParams(
                    name="link_frame_barrier", arguments={"enabled": True}
                )
            )
        )
        assert called.root.isError is True
        payload = called.root.structuredContent
        assert payload is not None, called
        assert payload["error"]["code"] == "timed_unsupported_tool", payload


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
