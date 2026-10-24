"""ROM-free ownership and retry contracts for Session cleanup."""

from __future__ import annotations

import threading
import time

import pytest

import pokered_harness.session as session_module
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


def _worker(call, *, name=None):
    errors = []

    def run():
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - thread failure collection
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True, name=name)
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


def test_failed_stop_observer_keeps_its_attempt_result_during_retry(monkeypatch):
    """A blocked first observer must report its own failed stop after retry."""

    session, pyboy, _ = _session()
    first_failure = OSError("first stop failed")
    stop_calls = []
    first_done_observed = threading.Event()
    release_first_observer = threading.Event()
    original_stop_attempt = session_module._StopAttempt
    attempts = []

    class GatedDone:
        """Pause only the first close caller after its attempt is committed."""

        def __init__(self, event):
            self._event = event

        def is_set(self):
            return self._event.is_set()

        def set(self):
            self._event.set()

        def wait(self, timeout=None):
            completed = self._event.wait(timeout)
            if completed:
                first_done_observed.set()
                assert release_first_observer.wait(5), (
                    "test did not release the first close observer"
                )
            return completed

    def make_attempt(owner_id):
        attempt = original_stop_attempt(owner_id)
        if not attempts:
            attempt.done = GatedDone(attempt.done)
        attempts.append(attempt)
        return attempt

    monkeypatch.setattr(session_module, "_StopAttempt", make_attempt)

    def stop(save=False):
        stop_calls.append(save)
        if len(stop_calls) == 1:
            raise first_failure

    pyboy.stop = stop
    first_close, first_errors = _worker(session.close)
    try:
        assert first_done_observed.wait(5), "first close did not publish its result"
        assert stop_calls == [False]

        # The first attempt is already committed as failed, but its caller is
        # intentionally still blocked in ``done.wait``.  A retry must be allowed
        # to complete without changing what that older caller eventually sees.
        session.close()
        assert stop_calls == [False, False]
        assert session._stop_complete
    finally:
        release_first_observer.set()
        _join(first_close)

    assert first_errors == [first_failure]
    assert first_errors[0] is first_failure
    assert not first_close.is_alive()
    assert not any(
        thread.is_alive() and thread.name == "pokered-session-stop"
        for thread in threading.enumerate()
    )


def test_close_observer_before_worker_keeps_failed_attempt_during_retry(monkeypatch):
    """An observer admitted before worker launch retains the first result."""

    session, pyboy, _ = _session()
    lock = ObservedLock()
    session._lock = lock
    first_failure = OSError("first stop failed before observer resumed")
    stop_calls = []
    observer_done_observed = threading.Event()
    release_observer = threading.Event()
    original_stop_attempt = session_module._StopAttempt
    attempts = []

    class GatedDone:
        def __init__(self, event):
            self._event = event

        def is_set(self):
            return self._event.is_set()

        def set(self):
            self._event.set()

        def wait(self, timeout=None):
            completed = self._event.wait(timeout)
            if completed and threading.current_thread().name == "observer":
                observer_done_observed.set()
                assert release_observer.wait(5), "test did not release observer"
            return completed

    def make_attempt(owner_id):
        attempt = original_stop_attempt(owner_id)
        if not attempts:
            attempt.done = GatedDone(attempt.done)
        attempts.append(attempt)
        return attempt

    monkeypatch.setattr(session_module, "_StopAttempt", make_attempt)

    def stop(save=False):
        stop_calls.append(save)
        if len(stop_calls) == 1:
            raise first_failure

    pyboy.stop = stop
    lock.lock.acquire()
    lock_held = True
    first_close, first_errors = _worker(session.close, name="first-close")
    observer = None
    observer_errors = None
    try:
        assert lock.timed_acquire.wait(1), "first close did not claim the lock phase"
        lock.timed_acquire.clear()
        observer, observer_errors = _worker(session.close, name="observer")
        assert lock.timed_acquire.wait(1), "observer did not enter cleanup"
        lock.release()
        lock_held = False

        assert observer_done_observed.wait(5), "observer did not publish its wait"
        session.close()
        assert stop_calls == [False, False]
        assert session._stop_complete
    finally:
        if lock_held:
            lock.release()
        release_observer.set()
        _join(first_close)
        if observer is not None:
            _join(observer)

    assert first_errors == [first_failure]
    assert observer_errors == [first_failure]
    assert first_errors[0] is first_failure
    assert observer_errors[0] is first_failure
    assert not any(
        thread.is_alive() and thread.name == "pokered-session-stop"
        for thread in threading.enumerate()
    )


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
        with session.locked():  # noqa: SIM117 - deliberate nested re-entrant scope
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
    [0, -1, float("inf"), float("nan"), 10**1000, True, False, None, "1"],
)
def test_invalid_timeout_does_not_change_lifecycle(timeout):
    session, pyboy, _ = _session()
    with pytest.raises(ValueError, match="finite and positive"):
        session.close(timeout_s=timeout)
    assert not session.closed
    assert not pyboy.stopped
    session.step()
