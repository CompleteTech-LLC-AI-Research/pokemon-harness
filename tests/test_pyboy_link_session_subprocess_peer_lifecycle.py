"""ROM-free peer lifecycle, watchdog and result-validation regressions.

Split from ``tests/test_pyboy_link_session_subprocess.py`` for #131 with no behavior
change: every assertion and test ID is preserved verbatim. These cases run
without ROM assets and exercise peer supervision, shutdown, sync-boundary and
result-validation helpers.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _pyboy_link_session_subprocess_support as _subprocess_support
from tests._pyboy_link_session_subprocess_support import (
    _MAX_FAILURE_SENTINEL_ERROR,
    _SUPERVISOR_RESULT_SOURCE,
    _assert_peer_success,
    _assert_strict_peer_result,
    _captured_text,
    _collect_pair,
    _complete_peer_result,
    _CompletedPeer,
    _drain_stream,
    _kill_without_waiting,
    _PairDeadlineExceeded,
    _start_pipe_drainers,
    _watchdog_dumps_ready,
)
from tests._tcp_trade_peer import _hold_at_sync_boundary


@pytest.mark.parametrize(
    "watchdog_case",
    [
        "invalid-optin",
        "destinations",
        "wrapper",
        "real-output",
        "lifecycle",
        "arm-error",
        "cancel-error",
        "close-error",
    ],
)
def test_peer_trace_watchdog(watchdog_case, monkeypatch):
    """Opt-in peer stack diagnostics remain bounded and preserve peer outcomes."""
    from tests import _tcp_trade_peer as peer

    delay_env = "POKERED_PEER_TRACE_AFTER_SECONDS"
    dir_env = "POKERED_PEER_TRACE_DIR"
    monkeypatch.delenv(dir_env, raising=False)
    if watchdog_case == "invalid-optin":

        def unexpected(*args, **kwargs):
            pytest.fail("disabled watchdog must not open files or arm diagnostics")

        monkeypatch.setattr(peer.faulthandler, "dump_traceback_later", unexpected)
        monkeypatch.setattr(peer.tempfile, "mkstemp", unexpected)
        for value in (None, "", "garbage", "0", "-1", "nan", "inf", "-inf"):
            if value is None:
                monkeypatch.delenv(delay_env, raising=False)
            else:
                monkeypatch.setenv(delay_env, value)
            assert peer._PeerTraceWatchdog.from_env() is None
        return

    if watchdog_case == "destinations":
        monkeypatch.setenv(delay_env, "0.05")
        trace = peer._PeerTraceWatchdog.from_env()
        assert trace.destination is peer.sys.stderr
        assert not trace.owned
        trace.close()
        with tempfile.TemporaryDirectory(prefix="peer-watchdog-paths-") as directory:
            root = Path(directory)
            missing = root / "missing"
            for configured in ("", str(missing), __file__):
                monkeypatch.setenv(dir_env, configured)
                trace = peer._PeerTraceWatchdog.from_env()
                assert trace.destination is peer.sys.stderr
                assert not trace.owned
                trace.close()
            assert not missing.exists()
            assert list(root.iterdir()) == []
            monkeypatch.setenv(dir_env, directory)
            first = peer._PeerTraceWatchdog.from_env()
            try:
                (first_path,) = root.iterdir()
                os.write(first.destination, b"preserve existing artifact")
                second = peer._PeerTraceWatchdog.from_env()
                try:
                    paths = list(root.iterdir())
                    assert len(paths) == 2
                    assert all(path.name.startswith(f"peer-trace-{os.getpid()}-") for path in paths)
                    assert first_path.read_bytes() == b"preserve existing artifact"
                    if os.name == "posix":
                        assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
                finally:
                    second.close()
            finally:
                first.close()

            def fail_open(**kwargs):
                raise OSError("diagnostic artifact open failed")

            monkeypatch.setattr(peer.tempfile, "mkstemp", fail_open)
            trace = peer._PeerTraceWatchdog.from_env()
            assert trace.destination is peer.sys.stderr
            assert not trace.owned
            trace.close()
        return

    if watchdog_case == "wrapper":
        events = []

        class Trace:
            def start(self):
                events.append("start")

            def close(self):
                events.append("close")

        trace = Trace()
        monkeypatch.setattr(peer._PeerTraceWatchdog, "from_env", lambda: trace)
        failure = RuntimeError("original peer failure")
        for outcome in (17, failure):
            events.clear()

            def run_peer(*, trace, outcome=outcome):
                assert trace is not None
                events.extend(("cleanup", "emit_result"))
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            monkeypatch.setattr(peer, "_run_peer", run_peer)
            if isinstance(outcome, Exception):
                with pytest.raises(RuntimeError) as raised:
                    peer.main()
                assert raised.value is failure
            else:
                assert peer.main() == outcome
            assert events == ["start", "cleanup", "emit_result", "close"]
        monkeypatch.setattr(peer._PeerTraceWatchdog, "from_env", lambda: None)
        monkeypatch.setattr(peer, "_run_peer", lambda *, trace: 23 if trace is None else 99)
        assert peer.main() == 23

        def disabled_failure(*, trace):
            assert trace is None
            raise failure

        monkeypatch.setattr(peer, "_run_peer", disabled_failure)
        with pytest.raises(RuntimeError) as raised:
            peer.main()
        assert raised.value is failure
        return

    if watchdog_case == "real-output":
        # Real faulthandler runs only in this disposable Python child. No ROM
        # imports/loads, emulator, TCP connection, or repository artifacts.
        assert not _watchdog_dumps_ready("Timeout (first)\n", 1)
        assert not _watchdog_dumps_ready(
            'Timeout (first)\nFile "<string>", line 1\nFile "<string>", line 2\nTimeout (second)\n',
            2,
        )
        assert _watchdog_dumps_ready(
            'Timeout (first)\nFile "<string>", line 1\nTimeout (second)\nFile "<string>", line 2\n',
            2,
        )
        script = """
import json, os, sys, time
from pathlib import Path
from tests import _tcp_trade_peer as peer
from tests._pyboy_link_session_subprocess_support import _watchdog_dumps_ready

directory = Path(sys.argv[1])
os.environ['POKERED_PEER_TRACE_AFTER_SECONDS'] = '0.05'
os.environ['POKERED_PEER_TRACE_DIR'] = str(directory)
trace = peer._PeerTraceWatchdog.from_env()
assert trace is not None
try:
    trace.start()
    files = list(directory.iterdir())
    assert len(files) == 1, files
    path = files[0]
    assert path.name.startswith(f'peer-trace-{os.getpid()}-')
    def wait_for_dumps(count):
        cutoff = time.monotonic() + 5.0
        data = ""
        while time.monotonic() < cutoff:
            data = path.read_text(errors='replace')
            # faulthandler writes each dump incrementally.  The timeout
            # header can be visible before the frame lines that make the
            # dump useful (and that the assertions below require).
            if _watchdog_dumps_ready(data, count):
                return data
            time.sleep(0.01)
        raise AssertionError('watchdog did not emit expected dump: ' + data)
    first = wait_for_dumps(1)
    trace.cleanup(time.monotonic() + 5.0)
    second = wait_for_dumps(2)
    assert 'File "<string>"' in first
    assert second.count('Timeout (') == 2
finally:
    trace.close()
# Cancel a fresh pending timer, then stay alive past its deadline. Inspecting
# the artifact after child exit alone could hide an uncancelled timer.
os.environ['POKERED_PEER_TRACE_AFTER_SECONDS'] = '0.1'
cancelled = peer._PeerTraceWatchdog.from_env()
cancelled_path, = set(directory.iterdir()) - {path}
try:
    cancelled.start()
finally:
    cancelled.close()
before = cancelled_path.read_bytes()
assert before == b''
time.sleep(0.25)
assert cancelled_path.read_bytes() == before
assert path.read_text(errors='replace').count('Timeout (') == 2
print(json.dumps({'pid': os.getpid(), 'name': path.name, 'dumps': 2,
                  'cancelled_empty': True}), flush=True)
"""
        with tempfile.TemporaryDirectory(prefix="peer-watchdog-test-") as directory:
            completed = subprocess.run(
                [sys.executable, "-c", script, directory],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            result = json.loads(completed.stdout)
            assert result["dumps"] == 2
            assert result["cancelled_empty"] is True
            assert result["name"].startswith(f"peer-trace-{result['pid']}-")
            assert len(list(Path(directory).iterdir())) == 2
        return

    events = []
    clock = [100.0]
    monkeypatch.setattr(peer.time, "monotonic", lambda: clock[0])

    destination = 987654
    closed = []
    arms = []

    def arm(delay, **kwargs):
        events.append("arm")
        arms.append((delay, kwargs))
        assert not closed
        if watchdog_case == "arm-error":
            raise OSError("diagnostic arm failed")

    def cancel():
        events.append("cancel")
        assert not closed
        if watchdog_case == "cancel-error":
            raise OSError("diagnostic cancellation failed")

    monkeypatch.setattr(peer.faulthandler, "dump_traceback_later", arm)
    monkeypatch.setattr(peer.faulthandler, "cancel_dump_traceback_later", cancel)
    original_close = os.close

    def close(fd):
        if fd != destination:
            return original_close(fd)
        events.append("close")
        closed.append(fd)
        if watchdog_case == "close-error":
            raise OSError("diagnostic close failed")

    monkeypatch.setattr(peer.os, "close", close)
    trace = peer._PeerTraceWatchdog(10.0, destination, owned=True)
    failure = RuntimeError("original peer failure")

    def run_peer(*, trace):
        # Cleanup before initial due must not replace/postpone its timer.
        clock[0] = 105.0
        trace.cleanup(130.0)
        assert len(arms) == 1
        clock[0] = 111.0
        trace.cleanup(113.0)
        trace.cleanup(140.0)
        assert len(arms) == 2
        assert arms[1][0] == 2.0
        events.append("emit_result")
        assert not closed
        raise failure

    monkeypatch.setattr(peer._PeerTraceWatchdog, "from_env", lambda: trace)
    monkeypatch.setattr(peer, "_run_peer", run_peer)
    with pytest.raises(RuntimeError) as raised:
        peer.main()
    assert raised.value is failure
    assert arms[0][0] == 10.0
    for _delay, kwargs in arms:
        assert kwargs["file"] == destination
        assert kwargs.get("repeat", False) is False
        assert kwargs.get("exit", False) is False
    assert events.index("emit_result") < events.index("cancel")
    if watchdog_case == "cancel-error":
        assert not closed
    else:
        assert closed == [destination]
        assert events.index("cancel") < events.index("close")
        trace.close()
        assert closed == [destination]


def test_setup_handshake_failure_returns_bounded_non_success_sentinels():
    """A pre-result child failure is fail-closed without waiting for its peer."""
    failure_command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('setup-failure-' + 'x' * 4096); sys.exit(17)",
    ]
    blocked_command = [sys.executable, "-c", "import time; time.sleep(30)"]
    listener = subprocess.Popen(
        failure_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    connector = subprocess.Popen(
        blocked_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    try:
        result_a, result_b = _collect_pair(
            listener,
            connector,
            deadline_at=started + 5.0,
        )
        assert time.monotonic() - started < 2.0
        for result in (result_a, result_b):
            assert result["_result_source"] == _SUPERVISOR_RESULT_SOURCE
            assert result["_failure_phase"] == "setup/handshake"
            assert result["_drive_status"] == "error"
            assert result["_drive_error"]
            assert len(result["_drive_error"]) <= _MAX_FAILURE_SENTINEL_ERROR
            with pytest.raises(AssertionError, match="did not complete successfully"):
                _assert_peer_success(result, label="peer")
    finally:
        _kill_without_waiting((listener, connector))
        listener.wait(timeout=2.0)
        connector.wait(timeout=2.0)


@pytest.mark.parametrize("missing_row", ("LinkMenu", "party_after.mon_records"))
def test_collect_pair_rejects_missing_or_partial_required_rows(missing_row: str):
    """A cleanly exited child still cannot pass with an incomplete JSON row."""
    incomplete = _complete_peer_result()
    if missing_row == "LinkMenu":
        del incomplete[missing_row]
    else:
        del incomplete["party_after"]["mon_records"]
    listener = _CompletedPeer(incomplete)
    connector = _CompletedPeer(_complete_peer_result(role="connect"))

    with pytest.raises(AssertionError, match="missing required result rows"):
        _collect_pair(
            listener,
            connector,
            deadline_at=time.monotonic() + 1.0,
        )


@pytest.mark.parametrize("goal", ("trade", "battle"))
def test_strict_acceptance_rejects_link_menu_only_result(goal: str):
    """The LinkMenu milestone is not strict trade or battle completion."""
    result = _complete_peer_result()
    result["LinkMenu"] = 1

    with pytest.raises(AssertionError, match="LinkMenu alone is insufficient"):
        _assert_strict_peer_result(
            result,
            label="listener",
            goal=goal,
            expected_role="listen",
            expected_version="yellow",
        )


@pytest.mark.parametrize("goal", ("trade", "battle"))
def test_strict_acceptance_rejects_missing_native_edge_req(goal: str):
    """Gameplay-shaped counters without native EDGE_REQ traffic cannot pass."""
    result = _complete_peer_result()
    if goal == "trade":
        result["_AddEnemyMonToPlayerParty"] = 1
    else:
        for symbol in (
            "DisplayLinkBattleVersusTextBox",
            "MoveSelectionMenu",
            "LinkBattleExchangeData",
        ):
            result[symbol] = 1
        result["ExecutePlayerMove"] = 1
    for field in (
        "edge_req_sent",
        "edge_req_received",
        "edge_resp_sent",
        "edge_resp_received",
    ):
        result["_backend_stats"][field] = 0

    with pytest.raises(AssertionError, match="no native EDGE_REQ"):
        _assert_strict_peer_result(
            result,
            label="listener",
            goal=goal,
            expected_role="listen",
            expected_version="yellow",
        )


class _ShutdownBackend:
    """Require an owner drain before the teardown marker can be acknowledged."""

    def __init__(self, *, fail_at=None, missing_ready=False):
        from pokered_harness.link.network_backend import NetworkBackendError

        self.trace = []
        self.marker = None
        self.polls = {}
        self.drains = 0
        self.fail_at = fail_at
        self.failure = NetworkBackendError("backend shutdown failure")
        self.missing_ready = missing_ready

    def record(self, *event):
        self.trace.append(event)
        if event == self.fail_at:
            raise self.failure

    def announce_sync(self, *, sync_id):
        self.record("announce", sync_id)
        self.marker = sync_id

    def poll_peer_sync(self, *, sync_id):
        self.record("poll", sync_id)
        self.polls[sync_id] = self.polls.get(sync_id, 0) + 1
        if self.missing_ready:
            return False
        return self.polls[sync_id] > 1

    def service_pending_edges(self, *, max_edges):
        assert max_edges == 1
        self.record("service", self.marker)
        return 1

    def wait_for_wire_idle(self, *, timeout, progress_callback, stable_checks):
        assert timeout == 10.0
        assert stable_checks == 8
        self.drains += 1
        self.record("drain", self.drains)
        progress_callback()
        self.record("idle", self.drains)

    def step(self, frames):
        # The first drain owns real emulator progress before any marker exists.
        assert self.marker is None
        assert frames == 1
        self.record("step", frames)

    def cooperative_sync(self, *, sync_id, timeout, step_frames):
        assert (sync_id, timeout, step_frames) == (123, 10.0, 1)
        self.record("ready", sync_id)
        self.step(step_frames)


def _run_fake_shutdown(backend):
    from tests._tcp_trade_peer import _peer_shutdown_sync

    clock = iter(range(1000))
    _peer_shutdown_sync(
        backend,
        ready_sync_id=124,
        timeout=10.0,
        step=backend.step,
        backend_snapshot=dict,
        monotonic=lambda: next(clock) * 0.1,
        sleep=lambda seconds: backend.record("sleep", seconds),
    )


def test_peer_shutdown_drains_live_serial_work_before_starting_teardown_marker():
    """LinkMenu is not wire-idle until owner-driven drain proves it."""
    backend = _ShutdownBackend()
    _run_fake_shutdown(backend)
    assert [
        event for event in backend.trace if event[0] in ("step", "drain", "idle", "announce")
    ] == [
        ("drain", 1),
        ("step", 1),
        ("idle", 1),
        ("announce", 124),
        ("drain", 2),
        ("idle", 2),
    ]
    assert backend.polls == {124: 2}
    assert backend.trace[-1] == ("idle", 2)
    assert backend.trace.index(("announce", 124)) > backend.trace.index(("idle", 1))
    assert [event for event in backend.trace if event[0] == "service"] == [
        ("service", 124),
        ("service", 124),
    ]


@pytest.mark.parametrize(
    "failure",
    [
        ("drain", 1),
        ("announce", 124),
        ("poll", 124),
        ("service", 124),
        ("drain", 2),
    ],
)
def test_peer_shutdown_protocol_propagates_backend_errors(failure):
    from pokered_harness.link.network_backend import NetworkBackendError

    backend = _ShutdownBackend(fail_at=failure)
    with pytest.raises(NetworkBackendError) as raised:
        _run_fake_shutdown(backend)
    assert raised.value is backend.failure
    assert backend.trace[-1] == failure


def test_peer_shutdown_ready_marker_times_out_without_post_marker_ticks():
    backend = _ShutdownBackend(missing_ready=True)
    with pytest.raises(RuntimeError, match="peer shutdown sync 124 did not converge"):
        _run_fake_shutdown(backend)
    assert backend.drains == 1
    assert backend.marker == 124
    assert 90 <= backend.polls[124] <= 100
    release = backend.trace.index(("announce", 124))
    assert not any(event[0] == "step" for event in backend.trace[release:])
    assert ("service", 124) in backend.trace[release:]


@pytest.mark.parametrize("goal", ["link_menu", "trade", "battle"])
def test_link_menu_finish_starts_teardown_only_through_draining_helper(goal):
    from tests._tcp_trade_peer import _finish_link_menu_phase

    backend = _ShutdownBackend()
    shutdown_calls = []

    def shutdown(**kwargs):
        shutdown_calls.append(kwargs)

    _finish_link_menu_phase(
        goal,
        cooperative_sync=backend.cooperative_sync,
        peer_shutdown_sync=shutdown,
    )
    if goal == "link_menu":
        assert shutdown_calls == [{"ready_sync_id": 126, "timeout": 10.0}]
        assert backend.trace == []
    else:
        assert shutdown_calls == []
        # Gameplay continuations retain their existing bounded rendezvous.
        assert backend.trace == [("ready", 123), ("step", 1)]


def test_hold_at_sync_boundary_does_not_tick_past_ready_marker():
    """The release rendezvous uses owner dispatch, never blind ROM ticks."""

    class FakeBackend:
        def __init__(self):
            self.announced: list[int] = []

        def announce_sync(self, *, sync_id: int) -> None:
            self.announced.append(sync_id)

        def poll_peer_sync(self, *, sync_id: int) -> bool:
            # A peer that has already reached each matching phase.
            return sync_id in (113, 114)

    service_calls = 0

    def service_pending_edges() -> int:
        nonlocal service_calls
        service_calls += 1
        return 0

    backend = FakeBackend()
    _hold_at_sync_boundary(
        backend,
        ready_sync_id=113,
        release_sync_id=114,
        timeout=1.0,
        service_pending_edges=service_pending_edges,
    )

    assert backend.announced == [113, 114]
    # Both peer markers were already available, so no ROM tick is possible
    # in this helper; the callback exists solely for any already-admitted
    # owner edge that may arrive at a real boundary.
    assert service_calls == 0


def test_hold_at_sync_boundary_ticks_timed_rom_phase():
    """Timed ROM work keeps the owner emulator advancing during rendezvous."""

    class FakeBackend:
        def __init__(self):
            self.announced: list[int] = []
            self.polls: dict[int, int] = {}

        def announce_sync(self, *, sync_id: int) -> None:
            self.announced.append(sync_id)

        def poll_peer_sync(self, *, sync_id: int) -> bool:
            polls = self.polls.get(sync_id, 0)
            self.polls[sync_id] = polls + 1
            return polls >= 1

    progress_calls = 0

    def progress() -> None:
        nonlocal progress_calls
        progress_calls += 1

    backend = FakeBackend()
    _hold_at_sync_boundary(
        backend,
        ready_sync_id=115,
        release_sync_id=116,
        timeout=1.0,
        service_pending_edges=lambda: 0,
        progress_callback=progress,
    )

    assert backend.announced == [115, 116]
    assert progress_calls == 2


@pytest.mark.parametrize(
    ("encoding", "errors", "fragments", "expected"),
    [
        ("utf-8", "strict", [b"\xe2", b"\x82", b"\xac\r", b"\nend\r"], "€\nend\n"),
        ("utf-8", "replace", [b"prefix", b"\xe2"], "prefix�"),
        ("latin-1", "strict", [b"\xe9\r", b"\n"], "é\n"),
    ],
)
def test_collect_pair_enforces_hard_deadline_without_waiting_for_peers(
    monkeypatch, encoding, errors, fragments, expected
):
    """Capture live partial output without extending the pair deadline."""
    pending = iter([*fragments, b""])
    stream = SimpleNamespace(
        buffer=SimpleNamespace(read1=lambda size: next(pending)),
        encoding=encoding,
        errors=errors,
        close=lambda: None,
    )
    chunks = []
    _drain_stream(stream, chunks)
    assert "".join(chunks) == expected
    chunks = []
    _drain_stream(io.StringIO(expected), chunks)
    assert "".join(chunks) == expected

    markers = {"stdout": "short-live-stdout", "stderr": "short-live-stderr"}
    command = [
        sys.executable,
        "-c",
        (
            "import sys, time; "
            "print('short-live-stdout', end='', flush=True); "
            "print('short-live-stderr', end='', file=sys.stderr, flush=True); "
            "time.sleep(30)"
        ),
    ]
    procs = []
    try:
        readiness_deadline = time.monotonic() + 5.0
        for _ in range(2):
            procs.append(
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            )
        listener, connector = procs
        drainers = {proc: _start_pipe_drainers(proc) for proc in procs}
        while time.monotonic() < readiness_deadline:
            if all(
                _captured_text(captured, name) == marker
                for captured, _readers in drainers.values()
                for name, marker in markers.items()
            ):
                break
            time.sleep(0.001)
        # Assert before any collector kill/EOF, using the very same captures
        # returned to the collector. A kill-triggered drain cannot pass this.
        for proc, (captured, readers) in drainers.items():
            assert proc.poll() is None
            assert all(reader.is_alive() for reader in readers)
            for name, marker in markers.items():
                assert _captured_text(captured, name) == marker
        # ``_collect_pair`` resolves ``_start_pipe_drainers`` in the shared
        # support module, so the patch must target that module's binding.
        monkeypatch.setattr(
            _subprocess_support, "_start_pipe_drainers", lambda proc: drainers[proc]
        )
        started = time.monotonic()
        deadline_at = started + 0.25
        with pytest.raises(_PairDeadlineExceeded, match="hard deadline") as raised:
            _collect_pair(
                listener,
                connector,
                deadline_at=deadline_at,
            )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        for marker in markers.values():
            assert str(raised.value).count(marker) == 2
    finally:
        _kill_without_waiting(procs)
        for proc in procs:
            proc.wait(timeout=2.0)
    assert listener.returncode is not None
    assert connector.returncode is not None


def test_partial_peer_sentinel_is_fatal_before_gameplay_assertions():
    """A deadline/error sentinel cannot pass on counters alone."""
    partial = {
        "LinkMenu": 1,
        "_drive_status": "deadline",
        "_drive_error": "trade did not complete before deadline",
        "_deadline_exceeded": True,
        "_supervisor_returncode": 1,
    }
    with pytest.raises(AssertionError, match="did not complete successfully"):
        _assert_peer_success(partial, label="listener")
