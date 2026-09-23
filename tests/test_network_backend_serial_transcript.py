"""Serial transcript recording, completion IRQ ordering and write failures (#128).

Split from ``tests/test_network_backend.py`` for #128 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import json
import struct
import threading
import time

import pytest

from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
)
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._network_backend_support import (
    _OP_EDGE_REQ,
    _OP_EDGE_RESP,
    _CompletingSlaveCore,
)


def test_two_serialcores_exchange_byte_via_network_backend():
    """Canonical proof: two :class:`SerialCore` instances, each paired
    with its own :class:`NetworkBackend`, exchange a full byte over
    a socketpair. Master-mode edges on one side are handled by the
    peer's reader thread driving its local ``SerialCore`` — exactly
    the shape a two-process two-PyBoy setup needs.
    """
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    ba, bb = NetworkBackend.pair()

    master = SerialCore(backend=ba)
    slave = SerialCore()  # no outgoing backend; driven by bb's reader

    master.set_SB(0xAA)
    slave.set_SB(0x55)
    master.set_SC(0x81)  # internal clock
    slave.set_SC(0x80)  # external clock

    # Record slave-side IRQ fires.
    slave_irqs: list[int] = []

    def slave_irq():
        slave_irqs.append(1)

    ba.start_receiver(local_core=master)  # not strictly needed; keeps
    # reader idle for master side
    bb.start_receiver(local_core=slave, irq_callback=slave_irq)

    try:
        irq = master.tick(CYCLES_PER_BYTE_DMG)
        assert irq is True, "master didn't complete transfer"

        # A response can arrive before the worker accounts for it and fires IRQ.
        bb.wait_for_wire_idle(timeout=1.0)
        assert master.SB == 0x55, f"master got 0x{master.SB:02x}"
        assert slave.SB == 0xAA, f"slave got 0x{slave.SB:02x}"
        assert slave_irqs == [1], "slave IRQ callback should fire once"
    finally:
        ba.stop()
        bb.stop()


def test_serial_transcript_is_disabled_by_default_and_validates_capacity():
    """Transcript diagnostics are opt-in and have a deliberately small API."""
    backend, peer = NetworkBackend.pair()
    try:
        expected_disabled = {
            "enabled": False,
            "capacity": 0,
            "dropped": 0,
            "records": [],
        }
        assert backend.snapshot_stats()["serial_transcript"] == expected_disabled
        assert backend.debug_snapshot()["serial_transcript"] == expected_disabled

        with pytest.raises(ValueError, match="between 1 and"):
            backend.enable_serial_transcript(max_entries=0)
        with pytest.raises(ValueError, match="between 1 and"):
            backend.enable_serial_transcript(max_entries=4097)
        assert backend.snapshot_stats()["serial_transcript"] == expected_disabled
    finally:
        backend.stop()
        peer.stop()


def test_serial_transcript_ring_drops_oldest_records_in_sequence_order():
    """A small transcript remains bounded while preserving local event order."""
    backend, peer_backend = NetworkBackend.pair()
    backend.enable_serial_transcript(max_entries=2)

    def reply_once() -> None:
        frame = peer_backend._recv_exactly(2)
        assert frame == struct.pack(">BB", _OP_EDGE_REQ, 1)
        peer_backend._sock.sendall(struct.pack(">BB", _OP_EDGE_RESP, 0))

    peer = threading.Thread(target=reply_once, daemon=True)
    peer.start()
    backend.start_receiver(local_core=None)
    try:
        assert backend.on_edge(our_bit=1, our_role=1) == 0
        peer.join(timeout=1.0)
        assert not peer.is_alive()

        transcript = backend.snapshot_stats()["serial_transcript"]
        records = transcript["records"]
        assert transcript["enabled"] is True
        assert transcript["capacity"] == 2
        assert transcript["dropped"] == 1
        assert [record["sequence"] for record in records] == [2, 3]
        assert [record["event"] for record in records] == [
            "edge_resp_received",
            "edge_resp_consumed",
        ]
        timestamps = [record["monotonic_s"] for record in records]
        assert timestamps == sorted(timestamps)

        backend.disable_serial_transcript()
        assert backend.snapshot_stats()["serial_transcript"] == {
            "enabled": False,
            "capacity": 0,
            "dropped": 0,
            "records": [],
        }
    finally:
        peer.join(timeout=1.0)
        backend.stop()
        peer_backend.stop()


def test_serial_transcript_copies_worker_edge_byte_completion_and_irq_outcome():
    """Snapshots retain a local, immutable copy of worker-side edge evidence."""
    master_backend, slave_backend = NetworkBackend.pair()
    slave_core = _CompletingSlaveCore()
    slave_irqs: list[int] = []
    master_backend.enable_serial_transcript(max_entries=16)
    slave_backend.enable_serial_transcript(max_entries=16)
    master_backend.start_receiver(local_core=None)
    slave_backend.start_receiver(
        local_core=slave_core,
        irq_callback=lambda: slave_irqs.append(1),
    )
    try:
        assert master_backend.on_edge(our_bit=1, our_role=1) == 0
        slave_backend.wait_for_wire_idle(timeout=1.0)
        assert slave_irqs == [1]

        records = slave_backend.snapshot_stats()["serial_transcript"]["records"]
        worker_edge = next(record for record in records if record["event"] == "worker_edge_applied")
        assert worker_edge["direction"] == "peer_to_local"
        assert worker_edge["edge_bit"] == 1
        assert worker_edge["response_bit"] == 0
        assert worker_edge["byte_complete"] is True
        assert worker_edge["local_state_before"] == {
            "core_present": True,
            "type": "_CompletingSlaveCore",
            "transfer_enabled": 1,
            "internal_clock": 0,
            "SB": 0,
            "SC": 0x80,
        }
        assert worker_edge["local_state_after"]["transfer_enabled"] == 0
        assert worker_edge["local_state_after"]["SB"] == 1
        assert any(
            record["event"] == "irq_callback" and record["outcome"] == "success"
            for record in records
        )

        # The public snapshot owns both outer records and nested core state.
        worker_edge["local_state_before"]["SB"] = 99
        fresh_records = slave_backend.snapshot_stats()["serial_transcript"]["records"]
        fresh_worker_edge = next(
            record for record in fresh_records if record["event"] == "worker_edge_applied"
        )
        assert fresh_worker_edge["local_state_before"]["SB"] == 0
    finally:
        master_backend.stop()
        slave_backend.stop()


def test_worker_slave_completion_irq_precedes_delayed_edge_response_write(monkeypatch):
    """The eighth slave edge completes locally before its TCP reply can unblock.

    A response write is transport acknowledgement, not emulated hardware.
    Holding it proves the compatibility worker exposes the completed SB/SC
    state and serial IRQ before the master is allowed to continue.
    """
    master_backend, slave_backend = NetworkBackend.pair()
    slave_core = _CompletingSlaveCore()
    irq_fired = threading.Event()
    response_write_entered = threading.Event()
    release_response_write = threading.Event()
    result: list[object] = []
    order: list[str] = []
    original_send_frame = slave_backend._send_frame

    def delayed_response_write(frame, *, timeout, operation, cancel_event=None):
        if frame[0] == _OP_EDGE_RESP:
            order.append("response_write")
            response_write_entered.set()
            assert release_response_write.wait(timeout=1.0)
        return original_send_frame(
            frame,
            timeout=timeout,
            operation=operation,
            cancel_event=cancel_event,
        )

    def irq_callback() -> None:
        order.append("irq")
        irq_fired.set()

    def send_master_edge() -> None:
        try:
            result.append(master_backend.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - assert on the owning test thread
            result.append(exc)

    monkeypatch.setattr(slave_backend, "_send_frame", delayed_response_write)
    master_backend.start_receiver(local_core=None)
    slave_backend.start_receiver(local_core=slave_core, irq_callback=irq_callback)
    sender = threading.Thread(target=send_master_edge, daemon=True)
    try:
        sender.start()
        assert response_write_entered.wait(timeout=1.0)
        assert irq_fired.is_set(), "slave IRQ waited for EDGE_RESP TCP write"
        assert order == ["irq", "response_write"]
        assert slave_core.transfer_enabled == 0
        assert slave_core.SB == 1

        release_response_write.set()
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert result == [0]
        assert slave_backend.debug_snapshot()["irq_callbacks"] == 1
    finally:
        release_response_write.set()
        sender.join(timeout=1.0)
        master_backend.stop()
        slave_backend.stop()


def test_owner_dispatch_completion_irq_precedes_delayed_edge_response_write(monkeypatch):
    """The production owner path has the same eighth-edge ordering contract."""
    master_backend, slave_backend = NetworkBackend.pair()
    slave_core = _CompletingSlaveCore()
    irq_fired = threading.Event()
    response_write_entered = threading.Event()
    release_response_write = threading.Event()
    result: list[object] = []
    order: list[str] = []
    original_send_frame = slave_backend._send_frame

    def delayed_response_write(frame, *, timeout, operation, cancel_event=None):
        if frame[0] == _OP_EDGE_RESP:
            order.append("response_write")
            response_write_entered.set()
            assert release_response_write.wait(timeout=1.0)
        return original_send_frame(
            frame,
            timeout=timeout,
            operation=operation,
            cancel_event=cancel_event,
        )

    def irq_callback() -> None:
        order.append("irq")
        irq_fired.set()

    def send_master_edge() -> None:
        try:
            result.append(master_backend.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - assert on the owning test thread
            result.append(exc)

    monkeypatch.setattr(slave_backend, "_send_frame", delayed_response_write)
    master_backend.start_receiver(local_core=None)
    slave_backend.start_receiver(
        local_core=slave_core,
        irq_callback=irq_callback,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    sender = threading.Thread(target=send_master_edge, daemon=True)
    try:
        sender.start()
        deadline = time.monotonic() + 1.0
        while slave_backend.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline, "owner dispatch never received EDGE_REQ"
            time.sleep(0.001)
        assert slave_backend.service_pending_edges() == 1
        assert response_write_entered.wait(timeout=1.0)
        assert irq_fired.is_set(), "owner IRQ waited for EDGE_RESP TCP write"
        assert order == ["irq", "response_write"]
        assert slave_core.transfer_enabled == 0
        assert slave_core.SB == 1

        release_response_write.set()
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert result == [0]
        assert slave_backend.debug_snapshot()["irq_callbacks"] == 1
    finally:
        release_response_write.set()
        sender.join(timeout=1.0)
        master_backend.stop()
        slave_backend.stop()


def test_worker_response_write_failure_closes_after_local_completion_irq(monkeypatch):
    """A failed EDGE_RESP remains terminal after the authentic local IRQ."""
    master_backend, slave_backend = NetworkBackend.pair()
    slave_core = _CompletingSlaveCore()
    irq_fired = threading.Event()
    result: list[object] = []
    original_send_frame = slave_backend._send_frame

    def failed_response_write(frame, *, timeout, operation, cancel_event=None):
        if frame[0] == _OP_EDGE_RESP:
            raise OSError("forced EDGE_RESP write failure")
        return original_send_frame(
            frame,
            timeout=timeout,
            operation=operation,
            cancel_event=cancel_event,
        )

    def send_master_edge() -> None:
        try:
            result.append(master_backend.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - assert on the owning test thread
            result.append(exc)

    monkeypatch.setattr(slave_backend, "_send_frame", failed_response_write)
    master_backend.start_receiver(local_core=None)
    slave_backend.start_receiver(local_core=slave_core, irq_callback=irq_fired.set)
    sender = threading.Thread(target=send_master_edge, daemon=True)
    try:
        sender.start()
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert irq_fired.is_set()
        assert slave_core.transfer_enabled == 0
        assert result and isinstance(result[0], NetworkBackendError)
        assert not slave_backend.connected
        assert slave_backend.debug_snapshot()["edge_resp_sent"] == 0
    finally:
        sender.join(timeout=1.0)
        master_backend.stop()
        slave_backend.stop()


def test_owner_response_write_failure_closes_after_local_completion_irq(monkeypatch):
    """Production owner completion survives a terminal response-worker failure."""
    master_backend, slave_backend = NetworkBackend.pair()
    slave_core = _CompletingSlaveCore()
    irq_fired = threading.Event()
    result: list[object] = []
    original_send_frame = slave_backend._send_frame

    def failed_response_write(frame, *, timeout, operation, cancel_event=None):
        if frame[0] == _OP_EDGE_RESP:
            raise OSError("forced owner EDGE_RESP write failure")
        return original_send_frame(
            frame,
            timeout=timeout,
            operation=operation,
            cancel_event=cancel_event,
        )

    def send_master_edge() -> None:
        try:
            result.append(master_backend.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - assert on the owning test thread
            result.append(exc)

    monkeypatch.setattr(slave_backend, "_send_frame", failed_response_write)
    master_backend.start_receiver(local_core=None)
    slave_backend.start_receiver(
        local_core=slave_core,
        irq_callback=irq_fired.set,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    sender = threading.Thread(target=send_master_edge, daemon=True)
    try:
        sender.start()
        deadline = time.monotonic() + 1.0
        while slave_backend.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline, "owner dispatch never received EDGE_REQ"
            time.sleep(0.001)
        assert slave_backend.service_pending_edges() == 1
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert irq_fired.is_set()
        assert slave_core.transfer_enabled == 0
        assert result and isinstance(result[0], NetworkBackendError)
        assert not slave_backend.connected
        assert slave_backend.debug_snapshot()["edge_resp_sent"] == 0
    finally:
        sender.join(timeout=1.0)
        master_backend.stop()
        slave_backend.stop()


def _complete_worker_edge_with_transcript_context(
    provider,
    *,
    max_bytes: int = 1024,
) -> dict[str, object]:
    """Return one completed worker record produced with a context provider."""
    master_backend, slave_backend = NetworkBackend.pair()
    slave_core = _CompletingSlaveCore()
    master_backend.enable_serial_transcript(max_entries=16)
    slave_backend.enable_serial_transcript(max_entries=16)
    slave_backend.set_serial_transcript_context_provider(
        provider,
        max_bytes=max_bytes,
    )
    master_backend.start_receiver(local_core=None)
    slave_backend.start_receiver(local_core=slave_core)
    try:
        assert master_backend.on_edge(our_bit=1, our_role=1) == 0
        slave_backend.wait_for_wire_idle(timeout=1.0)
        records = slave_backend.snapshot_stats()["serial_transcript"]["records"]
        return next(record for record in records if record["event"] == "worker_edge_applied")
    finally:
        master_backend.stop()
        slave_backend.stop()


def test_serial_transcript_completion_copies_provider_context():
    """Completed-byte records retain a deep copied provider context."""
    supplied = {
        "pc": 0x1234,
        "serial": {"ignoring_initial_data": 1, "byte_ordinal": 3},
    }

    record = _complete_worker_edge_with_transcript_context(lambda: supplied)

    assert record["byte_complete"] is True
    assert record["completion_context"] == {
        "status": "ok",
        "truncated": False,
        "value": {
            "pc": 0x1234,
            "serial": {"ignoring_initial_data": 1, "byte_ordinal": 3},
        },
    }
    # The diagnostic record must not retain aliases into the provider's data.
    supplied["pc"] = 0xFFFF
    supplied["serial"]["byte_ordinal"] = 99
    assert record["completion_context"]["value"] == {
        "pc": 0x1234,
        "serial": {"ignoring_initial_data": 1, "byte_ordinal": 3},
    }


def test_serial_transcript_context_provider_failure_is_worker_safe():
    """A provider exception is recorded compactly and cannot kill the worker."""

    def provider() -> dict[str, object]:
        raise RuntimeError("provider secret must not escape diagnostics")

    record = _complete_worker_edge_with_transcript_context(provider)

    assert record["byte_complete"] is True
    assert record["completion_context"] == {
        "status": "provider_error",
        "error_type": "RuntimeError",
    }
    assert "provider secret" not in repr(record["completion_context"])


def test_serial_transcript_context_provider_is_disabled_by_default_and_bounded():
    """Provider work is opt-in; enabled context has a strict serialized bound."""
    backend, peer = NetworkBackend.pair()
    calls: list[object] = []

    def disabled_provider() -> dict[str, object]:
        calls.append(object())
        return {"pc": 0x1234}

    try:
        backend.set_serial_transcript_context_provider(disabled_provider, max_bytes=96)
        assert backend.snapshot_stats()["serial_transcript"] == {
            "enabled": False,
            "capacity": 0,
            "dropped": 0,
            "records": [],
        }
        # A disabled transcript must not invoke a provider even if a caller
        # reaches the completion-recording path directly.
        backend._record_serial_event("worker_edge_applied", byte_complete=True)
        assert calls == []
    finally:
        backend.stop()
        peer.stop()

    supplied = {"pc": 0x1234, "oversized": "x" * 10_000}
    record = _complete_worker_edge_with_transcript_context(lambda: supplied, max_bytes=96)
    context = record["completion_context"]
    assert context["status"] == "ok"
    assert context["truncated"] is True
    assert context["value"]["pc"] == 0x1234
    encoded = json.dumps(
        context["value"],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= 96
    supplied["pc"] = 0xFFFF
    assert context["value"]["pc"] == 0x1234


def test_multiple_bytes_exchange():
    """Two cores exchange three bytes in a row via the reader-thread
    model. Proves the backend works across multiple transfers without
    resetting."""
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    ba, bb = NetworkBackend.pair()
    master = SerialCore(backend=ba)
    slave = SerialCore()

    ba.start_receiver(local_core=master)
    bb.start_receiver(local_core=slave)

    try:
        for master_byte, slave_byte in [(0x01, 0xFE), (0xAA, 0x55), (0x42, 0x24)]:
            master.set_SB(master_byte)
            slave.set_SB(slave_byte)
            master.set_SC(0x81)
            slave.set_SC(0x80)
            master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG)
            bb.wait_for_wire_idle(timeout=1.0)
            assert master.SB == slave_byte, (
                f"master expected 0x{slave_byte:02x}, got 0x{master.SB:02x}"
            )
            assert slave.SB == master_byte, (
                f"slave expected 0x{master_byte:02x}, got 0x{slave.SB:02x}"
            )
    finally:
        ba.stop()
        bb.stop()
