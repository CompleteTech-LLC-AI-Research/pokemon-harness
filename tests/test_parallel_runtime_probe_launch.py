"""Authored NOTRUN controlled allocation/broker controls; no live-process proof."""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_probe_broker as broker_module
from scripts import production_gate_parallel_probe_launch as launch_module
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from scripts.production_gate_parallel_probe_bootstrap import PROBE_NODE_SOURCE
from scripts.production_gate_parallel_probe_protocol import (
    ProbeContext,
    ProbeProtocolError,
    canonical_probe_frame,
)


def launch_case(tmp_path):
    budget = MeasurementBudget(100.0, 120.0, clock=lambda: 10.0)
    root = SimpleNamespace(
        validated=True,
        instance=(os.getpid(), 1),
        exited=lambda: False,
        bound_descriptor=lambda: 3,
    )
    program = {"command": ["controlled-unused"], "environment": {}, "cwd": str(tmp_path)}
    context = ProbeContext(
        "scope",
        "nonce",
        b"k" * 32,
        hashlib.sha256(PROBE_NODE_SOURCE.encode()).hexdigest(),
        hashlib.sha256(canonical_probe_frame(program)).hexdigest(),
        100.0,
        120.0,
    )
    return launch_module.ProbeLaunch(
        context,
        budget,
        root_pin=root,
        control_python=Path("/usr/bin/python3"),
        directory=tmp_path / "p",
        command=program["command"],
        environment={},
        cwd=tmp_path,
    )


def test_measurement_without_broker_never_falls_back_to_subprocess():
    budget = MeasurementBudget(100.0, 120.0, clock=lambda: 10.0)
    with pytest.raises(IdentityDeadline, match="continuing root broker"):
        budget.run_probe(["controlled-unused"], environment={}, cwd=Path("/"))


def test_probe_result_crossing_bound_is_not_returned():
    now, calls = [10.0], []

    def runner(command, **kwargs):
        calls.append(kwargs["budget"])
        now[0] = 100.0
        return object()

    budget = MeasurementBudget(100.0, 120.0, clock=lambda: now[0], probe_runner=runner)
    with pytest.raises(IdentityDeadline, match="frozen deadline"):
        budget.run_probe(["controlled-unused"], environment={}, cwd=Path("/"))
    assert calls == [budget]


def test_broker_retains_scope_before_failed_allocation(tmp_path, monkeypatch):
    broker = broker_module.RootProbeBroker(
        root_pin=object(),
        control_python=Path("/usr/bin/python3"),
        directory=tmp_path,
        resource_owner=object(),
    )

    class FailedLaunch:
        def __init__(self, *args, **kwargs):
            self.failures, self.owner = ["controlled allocation refusal"], None

        def allocate(self):
            assert broker.scopes == [self]
            raise ProbeProtocolError("controlled allocation refusal")

    monkeypatch.setattr(broker_module, "ProbeLaunch", FailedLaunch)
    budget = MeasurementBudget(100.0, 120.0, clock=lambda: 10.0)
    with pytest.raises(ProbeProtocolError, match="allocation refusal"):
        broker.begin(["controlled-unused"], environment={}, cwd=tmp_path, budget=budget)
    assert broker.pending() and len(broker.scopes) == 1
    with pytest.raises(ProbeProtocolError, match="unresolved prior custody"):
        broker.begin(["controlled-unused"], environment={}, cwd=tmp_path, budget=budget)


def test_first_holder_pin_failure_keeps_original_popen_and_sends_no_config(tmp_path, monkeypatch):
    scope = launch_case(tmp_path)
    process = SimpleNamespace(pid=201, stdout=None, stderr=None)
    allocations = []

    def popen(*args, **kwargs):
        allocations.append(kwargs)
        return process

    def no_pin(pid):
        assert scope.process is process and pid == 201
        raise OSError("controlled first pidfd refusal")

    monkeypatch.setattr(launch_module, "read_subreaper", lambda libc: 1)
    monkeypatch.setattr(
        launch_module, "subprocess", SimpleNamespace(Popen=popen, DEVNULL=-3, PIPE=-1)
    )
    monkeypatch.setattr(
        launch_module,
        "os",
        SimpleNamespace(getpid=os.getpid, fsencode=os.fsencode, pidfd_open=no_pin),
    )
    try:
        with pytest.raises(OSError, match="first pidfd refusal"):
            scope.allocate()
        assert len(allocations) == 1 and scope.process is process
        assert scope.holder_pin is None and scope.owner is None and scope.failures
        assert scope.pending_initial is None and not scope.acknowledged
        with pytest.raises(ProbeProtocolError, match="cannot be retried"):
            scope.allocate()
    finally:
        for channel in (scope.parent, scope.child):
            if channel is not None:
                channel.close()  # controlled Popen made no real child


def test_partial_allocation_output_pulse_is_nonblocking_and_bounded(tmp_path, monkeypatch):
    scope = launch_case(tmp_path)
    calls, raw = [], b"x" * 16
    scope.process = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 8), stderr=None)
    monkeypatch.setattr(launch_module, "OUTPUT_LIMIT", 8)

    def read(fd, limit):
        calls.append(("read", fd, limit))
        return raw

    monkeypatch.setattr(
        launch_module,
        "os",
        SimpleNamespace(
            set_blocking=lambda fd, flag: calls.append(("nonblocking", fd, flag)),
            read=read,
        ),
    )
    scope.pulse()
    assert calls == [("nonblocking", 8, False), ("read", 8, launch_module.OUTPUT_PULSE_BYTES)]
    assert bytes(scope.output["stdout"]) == raw[:8] and scope.output_counts["stdout"] == 16
    assert scope.output_hashes["stdout"].hexdigest() == hashlib.sha256(raw).hexdigest()
    assert scope.failures and not scope.released
