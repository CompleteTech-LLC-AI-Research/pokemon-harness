from __future__ import annotations

import socket as _socket
import threading
import time as _time
from types import SimpleNamespace

import pytest

from pokered_harness.events import EventBus
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_core import SerialCore
from pokered_harness.mcp_server import (
    LinkState,
    McpHarnessError,
    _attach_network_backend,
    _error_code,
    _negotiate_network_clock_role,
    _wait_for_network_hello,
    dispatch_tool,
)
from pokered_harness.session import (
    Session,
)
from pokered_harness.symbols.loader import load_sym_text
from tests._mcp_server_support import (
    _connected_endpoint_pair,
    _disconnect_endpoint_pair,
    _endpoint_session,
    _free_port,
    _wait_remote_mode,
)

# -- remote (two-process) link tools ----------------------------------------
#
# These exercise the MCP surface for two-agent trading: link_listen spawns
# a background accept on a local port; link_connect from another "session"
# attaches; link_status reports the transitions; link_disconnect tears down.
#
# We use real localhost sockets via NetworkBackend, because the whole point
# of the tool is to wire up a native serial backend — stubbing it out would
# prove nothing.


def test_link_listen_then_connect_updates_status():
    s_listener, _ = _endpoint_session()
    s_connector, _ = _endpoint_session()
    link_l = LinkState(primary_version="blue")
    link_c = LinkState(primary_version="yellow")

    port = _free_port()
    listen_result = dispatch_tool(s_listener, "link_listen", {"port": port}, link=link_l)
    assert listen_result["remote_mode"] == "listening"
    assert listen_result["remote_role"] == "listener"
    assert listen_result["rom_version"] == "blue"

    # Poll link_status until bind completes (accept() is on a bg thread).
    for _ in range(100):
        status = dispatch_tool(s_listener, "link_status", {}, link=link_l)
        if status["remote_mode"] == "listening":
            break
        _time.sleep(0.01)
    assert status["remote_mode"] == "listening"

    # Connect from the other side.
    connect_result = dispatch_tool(
        s_connector,
        "link_connect",
        {"host": "127.0.0.1", "port": port, "rom_version": "yellow"},
        link=link_c,
    )
    assert connect_result["remote_mode"] == "connected"
    assert connect_result["remote_role"] == "connector"
    assert connect_result["peer_rom_version"] == "blue"

    _wait_remote_mode(link_l, "connected")
    status_l = dispatch_tool(s_listener, "link_status", {}, link=link_l)
    status_c = dispatch_tool(s_connector, "link_status", {}, link=link_c)
    assert status_l["remote_mode"] == "connected"
    assert status_l["remote_peer_rom_version"] == "yellow"
    assert status_c["remote_mode"] == "connected"
    assert status_c["remote_peer_rom_version"] == "blue"
    network_l = link_l.network_session
    network_c = link_c.network_session
    assert network_l is not None
    assert network_c is not None

    # Cleanup in the proper order — connector first drops the cable,
    # then listener notices.
    dispatch_tool(s_connector, "link_disconnect", {}, link=link_c)
    assert link_c.remote_mode == "idle"
    assert link_c.network_session is None
    assert network_c._pyboys == []
    assert s_connector._serial_hooks == []
    # Listener sees its link drop on the next status poll.
    for _ in range(100):
        status_l = dispatch_tool(s_listener, "link_status", {}, link=link_l)
        if status_l["remote_mode"] == "idle":
            break
        _time.sleep(0.01)
    assert status_l["remote_mode"] == "idle"
    assert link_l.network_session is None
    assert network_l._pyboys == []
    assert s_listener._serial_hooks == []
    dispatch_tool(s_listener, "link_disconnect", {}, link=link_l)


def test_link_frame_barrier_arms_and_clears_a_connected_remote_link():
    s_listener, link_l, s_connector, link_c = _connected_endpoint_pair()
    try:
        for session, link in ((s_listener, link_l), (s_connector, link_c)):
            network = link.network_session
            assert network is not None
            armed = dispatch_tool(session, "link_frame_barrier", {"enabled": True}, link=link)
            assert armed == {
                "enabled": True,
                "network_frame_barrier": True,
                "remote_mode": "connected",
            }, armed
            # The applied flag is read back from the session itself rather than
            # reported from the request, so a silently-ignored call fails here.
            assert network.network_frame_barrier() is True
            cleared = dispatch_tool(session, "link_frame_barrier", {"enabled": False}, link=link)
            assert cleared == {
                "enabled": False,
                "network_frame_barrier": False,
                "remote_mode": "connected",
            }, cleared
            assert network.network_frame_barrier() is False
    finally:
        _disconnect_endpoint_pair(s_listener, link_l, s_connector, link_c)


def test_link_frame_barrier_requires_a_connected_remote_link():
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    with pytest.raises(McpHarnessError, match="connected remote link") as exc_info:
        dispatch_tool(s, "link_frame_barrier", {"enabled": True}, link=link)
    assert exc_info.value.code == "not_connected"


def test_link_frame_barrier_rejects_a_non_boolean_argument():
    s_listener, link_l, s_connector, link_c = _connected_endpoint_pair()
    try:
        for value in ("yes", 1, None):
            with pytest.raises(McpHarnessError, match="boolean") as exc_info:
                dispatch_tool(s_listener, "link_frame_barrier", {"enabled": value}, link=link_l)
            assert exc_info.value.code == "invalid_argument"
        # A rejected call must not have applied anything.
        assert link_l.network_session.network_frame_barrier() is False
    finally:
        _disconnect_endpoint_pair(s_listener, link_l, s_connector, link_c)


def test_listener_rejects_bad_peer_and_accepts_next_peer():
    s_listener, _ = _endpoint_session()
    link = LinkState(primary_version="blue")
    port = _free_port()
    dispatch_tool(s_listener, "link_listen", {"port": port}, link=link)
    bad = _socket.create_connection(("127.0.0.1", port), timeout=1.0)
    bad.close()
    try:
        for _ in range(100):
            if link._listener_error is not None:
                break
            _time.sleep(0.01)
        assert link.remote_mode == "listening"

        good = NetworkBackend.connect("127.0.0.1", port, local_rom_version="yellow")
        # A raw NetworkBackend peer still needs its reader to consume the
        # listener's versioned HELLO; no emulator attachment is needed for
        # this handshake-only adversarial peer.
        assert SerialCore is not None
        good.start_receiver(SerialCore(False))
        assert good.wait_for_hello(timeout=1.0) == "blue"
        try:
            for _ in range(100):
                if link.remote_mode == "connected":
                    break
                _time.sleep(0.01)
            assert link.remote_mode == "connected"
            assert good.peer_rom_version == "blue"
        finally:
            good.close()
    finally:
        dispatch_tool(s_listener, "link_disconnect", {}, link=link)


def test_listener_cleans_native_hooks_when_adapter_install_fails(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    install_entered = threading.Event()
    deactivated: list[Session] = []

    def failing_native_attach(*_args, **_kwargs):
        install_entered.set()
        raise RuntimeError("native adapter install failed")

    monkeypatch.setattr(
        "pokered_harness.mcp_server._attach_network_backend",
        failing_native_attach,
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._deactivate_link_hooks",
        lambda session, _peer=None, **_kwargs: deactivated.append(session),
    )

    port = _free_port()
    dispatch_tool(s, "link_listen", {"port": port}, link=link)
    peer = _socket.create_connection(("127.0.0.1", port), timeout=1.0)
    try:
        assert install_entered.wait(timeout=2.0)
        deadline = _time.monotonic() + 2.0
        while not deactivated and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert deactivated == [s]
        assert link.remote_mode == "listening"
    finally:
        peer.close()
        dispatch_tool(s, "link_disconnect", {}, link=link)


def test_link_rejects_rom_version_override_for_primary_session():
    s, _ = _endpoint_session()
    link = LinkState(primary_version="blue")
    with pytest.raises(McpHarnessError, match="does not match") as exc_info:
        dispatch_tool(
            s,
            "link_listen",
            {"port": _free_port(), "rom_version": "yellow"},
            link=link,
        )
    assert exc_info.value.code == "rom_version_mismatch"
    assert link.remote_mode == "idle"


def test_error_code_maps_network_backend_failures():
    assert _error_code(NetworkBackendError("backend closed")) == "link_error"
    assert _error_code(NetworkBackendError("no peer response within 1s")) == "timeout"


def test_native_network_attach_honors_mcp_hello_deadline():
    core = SimpleNamespace(backend=None, SB=0, SC=0)
    core.set_SB = lambda value: setattr(core, "SB", value)
    core.set_SC = lambda value: setattr(core, "SC", value)
    core.apply_external_edge = lambda _peer_bit: False
    core.peek_out_bit = lambda: 1
    pyboy = SimpleNamespace(mb=SimpleNamespace(serial=core))
    pyboy.tick = lambda *_args, **_kwargs: True
    session = Session(
        pyboy=pyboy,
        symbols=load_sym_text("00:0000 Label\n"),
        event_bus=EventBus(),
    )
    local_sock, peer_sock = _socket.socketpair()
    backend = NetworkBackend(local_sock, local_rom_version="red")
    started = _time.monotonic()
    try:
        with pytest.raises(NetworkBackendError, match="within 0.05s"):
            _attach_network_backend(
                session,
                backend,
                is_internal_clock=False,
                local_rom_version="red",
                network_hello_timeout_s=0.05,
            )
        assert _time.monotonic() - started < 1.0
        assert backend._closed is True
        assert backend._reader is not None and not backend._reader.is_alive()
        assert backend._edge_worker is not None and not backend._edge_worker.is_alive()
    finally:
        backend.stop(timeout_s=1.0)
        peer_sock.close()


def test_network_clock_role_negotiation_holds_session_lock():
    """Native serial register negotiation is serialized with Session work."""

    class TrackingRLock:
        def __init__(self):
            self._lock = threading.RLock()
            self.owner: int | None = None

        def acquire(self, timeout=-1):
            acquired = (
                self._lock.acquire() if timeout == -1 else self._lock.acquire(timeout=timeout)
            )
            if acquired:
                self.owner = threading.get_ident()
            return acquired

        def release(self):
            self.owner = None
            self._lock.release()

    session, _ = _endpoint_session()
    lock = TrackingRLock()
    session._lock = lock  # type: ignore[assignment]
    observed: list[bool] = []

    class ProbeNetworkSession:
        def negotiate_network_clock_role(self, peer_rom_version):
            observed.append(lock.owner == threading.get_ident())
            return True

    result = _negotiate_network_clock_role(
        session,
        ProbeNetworkSession(),  # type: ignore[arg-type]
        "blue",
        timeout_s=1.0,
    )

    assert result is True
    assert observed == [True]


def test_native_network_hello_timeout_has_structured_timeout_code(monkeypatch):
    session, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    transport = SimpleNamespace(closed=False)

    def close_transport():
        transport.closed = True

    transport.close = close_transport
    monkeypatch.setattr(
        "pokered_harness.mcp_server._supports_bit_accurate_network",
        lambda _session: True,
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server.NetworkBackend.connect",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._attach_network_backend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            NetworkBackendError("peer HELLO not received within 0.05s")
        ),
    )

    with pytest.raises(McpHarnessError) as exc_info:
        dispatch_tool(
            session,
            "link_connect",
            {"host": "127.0.0.1", "port": _free_port(), "timeout_s": 0.1},
            link=link,
        )

    assert exc_info.value.code == "timeout"
    assert transport.closed is True
    assert link.remote_mode == "idle"


def test_network_hello_rejects_peer_that_closes_after_hello():
    class ClosedAfterHello:
        _hello_received = threading.Event()
        _reader_exc = None
        connected = False

        @property
        def peer_rom_version(self):
            return "blue"

    backend = ClosedAfterHello()
    backend._hello_received.set()
    with pytest.raises(NetworkBackendError, match="closed during HELLO"):
        _wait_for_network_hello(backend, None, 1.0)  # type: ignore[arg-type]


def test_link_disconnect_cancels_in_progress_connect(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    entered = threading.Event()
    result: list[Exception] = []

    def blocked_connect(*_args, cancel_event=None, **_kwargs):
        entered.set()
        assert cancel_event is not None
        while not cancel_event.is_set():
            _time.sleep(0.01)
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
            result.append(exc)

    worker = threading.Thread(target=connect, daemon=True)
    worker.start()
    assert entered.wait(timeout=1.0)
    dispatch_tool(s, "link_disconnect", {}, link=link)
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result and isinstance(result[0], McpHarnessError)
    assert result[0].code == "link_cancelled"
    assert link.remote_mode == "idle"


def test_link_connect_cleans_unpublished_resources_on_unexpected_failure(monkeypatch):
    s, _ = _endpoint_session()
    link = LinkState(primary_version="red")
    transport = type("FakeTransport", (), {})()
    transport.closed = False

    def close_transport():
        transport.closed = True

    transport.close = close_transport
    detached = threading.Event()

    class FakeNetworkSession:
        def __init__(self):
            self.calls = 0
            self.fail = True

        def detach_all(self):
            self.calls += 1
            detached.set()
            if self.fail:
                raise RuntimeError("detach failed")

    network_session = FakeNetworkSession()

    monkeypatch.setattr(
        "pokered_harness.mcp_server._supports_bit_accurate_network",
        lambda _session: True,
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server.NetworkBackend.connect",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "pokered_harness.mcp_server._attach_network_backend",
        lambda *_args, **_kwargs: network_session,
    )

    def fail_handshake(*_args, **_kwargs):
        raise RuntimeError("unexpected handshake fault")

    monkeypatch.setattr(
        "pokered_harness.mcp_server._wait_for_network_hello",
        fail_handshake,
    )

    with pytest.raises(RuntimeError, match="unexpected handshake fault"):
        dispatch_tool(
            s,
            "link_connect",
            {"host": "127.0.0.1", "port": _free_port()},
            link=link,
        )

    assert detached.is_set()
    assert transport.closed is True
    assert link.remote_mode == "disconnecting"
    assert link.remote_link is None
    assert link.network_session is None
    assert link._pending_network_session is network_session

    # The failed detach remains owned by LinkState rather than being reported
    # as a clean idle transition. A later disconnect retries the exact handle.
    network_session.fail = False
    assert dispatch_tool(s, "link_disconnect", {}, link=link) == {"remote_mode": "idle"}
    assert network_session.calls == 2
    assert link._pending_network_session is None
