"""Local admission respects remote ownership and bypass cancellation."""

import threading
from unittest.mock import Mock

import pytest

from pokered_harness import mcp_server
from pokered_harness.mcp_server import LinkState, McpHarnessError, dispatch_tool
from tests.test_mcp_local_runtime_contract import _sessions, _forbid_pair_mutation
from tests.test_mcp_local_locking import TrackedRLock
from tests.test_link_pair import _make_session
from tests.test_mcp_server import _free_port, _wait_remote_mode


@pytest.mark.parametrize("field,value", [
    ("remote_mode", "listening"), ("remote_mode", "connecting"),
    ("remote_mode", "connected"), ("_disconnecting", True),
    ("remote_link", object()), ("remote_endpoint", object()),
    ("network_session", object()), ("_listener_socket", object()),
    ("_listener_thread", object()),
])
def test_local_pair_rejects_remote_ownership_without_mutation(monkeypatch, field, value):
    primary, peer, link = _sessions()
    setattr(link, field, value)
    before = vars(link).copy()
    memories = [dict(s._pyboy.memory._m) for s in (primary, peer)]
    forbidden = _forbid_pair_mutation(monkeypatch, primary, peer)
    with pytest.raises(McpHarnessError) as raised:
        dispatch_tool(primary, "link_pair", {}, link=link)
    assert raised.value.code == ("link_busy" if field == "_disconnecting" else "remote_busy")
    forbidden.assert_not_called()
    assert vars(link) == before
    assert [dict(s._pyboy.memory._m) for s in (primary, peer)] == memories


@pytest.mark.parametrize("connected", [False, True])
def test_live_remote_mode_rejects_local_until_disconnect(connected):
    primary, peer, link = _sessions()
    other, _, other_link = _sessions()
    port = _free_port()
    try:
        dispatch_tool(primary, "link_listen", {"port": port}, link=link)
        if connected:
            dispatch_tool(other, "link_connect", {"host": "127.0.0.1", "port": port}, link=other_link)
            _wait_remote_mode(link, "connected")
        with pytest.raises(McpHarnessError) as raised:
            dispatch_tool(primary, "link_pair", {}, link=link)
        assert raised.value.code == "remote_busy"
        assert link.local_link_session is None
        dispatch_tool(primary, "link_disconnect", {}, link=link)
        assert dispatch_tool(primary, "link_pair", {}, link=link)["paired"]
        dispatch_tool(primary, "link_unpair", {}, link=link)
    finally:
        dispatch_tool(other, "link_disconnect", {}, link=other_link)
        dispatch_tool(primary, "link_disconnect", {}, link=link)


def test_pair_queued_behind_real_connect_rejects_after_connection(monkeypatch):
    primary, _, link = _sessions()
    other, _, other_link = _sessions()
    link._operation_lock = TrackedRLock()
    entered, release = threading.Event(), threading.Event()
    connect = mcp_server.NetworkBackend.connect
    errors = []
    port = _free_port()

    def pause_connect(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return connect(*args, **kwargs)

    def run(command, arguments):
        try:
            dispatch_tool(primary, command, arguments, link=link)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(mcp_server.NetworkBackend, "connect", pause_connect)
    connector = threading.Thread(target=run, args=("link_connect", {"host": "127.0.0.1", "port": port}))
    pairer = threading.Thread(target=run, args=("link_pair", {}))
    try:
        dispatch_tool(other, "link_listen", {"port": port}, link=other_link)
        connector.start()
        assert entered.wait(5)
        pairer.start()
        assert link._operation_lock.blocked.wait(5)
    finally:
        release.set()
        if connector.ident is not None:
            connector.join(5)
        if pairer.ident is not None:
            pairer.join(5)
        dispatch_tool(primary, "link_disconnect", {}, link=link)
        dispatch_tool(other, "link_disconnect", {}, link=other_link)
    assert not connector.is_alive() and not pairer.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], McpHarnessError)
    assert errors[0].code == "remote_busy"
    assert link.local_link_session is None


def test_disconnect_during_attach_rolls_back_real_backends_once(monkeypatch):
    primary, peer, link = _sessions()
    originals = [s._pyboy.mb.serial.backend for s in (primary, peer)]
    provider = mcp_server.PyBoyLinkSession.local()
    attach = provider.attach
    detach = Mock(wraps=provider.detach_all)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def pause_attach(pyboy):
        attach(pyboy)
        if pyboy is peer._pyboy:
            entered.set()
            assert release.wait(5)

    original_deactivate = mcp_server._deactivate_link_hooks

    def observe_cancellation(*args):
        cancelled.set()  # disconnect has changed generation before reaching here
        original_deactivate(*args)

    monkeypatch.setattr(provider, "attach", pause_attach)
    monkeypatch.setattr(provider, "detach_all", detach)
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)
    monkeypatch.setattr(mcp_server, "_deactivate_link_hooks", observe_cancellation)

    def pair():
        try:
            dispatch_tool(primary, "link_pair", {}, link=link)
        except BaseException as exc:
            errors.append(exc)

    pairer = threading.Thread(target=pair)
    disconnector = threading.Thread(target=lambda: dispatch_tool(primary, "link_disconnect", {}, link=link))
    pairer.start()
    try:
        assert entered.wait(5)
        disconnector.start()
        assert cancelled.wait(5), "disconnect was blocked by operation lock"
    finally:
        release.set()
        pairer.join(5)
        if disconnector.ident is not None:
            disconnector.join(5)
    assert not pairer.is_alive() and not disconnector.is_alive()
    assert len(errors) == 1 and errors[0].code == "link_cancelled"
    detach.assert_called_once_with()
    assert [s._pyboy.mb.serial.backend for s in (primary, peer)] == originals
    assert link.local_link_session is None and link.pair is None


def test_disconnect_cancels_injected_pair_without_discarding_preseed(monkeypatch):
    from pokered_harness.link.pair import LinkPair
    from tests.test_link_pair import FakeFactory

    # This test exercises the explicitly injected semantic compatibility pair.
    # Use the plain fake emulator shells so the production native-runtime guard
    # does not reject the deliberately diagnostic path before the race runs.
    primary, _ = _make_session()
    peer, _ = _make_session()
    link = LinkState(peer_session=peer, peer_version="blue")
    pair = LinkPair(primary, peer, version_primary="red", version_peer="blue",
                    bridge_factory=FakeFactory())
    link.pair = pair
    pair_method = pair.pair

    def raced_pair():
        pair_method()
        # Deterministic completed disconnect between work and publication.
        dispatch_tool(primary, "link_disconnect", {}, link=link)

    monkeypatch.setattr(pair, "pair", raced_pair)
    with pytest.raises(McpHarnessError) as raised:
        dispatch_tool(primary, "link_pair", {}, link=link)
    assert raised.value.code == "link_cancelled"
    assert link.pair is pair and not pair.paired
    assert link.local_link_session is None


@pytest.mark.parametrize("side", ["primary", "peer"])
@pytest.mark.parametrize("operation", ["step", "read", "save"])
def test_cancelled_attach_holds_both_locks_until_rollback_finishes(monkeypatch, side, operation):
    from tests.test_mcp_local_locking import _contend

    primary, peer, link = _sessions()
    for session in (primary, peer):
        session._lock = TrackedRLock()
    originals = [s._pyboy.mb.serial.backend for s in (primary, peer)]
    provider = mcp_server.PyBoyLinkSession.local()
    attach, detach = provider.attach, provider.detach_all
    entered, release = threading.Event(), threading.Event()

    def cancel_after_attach(pyboy):
        attach(pyboy)
        if pyboy is peer._pyboy:
            dispatch_tool(primary, "link_disconnect", {}, link=link)

    def paused_detach():
        entered.set()
        assert release.wait(5)
        detach()

    detach_spy = Mock(side_effect=paused_detach)
    monkeypatch.setattr(provider, "attach", cancel_after_attach)
    monkeypatch.setattr(provider, "detach_all", detach_spy)
    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", lambda: provider)

    def pair():
        with pytest.raises(McpHarnessError) as raised:
            dispatch_tool(primary, "link_pair", {}, link=link)
        assert raised.value.code == "link_cancelled"

    _contend(primary, peer, side, operation, pair, entered, release)
    detach_spy.assert_called_once_with()
    assert [s._pyboy.mb.serial.backend for s in (primary, peer)] == originals
    assert link.local_link_session is None
