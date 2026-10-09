"""Authored NOTRUN escalation controls with controlled kernel operations.

Real owner/budget/stop code is used; pidfd poll/signal and channels are doubles.
These cannot establish Linux lineage, actual bounded teardown or producer proof.
"""

import signal
from types import SimpleNamespace

import pytest

from scripts.production_gate_parallel_measurement_budget import MeasurementBudget
from scripts.production_gate_parallel_probe_descriptors import RetainedDescriptor
from scripts.production_gate_parallel_probe_owner import ProbePeer
from scripts.production_gate_parallel_probe_stopping import ProbeStopState
from tests.test_parallel_runtime_probe_owner import owner_case


@pytest.mark.parametrize(
    "case",
    [
        "failure",
        "late_deadline",
        "tightening",
        "unknown_clock",
        "signal_refusal",
        "poll_refusal",
        "unvalidated",
        "blocked_channel",
    ],
)
def test_real_owner_stop_uses_original_bound_and_only_exact_retained_instances(
    tmp_path, monkeypatch, case
):
    owner, now = owner_case(tmp_path)
    holder_duplicate = RetainedDescriptor(1004, instance=(200, 2), role="holder", validated=True)
    guardian = RetainedDescriptor(1005, instance=(201, 3), role="guardian", validated=True)
    payload = RetainedDescriptor(1006, instance=(202, 4), role="payload", validated=True)
    owner.custody.roles.update(holder=holder_duplicate, guardian=guardian, payload=payload)
    original_records = [owner.holder_pin, holder_duplicate, guardian, payload]
    signals, refused = [], []

    def poll_instance(record):
        if case == "poll_refusal" and record is owner.holder_pin:
            raise OSError("controlled original binding observation refusal")
        return False

    def signal_instance(record, number):
        if case == "signal_refusal" and record is guardian and not refused:
            refused.append((record.instance, number))
            raise OSError("controlled first signal refusal")
        signals.append((record.instance, number))

    monkeypatch.setattr(RetainedDescriptor, "exited", poll_instance)
    monkeypatch.setattr(RetainedDescriptor, "signal", signal_instance)
    owner.custody.refuse("controlled operation failure")
    tightened = [120.0]
    if case == "tightening":
        owner.budget = MeasurementBudget(
            100, 120, clock=lambda: now[0], cleanup_limit=lambda: tightened[0]
        )
    if case == "late_deadline":
        owner.custody.failures.clear()
        now[0] = 110.0
    elif case == "unknown_clock":
        now[0] = float("nan")
    elif case == "unvalidated":
        payload.validated = False
    elif case == "blocked_channel":

        def blocked_send(_raw):
            raise BlockingIOError("controlled full control channel")

        owner.peers = [ProbePeer(SimpleNamespace(send=blocked_send), record=guardian)]
    owner._request_stop()
    known = {(200, 2), (201, 3)} if case == "unvalidated" else {(200, 2), (201, 3), (202, 4)}
    first_term = {pair for pair, number in signals if number == signal.SIGTERM}
    assert first_term == (known - {(201, 3)} if case == "signal_refusal" else known)
    assert len(first_term) == sum(number == signal.SIGTERM for _, number in signals), (
        "duplicate H pins must not signal twice"
    )
    assert owner.custody.failures and not owner.operation_succeeded()
    assert all(not record.closed for record in original_records)
    if case == "unknown_clock":
        assert {pair for pair, number in signals if number == signal.SIGKILL} == known
        assert owner.stop_state.unknown_clock
        return
    first_bound = owner.stop_state.kill_bound
    expected_start = 100.0 if case == "late_deadline" else 10.0
    assert owner.stop_state.started == expected_start and first_bound == expected_start + 12
    if case == "signal_refusal":
        now[0] += 0.1
        owner._request_stop()
        assert ((201, 3), signal.SIGTERM) in signals
        assert owner.stop_state.kill_bound == first_bound
    if case == "tightening":
        tightened[0] = 11.0
        now[0] = 11.0
    else:
        now[0] = first_bound
    owner._request_stop()
    assert {pair for pair, number in signals if number == signal.SIGKILL} == known
    assert owner.stop_state.kill_bound <= first_bound
    before = list(signals)
    owner._request_stop()
    assert signals == before, "successful signals are latched; refusals remain retryable"
    assert all(not record.closed for record in original_records)


@pytest.mark.parametrize("grace", [True, -1, 6, float("nan"), float("inf")])
def test_unknown_or_unadmitted_grace_cannot_create_a_stop_budget(grace):
    with pytest.raises(ValueError, match="admitted range"):
        ProbeStopState(grace)
