"""Focused ownership/admission coverage for the managed core boundary."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from pokered_harness import mcp_server
from pokered_harness.ownership import EmulatorOwnershipError, owner_for, owner_group
from pokered_harness.session import Session, SessionCloseTimeout, locked_sessions
from tests.test_session import _session


def test_duplicate_session_claim_is_rejected_without_native_mutation() -> None:
    first, pyboy, _ = _session()
    try:
        with pytest.raises(EmulatorOwnershipError, match="live Session"):
            Session(pyboy=pyboy, symbols=first.symbols)
    finally:
        first.close()


def test_group_scope_orders_reversed_inputs_and_rejects_reverse_single_entry() -> None:
    primary, primary_pyboy, _ = _session()
    peer, peer_pyboy, _ = _session()
    try:
        primary_owner = owner_for(primary_pyboy)
        peer_owner = owner_for(peer_pyboy)
        with owner_group((peer_owner, primary_owner)):
            assert primary.current_tick() == 0
            assert peer.current_tick() == 0

        lower, higher = sorted((primary, peer), key=lambda item: item._owner.order)
        with (
            higher.locked(),
            pytest.raises(EmulatorOwnershipError, match="canonical order"),
            lower.locked(),
        ):
            pass

        with locked_sessions(peer, primary):
            primary.reset_tick(2)
            peer.reset_tick(3)
        assert (primary.current_tick(), peer.current_tick()) == (2, 3)
    finally:
        primary.close()
        peer.close()


def test_mutating_mcp_dispatch_fails_before_operation_lock_from_owner_scope() -> None:
    primary, _, _ = _session()
    peer, _, _ = _session()
    link = mcp_server.LinkState(peer_session=peer)
    try:
        with (
            primary.locked(),
            pytest.raises(
                EmulatorOwnershipError,
                match="outside an emulator ownership scope",
            ),
        ):
            mcp_server.dispatch_tool(
                primary,
                "link_step",
                {"count": 1},
                link=link,
            )
    finally:
        primary.close()
        peer.close()


def test_status_and_disconnect_are_safe_from_owner_scope() -> None:
    primary, _, _ = _session()
    peer, _, _ = _session()
    link = mcp_server.LinkState(peer_session=peer)
    try:
        with locked_sessions(primary, peer):
            status = mcp_server.dispatch_tool(primary, "link_status", {}, link=link)
            disconnected = mcp_server.dispatch_tool(
                primary,
                "link_disconnect",
                {},
                link=link,
            )
        assert status["paired"] is False
        assert disconnected == {"remote_mode": "idle"}
    finally:
        primary.close()
        peer.close()


def test_owner_claim_provider_is_single_writer_and_releases() -> None:
    pyboy = object()
    owner = owner_for(pyboy)

    # Plain object instances cannot be weak-referenced, so use a weakrefable
    # provider shell for the metadata claim contract.
    class Provider:
        pass

    first = Provider()
    second = Provider()
    owner.claim_provider(first)
    with pytest.raises(EmulatorOwnershipError, match="another provider"):
        owner.claim_provider(second)
    owner.release_provider(first)
    owner.claim_provider(second)
    owner.release_provider(second)


def test_bounded_owner_scope_rejects_context_only_lock() -> None:
    owner = owner_for(SimpleNamespace())
    original_lock = owner.lock

    class ContextOnlyLock:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    owner.lock = ContextOnlyLock()
    try:
        with (
            pytest.raises(
                EmulatorOwnershipError,
                match="does not support bounded acquisition",
            ),
            owner.access(timeout=0.01),
        ):
            raise AssertionError("an unbounded compatibility lock was entered")
    finally:
        owner.lock = original_lock


def test_local_link_step_times_out_before_provider_mutation(monkeypatch) -> None:
    primary, _, _ = _session()
    peer, _, _ = _session()
    link = mcp_server.LinkState(peer_session=peer)
    provider_calls: list[tuple[int, bool]] = []
    link.local_link_session = SimpleNamespace(
        step_interleaved=lambda count, render: provider_calls.append((count, render))
    )
    entered = threading.Event()
    release = threading.Event()

    def hold_primary() -> None:
        with primary.locked(timeout_s=1.0):
            entered.set()
            release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_primary, name="blocked-link-step-owner")
    holder.start()
    assert entered.wait(timeout=1.0)
    monkeypatch.setattr(mcp_server, "_DEFAULT_CLEANUP_TIMEOUT_S", 0.02)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            mcp_server._dispatch_link_tool(
                primary,
                "link_step",
                {"count": 1},
                link,
            )
    finally:
        release.set()
        holder.join(timeout=2.0)
    assert not holder.is_alive()
    assert time.monotonic() - started < 0.5
    assert provider_calls == []
    assert primary.current_tick() == 0
    assert peer.current_tick() == 0
    primary.close()
    peer.close()


def test_local_link_pair_times_out_before_attach_mutation(monkeypatch) -> None:
    primary, primary_pyboy, _ = _session()
    peer, peer_pyboy, _ = _session()
    link = mcp_server.LinkState(peer_session=peer)
    attached: list[object] = []

    class StubLocalLink:
        def attach(self, pyboy):
            attached.append(pyboy)

        def detach_all(self):
            attached.append("detach")

    local_link = StubLocalLink()
    monkeypatch.setattr(
        mcp_server.PyBoyLinkSession,
        "local",
        classmethod(lambda cls: local_link),
    )
    monkeypatch.setattr(
        mcp_server,
        "_supports_bit_accurate_network",
        lambda _session: True,
    )
    entered = threading.Event()
    release = threading.Event()

    def hold_primary() -> None:
        with primary.locked(timeout_s=1.0):
            entered.set()
            release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_primary, name="blocked-link-pair-owner")
    holder.start()
    assert entered.wait(timeout=1.0)
    monkeypatch.setattr(mcp_server, "_DEFAULT_CLEANUP_TIMEOUT_S", 0.02)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            mcp_server._dispatch_link_tool(
                primary,
                "link_pair",
                {},
                link,
            )
    finally:
        release.set()
        holder.join(timeout=2.0)
    assert not holder.is_alive()
    assert time.monotonic() - started < 0.5
    assert attached == []
    assert link.local_link_session is None
    assert primary_pyboy is not peer_pyboy
    primary.close()
    peer.close()


def test_link_unpair_times_out_before_provider_mutation(monkeypatch) -> None:
    primary, _, _ = _session()
    peer, _, _ = _session()
    link = mcp_server.LinkState(peer_session=peer)
    unpair_calls: list[bool] = []
    pair = SimpleNamespace(
        paired=True,
        peer=peer,
        unpair=lambda: unpair_calls.append(True),
    )
    link.pair = pair
    entered = threading.Event()
    release = threading.Event()

    def hold_primary() -> None:
        with primary.locked(timeout_s=1.0):
            entered.set()
            release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_primary, name="blocked-link-unpair-owner")
    holder.start()
    assert entered.wait(timeout=1.0)
    monkeypatch.setattr(mcp_server, "_DEFAULT_CLEANUP_TIMEOUT_S", 0.02)
    started = time.monotonic()
    try:
        with pytest.raises(mcp_server.McpHarnessError) as exc_info:
            mcp_server._dispatch_link_tool(
                primary,
                "link_unpair",
                {},
                link,
            )
        assert exc_info.value.code == "link_teardown_failed"
    finally:
        release.set()
        holder.join(timeout=2.0)
    assert not holder.is_alive()
    assert time.monotonic() - started < 0.5
    assert unpair_calls == []
    assert link.pair is pair
    primary.close()
    peer.close()


def test_reentrant_close_claim_is_retryable_after_callback_returns() -> None:
    session, pyboy, _ = _session()
    stops: list[bool] = []
    pyboy.stop = lambda save=False: stops.append(save)

    def close_from_callback(_context: object) -> None:
        with pytest.raises(SessionCloseTimeout, match="current emulator operation"):
            session.close(timeout_s=0.01)

    session.serial_hook("DisplayTextID", close_from_callback)
    try:
        pyboy.fire(0x02, 0x4A12)
        session.close()
        assert stops == [False]
    finally:
        if not session._stopped:
            session.close()


@pytest.mark.parametrize("operation", ["step", "load_state", "run_until_event"])
def test_attached_backend_can_reject_independent_session_operations(operation: str) -> None:
    session, pyboy, _ = _session()

    def reject(name: str) -> None:
        raise RuntimeError(f"provider owns {name}")

    pyboy.mb = SimpleNamespace(
        serial=SimpleNamespace(backend=SimpleNamespace(validate_session_operation=reject))
    )
    try:
        with pytest.raises(RuntimeError, match=f"provider owns {operation.split('_')[0]}"):
            if operation == "step":
                session.step()
            elif operation == "load_state":
                session.load_state(b"state")
            else:
                session.run_until_event("never", max_ticks=1)
    finally:
        session.close()
