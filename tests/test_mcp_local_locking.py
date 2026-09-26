"""Deterministic contention checks for compound local MCP operations."""

import threading
from types import SimpleNamespace

import pytest

from pokered_harness.mcp_server import LinkState, McpHarnessError, dispatch_tool
from tests.test_link_pair import _make_session


class TrackedRLock:
    """Report a real failed acquisition, never infer blocking from timing."""

    def __init__(self):
        self.lock = threading.RLock()
        self.blocked = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if not blocking:
            acquired = self.lock.acquire(False)
            if not acquired:
                self.blocked.set()
            return acquired
        if self.lock.acquire(False):
            return True
        self.blocked.set()
        if timeout == -1:
            return self.lock.acquire()
        return self.lock.acquire(timeout=timeout)

    def release(self):
        self.lock.release()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.blocked.set()
            assert self.lock.acquire(timeout=5), "session lock remained held"
        return self

    def __exit__(self, *_args):
        self.lock.release()


def _setup():
    primary, _ = _make_session()
    peer, _ = _make_session()
    for session in (primary, peer):
        session._lock = TrackedRLock()
    return primary, peer, LinkState(peer_session=peer)


def _contend(primary, peer, side, operation, owner, entered, release):
    target = primary if side == "primary" else peer
    errors, results = [], []
    done = threading.Event()

    def run_owner():
        try:
            results.append(owner())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def run_contender():
        try:
            if operation == "step":
                target.step(1)
            elif operation == "read":
                target.read_game_state()
            else:
                target.save_state()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    owner_thread = threading.Thread(target=run_owner)
    contender = threading.Thread(target=run_contender)
    owner_thread.start()
    try:
        assert entered.wait(5), "owner did not reach pause"
        contender.start()
        assert target._lock.blocked.wait(5), "contender did not encounter held lock"
        assert not done.is_set()
    finally:
        release.set()
        owner_thread.join(5)
        if contender.ident is not None:
            contender.join(5)
    assert not owner_thread.is_alive() and not contender.is_alive()
    assert not errors
    return results[0]


@pytest.mark.parametrize("side", ["primary", "peer"])
@pytest.mark.parametrize("operation", ["step", "read", "save"])
@pytest.mark.parametrize("pause_at", ["provider", "bookkeeping"])
def test_local_step_holds_both_locks_through_bookkeeping(monkeypatch, side, operation, pause_at):
    primary, peer, link = _setup()
    entered, release = threading.Event(), threading.Event()

    def pause():
        entered.set()
        assert release.wait(5)

    def step(count, *, render):
        assert count == 2 and render is False
        if pause_at == "provider":
            pause()
        for session in (primary, peer):
            session._pyboy.tick(count, render=render)

    advance_tick = primary._advance_tick

    def paused_advance(count):
        # Pause inside the bookkeeping step (both owner locks are held here),
        # exposing lost ordinary increments if the compound operation drops
        # its lock.  The interleaved path advances the clock through
        # ``_advance_tick`` rather than ``reset_tick`` so that stepping does
        # not invalidate the battle/menu observations recorded while stepping.
        pause()
        return advance_tick(count)

    if pause_at == "bookkeeping":
        monkeypatch.setattr(primary, "_advance_tick", paused_advance)
    link.local_link_session = SimpleNamespace(step_interleaved=step)
    result = _contend(
        primary,
        peer,
        side,
        operation,
        lambda: dispatch_tool(primary, "link_step", {"count": 2}, link=link),
        entered,
        release,
    )
    assert result == {"primary_tick": 2, "peer_tick": 2}
    expected = [2, 2]
    if operation == "step":
        expected[side == "peer"] += 1
    assert [primary.current_tick(), peer.current_tick()] == expected


@pytest.mark.parametrize("side", ["primary", "peer"])
@pytest.mark.parametrize("operation", ["step", "read", "save"])
def test_local_unpair_holds_both_locks(monkeypatch, side, operation):
    primary, peer, link = _setup()
    entered, release = threading.Event(), threading.Event()

    def detach():
        entered.set()
        assert release.wait(5)
        assert link.local_link_session is provider

    provider = SimpleNamespace(detach_all=detach)
    link.local_link_session = provider
    result = _contend(
        primary,
        peer,
        side,
        operation,
        lambda: dispatch_tool(primary, "link_unpair", {}, link=link),
        entered,
        release,
    )
    assert result == {"paired": False}
    assert link.local_link_session is None


@pytest.mark.parametrize("operation", ["link_step", "link_unpair"])
def test_local_provider_exception_releases_both_locks(operation):
    primary, peer, link = _setup()

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider failed")

    provider = SimpleNamespace(step_interleaved=fail, detach_all=fail)
    link.local_link_session = provider
    if operation == "link_unpair":
        with pytest.raises(McpHarnessError, match="provider failed") as raised:
            dispatch_tool(primary, operation, {"count": 2}, link=link)
        assert raised.value.code == "link_teardown_failed"
    else:
        with pytest.raises(RuntimeError, match="provider failed"):
            dispatch_tool(primary, operation, {"count": 2}, link=link)
    assert link.local_link_session is provider
    acquired = []

    def check():
        for session in (primary, peer):
            success = session._lock.lock.acquire(blocking=False)
            acquired.append(success)
            if success:
                session._lock.lock.release()

    thread = threading.Thread(target=check)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert acquired == [True, True]
