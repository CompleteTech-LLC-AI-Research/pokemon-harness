"""Adversarial regressions retained with independent reviewer's permission."""
import threading

import pytest
from pyboy.core.serial import Serial, SerialBackendError

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


@pytest.mark.parametrize("action", ["cancel", "external", "rearm"])
def test_owner_change_revalidates_pending_edge(action):
    class Backend:
        def on_edge(self, *_):
            raise AssertionError("stale edge committed")

    s = Serial(backend=Backend())
    s.set_SB(0xA5)
    s.set_SC(0x81)
    seen = []

    def pump(event):
        seen.append(event)
        s.set_SC({"cancel": 0, "external": 0x80, "rearm": 0x81}[action])

    s.set_owner_pump(pump)
    assert s.tick(s.clock_target) is False
    s.check_error()
    assert len(seen) == 1
    assert s._bits_remaining == (0 if action == "cancel" else 8)
    assert s._shift_register == 0xA5


def test_off_owner_fault_preserves_uncommitted_edge():
    s = Serial()
    s.set_SB(0xA5)
    s.set_SC(0x81)
    s.set_owner_pump(lambda event: pytest.fail("wrong owner executed callback"))
    thread = threading.Thread(target=lambda: s.tick(s.clock_target))
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    with pytest.raises(SerialBackendError) as raised:
        s.check_error()
    assert "off owner" in str(raised.value.__cause__)
    assert s._bits_remaining == 8 and s._shift_register == 0xA5


def test_recursive_serial_tick_latches_first_fault_without_shift():
    s = Serial()
    s.set_SB(0xA5)
    s.set_SC(0x81)
    s.set_owner_pump(lambda event: s.tick(event[2] + 1))
    assert s.tick(s.clock_target) is False
    with pytest.raises(SerialBackendError) as raised:
        s.check_error()
    assert "recursive serial" in str(raised.value.__cause__)
    assert not s.owner_pump_active
    assert s._bits_remaining == 8 and s._shift_register == 0xA5


def test_callback_prefix_is_not_rolled_back_after_fault():
    s = Serial()
    s.set_SB(0xA5)
    s.set_SC(0x81)

    def pump(event):
        s.set_SC(0x80)
        s.apply_external_edge(0)
        raise ValueError("fault after serviced incoming prefix")

    s.set_owner_pump(pump)
    assert s.tick(s.clock_target) is False
    with pytest.raises(SerialBackendError) as raised:
        s.check_error()
    assert isinstance(raised.value.__cause__, ValueError)
    assert s._bits_remaining == 7 and s._shift_register == 0x4A
    with pytest.raises(SerialBackendError):
        s.apply_external_edge(1)
