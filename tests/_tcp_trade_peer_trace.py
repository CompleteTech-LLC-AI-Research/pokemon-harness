"""Bounded Python-only stack diagnostics for the TCP trade peer."""

from __future__ import annotations

import io
import math
import os
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

_MAX_ATTEMPTS = 2
_MAX_THREADS = 100
_MAX_FRAMES = 100
_MAX_NAME = 500
_MAX_DUMP_BYTES = 512 * 1024
_MAX_WARNING_BYTES = 1024
_CLOSE_SECONDS = 0.250
_WARNING = b"[peer trace] optional stack diagnostic warning\n"
_WARNING_LOCK = threading.Lock()
_WARNING_STARTED = False


def _close_descriptor_best_effort(descriptor):
    try:
        os.close(descriptor)
    except BaseException:  # noqa: BLE001 - cleanup cannot replace peer behavior.
        return False
    return True


def _clear_frame_mapping_best_effort(snapshot):
    try:
        snapshot.clear()
    except BaseException:  # noqa: BLE001 - frame cleanup is best effort.
        return False
    return True


def _new_thread(target, *, name):
    """One patchable factory; production always uses the standard Thread."""
    return threading.Thread(target=target, name=name, daemon=True)


def _safe_duplicate_stderr(stream=None):
    """Duplicate only a known built-in text/buffer/file stream descriptor."""
    stream = sys.stderr if stream is None else stream
    if type(stream) not in (io.TextIOWrapper, io.BufferedWriter, io.FileIO):
        return None
    duplicate = None
    try:
        descriptor = stream.fileno()
        if type(descriptor) is not int or descriptor < 0:
            return None
        os.fstat(descriptor)
        duplicate = os.dup(descriptor)
        os.fstat(duplicate)
        return duplicate
    except BaseException:  # noqa: BLE001 - diagnostics cannot replace peer behavior.
        if duplicate is not None:
            _close_descriptor_best_effort(duplicate)
        return None


def _write_all(descriptor, payload):
    """Write one bounded byte payload, tolerating EINTR and short writes."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        except BaseException:  # noqa: BLE001 - a diagnostic write is best effort.
            return False
        if written <= 0:
            return False
        offset += written
    return True


def _trace_warning(_message=""):
    """Schedule at most one constant, best-effort warning without blocking."""
    global _WARNING_STARTED
    with _WARNING_LOCK:
        if _WARNING_STARTED:
            return
        _WARNING_STARTED = True
        descriptor = _safe_duplicate_stderr()
    if descriptor is None:
        return
    entered = threading.Event()

    def emit():
        entered.set()
        try:
            _write_all(descriptor, _WARNING[:_MAX_WARNING_BYTES])
        finally:
            _close_descriptor_best_effort(descriptor)

    try:
        thread = _new_thread(emit, name=f"peer-trace-warning-{os.getpid()}")
    except BaseException:  # noqa: BLE001 - never fall back to a synchronous write.
        _close_descriptor_best_effort(descriptor)
        return
    try:
        thread.start()
    except BaseException:  # noqa: BLE001 - start may have launched the target.
        # Once start() is invoked, an absent ident/entry is not proof that no
        # writer can appear. Keep the fd reserved until emit() closes it.
        return


def _snapshot_stack(frames=None):
    """Format bounded Python frames and drop every frame reference before return."""
    try:
        snapshot = sys._current_frames() if frames is None else frames
    except BaseException:  # noqa: BLE001
        return b""
    output = bytearray()
    frame = None
    code = None

    def append_record(text):
        encoded = text.encode("utf-8", "backslashreplace")
        if len(output) + len(encoded) <= _MAX_DUMP_BYTES:
            output.extend(encoded)
            return True
        marker = b"[stack output truncated at record boundary]\n"
        if len(output) + len(marker) <= _MAX_DUMP_BYTES:
            output.extend(marker)
        return False

    try:
        append_record("Timeout (Python stack snapshot)!\n")
        for ident in sorted(snapshot)[:_MAX_THREADS]:
            if not append_record(f"Thread 0x{ident:x} (most recent call first):\n"):
                break
            frame = snapshot[ident]
            for _ in range(_MAX_FRAMES):
                if frame is None:
                    break
                code = frame.f_code
                filename = (
                    str(code.co_filename).replace("\n", "\\n").replace("\r", "\\r")[:_MAX_NAME]
                )
                function = str(code.co_name).replace("\n", "\\n").replace("\r", "\\r")[:_MAX_NAME]
                if not append_record(
                    f'  File "{filename}", line {frame.f_lineno}, in {function}\n'
                ):
                    break
                frame = frame.f_back
            if len(output) >= _MAX_DUMP_BYTES:
                break
    except BaseException:  # noqa: BLE001 - partial diagnostics are still disposable.
        return bytes(output)
    finally:
        frame = None
        code = None
        _clear_frame_mapping_best_effort(snapshot)
        snapshot = None
    return bytes(output)


class _WorkerRecord:
    __slots__ = (
        "cancel",
        "descriptor",
        "done",
        "due",
        "entered",
        "generation",
        "resolved",
        "state",
        "thread",
        "wake",
    )

    def __init__(self, descriptor, due, generation):
        self.descriptor = descriptor
        self.due = due
        self.generation = generation
        self.cancel = threading.Event()
        self.wake = threading.Event()
        self.entered = threading.Event()
        self.resolved = threading.Event()
        self.done = threading.Event()
        self.thread = None
        self.state = "RESERVED"


class _PeerTraceWatchdog:
    """Own at most two one-shot Python frame snapshots and their output fd."""

    def __init__(self, delay, destination, *, owned=False):
        self.delay = delay
        self.destination = destination
        self.owned = owned
        self._descriptor = (
            destination if type(destination) is int else _safe_duplicate_stderr(destination)
        )
        self._lock = threading.Lock()
        self._records = []
        self._waiters = deque()
        self._writer_owner = None
        self._generation = 0
        self._started = False
        self._closing = False
        self._close_called = False
        self._closed = False
        self._reaper_state = None
        self._reaper_thread = None
        self._reaper_entered = threading.Event()
        self._reaper_resolved = threading.Event()
        self._fd_state = "CONTROLLER" if self._descriptor is not None else "RELEASED"
        self.attempts = 0
        self.initial_due = None

    @classmethod
    def from_env(cls):
        raw = os.environ.get("POKERED_PEER_TRACE_AFTER_SECONDS")
        if raw is None:
            return None
        try:
            delay = float(raw)
            if not math.isfinite(delay) or delay <= 0:
                raise ValueError
        except BaseException:  # noqa: BLE001
            _trace_warning("invalid trace configuration")
            return None
        destination = sys.stderr
        owned = False
        directory = os.environ.get("POKERED_PEER_TRACE_DIR")
        if directory is not None:
            try:
                if not directory or not Path(directory).is_dir():
                    raise ValueError
                destination, _artifact = tempfile.mkstemp(
                    prefix=f"peer-trace-{os.getpid()}-", suffix=".log", dir=directory
                )
                owned = True
            except BaseException:  # noqa: BLE001
                _trace_warning("trace artifact open failed")
                destination = sys.stderr
        trace = cls(delay, destination, owned=owned)
        if trace._descriptor is None:
            trace._closed = True
            return None
        return trace

    @property
    def _generation_value(self):
        with self._lock:
            return self._generation

    def _reserve_locked(self, due):
        if self._closing or self.attempts >= _MAX_ATTEMPTS or self._descriptor is None:
            return None
        self.attempts += 1
        record = _WorkerRecord(self._descriptor, due, self._generation)
        self._records.append(record)
        return record

    def _launch(self, record):
        try:
            thread = _new_thread(
                lambda: self._run_record(record),
                name=f"peer-stack-{os.getpid()}-{self.attempts}",
            )
        except BaseException:  # noqa: BLE001
            with self._lock:
                record.state = "START_FAILED"
                record.resolved.set()
                record.done.set()
            _trace_warning("trace worker start failed")
            return
        with self._lock:
            record.thread = thread
            if self._closing:
                record.state = "START_FAILED"
                record.resolved.set()
                record.done.set()
                return
            record.state = "STARTING"
        try:
            thread.start()
        except BaseException:  # noqa: BLE001
            with self._lock:
                record.state = "AMBIGUOUS_START"
                record.resolved.set()
            _trace_warning("trace worker start failed")
            return
        with self._lock:
            record.state = "STARTED"
            record.resolved.set()

    def start(self):
        with self._lock:
            if self._started or self._closing or self._descriptor is None:
                return
            self._started = True
            self.initial_due = time.monotonic() + self.delay
            record = self._reserve_locked(self.initial_due)
        if record is not None:
            self._launch(record)

    def cleanup(self, deadline):
        now = time.monotonic()
        with self._lock:
            if (
                self._closing
                or self._closed
                or self.attempts != 1
                or self.initial_due is None
                or now < self.initial_due
            ):
                return
            delay = min(self.delay, max(0.000001, deadline - now))
            record = self._reserve_locked(now + delay)
        if record is not None:
            self._launch(record)

    def _stale_locked(self, record):
        return (
            self._closing
            or record.cancel.is_set()
            or record.generation != self._generation
            or self._descriptor is None
        )

    def _run_record(self, record):
        record.entered.set()
        record.resolved.wait()
        try:
            with self._lock:
                if record.state == "START_FAILED" or self._stale_locked(record):
                    return
                if record.state == "AMBIGUOUS_START":
                    record.state = "STARTED"
                record.state = "WAITING"
            if record.cancel.wait(max(0.0, record.due - time.monotonic())):
                return
            with self._lock:
                if self._stale_locked(record):
                    return
                record.state = "SNAPSHOTTING"
            payload = _snapshot_stack()
            if not payload:
                return
            with self._lock:
                if self._stale_locked(record):
                    return
                record.state = "READY_TO_WRITE"
            if not self._claim_writer(record):
                return
            _write_all(record.descriptor, payload)
        except BaseException:  # noqa: BLE001 - a child diagnostic cannot mask peer outcome.
            return
        finally:
            self._finish_record(record)

    def _claim_writer(self, record):
        while True:
            with self._lock:
                if self._stale_locked(record):
                    self._remove_waiter_locked(record)
                    return False
                if self._writer_owner is None:
                    self._remove_waiter_locked(record)
                    self._writer_owner = record
                    record.state = "WRITING"
                    return True
                if record not in self._waiters:
                    record.wake.clear()
                    self._waiters.append(record)
                    record.state = "READY_TO_WRITE"
                wake = record.wake
            wake.wait()

    def _remove_waiter_locked(self, record):
        try:
            self._waiters.remove(record)
        except ValueError:
            return

    def _wake_next_locked(self):
        while self._waiters:
            record = self._waiters.popleft()
            if self._stale_locked(record):
                record.wake.set()
                continue
            record.wake.set()
            break

    def _finish_record(self, record):
        with self._lock:
            self._remove_waiter_locked(record)
            if self._writer_owner is record:
                self._writer_owner = None
                self._wake_next_locked()
            record.state = "DONE"
            record.done.set()

    def _signal_cancellation(self):
        with self._lock:
            self._closing = True
            self._generation += 1
            for record in self._records:
                record.cancel.set()
                record.wake.set()

    def _join_until(self, deadline):
        with self._lock:
            records = tuple(self._records)
        pending = False
        for record in records:
            with self._lock:
                state = record.state
                thread = record.thread
            if state in ("DONE", "START_FAILED"):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if not record.resolved.wait(remaining):
                pending = True
                continue
            if thread is None:
                pending = True
                continue
            try:
                thread.join(max(0.0, deadline - time.monotonic()))
                alive = thread.is_alive()
            except BaseException:  # noqa: BLE001
                alive = True
            if alive:
                pending = True
            else:
                with self._lock:
                    record.state = "DONE"
                    record.done.set()
        return not pending

    def _start_reaper(self):
        with self._lock:
            if self._reaper_state is not None:
                return
            self._reaper_state = "REAPER_STARTING"

        def run_after_resolution():
            self._reaper_entered.set()
            self._reaper_resolved.wait()
            self._reap()

        thread = None
        try:
            thread = _new_thread(run_after_resolution, name=f"peer-trace-reaper-{os.getpid()}")
            with self._lock:
                self._reaper_thread = thread
                self._fd_state = "REAPER_STARTING"
            thread.start()
        except BaseException:  # noqa: BLE001
            with self._lock:
                # A failed start call leaves reaper ownership ambiguous too.
                # A late reaper may join, but cannot release a retained fd.
                self._reaper_state = "PROCESS_EXIT_RETAINED"
                self._fd_state = "PROCESS_EXIT_RETAINED"
                self._reaper_resolved.set()
            return
        with self._lock:
            self._reaper_state = "REAPER"
            self._fd_state = "REAPER"
            self._reaper_resolved.set()

    def _reap(self):
        with self._lock:
            records = tuple(self._records)
        for record in records:
            record.resolved.wait()
            with self._lock:
                state = record.state
                thread = record.thread
            if state == "START_FAILED" or thread is None:
                continue
            try:
                thread.join()
            except BaseException:  # noqa: BLE001
                with self._lock:
                    self._fd_state = "PROCESS_EXIT_RETAINED"
                    self._reaper_state = "PROCESS_EXIT_RETAINED"
                return
        self._close_destination(reaper=True)

    def _close_destination(self, *, reaper=False):
        with self._lock:
            allowed = ("CONTROLLER", "REAPER") if reaper else ("CONTROLLER",)
            if self._fd_state not in allowed:
                return
            descriptor = self._descriptor
            if descriptor is None:
                self._fd_state = "RELEASED"
                self._closed = True
                return
            self._fd_state = "CLOSE_CALL_IN_PROGRESS"
        try:
            os.close(descriptor)
        except BaseException:  # noqa: BLE001 - never retry an ambiguous descriptor close.
            with self._lock:
                self._fd_state = "PROCESS_EXIT_RETAINED"
            return
        with self._lock:
            self._descriptor = None
            self._fd_state = "RELEASED"
            self._closed = True

    def close(self):
        with self._lock:
            if self._close_called:
                return
            self._close_called = True
        try:
            self._signal_cancellation()
        except BaseException:  # noqa: BLE001 - retain fd if cancellation is uncertain.
            with self._lock:
                self._fd_state = "PROCESS_EXIT_RETAINED"
            return
        deadline = time.monotonic() + _CLOSE_SECONDS
        if self._join_until(deadline):
            self._close_destination()
        else:
            with self._lock:
                self._fd_state = "REAPER_STARTING"
            self._start_reaper()
