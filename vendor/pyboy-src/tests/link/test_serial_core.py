"""Regression contract for source-PyBoy owner-dispatch wakes.

These tests deliberately describe the post-response owner-wake boundary used
by the TCP backend.  They must run against the source runtime
(``PYBOY_NO_CYTHON=1``), because the patched serial core is the supported
link-cable implementation.
"""

from pyboy.core import serial as serial_module


def test_owner_dispatch_signal_coalesces_wakes_and_skips_idle_batches():
    """Several transport notifications produce one owner callback, never idle polls."""
    signal_type = getattr(serial_module, "OwnerDispatchSignal", None)
    assert signal_type is not None, "source serial must expose OwnerDispatchSignal"
    signal = signal_type()
    callbacks: list[str] = []
    core = serial_module.Serial()
    core.owner_dispatch_enabled = True
    core.owner_dispatch_callback = lambda: callbacks.append("drain")
    core.owner_dispatch_signal = signal

    # A CPU batch without queued transport work must remain entirely native.
    core.dispatch_owner()
    assert callbacks == []

    # A boolean wake is deliberately coalescing: the owner drains all queued
    # backend work in one callback, then the next idle batch does nothing.
    signal.notify()
    signal.notify()
    core.dispatch_owner()
    assert callbacks == ["drain"]
    core.dispatch_owner()
    assert callbacks == ["drain"]


def test_owner_dispatch_after_eighth_external_edge_requires_a_signal():
    """The post-byte IRQ wake survives disarm without polling every CPU batch."""
    signal_type = getattr(serial_module, "OwnerDispatchSignal", None)
    assert signal_type is not None, "source serial must expose OwnerDispatchSignal"
    signal = signal_type()
    callbacks: list[str] = []
    core = serial_module.Serial()
    core.owner_dispatch_enabled = True
    core.owner_dispatch_callback = lambda: callbacks.append("drain")
    core.owner_dispatch_signal = signal
    core.set_SB(0x00)
    core.set_SC(0x80)  # armed external-clock (slave) transfer

    for _ in range(8):
        core.apply_external_edge(1)
    assert core.transfer_enabled == 0

    # The completion has disarmed SC bit 7, so dispatch must neither depend
    # on it nor become an idle Python callback on every instruction batch.
    core.dispatch_owner()
    assert callbacks == []
    signal.notify()
    signal.notify()
    core.dispatch_owner()
    assert callbacks == ["drain"]
    core.dispatch_owner()
    assert callbacks == ["drain"]
