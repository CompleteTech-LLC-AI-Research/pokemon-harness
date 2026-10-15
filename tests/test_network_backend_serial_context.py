"""Optional completion-context failures must not become serial-edge failures (#618).

These are ROM-free controls with counted core doubles and real socketpair IO.
They establish backend boundaries, not native core or gameplay qualification.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pokered_harness.link import _network_backend_io_mixin as io_module
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._network_backend_support import _OP_EDGE_RESP

pytestmark = pytest.mark.timing_sensitive


class _FaultJSON:
    """Replace only the normalizer's binding, never the global json module."""

    def __init__(self, stage, error_type=RecursionError):
        self.stage = stage
        self.error_type = error_type
        self.dumps_calls = 0
        self.loads_calls = 0
        self.hits = []

    def dumps(self, value, **kwargs):
        self.dumps_calls += 1
        stage = "initial_dumps" if self.dumps_calls == 1 else "per_key_dumps"
        if self.stage == stage and not self.hits:
            self.hits.append(stage)
            raise self.error_type("normalization secret")
        if self.stage == "utf8_encode" and self.dumps_calls == 2:
            self.hits.append("utf8_encode")
            # A plain string with a lone surrogate fails real UTF-8 encoding.
            return "\ud800"
        return json.dumps(value, **kwargs)

    def loads(self, value):
        self.loads_calls += 1
        if self.stage == "loads" and not self.hits:
            self.hits.append("loads")
            raise self.error_type("normalization secret")
        return json.loads(value)


FAULTS = [
    pytest.param(stage, error, id=f"{stage}-{error.__name__}")
    for stage in ("initial_dumps", "loads", "per_key_dumps")
    for error in (RecursionError, RuntimeError)
] + [pytest.param("utf8_encode", UnicodeEncodeError, id="utf8_encode-UnicodeEncodeError")]


@contextmanager
def _diagnostic_backend(provider, *, max_bytes=1024, enabled=True):
    backend, peer = NetworkBackend.pair()
    try:
        backend.set_serial_transcript_context_provider(provider, max_bytes=max_bytes)
        if enabled:
            backend.enable_serial_transcript(max_entries=16)
        yield backend
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


@pytest.mark.parametrize("stage,error_type", FAULTS)
def test_normalization_stage_errors_are_compact(monkeypatch, stage, error_type):
    codec = _FaultJSON(stage, error_type)
    monkeypatch.setattr(io_module, "json", codec)
    with _diagnostic_backend(lambda: {"pc": 0x1234}) as backend:
        assert backend._serial_completion_context() == {"status": "invalid_result"}
    assert codec.hits == [stage]
    assert codec.dumps_calls == (2 if stage in {"per_key_dumps", "utf8_encode"} else 1)
    assert codec.loads_calls == (0 if stage == "initial_dumps" else 1)


def _finite_nested_context():
    """At most 2081 containers, <64 KiB JSON text; no recursion-limit change."""
    limit = sys.getrecursionlimit()
    assert 100 <= limit <= 2048, "outside the reviewed finite-container execution budget"
    depth = limit + 32
    assert depth <= 2080
    nested = 0
    for index in range(depth):
        nested = [nested] if index % 2 else {"x": nested}
    supplied = {"deep": nested}
    assert depth * 6 + 16 < 64 * 1024
    # Establish the actual stdlib trigger instead of assuming a particular
    # Python encoder's depth behavior. An unexpected non-trigger is a failure.
    with pytest.raises(RecursionError):
        json.dumps(supplied, sort_keys=True, separators=(",", ":"))
    return supplied


def test_finite_plain_nested_context_is_contained():
    supplied = _finite_nested_context()
    with _diagnostic_backend(lambda: supplied) as backend:
        assert backend._serial_completion_context() == {"status": "invalid_result"}


@pytest.mark.parametrize("kind", ["cycle", "non_json", "non_dict"])
def test_invalid_context_shapes_remain_invalid(kind):
    supplied = {"value": object()} if kind == "non_json" else []
    if kind == "cycle":
        supplied = {}
        supplied["cycle"] = supplied
    with _diagnostic_backend(lambda: supplied) as backend:
        assert backend._serial_completion_context() == {"status": "invalid_result"}


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_direct_provider_failure_keeps_its_existing_shape(error_type):
    def provider():
        raise error_type("provider secret")

    with _diagnostic_backend(provider) as backend:
        assert backend._serial_completion_context() == {
            "status": "provider_error",
            "error_type": error_type.__name__,
        }


def test_nested_context_and_public_snapshots_are_detached():
    supplied = {"pc": 0x1234, "serial": {"bits": [0, 1]}}
    with _diagnostic_backend(lambda: supplied) as backend:
        context = backend._serial_completion_context()
        assert context == {"status": "ok", "truncated": False, "value": supplied}
        backend._record_serial_event("control", completion_context=context)
        first = backend.snapshot_stats()["serial_transcript"]["records"][0]
        supplied["serial"]["bits"].append(9)
        first["completion_context"]["value"]["serial"]["bits"].append(8)
        fresh = backend.snapshot_stats()["serial_transcript"]["records"][0]
        assert fresh["completion_context"]["value"]["serial"]["bits"] == [0, 1]


@pytest.mark.parametrize("max_bytes", [1, 2, 7, 96, 4096])
@pytest.mark.parametrize("empty", [False, True], ids=["nonempty", "empty"])
def test_retained_value_budget_and_two_byte_empty_floor(max_bytes, empty):
    supplied = {} if empty else {"a": 1, "oversized": "x" * 5000}
    with _diagnostic_backend(lambda: supplied, max_bytes=max_bytes) as backend:
        context = backend._serial_completion_context()
        value_bytes = json.dumps(context["value"], sort_keys=True, separators=(",", ":")).encode()
        assert context["status"] == "ok"
        assert len(value_bytes) <= max(2, max_bytes)
        if empty:
            assert context == {"status": "ok", "truncated": False, "value": {}}
        elif max_bytes < 7:
            assert context == {"status": "ok", "truncated": True, "value": {}}
        else:
            assert context == {"status": "ok", "truncated": True, "value": {"a": 1}}
        # Envelope fields are deliberately outside the retained-value budget.
        assert len(json.dumps(context, separators=(",", ":")).encode()) > len(value_bytes)


class _Core:
    def __init__(self):
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80
        self.outgoing = 0
        self.applied = []
        self.latched = []
        self.failure = None

    def peek_out_bit(self):
        return self.outgoing

    def apply_external_edge(self, bit):
        self.applied.append(bit)
        if self.failure is not None:
            raise self.failure
        self.SB = bit
        self.SC = 0
        self.transfer_enabled = 0
        return True

    def latch_backend_error(self, error):
        self.latched.append(error)

    def arm(self, outgoing):
        assert self.transfer_enabled == 0
        self.outgoing = outgoing
        self.SC = 0x80
        self.transfer_enabled = 1


@contextmanager
def _completion_pair(owner, provider, *, enabled=True, irq_failure=None):
    master, slave = NetworkBackend.pair()
    core = _Core()
    pair = SimpleNamespace(master=master, slave=slave, core=core, owner=owner, irqs=[], senders=[])

    def irq():
        pair.irqs.append(core.SB)
        if irq_failure is not None:
            raise irq_failure

    try:
        slave.set_serial_transcript_context_provider(provider)
        if enabled:
            slave.enable_serial_transcript(max_entries=32)
        master.start_receiver(local_core=None)
        slave.start_receiver(
            local_core=core,
            irq_callback=irq,
            serial_gate=SerialOperationGate(),
            dispatch_to_owner=owner,
        )
        yield pair
    finally:
        master_stopped = master.stop(timeout_s=1.0)
        slave_stopped = slave.stop(timeout_s=1.0)
        for sender in pair.senders:
            sender.join(timeout=1.0)
            assert not sender.is_alive(), "owned sender survived backend teardown"
        assert master_stopped and slave_stopped
        for backend in (master, slave):
            for worker in (backend._reader, backend._edge_worker):
                assert worker is None or not worker.is_alive()


def _send(pair, owner, bit, *, owner_error=None):
    result = []

    def send():
        try:
            result.append(pair.master.on_edge(our_bit=bit, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - transferred to the test thread.
            result.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    pair.senders.append(sender)
    sender.start()
    if owner:
        deadline = time.monotonic() + 1.0
        # Admission precedes queue publication; observe both, not pending alone.
        while not (
            pair.slave.debug_snapshot()["pending_edge_requests"] == 1
            and pair.slave._edge_queue.qsize() == 1
        ):
            assert time.monotonic() < deadline, "owner request was not queued"
            time.sleep(0.001)
        if owner_error is None:
            assert pair.slave.service_pending_edges(max_edges=1) == 1
        else:
            with pytest.raises(NetworkBackendError) as caught:
                pair.slave.service_pending_edges(max_edges=1)
            assert caught.value.__cause__ is owner_error
    sender.join(timeout=1.0)
    assert not sender.is_alive(), "master did not receive a terminal result"
    return result


def _assert_healthy_completion(pair, count):
    pair.slave.wait_for_wire_idle(timeout=1.0)
    pair.master.wait_for_wire_idle(timeout=1.0)
    assert pair.master.debug_snapshot()["edge_resp_received"] == count
    assert pair.master.connected and pair.slave.connected
    snapshot = pair.slave.debug_snapshot()
    assert snapshot["pending_edge_requests"] == 0
    assert snapshot["edge_resp_sent"] == count
    assert snapshot["irq_callbacks"] == count
    assert snapshot["owner_edge_errors"] == 0
    assert snapshot["owner_edge_applied"] == (count if pair.owner else 0)
    assert snapshot["edge_req_received"] == count
    assert pair.slave._reader_exc is None
    for backend in (pair.master, pair.slave):
        assert backend._reader.is_alive()
        assert backend._edge_worker.is_alive()
    assert pair.core.latched == []
    assert pair.core.transfer_enabled == 0
    assert pair.core.SC == 0
    assert len(pair.core.applied) == count
    assert len(pair.irqs) == count
    records = pair.slave.snapshot_stats()["serial_transcript"]["records"]
    completions = [
        r for r in records if r["event"] in {"owner_edge_applied", "worker_edge_applied"}
    ]
    expected_event = "owner_edge_applied" if pair.owner else "worker_edge_applied"
    assert all(record["event"] == expected_event for record in completions)
    return completions


@pytest.mark.parametrize("owner", [False, True], ids=["worker", "owner"])
@pytest.mark.parametrize("stage,error_type", FAULTS)
def test_normalization_failure_preserves_two_actual_completions(
    monkeypatch, owner, stage, error_type
):
    codec = _FaultJSON(stage, error_type)
    monkeypatch.setattr(io_module, "json", codec)
    calls = []

    def provider():
        calls.append(1)
        return {"pc": 0x1234}

    with _completion_pair(owner, provider) as pair:
        assert _send(pair, owner, 1) == [0]
        first = _assert_healthy_completion(pair, 1)
        assert first[0]["completion_context"] == {"status": "invalid_result"}
        assert first[0]["byte_complete"] is True
        assert first[0]["response_bit"] == 0
        assert pair.core.SB == 1
        assert pair.irqs == [1]
        assert codec.hits == [stage]
        pair.core.arm(1)
        assert _send(pair, owner, 0) == [1]
        completions = _assert_healthy_completion(pair, 2)
        assert len(completions) == 2
        assert completions[1]["completion_context"] == {
            "status": "ok",
            "truncated": False,
            "value": {"pc": 0x1234},
        }
        assert pair.core.applied == [1, 0]
        assert pair.irqs == [1, 0]
        assert pair.core.SB == 0
        assert calls == [1, 1]


@pytest.mark.parametrize("owner", [False, True], ids=["worker", "owner"])
@pytest.mark.parametrize("enabled", [False, True], ids=["disabled", "enabled"])
def test_completion_provider_bypass_and_positive_control(owner, enabled):
    supplied = {"nested": {"bits": [1]}}
    calls = []

    def provider():
        calls.append(1)
        if not enabled:
            raise AssertionError("disabled completion invoked its provider")
        return supplied

    with _completion_pair(owner, provider, enabled=enabled) as pair:
        assert _send(pair, owner, 1) == [0]
        records = _assert_healthy_completion(pair, 1)
        assert calls == ([1] if enabled else [])
        if enabled:
            assert records[0]["completion_context"]["value"] == supplied
            supplied["nested"]["bits"].append(9)
            fresh = pair.slave.snapshot_stats()["serial_transcript"]["records"]
            edge = next(r for r in fresh if r["event"].endswith("edge_applied"))
            assert edge["completion_context"]["value"]["nested"]["bits"] == [1]
        else:
            assert records == []
            assert pair.slave.snapshot_stats()["serial_transcript"]["enabled"] is False


@pytest.mark.parametrize("owner", [False, True], ids=["worker", "owner"])
def test_completion_without_context_provider(owner):
    with _completion_pair(owner, None) as pair:
        assert _send(pair, owner, 1) == [0]
        records = _assert_healthy_completion(pair, 1)
        assert records[0]["completion_context"] is None


@pytest.mark.parametrize("owner", [False, True], ids=["worker", "owner"])
def test_core_apply_failure_is_not_diagnostic_data(owner):
    calls = []
    cause = ValueError("genuine core apply failure")
    with _completion_pair(owner, lambda: calls.append(1)) as pair:
        pair.core.failure = cause
        result = _send(pair, owner, 1, owner_error=cause if owner else None)
        assert len(result) == 1 and isinstance(result[0], NetworkBackendError)
        assert pair.core.applied == [1]
        assert pair.irqs == []
        assert calls == []
        assert not pair.slave.connected
        snapshot = pair.slave.debug_snapshot()
        assert snapshot["edge_resp_sent"] == 0
        assert snapshot["pending_edge_requests"] == 0
        if owner:
            assert pair.core.latched == [cause]
            assert snapshot["owner_edge_errors"] == 1
            assert pair.slave.service_pending_edges(max_edges=1) == 0
        else:
            assert pair.slave._reader_exc is cause
        assert pair.core.applied == [1]


@pytest.mark.parametrize("owner", [False, True], ids=["worker", "owner"])
def test_irq_failure_keeps_owner_and_worker_policies(monkeypatch, owner):
    codec = _FaultJSON("per_key_dumps")
    monkeypatch.setattr(io_module, "json", codec)
    cause = ValueError("genuine IRQ failure")
    with _completion_pair(owner, lambda: {"pc": 1}, irq_failure=cause) as pair:
        result = _send(pair, owner, 1, owner_error=cause if owner else None)
        assert codec.hits == ["per_key_dumps"]
        assert pair.core.applied == [1]
        assert pair.irqs == [1]
        assert pair.core.transfer_enabled == 0
        if owner:
            assert len(result) == 1 and isinstance(result[0], NetworkBackendError)
            assert not pair.slave.connected
            assert pair.core.latched == [cause]
            assert pair.slave.debug_snapshot()["edge_resp_sent"] == 0
            assert pair.slave.service_pending_edges(max_edges=1) == 0
        else:
            assert result == [0]
            _assert_healthy_completion(pair, 1)
            assert pair.slave.debug_snapshot()["irq_callback_errors"] == 1
            assert pair.slave.debug_snapshot()["last_irq_callback_error"] == "ValueError"
        assert pair.core.applied == [1]


@pytest.mark.parametrize("owner", [False, True], ids=["worker", "owner"])
def test_response_write_failure_remains_terminal_after_context_failure(monkeypatch, owner):
    codec = _FaultJSON("loads")
    monkeypatch.setattr(io_module, "json", codec)
    cause = OSError("genuine response write failure")
    with _completion_pair(owner, lambda: {"pc": 1}) as pair:
        original = pair.slave._send_frame

        def fail_response(frame, *, timeout, operation, cancel_event=None):
            if frame[0] == _OP_EDGE_RESP:
                raise cause
            return original(frame, timeout=timeout, operation=operation, cancel_event=cancel_event)

        monkeypatch.setattr(pair.slave, "_send_frame", fail_response)
        result = _send(pair, owner, 1)
        assert len(result) == 1 and isinstance(result[0], NetworkBackendError)
        assert codec.hits == ["loads"]
        assert pair.core.applied == [1]
        assert pair.irqs == [1]
        assert pair.core.transfer_enabled == 0
        assert pair.core.latched == []
        assert not pair.slave.connected
        snapshot = pair.slave.debug_snapshot()
        assert snapshot["edge_resp_sent"] == 0
        assert snapshot["pending_edge_requests"] == 0
        assert snapshot["owner_edge_errors"] == 0
        if owner:
            assert pair.slave._reader_exc is cause


@pytest.mark.parametrize("stage", ["initial_dumps", "loads", "per_key_dumps"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_normalization_does_not_suppress_process_control(monkeypatch, stage, error_type):
    codec = _FaultJSON(stage, error_type)
    monkeypatch.setattr(io_module, "json", codec)
    with (
        _diagnostic_backend(lambda: {"pc": 1}) as backend,
        pytest.raises(error_type, match="normalization secret"),
    ):
        backend._serial_completion_context()
    assert codec.hits == [stage]


@pytest.mark.parametrize("owner", [False, True], ids=["worker", "owner"])
def test_real_nested_failure_preserves_completion_and_next_exchange(owner):
    supplied = _finite_nested_context()
    calls = []

    def provider():
        calls.append(1)
        return supplied if len(calls) == 1 else {"pc": 2}

    with _completion_pair(owner, provider) as pair:
        assert _send(pair, owner, 1) == [0]
        records = _assert_healthy_completion(pair, 1)
        assert records[0]["completion_context"] == {"status": "invalid_result"}
        assert pair.core.applied == [1] and pair.irqs == [1]
        pair.core.arm(1)
        assert _send(pair, owner, 0) == [1]
        records = _assert_healthy_completion(pair, 2)
        assert records[1]["completion_context"] == {
            "status": "ok",
            "truncated": False,
            "value": {"pc": 2},
        }
        assert pair.core.applied == [1, 0] and pair.irqs == [1, 0]
        assert calls == [1, 1]
