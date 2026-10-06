"""Assertion-owning helpers for the two peer watchdog stress cases.

This module is intentionally not a pytest collection module.
"""

from __future__ import annotations

import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def run_blocked_sink_case():
    script = r"""
import ctypes, io, os, sys, tempfile, threading, time, uuid
from pathlib import Path
from tests import _tcp_trade_peer_trace as trace_module

mode, directory = sys.argv[1:]
read_fd = write_fd = None
if os.name == "nt":
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    name = rf"\\.\pipe\peer-trace-{os.getpid()}-{uuid.uuid4().hex}"
    kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.LPVOID]
    kernel.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE,
                                              ctypes.POINTER(wintypes.DWORD),
                                              wintypes.LPVOID, wintypes.LPVOID]
    kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel.ConnectNamedPipe.restype = wintypes.BOOL
    server = kernel.CreateNamedPipeW(name, 2, 1, 1, 4096, 4096, 0, None)
    assert server not in (None, ctypes.c_void_p(-1).value)
    client = kernel.CreateFileW(name, 0x80000000, 0, None, 3, 0, None)
    assert client not in (None, ctypes.c_void_p(-1).value)
    if not kernel.ConnectNamedPipe(server, None):
        assert ctypes.get_last_error() == 535
    block = ctypes.create_string_buffer(b"x" * 4096)
    written = wintypes.DWORD()
    requested = len(block) - 1
    total_written = 0
    while True:
        if not kernel.WriteFile(server, block, requested, ctypes.byref(written), None):
            error = ctypes.get_last_error()
            raise OSError(error, f"WriteFile failed while filling pipe: {error}")
        count = written.value
        assert 0 <= count <= requested, count
        total_written += count
        if count < requested:
            assert total_written > 0, total_written
            break
        assert count > 0, count
    mode_value = wintypes.DWORD(0)
    assert kernel.SetNamedPipeHandleState(server, ctypes.byref(mode_value), None, None)
    write_fd = msvcrt.open_osfhandle(server, os.O_WRONLY | os.O_BINARY)
    read_fd = msvcrt.open_osfhandle(client, os.O_RDONLY | os.O_BINARY)
else:
    import fcntl
    read_fd, write_fd = os.pipe()
    flags = fcntl.fcntl(write_fd, fcntl.F_GETFL)
    fcntl.fcntl(write_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    while True:
        try:
            os.write(write_fd, b"x" * 4096)
        except BlockingIOError:
            break
    fcntl.fcntl(write_fd, fcntl.F_SETFL, flags)

if mode == "warning":
    drain_enabled = threading.Event()
    warning_bytes = bytearray()
    def drain_warning():
        drain_enabled.wait()
        while True:
            part = os.read(read_fd, 65536)
            if not part:
                break
            warning_bytes.extend(part)
    reader = threading.Thread(target=drain_warning, daemon=True)
    reader.start()
    sys.stderr = io.TextIOWrapper(os.fdopen(write_fd, "wb", buffering=0), write_through=True)
    started = []
    real_factory = trace_module._new_thread
    def counted_factory(target, *, name):
        started.append(name)
        return real_factory(target, name=name)
    trace_module._new_thread = counted_factory
    os.environ["POKERED_PEER_TRACE_AFTER_SECONDS"] = "not-a-number-secret"
    os.environ["POKERED_PEER_TRACE_DIR"] = directory
    assert trace_module._PeerTraceWatchdog.from_env() is None
    for _ in range(8):
        trace_module._trace_warning("secret-value")
    assert len(started) == 1
    print("PEER_PROGRESS", flush=True)
    input()
    drain_enabled.set()
    sys.stderr.close()
    reader.join(5.0)
    assert not reader.is_alive()
    assert b"secret-value" not in warning_bytes
    os.close(read_fd)
    assert list(Path(directory).iterdir()) == []
else:
    drain_enabled = threading.Event()
    def drain():
        drain_enabled.wait()
        while os.read(read_fd, 65536):
            pass
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    trace = trace_module._PeerTraceWatchdog(0.02, write_fd, owned=True)
    completed = []
    original_write = os.write
    def observed_write(fd, data):
        if fd == write_fd:
            print("WRITE_ATTEMPT", flush=True)
            result = original_write(fd, data)
            completed.append(True)
            print("WRITE_COMPLETE", flush=True)
            return result
        return original_write(fd, data)
    trace_module.os.write = observed_write
    trace.start()
    limit = time.monotonic() + 2.0
    while time.monotonic() < limit and not trace._writer_owner:
        time.sleep(0.005)
    assert trace._writer_owner is not None
    close_started = time.monotonic()
    trace.close()
    close_elapsed = time.monotonic() - close_started
    assert 0 <= close_elapsed <= 0.350, close_elapsed
    os.fstat(write_fd)
    assert not completed
    print("BLOCKED_CONFIRMED", flush=True)
    time.sleep(0.05)
    print("PEER_PROGRESS", flush=True)
    input()
    drain_enabled.set()
    reader.join(5.0)
    assert not reader.is_alive()
    try:
        os.fstat(write_fd)
    except OSError:
        pass
    else:
        raise AssertionError("reaper did not release the descriptor after drain")
    os.close(read_fd)
    print("DRAINED", flush=True)
"""

    def run_blocked_child(mode):
        temporary = tempfile.TemporaryDirectory(prefix="peer-watchdog-sink-")
        directory = temporary.name
        process = None
        pump = None
        pump_started = False
        killer = None
        killer_started = False
        killer_deadline = None
        reaped = True
        stdout_events = queue.Queue()
        cleanup_errors = []

        def note_cleanup_failure(label, error):
            cleanup_errors.append(f"{label}: {error!r}")

        def close_stdin():
            if process is None or process.stdin is None or process.stdin.closed:
                return
            try:
                process.stdin.write("release\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                note_cleanup_failure("release child stdin", error)
            try:
                process.stdin.close()
            except (OSError, ValueError) as error:
                note_cleanup_failure("close child stdin", error)

        def finish_cleanup(primary_error):
            nonlocal reaped
            if process is not None and not reaped:
                close_stdin()
                try:
                    process.wait(timeout=2.0)
                    reaped = True
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        note_cleanup_failure("kill child after grace period", error)
                    try:
                        process.wait(timeout=2.0)
                        reaped = True
                    except subprocess.TimeoutExpired as error:
                        note_cleanup_failure("reap killed child", error)
                        if killer_deadline is not None:
                            remaining = max(0.0, killer_deadline - time.monotonic()) + 2.0
                            try:
                                process.wait(timeout=remaining)
                                reaped = True
                            except subprocess.TimeoutExpired as outer_error:
                                note_cleanup_failure(
                                    "reap child after outer kill deadline", outer_error
                                )
                            except OSError as outer_error:
                                note_cleanup_failure("wait after outer kill deadline", outer_error)
                    except OSError as error:
                        note_cleanup_failure("wait for killed child", error)
                except OSError as error:
                    note_cleanup_failure("gracefully reap child", error)

            if reaped:
                if killer_started:
                    killer.cancel()
                if process is not None and process.stderr is not None and not process.stderr.closed:
                    try:
                        process.stderr.close()
                    except OSError as error:
                        note_cleanup_failure("close child stderr", error)
            if pump_started and pump is not None:
                pump.join(timeout=2.0)
                if pump.is_alive():
                    note_cleanup_failure("stdout pump did not stop within 2 seconds", pump.name)
            elif process is not None and process.stdout is not None and not process.stdout.closed:
                try:
                    process.stdout.close()
                except OSError as error:
                    note_cleanup_failure("close child stdout without pump", error)

            if reaped:
                try:
                    temporary.cleanup()
                except OSError as error:
                    note_cleanup_failure("remove reaped-child temporary directory", error)
            else:
                retained = str(Path(directory).resolve())
                try:
                    temporary._finalizer.detach()
                except (AttributeError, RuntimeError, ValueError) as error:
                    note_cleanup_failure("detach unreaped-child directory finalizer", error)
                note_cleanup_failure(
                    "child remains unreaped; temporary directory retained",
                    retained,
                )

            if cleanup_errors:
                details = "; ".join(cleanup_errors)
                if primary_error is not None:
                    primary_error.add_note(f"secondary watchdog cleanup failures: {details}")
                else:
                    raise AssertionError(f"watchdog cleanup failed: {details}")

        try:
            process = subprocess.Popen(
                [sys.executable, "-c", script, mode, directory],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            reaped = False
            marker_deadline = time.monotonic() + 2.0

            def pump_stdout():
                try:
                    for line in process.stdout:
                        stdout_events.put((time.monotonic(), "line", line.rstrip("\r\n")))
                except (OSError, UnicodeError, ValueError) as error:
                    stdout_events.put((time.monotonic(), "error", repr(error)))
                finally:
                    try:
                        process.stdout.close()
                    except (OSError, ValueError) as error:
                        stdout_events.put((time.monotonic(), "error", f"close stdout: {error!r}"))
                    stdout_events.put((time.monotonic(), "eof", None))

            pump = threading.Thread(target=pump_stdout, name="peer-watchdog-stdout", daemon=True)
            pump.start()
            pump_started = True

            def kill_child_at_deadline():
                try:
                    process.kill()
                except ProcessLookupError:
                    return

            killer = threading.Timer(15.0, kill_child_at_deadline)
            killer.daemon = True
            killer_deadline = time.monotonic() + 15.0
            killer.start()
            killer_started = True
            seen = []
            required = (
                ("PEER_PROGRESS",)
                if mode == "warning"
                else ("WRITE_ATTEMPT", "BLOCKED_CONFIRMED", "PEER_PROGRESS")
            )

            def missing_markers():
                return sorted(set(required) - set(seen))

            def next_stdout_event(deadline, context):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"{context} deadline expired: missing={missing_markers()}, "
                        f"seen={sorted(seen)}"
                    )
                try:
                    event = stdout_events.get(timeout=remaining)
                except queue.Empty as error:
                    raise AssertionError(
                        f"{context} deadline expired: missing={missing_markers()}, "
                        f"seen={sorted(seen)}"
                    ) from error
                timestamp, kind, value = event
                assert timestamp <= deadline, (
                    f"{context} event arrived late: missing={missing_markers()}, "
                    f"seen={sorted(seen)}, event={event}"
                )
                return kind, value

            while missing_markers():
                kind, value = next_stdout_event(marker_deadline, "required markers")
                if kind == "eof":
                    raise AssertionError(
                        f"child exited before markers: missing={missing_markers()}, "
                        f"seen={sorted(seen)}"
                    )
                if kind == "error":
                    raise AssertionError(
                        f"stdout pump failed: missing={missing_markers()}, "
                        f"seen={sorted(seen)}, error={value}"
                    )
                seen.append(value)
            assert not missing_markers(), f"missing={missing_markers()}, seen={sorted(seen)}"
            process.stdin.write("release\n")
            process.stdin.flush()
            process.stdin.close()
            returncode = process.wait(timeout=10.0)
            reaped = True
            assert returncode == 0, process.stderr.read()
            eof_deadline = time.monotonic() + 2.0
            while True:
                kind, value = next_stdout_event(eof_deadline, "stdout EOF")
                if kind == "eof":
                    break
                if kind == "error":
                    raise AssertionError(f"stdout pump failed: {value}")
                seen.append(value)
            pump.join(timeout=2.0)
            assert not pump.is_alive(), "stdout pump did not stop after EOF"
            if mode == "dump":
                assert "DRAINED" in seen
        finally:
            finish_cleanup(sys.exception())

    run_blocked_child("dump")
    run_blocked_child("warning")
    run_ambiguous_warning_case()


def run_gil_held_delay_case():
    script = r"""
import ctypes, json, os, sys, time
from pathlib import Path
from tests import _tcp_trade_peer_trace as trace_module
from tests._pyboy_link_session_subprocess_support import _watchdog_dumps_ready

directory = Path(sys.argv[1])
os.environ['POKERED_PEER_TRACE_AFTER_SECONDS'] = '1.0'
os.environ['POKERED_PEER_TRACE_DIR'] = str(directory)
trace = trace_module._PeerTraceWatchdog.from_env()
assert trace is not None
trace.start()
print(json.dumps({'kind': 'ARMED', 'initial_due': trace.initial_due}), flush=True)
if os.name == 'nt':
    native = ctypes.PyDLL('msvcrt')
    hold = native._wsystem
    hold.argtypes = [ctypes.c_wchar_p]
    hold.restype = ctypes.c_int
    result = hold('cmd /d /q /c "echo ENTERED&set /p release="')
else:
    native = ctypes.PyDLL(None)
    hold = native.system
    hold.argtypes = [ctypes.c_char_p]
    hold.restype = ctypes.c_int
    result = hold(b"printf 'ENTERED\\n'; IFS= read -r release")
assert result == 0
cutoff = time.monotonic() + 2.0
data = ''
path, = directory.iterdir()
while time.monotonic() < cutoff:
    data = path.read_text(errors='replace')
    if _watchdog_dumps_ready(data, 1):
        break
    time.sleep(0.01)
assert _watchdog_dumps_ready(data, 1), data
trace.close()
print('DUMP_READY', flush=True)
"""
    temporary = tempfile.TemporaryDirectory(prefix="peer-watchdog-gil-")
    directory = temporary.name
    process = None
    killer = None
    killer_started = False
    killer_deadline = None
    reaped = True
    cleanup_errors = []

    def note_cleanup_failure(label, error):
        cleanup_errors.append(f"{label}: {error!r}")

    def close_stdin():
        if process is None or process.stdin is None or process.stdin.closed:
            return
        try:
            process.stdin.write("release\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            note_cleanup_failure("release GIL child stdin", error)
        try:
            process.stdin.close()
        except (OSError, ValueError) as error:
            note_cleanup_failure("close GIL child stdin", error)

    def finish_cleanup(primary_error):
        nonlocal reaped
        if process is not None and not reaped:
            close_stdin()
            try:
                process.wait(timeout=2.0)
                reaped = True
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except OSError as error:
                    note_cleanup_failure("kill GIL child after grace period", error)
                try:
                    process.wait(timeout=2.0)
                    reaped = True
                except subprocess.TimeoutExpired as error:
                    note_cleanup_failure("reap killed GIL child", error)
                    if killer_deadline is not None:
                        remaining = max(0.0, killer_deadline - time.monotonic()) + 2.0
                        try:
                            process.wait(timeout=remaining)
                            reaped = True
                        except subprocess.TimeoutExpired as outer_error:
                            note_cleanup_failure(
                                "reap GIL child after outer kill deadline", outer_error
                            )
                        except OSError as outer_error:
                            note_cleanup_failure(
                                "wait for GIL child after outer kill deadline", outer_error
                            )
                except OSError as error:
                    note_cleanup_failure("wait for killed GIL child", error)
            except OSError as error:
                note_cleanup_failure("gracefully reap GIL child", error)

        if reaped:
            if killer_started:
                killer.cancel()
            if process is not None:
                for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError as error:
                            note_cleanup_failure(f"close GIL child {label}", error)
            try:
                temporary.cleanup()
            except OSError as error:
                note_cleanup_failure("remove reaped GIL-child temporary directory", error)
        else:
            retained = str(Path(directory).resolve())
            try:
                temporary._finalizer.detach()
            except (AttributeError, RuntimeError, ValueError) as error:
                note_cleanup_failure("detach unreaped GIL-child directory finalizer", error)
            note_cleanup_failure(
                "GIL child remains unreaped; temporary directory retained",
                retained,
            )

        if cleanup_errors:
            details = "; ".join(cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(f"secondary watchdog cleanup failures: {details}")
            else:
                raise AssertionError(f"GIL-child cleanup failed: {details}")

    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script, directory],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        reaped = False
        killer = threading.Timer(15.0, process.kill)
        killer.daemon = True
        killer_deadline = time.monotonic() + 15.0
        killer.start()
        killer_started = True
        armed = json.loads(process.stdout.readline())
        assert armed["kind"] == "ARMED"
        due = armed["initial_due"]
        assert type(due) in (int, float) and math.isfinite(due) and due > 0
        armed_receipt = time.monotonic()
        arm = due - 1.0
        assert arm <= armed_receipt < due
        entered = process.stdout.readline().rstrip()
        entered_receipt = time.monotonic()
        assert entered == "ENTERED"
        assert armed_receipt < entered_receipt < due - 0.100
        (path,) = Path(directory).iterdir()
        assert "Timeout (" not in path.read_text(errors="replace")
        release_at = arm + 1.300
        while True:
            remaining = release_at - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.010, remaining))
        release_receipt = time.monotonic()
        assert release_receipt >= release_at
        assert release_receipt >= due
        assert process.poll() is None
        assert "Timeout (" not in path.read_text(errors="replace")
        process.stdin.write("release\n")
        process.stdin.flush()
        assert process.stdout.readline().rstrip() == "DUMP_READY"
        process.stdin.close()
        returncode = process.wait(timeout=10.0)
        reaped = True
        assert returncode == 0, process.stderr.read()
        assert "Timeout (" in path.read_text(errors="replace")
    finally:
        finish_cleanup(sys.exception())


def run_ambiguous_warning_case():
    """Prove an ambiguous warning start cannot release a descriptor for reuse."""
    script = r"""
import json, os, sys, tempfile, threading
from pathlib import Path
from tests import _tcp_trade_peer_trace as trace_module

root = Path(sys.argv[1])
original_path = root / "original.stderr"
replacement_path = root / "replacement.stderr"
gate = threading.Event()
entered = threading.Event()
captured = []
worker = []

with open(original_path, "wb", buffering=0) as original:
    previous_stderr = sys.stderr
    sys.stderr = os.fdopen(os.dup(original.fileno()), "w", buffering=1,
                           encoding="utf-8")
    real_duplicate = trace_module._safe_duplicate_stderr
    def capture_duplicate(stream=None):
        descriptor = real_duplicate(stream)
        captured.append(descriptor)
        return descriptor
    trace_module._safe_duplicate_stderr = capture_duplicate

    def factory(target, *, name):
        def delayed_target():
            gate.wait()
            entered.set()
            target()
        actual = threading.Thread(target=delayed_target, name=name, daemon=True)
        class StartProxy:
            ident = None
            def start(self):
                actual.start()
                raise OSError("modeled post-launch start interruption")
        worker.append(actual)
        return StartProxy()

    trace_module._new_thread = factory
    try:
        trace_module._trace_warning("secret caller text")
    finally:
        sys.stderr.close()
        sys.stderr = previous_stderr

    assert len(captured) == 1
    descriptor, = captured
    assert type(descriptor) is int and descriptor >= 0
    replacement = os.open(replacement_path, os.O_WRONLY | os.O_CREAT, 0o600)
    closed_before_release = False
    try:
        try:
            os.fstat(descriptor)
        except OSError:
            closed_before_release = True
        if closed_before_release:
            os.dup2(replacement, descriptor)
        gate.set()
        worker[0].join(2.0)
        assert not worker[0].is_alive()
        assert entered.is_set()
        assert original_path.read_bytes() == trace_module._WARNING
        assert replacement_path.read_bytes() == b""
        if descriptor != replacement:
            try:
                os.fstat(descriptor)
            except OSError:
                pass
            else:
                os.close(descriptor)
    finally:
        if replacement != descriptor:
            os.close(replacement)

print(json.dumps({"warning_written_to_original": True,
                  "replacement_stayed_empty": True,
                  "closed_before_release": closed_before_release}), flush=True)
"""
    with tempfile.TemporaryDirectory(prefix="peer-watchdog-warning-start-") as directory:
        completed = subprocess.run(
            [sys.executable, "-c", script, directory],
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        result = json.loads(completed.stdout)
        assert result == {
            "warning_written_to_original": True,
            "replacement_stayed_empty": True,
            "closed_before_release": False,
        }


def run_ambiguous_start_case():
    """Prove delayed timer/reaper entry cannot touch a released descriptor."""
    script = r"""
import json, os, sys, tempfile, threading, time
from pathlib import Path
from tests import _tcp_trade_peer as peer
from tests import _tcp_trade_peer_trace as trace_module

root = Path(sys.argv[1])
artifact = root / "capture.bin"
descriptor = os.open(artifact, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
trace = peer._PeerTraceWatchdog(0.02, descriptor, owned=True)
worker_gate = threading.Event()
reaper_gate = threading.Event()
gates = [worker_gate, reaper_gate]
actual_threads = []
factory_calls = 0
snapshot_calls = 0
write_calls = 0

def factory(target, *, name):
    global factory_calls
    index = factory_calls
    factory_calls += 1
    assert index < len(gates), name
    gate = gates[index]
    def delayed_target():
        gate.wait()
        target()
    actual = threading.Thread(target=delayed_target, name=name, daemon=True)
    class StartProxy:
        ident = None
        def start(self):
            actual.start()
            raise OSError("modeled post-launch start interruption")
        def join(self, timeout=None):
            actual.join(timeout)
        def is_alive(self):
            return actual.is_alive()
    actual_threads.append(actual)
    return StartProxy()

trace_module._new_thread = factory
trace_module._trace_warning = lambda _message="": None
real_snapshot = trace_module._snapshot_stack
def count_snapshot(*args, **kwargs):
    global snapshot_calls
    snapshot_calls += 1
    return real_snapshot(*args, **kwargs)
trace_module._snapshot_stack = count_snapshot
real_write_all = trace_module._write_all
def count_write(descriptor_arg, payload):
    global write_calls
    write_calls += 1
    return real_write_all(descriptor_arg, payload)
trace_module._write_all = count_write
peer._PeerTraceWatchdog.from_env = lambda: trace
failure = RuntimeError("preserve exact peer workload failure")
def fail_peer(*, trace):
    record, = trace._records
    assert record.state == "AMBIGUOUS_START"
    assert not record.done.is_set()
    assert trace.attempts == 1
    raise failure
peer._run_peer = fail_peer

started_at = time.monotonic()
try:
    peer.main()
except RuntimeError as raised:
    assert raised is failure
else:
    raise AssertionError("peer workload failure was lost")
close_elapsed = time.monotonic() - started_at
record, = trace._records
assert close_elapsed < 1.0, close_elapsed
assert close_elapsed <= 0.350, close_elapsed
assert factory_calls == 2
assert record.state == "AMBIGUOUS_START"
assert not record.done.is_set()
assert trace._fd_state == "PROCESS_EXIT_RETAINED"
assert trace._reaper_state == "PROCESS_EXIT_RETAINED"
assert trace._descriptor == descriptor
os.fstat(descriptor)
assert artifact.read_bytes() == b""
assert snapshot_calls == 0
assert write_calls == 0

worker_gate.set()
reaper_gate.set()
for actual in actual_threads:
    actual.join(2.0)
assert len(actual_threads) == 2
assert all(not actual.is_alive() for actual in actual_threads)
assert record.done.is_set() and record.state == "DONE"
assert trace._fd_state == "PROCESS_EXIT_RETAINED"
assert trace._descriptor == descriptor
os.fstat(descriptor)
assert artifact.read_bytes() == b""
assert snapshot_calls == 0
assert write_calls == 0
os.close(descriptor)
print(json.dumps({"peer_exception_identity_preserved": True,
                  "bounded_close": True,
                  "worker_and_reaper_late_entry_safe": True,
                  "snapshot_calls": snapshot_calls,
                  "write_calls": write_calls,
                  "attempts": trace.attempts}), flush=True)
"""
    with tempfile.TemporaryDirectory(prefix="peer-watchdog-ambiguous-start-") as directory:
        completed = subprocess.run(
            [sys.executable, "-c", script, directory],
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        result = json.loads(completed.stdout)
        assert result == {
            "peer_exception_identity_preserved": True,
            "bounded_close": True,
            "worker_and_reaper_late_entry_safe": True,
            "snapshot_calls": 0,
            "write_calls": 0,
            "attempts": 1,
        }
