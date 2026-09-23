from __future__ import annotations

import socket as _socket
import threading
import time as _time
from contextlib import suppress

import pytest

from pokered_harness.link.network_backend import NetworkBackendError
from pokered_harness.mcp_server import (
    LinkState,
    McpHarnessError,
    _close_serial_link,
    _disconnect_remote,
    dispatch_tool,
)
from tests._mcp_server_support import (
    _endpoint_session,
    _free_port,
    _link_state_with_peer,
)


def test_link_listen_rejects_live_previous_listener_worker():
    s, _ = _endpoint_session()
    link = LinkState()
    entered = threading.Event()
    release = threading.Event()

    def stale_worker():
        entered.set()
        release.wait(timeout=2.0)

    worker = threading.Thread(target=stale_worker, daemon=True)
    worker.start()
    assert entered.wait(timeout=1.0)
    link._listener_thread = worker
    try:
        with pytest.raises(McpHarnessError, match="cleanup is still in progress"):
            dispatch_tool(
                s,
                "link_listen",
                {"port": _free_port()},
                link=link,
            )
        assert link._listener_thread is worker
    finally:
        release.set()
        worker.join(timeout=1.0)


def test_link_listen_rejects_concurrent_call():
    s, _ = _endpoint_session()
    link = LinkState()
    port = _free_port()
    dispatch_tool(s, "link_listen", {"port": port}, link=link)
    try:
        with pytest.raises(ValueError, match="remote link busy"):
            dispatch_tool(s, "link_listen", {"port": _free_port()}, link=link)
    finally:
        dispatch_tool(s, "link_disconnect", {}, link=link)


def test_link_listen_reservation_survives_disconnect_race(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState()
    bind_entered = threading.Event()
    release_bind = threading.Event()
    listen_result: list[Exception] = []
    disconnect_result: list[Exception] = []
    reconnect_result: list[Exception] = []
    reconnect_called = threading.Event()

    class FakeListener:
        closed = False

        def close(self):
            self.closed = True

    listener = FakeListener()

    def blocked_bind(_host, _port):
        bind_entered.set()
        assert release_bind.wait(timeout=2.0)
        return listener

    monkeypatch.setattr("pokered_harness.mcp_server._bind_listener", blocked_bind)

    def blocked_reconnect(*_args, **_kwargs):
        reconnect_called.set()
        raise NetworkBackendError("reconnect should wait for listener teardown")

    monkeypatch.setattr("pokered_harness.mcp_server.NetworkBackend.connect", blocked_reconnect)

    def listen() -> None:
        try:
            dispatch_tool(s, "link_listen", {"port": _free_port()}, link=link)
        except Exception as exc:  # noqa: BLE001
            listen_result.append(exc)

    listen_worker = threading.Thread(target=listen)
    listen_worker.start()
    assert bind_entered.wait(timeout=1.0)

    def disconnect() -> None:
        try:
            dispatch_tool(s, "link_disconnect", {}, link=link)
        except Exception as exc:  # noqa: BLE001
            disconnect_result.append(exc)

    disconnect_worker = threading.Thread(target=disconnect)
    disconnect_worker.start()
    # The listener reservation makes the disconnect observe active work even
    # though the socket has not been returned by _bind_listener yet.
    disconnect_worker.join(timeout=1.0)
    assert not disconnect_worker.is_alive()
    assert disconnect_result == []

    def reconnect() -> None:
        try:
            dispatch_tool(
                s,
                "link_connect",
                {"host": "127.0.0.1", "port": _free_port(), "timeout_s": 0.01},
                link=link,
            )
        except Exception as exc:  # noqa: BLE001
            reconnect_result.append(exc)

    reconnect_worker = threading.Thread(target=reconnect)
    reconnect_worker.start()
    _time.sleep(0.05)
    assert reconnect_worker.is_alive()
    assert not reconnect_called.is_set()

    release_bind.set()
    listen_worker.join(timeout=2.0)
    reconnect_worker.join(timeout=2.0)
    assert not listen_worker.is_alive()
    assert not reconnect_worker.is_alive()
    assert listen_result and isinstance(listen_result[0], McpHarnessError)
    assert listen_result[0].code == "link_cancelled"
    assert listener.closed is True
    assert reconnect_result and isinstance(reconnect_result[0], McpHarnessError)
    assert reconnect_result[0].code == "link_connect_failed"
    assert reconnect_called.is_set()
    assert link.remote_mode == "idle"
    assert link._listener_thread is None
    assert link._listener_start_in_progress is False


def test_link_status_serializes_dead_remote_cleanup_before_reconnect(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    reconnect_attempted = threading.Event()
    reconnect_result: list[Exception] = []

    class DeadRemote:
        connected = False
        _reader_exc = RuntimeError("peer closed")

        def close(self):
            cleanup_entered.set()
            assert release_cleanup.wait(timeout=2.0)

    link.remote_mode = "connected"
    link.remote_link = DeadRemote()  # type: ignore[assignment]

    def reconnect() -> None:
        reconnect_attempted.set()
        try:
            dispatch_tool(
                s,
                "link_connect",
                {"host": "127.0.0.1", "port": _free_port()},
                link=link,
            )
        except Exception as exc:  # noqa: BLE001
            reconnect_result.append(exc)

    status_result: list[dict] = []

    def status() -> None:
        status_result.append(dispatch_tool(s, "link_status", {}, link=link))

    status_worker = threading.Thread(target=status)
    status_worker.start()
    assert cleanup_entered.wait(timeout=1.0)
    reconnect_worker = threading.Thread(target=reconnect)
    reconnect_worker.start()
    assert reconnect_attempted.wait(timeout=1.0)
    assert link.remote_mode == "disconnecting"
    reconnect_worker.join(timeout=1.0)
    assert not reconnect_worker.is_alive()
    assert reconnect_result
    assert isinstance(reconnect_result[0], McpHarnessError)
    assert reconnect_result[0].code == "link_busy"

    release_cleanup.set()
    status_worker.join(timeout=2.0)
    assert not status_worker.is_alive()
    assert status_result and status_result[0]["remote_mode"] == "idle"
    assert link.remote_mode == "idle"


def test_link_status_does_not_publish_stale_error_after_reconnect(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")

    class DeadRemote:
        connected = False
        _reader_exc = RuntimeError("old peer closed")

        def close(self):
            return None

    link.remote_mode = "connected"
    link.remote_link = DeadRemote()  # type: ignore[assignment]
    original_disconnect = _disconnect_remote
    cleanup_returned = threading.Event()
    release_status = threading.Event()
    status_result: list[dict] = []

    def delayed_disconnect(*args, **kwargs):
        original_disconnect(*args, **kwargs)
        cleanup_returned.set()
        assert release_status.wait(timeout=2.0)

    monkeypatch.setattr("pokered_harness.mcp_server._disconnect_remote", delayed_disconnect)

    status_worker = threading.Thread(
        target=lambda: status_result.append(dispatch_tool(s, "link_status", {}, link=link))
    )
    status_worker.start()
    assert cleanup_returned.wait(timeout=1.0)

    try:
        with pytest.raises(McpHarnessError) as connect_error:
            dispatch_tool(
                s,
                "link_connect",
                {
                    "host": "127.0.0.1",
                    "port": _free_port(),
                    "timeout_s": 0.05,
                },
                link=link,
            )
        assert connect_error.value.code == "link_connect_failed"
    finally:
        release_status.set()
        status_worker.join(timeout=2.0)
        s.close()

    assert not status_worker.is_alive()
    assert status_result
    assert status_result[0]["remote_mode"] == "idle"
    assert status_result[0]["remote_error"] is None


def test_link_connect_reservation_survives_disconnect_race(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    connect_entered = threading.Event()
    release_connect = threading.Event()
    connect_result: list[Exception] = []
    disconnect_result: list[Exception] = []

    def blocked_connect(*_args, cancel_event=None, **_kwargs):
        connect_entered.set()
        assert cancel_event is not None
        while not cancel_event.is_set():
            _time.sleep(0.01)
        # Hold the cancelled adapter long enough for link_disconnect to prove
        # it waits for the reserved operation to unwind before publishing
        # idle. This models a bounded native adapter teardown, not a socket
        # timeout or an unbounded production wait.
        assert release_connect.wait(timeout=2.0)
        raise NetworkBackendError("connection cancelled")

    monkeypatch.setattr("pokered_harness.mcp_server.NetworkBackend.connect", blocked_connect)

    def connect() -> None:
        try:
            dispatch_tool(
                s,
                "link_connect",
                {"host": "127.0.0.1", "port": _free_port(), "timeout_s": 30},
                link=link,
            )
        except Exception as exc:  # noqa: BLE001
            connect_result.append(exc)

    connect_worker = threading.Thread(target=connect)
    connect_worker.start()
    assert connect_entered.wait(timeout=1.0)

    disconnect_done = threading.Event()

    def disconnect() -> None:
        try:
            dispatch_tool(s, "link_disconnect", {}, link=link)
        except Exception as exc:  # noqa: BLE001
            disconnect_result.append(exc)
        finally:
            disconnect_done.set()

    disconnect_worker = threading.Thread(target=disconnect)
    disconnect_worker.start()
    # The connect reservation is held while the native adapter is unwinding,
    # so disconnect cannot incorrectly return from an idle snapshot.
    assert not disconnect_done.wait(timeout=0.05)

    release_connect.set()
    connect_worker.join(timeout=2.0)
    disconnect_worker.join(timeout=2.0)
    assert not connect_worker.is_alive()
    assert not disconnect_worker.is_alive()
    assert disconnect_result == []
    assert connect_result and isinstance(connect_result[0], McpHarnessError)
    assert connect_result[0].code == "link_cancelled"
    assert link.remote_mode == "idle"


def test_link_connect_refuses_when_pair_active():
    s_primary, _, _, _, link, _ = _link_state_with_peer()
    dispatch_tool(s_primary, "link_pair", {}, link=link)
    with pytest.raises(ValueError, match="in-process pair is active"):
        dispatch_tool(
            s_primary,
            "link_connect",
            {"host": "127.0.0.1", "port": _free_port()},
            link=link,
        )


def test_link_listen_rejects_non_loopback_host():
    """Remote TCP is deliberately constrained to the unauthenticated
    localhost-only contract until an authenticated transport exists."""
    s, _ = _endpoint_session()
    link = LinkState()
    with pytest.raises(McpHarnessError, match="localhost-only"):
        dispatch_tool(
            s,
            "link_listen",
            {"port": _free_port(), "host": "1.2.3.4"},
            link=link,
        )
    assert link.remote_mode == "idle"


def test_link_disconnect_is_idempotent():
    s, _ = _endpoint_session()
    link = LinkState()
    # No link set up → disconnect should be a safe no-op.
    result = dispatch_tool(s, "link_disconnect", {}, link=link)
    assert result == {"remote_mode": "idle"}


def test_second_disconnect_reports_when_first_teardown_is_still_running(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState()
    link._disconnecting = True
    link._disconnect_owner = -1
    link._disconnect_done.clear()
    monkeypatch.setattr("pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.01)

    with pytest.raises(McpHarnessError, match="still in progress") as exc_info:
        dispatch_tool(s, "link_disconnect", {}, link=link)
    assert exc_info.value.code == "link_teardown_timeout"

    link._disconnect_done.set()
    link._disconnecting = False


def test_second_disconnect_surfaces_first_teardown_error():
    s, _ = _endpoint_session()
    link = LinkState()
    link._disconnecting = True
    link._disconnect_owner = -1
    link._remote_error = RuntimeError("worker leaked")
    link._disconnect_done.set()

    with pytest.raises(McpHarnessError, match="worker leaked") as exc_info:
        dispatch_tool(s, "link_disconnect", {}, link=link)
    assert exc_info.value.code == "link_teardown_failed"

    link._disconnecting = False
    link._remote_error = None


def test_close_serial_link_uses_one_total_worker_deadline():
    join_timeouts: list[float] = []

    class SpyThread(threading.Thread):
        def join(self, timeout=None):
            assert timeout is not None
            join_timeouts.append(timeout)
            _time.sleep(0.01)

        def is_alive(self):
            return True

    class FakeLink:
        def close(self):
            return None

    fake = FakeLink()
    fake._reader = SpyThread()
    fake._edge_worker = SpyThread()
    assert _close_serial_link(fake, timeout_s=0.05) is False  # type: ignore[arg-type]
    assert len(join_timeouts) == 2
    assert join_timeouts[1] < join_timeouts[0]


def test_link_disconnect_cleans_stale_listener_worker_from_idle_state():
    s, _ = _endpoint_session()
    link = LinkState()
    entered = threading.Event()

    def stale_worker() -> None:
        entered.set()
        link._listener_cancel.wait(timeout=2.0)

    worker = threading.Thread(target=stale_worker)
    worker.start()
    assert entered.wait(timeout=1.0)
    link._listener_thread = worker

    dispatch_tool(s, "link_disconnect", {}, link=link)
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert link.remote_mode == "idle"
    assert link._listener_thread is None


def test_link_status_reports_disconnect_in_progress():
    s, _ = _endpoint_session()
    link = LinkState()
    link.remote_mode = "disconnecting"
    link._disconnecting = True
    status = dispatch_tool(s, "link_status", {}, link=link)
    assert status["remote_mode"] == "disconnecting"


def test_link_disconnect_stops_listener_worker():
    s, _ = _endpoint_session()
    link = LinkState()
    dispatch_tool(s, "link_listen", {"port": _free_port()}, link=link)
    worker = link._listener_thread
    assert worker is not None
    dispatch_tool(s, "link_disconnect", {}, link=link)
    assert not worker.is_alive()
    assert link.remote_mode == "idle"
    assert link._listener_thread is None


def test_link_disconnect_closes_accepted_transport_during_handshake():
    """Cancellation must close a connection before HELLO ownership transfers."""
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    port = _free_port()
    before = {
        id(thread)
        for thread in threading.enumerate()
        if thread.name == "NetworkBackend.reader" and thread.is_alive()
    }
    peer = None
    accepted_readers = []
    try:
        dispatch_tool(s, "link_listen", {"port": port}, link=link)
        # Hold the accepted socket open without sending HELLO.  This keeps the
        # worker in its cancellable handshake wait instead of letting EOF close
        # the transport and hide a teardown leak.
        peer = _socket.create_connection(("127.0.0.1", port), timeout=1.0)
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline:
            accepted_readers = [
                thread
                for thread in threading.enumerate()
                if (
                    thread.name == "NetworkBackend.reader"
                    and thread.is_alive()
                    and id(thread) not in before
                )
            ]
            if accepted_readers:
                break
            _time.sleep(0.01)
        assert accepted_readers

        dispatch_tool(s, "link_disconnect", {}, link=link)

        assert link.remote_mode == "idle"
        assert link._listener_thread is None
        assert all(not thread.is_alive() for thread in accepted_readers)
    finally:
        if peer is not None:
            peer.close()
        with suppress(McpHarnessError):
            dispatch_tool(s, "link_disconnect", {}, link=link)


def test_link_disconnect_detaches_native_backend_under_session_lock():
    """Restoring the serial backend must not race an in-flight session step."""
    s, _ = _endpoint_session()
    link = LinkState()
    detached = threading.Event()
    disconnect_done = threading.Event()

    class FakeRemote:
        def close(self):
            return None

    class FakeNetworkSession:
        def detach_all(self):
            detached.set()

    link.remote_mode = "connected"
    link.remote_link = FakeRemote()  # type: ignore[assignment]
    link.network_session = FakeNetworkSession()  # type: ignore[assignment]

    def disconnect() -> None:
        dispatch_tool(s, "link_disconnect", {}, link=link)
        disconnect_done.set()

    with s.locked():
        worker = threading.Thread(target=disconnect)
        worker.start()
        assert not detached.wait(timeout=0.1)
        assert not disconnect_done.is_set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert detached.is_set()
    assert disconnect_done.is_set()
