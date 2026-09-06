"""Focused source-PyBoy serial owner-dispatch regressions."""

from pyboy.core.serial import Serial


def test_owner_dispatch_runs_after_slave_byte_completion():
    """A queued completion IRQ needs the next safe owner boundary.

    The eighth external edge clears ``transfer_enabled`` before the serial
    interrupt is delivered.  Owner dispatch must nevertheless invoke its
    installed callback so the completion can be observed without waiting for
    an unrelated frame-level pump.
    """
    serial = Serial()
    callbacks: list[str] = []
    serial.owner_dispatch_callback = lambda: callbacks.append("called")
    serial.owner_dispatch_enabled = True
    serial.set_SC(0x80)  # External-clock (slave) transfer.

    for _ in range(8):
        serial.apply_external_edge(0)

    assert serial.transfer_enabled == 0
    serial.dispatch_owner()

    assert callbacks == ["called"]
