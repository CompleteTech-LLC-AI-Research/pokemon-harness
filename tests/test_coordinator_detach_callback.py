"""Threading regressions for coordinated serial teardown callbacks."""

from __future__ import annotations

import threading

import pytest

from pokered_harness.link.serial_coordinator import LockstepCoordinator
from pokered_harness.link.serial_core import CYCLES_PER_BYTE_DMG, SerialCore

pytestmark = pytest.mark.timing_sensitive


def _arm_pair() -> tuple[SerialCore, SerialCore]:
    master = SerialCore()
    slave = SerialCore()
    master.set_SB(0xA5)
    slave.set_SB(0x5A)
    master.set_SC(0x81)
    slave.set_SC(0x80)
    return master, slave


def test_idle_detach_accepts_zero_timeout() -> None:
    """An already-idle coordinator can detach without a positive budget."""
    master, slave = _arm_pair()
    coordinator = LockstepCoordinator(master, slave)
    master_backend = master.backend
    slave_backend = slave.backend

    coordinator.detach(timeout_s=0.0)

    assert coordinator.attached is False
    assert master.backend is not master_backend
    assert slave.backend is not slave_backend


def test_detach_waits_for_completion_callback_without_coordinator_deadlock() -> None:
    """A callback may re-enter the coordinator while detach waits for it."""
    callback_started = threading.Event()
    callback_reentered = threading.Event()
    callback_release = threading.Event()
    callback_finished = threading.Event()
    detach_finished = threading.Event()
    callback_after_detach: list[bool] = []
    detach_errors: list[BaseException] = []
    holder: dict[str, LockstepCoordinator] = {}

    def on_slave_complete() -> None:
        callback_started.set()
        callback_after_detach.append(detach_finished.is_set())
        # This is the lock-order regression: detach must not hold the
        # coordinator lock while waiting for this callback to return.
        holder["coordinator"].attach()
        callback_reentered.set()
        assert callback_release.wait(timeout=2.0)
        callback_finished.set()

    master, slave = _arm_pair()
    coordinator = LockstepCoordinator(
        master,
        slave,
        on_b_transfer_complete=on_slave_complete,
    )
    holder["coordinator"] = coordinator
    backend = master.backend
    real_deactivate = backend.deactivate
    deactivate_entered = threading.Event()
    deactivate_release = threading.Event()

    def gated_deactivate() -> None:
        deactivate_entered.set()
        assert deactivate_release.wait(timeout=2.0)
        real_deactivate()

    backend.deactivate = gated_deactivate  # type: ignore[method-assign]

    tick_thread = threading.Thread(
        target=lambda: master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG),
        name="test-coordinator-tick",
    )
    tick_thread.start()
    assert callback_started.wait(timeout=2.0)

    def detach_coordinator() -> None:
        try:
            coordinator.detach()
        except BaseException as exc:  # noqa: BLE001 - surface thread failures
            detach_errors.append(exc)
        finally:
            detach_finished.set()

    detach_thread = threading.Thread(target=detach_coordinator, name="test-coordinator-detach")
    detach_thread.start()
    try:
        assert deactivate_entered.wait(timeout=2.0)
        deactivate_release.set()
        assert callback_reentered.wait(timeout=2.0)
        # The admitted callback is still blocked, so successful detach must
        # not have returned and restored either backend yet.
        assert not detach_finished.is_set()
        assert not callback_finished.is_set()
    finally:
        callback_release.set()

    tick_thread.join(timeout=2.0)
    detach_thread.join(timeout=2.0)
    assert not tick_thread.is_alive()
    assert not detach_thread.is_alive()
    assert callback_finished.is_set()
    assert detach_finished.is_set()
    assert not detach_errors
    assert callback_after_detach == [False]
    assert coordinator.attached is False


def test_backend_deactivate_waits_for_admitted_completion_callback() -> None:
    """The standalone backend barrier drains an admitted callback."""
    callback_calls: list[None] = []
    callback_started = threading.Event()
    callback_release = threading.Event()

    def on_slave_complete() -> None:
        callback_started.set()
        callback_calls.append(None)
        callback_release.wait(timeout=2.0)

    master, slave = _arm_pair()
    coordinator = LockstepCoordinator(
        master,
        slave,
        on_b_transfer_complete=on_slave_complete,
    )
    backend = master.backend

    # Drive the transfer in one thread and prove that standalone backend
    # deactivation cannot return while the callback is admitted and blocked.
    tick_thread = threading.Thread(
        target=lambda: master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG),
        name="test-coordinator-selected-callback",
    )
    tick_thread.start()
    assert callback_started.wait(timeout=2.0)

    deactivated = threading.Event()
    deactivate_errors: list[BaseException] = []

    def deactivate_backend() -> None:
        try:
            backend.deactivate(wait=True)
        except BaseException as exc:  # noqa: BLE001 - surface thread failures
            deactivate_errors.append(exc)
        finally:
            deactivated.set()

    deactivate_thread = threading.Thread(
        target=deactivate_backend,
        name="test-coordinator-selected-deactivate",
    )
    deactivate_thread.start()
    assert not deactivated.wait(timeout=0.1)
    callback_release.set()

    tick_thread.join(timeout=2.0)
    deactivate_thread.join(timeout=2.0)
    assert not tick_thread.is_alive()
    assert not deactivate_thread.is_alive()
    assert callback_calls == [None]
    assert deactivated.is_set()
    assert not deactivate_errors
    coordinator.detach()
    assert coordinator.attached is False


def test_callback_detach_is_retryable_and_preserves_backend_until_later_retry() -> None:
    """A callback cannot report successful teardown against live cores."""
    callback_started = threading.Event()
    callback_checked = threading.Event()
    callback_release = threading.Event()
    callback_errors: list[RuntimeError] = []
    coordinator: LockstepCoordinator
    master: SerialCore

    def on_slave_complete() -> None:
        callback_started.set()
        try:
            coordinator.detach(timeout_s=0.1)
        except RuntimeError as error:
            callback_errors.append(error)
        finally:
            callback_checked.set()
        assert coordinator.attached is True
        assert master.backend is backend
        callback_release.wait(timeout=2.0)

    master, slave = _arm_pair()
    coordinator = LockstepCoordinator(
        master,
        slave,
        on_b_transfer_complete=on_slave_complete,
    )
    backend = master.backend
    tick_thread = threading.Thread(
        target=lambda: master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG),
        name="test-coordinator-reentrant-detach",
    )
    tick_thread.start()
    assert callback_started.wait(timeout=2.0)
    try:
        assert callback_checked.wait(timeout=2.0)
        assert callback_errors
        assert "retry" in str(callback_errors[0])
        assert coordinator.attached is True
        assert master.backend is backend
    finally:
        callback_release.set()
    tick_thread.join(timeout=2.0)
    assert not tick_thread.is_alive()

    coordinator.detach(timeout_s=1.0)
    assert coordinator.attached is False
    assert master.backend is not backend


def test_detach_timeout_keeps_inactive_backends_for_retry() -> None:
    """A bounded timeout retains ownership until callbacks have drained."""
    callback_started = threading.Event()
    callback_release = threading.Event()

    def on_slave_complete() -> None:
        callback_started.set()
        callback_release.wait(timeout=2.0)

    master, slave = _arm_pair()
    coordinator = LockstepCoordinator(
        master,
        slave,
        on_b_transfer_complete=on_slave_complete,
    )
    backend = master.backend
    tick_thread = threading.Thread(
        target=lambda: master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG),
        name="test-coordinator-timeout-callback",
    )
    tick_thread.start()
    assert callback_started.wait(timeout=2.0)

    with pytest.raises(RuntimeError, match="deadline"):
        coordinator.detach(timeout_s=0.01)
    assert coordinator.attached is True
    assert master.backend is backend
    assert backend.active is False

    callback_release.set()
    tick_thread.join(timeout=2.0)
    assert not tick_thread.is_alive()
    coordinator.detach(timeout_s=1.0)
    assert coordinator.attached is False
    assert master.backend is not backend
