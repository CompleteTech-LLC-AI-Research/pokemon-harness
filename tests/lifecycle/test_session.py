"""Focused bounded-lock and hook-cleanup regressions for ``Session``."""

from __future__ import annotations

import math
import threading
import time

import pytest

import pokered_harness.session as session_module
from pokered_harness.events import EventBus
from pokered_harness.session import Session, SessionLockTimeout
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy

_SYM = """\
00:216F Serial_ExchangeBytes
"""


def _session() -> tuple[Session, FakePyBoy]:
    pyboy = FakePyBoy(DictMemory())
    session = Session(
        pyboy=pyboy,
        symbols=load_sym_text(_SYM),
        event_bus=EventBus(),
    )
    return session, pyboy


class _RejectingLock:
    """Lock probe that fails only when a bounded timeout is supplied."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def acquire(self, *, timeout: float) -> bool:
        self.timeouts.append(timeout)
        return False

    def release(self) -> None:  # pragma: no cover - acquisition always fails
        raise AssertionError("the rejecting lock must never be released")


def test_lock_and_deactivation_defaults_always_supply_a_finite_timeout() -> None:
    session, _ = _session()
    probe = _RejectingLock()
    session._lock = probe  # type: ignore[assignment]

    with pytest.raises(SessionLockTimeout), session.locked():
        raise AssertionError("the body must not run")
    with pytest.raises(SessionLockTimeout):
        session.deactivate_serial_hooks()
    with pytest.raises(SessionLockTimeout):
        session.deactivate_hooks_at("Serial_ExchangeBytes")

    assert probe.timeouts == [
        session_module._DEFAULT_CLOSE_TIMEOUT_S,
        session_module._DEFAULT_CLOSE_TIMEOUT_S,
        session_module._DEFAULT_CLOSE_TIMEOUT_S,
    ]
    assert all(math.isfinite(timeout) and timeout >= 0 for timeout in probe.timeouts)


def test_locked_timeout_is_bounded_and_does_not_enter_the_body() -> None:
    session, _ = _session()
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with session.locked(timeout_s=1.0):
            entered.set()
            release.wait(timeout=2.0)

    owner = threading.Thread(target=hold_lock, name="session-lock-owner")
    owner.start()
    assert entered.wait(timeout=1.0)

    started = time.monotonic()
    with pytest.raises(SessionLockTimeout, match="deadline"), session.locked(
        timeout_s=0.02
    ):
        raise AssertionError("the timed-out operation must not enter")
    assert time.monotonic() - started < 0.5

    release.set()
    owner.join(timeout=2.0)
    assert not owner.is_alive()


def test_serial_hook_deactivation_is_retryable_after_a_lock_timeout() -> None:
    session, pyboy = _session()
    calls: list[object] = []
    session.serial_hook("Serial_ExchangeBytes", calls.append)

    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with session.locked(timeout_s=1.0):
            entered.set()
            release.wait(timeout=2.0)

    owner = threading.Thread(target=hold_lock, name="serial-hook-lock-owner")
    owner.start()
    assert entered.wait(timeout=1.0)

    with pytest.raises(SessionLockTimeout, match="deactivation deadline"):
        session.deactivate_serial_hooks(timeout_s=0.02)

    release.set()
    owner.join(timeout=2.0)
    assert not owner.is_alive()

    assert session.deactivate_serial_hooks(timeout_s=1.0) == 1
    assert pyboy.fire(0x00, 0x216F) == 1
    assert calls == []


def test_deactivate_hooks_at_removes_registered_hooks_and_disables_raw_hooks() -> None:
    session, pyboy = _session()
    raw_calls: list[object] = []
    session.register_hook("Serial_ExchangeBytes", "serial_event")
    session.serial_hook("Serial_ExchangeBytes", raw_calls.append)

    # EventBus implementations may share one physical dispatcher for several
    # logical callbacks. Assert both callbacks' effects rather than an
    # implementation-specific physical hook count.
    assert pyboy.fire(0x00, 0x216F) >= 1
    assert session.events.count("serial_event") == 1
    assert len(raw_calls) == 1

    session.deactivate_hooks_at("Serial_ExchangeBytes", timeout_s=1.0)

    assert (0x00, 0x216F) not in pyboy._hooks
    assert pyboy.fire(0x00, 0x216F) == 0
    assert session.events.count("serial_event") == 1
    assert len(raw_calls) == 1
    assert session._serial_hooks == []

    # Address cleanup must also invalidate the EventBus slot. A subsequent
    # logical registration needs to install a fresh physical dispatcher.
    session.register_hook("Serial_ExchangeBytes", "serial_event_again")
    assert pyboy.fire(0x00, 0x216F) == 1
    assert session.events.count("serial_event_again") == 1
