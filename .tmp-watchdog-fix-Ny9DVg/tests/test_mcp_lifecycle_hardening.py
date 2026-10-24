"""Focused regressions for MCP/session lifecycle ownership.

These tests deliberately use ROM-free fakes.  Real-ROM protocol behavior is
covered by the link integration tiers; this file exercises the boundaries that
must remain safe when a request, worker, or asset fails.
"""

from __future__ import annotations

import threading
import time

import pytest

from pokered_harness.events import EventBus
from pokered_harness.mcp_server import (
    LinkState,
    McpHarnessError,
    _error_code,
    _error_reply,
    dispatch_tool,
    main,
)
from pokered_harness.session import (
    RomHashMismatch,
    RomNotFoundError,
    Session,
    SessionCloseTimeout,
    SessionLockTimeout,
    SymbolHashMismatch,
    SymbolNotFoundError,
)
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy

_LINK_SYM = """
00:216F Serial_ExchangeBytes
00:22C3 Serial_ExchangeNybble
00:2247 Serial_ExchangeLinkMenuSelection
00:22FA Serial_TryEstablishingExternallyClockedConnection
00:FFAA hSerialConnectionStatus
00:FFAC hSerialSendData
00:FFAD hSerialReceiveData
00:CC42 wLinkMenuSelectionSendBuffer
00:CC3D wLinkMenuSelectionReceiveBuffer
00:CC42 wSerialExchangeNybbleSendData
00:CC3E wSerialExchangeNybbleReceiveData
"""


def _session(*, pyboy: FakePyBoy | None = None) -> tuple[Session, FakePyBoy]:
    emulator = pyboy or FakePyBoy(DictMemory())
    return (
        Session(
            pyboy=emulator,
            symbols=load_sym_text(_LINK_SYM),
            event_bus=EventBus(),
        ),
        emulator,
    )


def test_deactivate_hooks_at_tolerates_absent_pyboy_hook() -> None:
    session, pyboy = _session()

    def _missing_hook(_bank: int, _addr: int) -> None:
        raise ValueError("Breakpoint not found for bank and addr")

    pyboy.hook_deregister = _missing_hook
    session.deactivate_hooks_at("Serial_ExchangeBytes")


class _BlockingPyBoy(FakePyBoy):
    def __init__(self) -> None:
        super().__init__(DictMemory())
        self.tick_entered = threading.Event()
        self.release_tick = threading.Event()
        self.stop_called = threading.Event()
        self.stop_during_tick = False
        self._activity_lock = threading.Lock()
        self._tick_active = False

    def tick(self, count: int = 1, render: bool = False) -> bool:
        del count, render
        with self._activity_lock:
            self._tick_active = True
        self.tick_entered.set()
        if not self.release_tick.wait(timeout=2.0):
            raise AssertionError("test did not release the emulator tick")
        with self._activity_lock:
            self._tick_active = False
        return True

    def stop(self, save: bool = False) -> None:
        del save
        with self._activity_lock:
            self.stop_during_tick = self._tick_active
        self.stop_called.set()
        self.stopped = True


class _BlockingStopPyBoy(FakePyBoy):
    def __init__(self) -> None:
        super().__init__(DictMemory())
        self.stop_entered = threading.Event()
        self.release_stop = threading.Event()

    def stop(self, save: bool = False) -> None:
        del save
        self.stop_entered.set()
        if not self.release_stop.wait(timeout=2.0):
            raise AssertionError("test did not release PyBoy.stop")
        self.stopped = True


def test_session_close_serializes_stop_after_an_inflight_tick() -> None:
    pyboy = _BlockingPyBoy()
    session, _ = _session(pyboy=pyboy)
    step_errors: list[Exception] = []
    close_errors: list[Exception] = []

    step_thread = threading.Thread(
        target=lambda: _capture_error(session.step, step_errors),
        name="test-session-step",
    )
    step_thread.start()
    assert pyboy.tick_entered.wait(timeout=1.0)

    close_thread = threading.Thread(
        target=lambda: _capture_error(session.close, close_errors),
        name="test-session-close",
    )
    close_thread.start()

    # Closing publishes the lifecycle transition immediately, but PyBoy.stop
    # must wait for the operation that already owns the emulator lock.
    assert not pyboy.stop_called.wait(timeout=0.1)
    assert session.closed is True

    pyboy.release_tick.set()
    step_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not step_thread.is_alive()
    assert not close_thread.is_alive()
    assert step_errors == []
    assert close_errors == []
    assert pyboy.stop_called.is_set()
    assert pyboy.stop_during_tick is False


def test_session_close_can_retry_after_inflight_operation_timeout() -> None:
    pyboy = _BlockingPyBoy()
    session, _ = _session(pyboy=pyboy)
    step_thread = threading.Thread(
        target=session.step, name="test-session-step-retry"
    )
    step_thread.start()
    assert pyboy.tick_entered.wait(timeout=1.0)

    with pytest.raises(SessionCloseTimeout):
        session.close(timeout_s=0.05)
    pyboy.release_tick.set()
    step_thread.join(timeout=2.0)
    assert not step_thread.is_alive()

    session.close(timeout_s=1.0)
    assert pyboy.stop_called.is_set()


def test_session_close_bounds_a_blocking_pyboy_stop_and_retries() -> None:
    pyboy = _BlockingStopPyBoy()
    session, _ = _session(pyboy=pyboy)

    started = time.monotonic()
    with pytest.raises(SessionCloseTimeout, match="PyBoy.stop"):
        session.close(timeout_s=0.05)
    assert time.monotonic() - started < 0.5
    assert session.closed is True
    assert pyboy.stop_entered.wait(timeout=0.5)

    with pytest.raises(SessionCloseTimeout):
        session.close(timeout_s=0.05)

    pyboy.release_stop.set()
    session.close(timeout_s=1.0)
    assert pyboy.stopped is True


def test_session_close_fails_closed_with_bounded_deadline() -> None:
    pyboy = _BlockingPyBoy()
    session, _ = _session(pyboy=pyboy)
    step_errors: list[Exception] = []
    step_thread = threading.Thread(
        target=lambda: _capture_error(session.step, step_errors),
        name="test-session-step-stubborn",
    )
    step_thread.start()
    assert pyboy.tick_entered.wait(timeout=1.0)

    started = time.monotonic()
    with pytest.raises(SessionCloseTimeout, match="shutdown deadline"):
        session.close(timeout_s=0.05)
    assert time.monotonic() - started < 0.5
    assert session.closed is True
    assert not pyboy.stop_called.is_set()

    pyboy.release_tick.set()
    step_thread.join(timeout=2.0)
    assert not step_thread.is_alive()
    assert step_errors == []


def test_session_locked_timeout_is_bounded() -> None:
    session, _ = _session()
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with session.locked():
            entered.set()
            release.wait(timeout=2.0)

    owner = threading.Thread(target=hold_lock, name="test-session-lock-owner")
    owner.start()
    assert entered.wait(timeout=1.0)

    started = time.monotonic()
    with pytest.raises(SessionLockTimeout, match="deadline"), session.locked(
        timeout_s=0.05
    ):
        raise AssertionError("the bounded lock should not be acquired")
    assert time.monotonic() - started < 0.5

    release.set()
    owner.join(timeout=2.0)
    assert not owner.is_alive()


def test_mcp_entrypoint_rejects_hash_bypass(monkeypatch) -> None:
    monkeypatch.setenv("POKERED_SKIP_SHA1", "1")
    with pytest.raises(SystemExit, match="rejected by the MCP production"):
        main()


def test_mcp_entrypoint_rejects_orphan_peer_symbol_pin(monkeypatch) -> None:
    for name in (
        "POKERED_SKIP_SHA1",
        "POKERED_PEER_ROM_PATH",
        "POKERED_PEER_SYM_PATH",
        "POKERED_PEER_ROM_SHA1",
        "POKERED_PEER_ROM_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POKERED_ROM_PATH", "/tmp/primary.gb")
    monkeypatch.setenv("POKERED_SYM_PATH", "/tmp/primary.sym")
    monkeypatch.setenv("POKERED_PEER_SYM_SHA1", "0" * 40)

    with pytest.raises(SystemExit, match="POKERED_PEER_SYM_SHA1 requires"):
        main()


def _capture_error(call, errors: list[Exception]) -> None:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - capture thread assertion aid
        errors.append(exc)


def test_asset_and_hash_failures_have_stable_mcp_codes(tmp_path) -> None:
    rom_path = tmp_path / "pokemon-red.gb"
    sym_path = tmp_path / "pokemon-red.sym"
    rom_path.write_bytes(b"rom bytes")
    sym_path.write_text("00:1234 Label\n", encoding="utf-8")

    with pytest.raises(RomNotFoundError) as missing_rom:
        Session.from_files(
            tmp_path / "missing.gb",
            sym_path,
            pyboy_factory=lambda _path: FakePyBoy(DictMemory()),
        )
    assert _error_code(missing_rom.value) == "rom_not_found"
    assert _error_reply(missing_rom.value).structuredContent["error"]["code"] == (
        "rom_not_found"
    )

    with pytest.raises(SymbolNotFoundError) as missing_sym:
        Session.from_files(
            rom_path,
            tmp_path / "missing.sym",
            pyboy_factory=lambda _path: FakePyBoy(DictMemory()),
        )
    assert _error_code(missing_sym.value) == "symbol_not_found"

    with pytest.raises(RomHashMismatch) as bad_rom_hash:
        Session.from_files(
            rom_path,
            sym_path,
            expected_rom_sha1="0" * 40,
            pyboy_factory=lambda _path: FakePyBoy(DictMemory()),
        )
    assert _error_code(bad_rom_hash.value) == "rom_hash_mismatch"

    with pytest.raises(SymbolHashMismatch) as bad_sym_hash:
        Session.from_files(
            rom_path,
            sym_path,
            expected_symbol_sha1="f" * 40,
            pyboy_factory=lambda _path: FakePyBoy(DictMemory()),
        )
    assert _error_code(bad_sym_hash.value) == "symbol_hash_mismatch"


class _StubbornRemote:
    def __init__(self, worker: threading.Thread) -> None:
        self._reader = worker
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def test_failed_remote_teardown_retains_worker_and_requires_retry(monkeypatch) -> None:
    session, pyboy = _session()
    session.serial_hook("Serial_ExchangeNybble", lambda _ctx: None)

    worker_release = threading.Event()
    worker = threading.Thread(
        target=lambda: worker_release.wait(timeout=2.0),
        name="test-stubborn-remote-worker",
    )
    worker.start()
    remote = _StubbornRemote(worker)
    link = LinkState()
    link.remote_mode = "connected"
    link.remote_link = remote  # type: ignore[assignment]
    link.remote_endpoint = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        "pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.01
    )

    try:
        with pytest.raises(McpHarnessError) as teardown_error:
            dispatch_tool(session, "link_disconnect", {}, link=link)
        assert teardown_error.value.code == "link_teardown_failed"
        assert link.remote_mode == "disconnecting"
        assert link._pending_remote_link is remote
        assert link.remote_link is None
        assert worker.is_alive()
        # The callback was physically deregistered even though the transport
        # worker still needs a later cleanup pass.
        assert pyboy._hooks == {}

        with pytest.raises(McpHarnessError) as transition_error:
            dispatch_tool(
                session,
                "link_listen",
                {"port": 1},
                link=link,
            )
        assert transition_error.value.code == "remote_busy"

        worker_release.set()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert dispatch_tool(session, "link_disconnect", {}, link=link) == {
            "remote_mode": "idle"
        }
        assert link._pending_remote_link is None
        assert remote.close_calls >= 2
    finally:
        worker_release.set()
        worker.join(timeout=1.0)


class _FailOnceNetworkSession:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = True

    def detach_all(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("serial backend detach failed")


def test_failed_network_session_detach_is_retriable(monkeypatch) -> None:
    session, pyboy = _session()
    session.serial_hook("Serial_ExchangeNybble", lambda _ctx: None)
    network_session = _FailOnceNetworkSession()
    link = LinkState()
    link.remote_mode = "connected"
    link.network_session = network_session  # type: ignore[assignment]
    link.remote_endpoint = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        "pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.05
    )

    with pytest.raises(McpHarnessError) as teardown_error:
        dispatch_tool(session, "link_disconnect", {}, link=link)
    assert teardown_error.value.code == "link_teardown_failed"
    assert link.remote_mode == "disconnecting"
    assert link._pending_network_session is network_session
    assert pyboy._hooks == {}

    network_session.fail = False
    assert dispatch_tool(session, "link_disconnect", {}, link=link) == {
        "remote_mode": "idle"
    }
    assert network_session.calls == 2
    assert link._pending_network_session is None


def test_remote_hook_cleanup_uses_one_total_deadline(monkeypatch) -> None:
    """A held emulator lock must not multiply cleanup by hook count."""
    session, _pyboy = _session()
    link = LinkState()
    link.remote_mode = "connected"
    link.remote_endpoint = object()  # type: ignore[assignment]
    entered = threading.Event()
    release = threading.Event()

    def hold_session() -> None:
        with session.locked():
            entered.set()
            release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_session, name="test-hook-lock-owner")
    holder.start()
    assert entered.wait(timeout=1.0)
    monkeypatch.setattr(
        "pokered_harness.mcp_server._DEFAULT_CLEANUP_TIMEOUT_S", 0.05
    )

    started = time.monotonic()
    try:
        with pytest.raises(McpHarnessError) as exc_info:
            dispatch_tool(session, "link_disconnect", {}, link=link)
        assert exc_info.value.code == "link_teardown_failed"
        assert time.monotonic() - started < 0.5
        assert link.remote_mode == "disconnecting"
        assert link._pending_remote_endpoint is not None
    finally:
        release.set()
        holder.join(timeout=1.0)

    assert not holder.is_alive()
    assert dispatch_tool(session, "link_disconnect", {}, link=link) == {
        "remote_mode": "idle"
    }
