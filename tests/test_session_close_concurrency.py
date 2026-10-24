"""ROM-free ownership and retry contracts for Session cleanup."""

from __future__ import annotations

import threading
import time

import pytest

from pokered_harness.session import SessionCleanupTimeoutError, SessionClosedError
from tests.test_session import _session


class ObservedLock:
    """Expose the start of close's timed acquire without polling or sleeps."""

    def __init__(self):
        self.lock = threading.RLock()
        self.timed_acquire = threading.Event()

    def acquire(self, **kwargs):
        if "timeout" in kwargs:
            self.timed_acquire.set()
        return self.lock.acquire(**kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


def _worker(call):
    errors = []

    def run():
        try:
            call()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def _join(thread):
    thread.join(timeout=2)
    assert not thread.is_alive(), "worker failed to finish"


def _blocked_tick():
    session, pyboy, _ = _session()
    session.serial_hook("DisplayTextID", lambda _ctx: pytest.fail("closed callback fired"))
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    stops = []

    def tick(*_args, **_kwargs):
        entered.set()
        try:
            assert release.wait(2), "test did not release tick"
        finally:
            exited.set()

    def stop(save=False):
        assert exited.is_set(), "stop overlapped an active tick"
        stops.append(save)

    pyboy.tick = tick
    pyboy.stop = stop
    thread, errors = _worker(session.step)
    assert entered.wait(1)
    return session, pyboy, release, stops, thread, errors


def test_close_times_out_without_stopping_active_tick_and_can_retry():
    session, pyboy, release, stops, thread, errors = _blocked_tick()
    try:
        start = time.monotonic()
        with pytest.raises(SessionCleanupTimeoutError, match="cleanup deadline"):
            session.close(timeout_s=0.02)
        assert time.monotonic() - start < 1
        assert session.closed
        assert not session._stop_complete
        assert stops == []
        # This must return while tick still owns the emulator lock.
        pyboy.fire(0x02, 0x4A12)
        assert all(not state.active for state, *_rest in session._serial_hooks)
    finally:
        release.set()
        _join(thread)
    assert errors == []
    with pytest.raises(SessionClosedError):
        session.step()
    session.close(save=True)
    assert stops == [True]
    assert session._stop_complete


def test_close_stops_after_tick_releases_before_deadline():
    session, pyboy, _ = _session()
    lock = ObservedLock()
    session._lock = lock
    entered, release, exited = (threading.Event() for _ in range(3))
    order = []

    def tick(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        order.append("tick exited")
        exited.set()

    def stop(save=False):
        assert exited.is_set()
        order.append("stop")

    pyboy.tick, pyboy.stop = tick, stop
    tick_thread, tick_errors = _worker(session.step)
    assert entered.wait(1)
    close_thread, close_errors = _worker(lambda: session.close(timeout_s=1))
    try:
        assert lock.timed_acquire.wait(1)
        assert session.closed
        assert order == []
    finally:
        release.set()
        _join(tick_thread)
        _join(close_thread)
    assert tick_errors == close_errors == []
    assert order == ["tick exited", "stop"]


def test_concurrent_closers_stop_once():
    session, pyboy, _ = _session()
    lock = ObservedLock()
    session._lock = lock
    entered, release = threading.Event(), threading.Event()
    stops = []

    def stop(save=False):
        stops.append(save)
        entered.set()
        assert release.wait(2)

    pyboy.stop = stop
    first, first_errors = _worker(lambda: session.close(save=True))
    assert entered.wait(1)
    lock.timed_acquire.clear()
    second, second_errors = _worker(session.close)
    try:
        assert lock.timed_acquire.wait(1)
        assert stops == [True]
    finally:
        release.set()
        _join(first)
        _join(second)
    assert first_errors == second_errors == []
    assert stops == [True]


def test_failed_stop_remains_retryable_and_execution_depth_balances():
    session, pyboy, _ = _session()
    stops = []

    def stop(save=False):
        stops.append(save)
        if len(stops) == 1:
            raise RuntimeError("stop failed")

    pyboy.stop = stop
    with pytest.raises(RuntimeError, match="stop failed"):
        session.close()
    assert session.closed
    assert not session._stop_complete
    session.close()
    session.close()
    assert stops == [False, False]
    assert session._stop_complete


@pytest.mark.parametrize("inside_tick", [False, True])
def test_reentrant_guarded_callback_close_defers_until_execution_exits(inside_tick):
    session, pyboy, _ = _session()
    stops, callbacks = [], []
    pyboy.stop = lambda save=False: stops.append(save)

    def callback(_ctx):
        callbacks.append("called")
        with pytest.raises(SessionCleanupTimeoutError, match="current emulator operation"):
            session.close(timeout_s=0.02)
        assert session.closed
        assert stops == []

    session.serial_hook("DisplayTextID", callback)
    if inside_tick:
        pyboy.tick = lambda *_args, **_kwargs: pyboy.fire(0x02, 0x4A12)
        session.step()
    else:
        pyboy.fire(0x02, 0x4A12)
    pyboy.fire(0x02, 0x4A12)
    assert callbacks == ["called"]
    session.close()
    assert stops == [False]


def test_nested_compound_raw_emulator_access_cannot_stop_mid_operation():
    session, pyboy, _ = _session()
    stops = []
    pyboy.stop = lambda save=False: stops.append(save)
    with session.locked():
        with session.locked():
            # Paired runtime operations can call the raw emulator here.
            with pytest.raises(SessionCleanupTimeoutError):
                session.close()
        with pytest.raises(SessionCleanupTimeoutError):
            session.close()
        assert stops == []
    session.close()
    assert stops == [False]


def test_recursive_stop_is_not_reported_complete():
    session, pyboy, _ = _session()
    pyboy.stop = lambda save=False: session.close()
    with pytest.raises(SessionCleanupTimeoutError):
        session.close()
    assert not session._stop_complete
    pyboy.stop = lambda save=False: None
    session.close()
    assert session._stop_complete


def test_active_callback_close_does_not_deadlock_with_external_closer():
    session, pyboy, _ = _session()
    lock = ObservedLock()
    session._lock = lock
    entered, release = threading.Event(), threading.Event()
    order = []

    def callback(_ctx):
        entered.set()
        assert release.wait(2)
        with pytest.raises(SessionCleanupTimeoutError, match="current emulator operation"):
            session.close()
        order.append("callback exited")

    session.serial_hook("DisplayTextID", callback)
    pyboy.tick = lambda *_args, **_kwargs: pyboy.fire(0x02, 0x4A12)
    pyboy.stop = lambda save=False: order.append("stop")
    tick_thread, tick_errors = _worker(session.step)
    assert entered.wait(1)
    close_thread, close_errors = _worker(session.close)
    try:
        assert lock.timed_acquire.wait(1)
    finally:
        release.set()
        _join(tick_thread)
        _join(close_thread)
    assert tick_errors == close_errors == []
    assert order == ["callback exited", "stop"]


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("inf"), float("nan"), 1e300, 10**1000, True, False, None, "1"],
)
def test_invalid_timeout_does_not_change_lifecycle(timeout):
    session, pyboy, _ = _session()
    with pytest.raises(ValueError, match="finite and positive"):
        session.close(timeout_s=timeout)
    assert not session.closed
    assert not pyboy.stopped
    session.step()
