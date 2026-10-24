"""Authored NOTRUN root-owner source controls; kernel/process operations controlled.

Private packet sockets below transfer synthetic terminal records. They do not
launch producers or prove the real owner loop, adoption, reaping or containment.
"""

import array
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_probe_owner as owner_module
from scripts.production_gate_parallel_measurement_budget import MeasurementBudget
from scripts.production_gate_parallel_probe_bootstrap import CONTROL_ENVIRONMENT, PROBE_NODE_SOURCE
from scripts.production_gate_parallel_probe_descriptors import RetainedDescriptor
from scripts.production_gate_parallel_probe_owner import ProbePeer, RootProbeOwner
from scripts.production_gate_parallel_probe_protocol import ProbeContext, ProbeProtocolError
from tests.test_parallel_runtime_probe_protocol import signed_bytes


def owner_case(tmp_path):
    now = [10.0]
    context = ProbeContext("controlled-owner", "n" * 64, b"k" * 32, "a" * 64, "b" * 64, 100, 120)
    root = RetainedDescriptor(1000, instance=(100, 1), validated=True)
    holder = RetainedDescriptor(1001, instance=(200, 2), parent=root.instance, validated=True)
    owner = RootProbeOwner(
        context,
        MeasurementBudget(100, 120, clock=lambda: now[0]),
        root_pin=root,
        holder_pin=holder,
        control_python=Path("/pinned/control/python"),
        endpoint=tmp_path / "private.sock",
    )
    return owner, now


def final_body(owner, role, instance, child, *, adopted=()):
    return {
        "scope": owner.context.scope,
        "nonce": owner.context.nonce,
        "source_sha256": owner.context.source_sha256,
        "program_sha256": owner.context.program_sha256,
        "phase": "final",
        "role": role,
        "pid": instance[0],
        "start": instance[1],
        "sequence": 3,
        "physical_empty": True,
        "echild": True,
        "subreaper_start": True,
        "subreaper_end": True,
        "proof": True,
        "timely_cleanup": True,
        "success": True,
        "custody_errors": [],
        "operation_errors": [],
        "remaining": [],
        "forks": [list(child)],
        "adopted": [list(value) for value in adopted],
        "reaped": [list(child), *(list(value) for value in adopted)],
        "killed": [],
        "child_status": 0,
        "job_deadline": owner.context.job_deadline,
        "cleanup_deadline": owner.context.cleanup_deadline,
    }


@pytest.mark.parametrize(
    "fault", ["none", "source", "python", "parent", "budget", "fd", "executable", "environment"]
)
def test_pinned_stage_source_readback_requires_exact_original_control_arguments(
    tmp_path, monkeypatch, fault
):
    owner, _ = owner_case(tmp_path)
    record = RetainedDescriptor(
        1002, instance=(201, 3), parent=(200, 2), role="guardian", validated=True
    )
    fields = [
        os.fsencode(owner.control_python),
        b"-I",
        b"-S",
        b"-c",
        PROBE_NODE_SOURCE.encode(),
        b"guardian",
        b"9",
        b"200",
        b"2",
        b"100.0",
        b"120.0",
    ]
    if fault not in ("none", "executable", "environment"):
        index, value = {
            "source": (4, b"unreviewed source"),
            "python": (0, b"/different/python"),
            "parent": (8, b"3"),
            "budget": (9, b"101.0"),
            "fd": (6, b"2"),
        }[fault]
        fields[index] = value
    environment = dict(CONTROL_ENVIRONMENT)
    if fault == "environment":
        environment["LD_PRELOAD"] = "/unreviewed/preload.so"

    def proc_bytes(path, *args, **kwargs):
        values = (
            fields
            if path.endswith("/cmdline")
            else [os.fsencode(k + "=" + v) for k, v in environment.items()]
        )
        return b"\0".join(values) + b"\0"

    monkeypatch.setattr(owner_module, "bounded_proc_bytes", proc_bytes)
    monkeypatch.setattr(
        owner_module.os,
        "readlink",
        lambda path: "/different/python" if fault == "executable" else str(owner.control_python),
    )
    monkeypatch.setattr(owner_module, "kernel_instance", lambda *args: (3, 200))
    if fault == "none":
        owner._node_source_matches(record)
    else:
        with pytest.raises(ProbeProtocolError):
            owner._node_source_matches(record)


@pytest.mark.parametrize(
    "fault", ["none", "changed_job", "changed_cleanup", "late_job", "late_cleanup", "replay"]
)
def test_actual_packet_terminal_is_bound_to_original_budgets_and_sequence(tmp_path, fault):
    owner, now = owner_case(tmp_path)
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    guardian = RetainedDescriptor(
        1002, instance=(201, 3), role="guardian", sequence=2, validated=True
    )
    payload = RetainedDescriptor(1003, instance=(202, 4), role="payload", validated=True)
    owner.custody.roles.update(guardian=guardian, payload=payload)
    peer = ProbePeer(receiver, record=guardian)
    body = final_body(owner, "guardian", guardian.instance, payload.instance)
    if fault == "changed_job":
        body["job_deadline"] += 1
    elif fault == "changed_cleanup":
        body["cleanup_deadline"] += 1
    elif fault == "late_job":
        now[0] = 100
    elif fault == "late_cleanup":
        now[0] = 120
    elif fault == "replay":
        body["sequence"] = 2
    try:
        sender.send(signed_bytes(body, owner.context))
        if fault in ("none", "late_job", "late_cleanup"):
            owner._read(peer)
            assert peer.final == body and guardian.sequence == 3
            if fault != "none":
                assert owner.custody.failures and not owner.operation_succeeded()
        else:
            with pytest.raises(ProbeProtocolError):
                owner._read(peer)
            assert peer.final is None and guardian.sequence == 2
    finally:
        receiver.close()
        sender.close()


def test_unexpected_diagnostic_rights_are_retained_before_frame_refusal(tmp_path):
    owner, _ = owner_case(tmp_path)
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    record = RetainedDescriptor(
        1002, instance=(201, 3), role="guardian", sequence=2, validated=True
    )
    peer = ProbePeer(receiver, record=record)
    source = os.pidfd_open(os.getpid())
    try:
        sender.sendmsg(
            [b"not a terminal frame"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source]))],
        )
        with pytest.raises(ProbeProtocolError, match="undeclared retained"):
            owner._read(peer)
        assert len(owner.custody.records) == 1
        retained = owner.custody.records[0]
        assert not retained.closed and not retained.validated
        assert os.fstat(retained.descriptor).st_ino == os.fstat(source).st_ino
        assert not owner.reconcile()
    finally:
        # Independent release of a test-owned self duplicate, not product proof.
        for retained in owner.custody.records:
            os.close(retained.descriptor)
        os.close(source)
        receiver.close()
        sender.close()


@pytest.mark.parametrize(
    "fault",
    ["none", "missing_holder", "missing_guardian", "root_wait", "live_payload", "late_cleanup"],
)
def test_physical_sweep_and_operation_success_are_distinct(tmp_path, monkeypatch, fault):
    owner, now = owner_case(tmp_path)
    holder = RetainedDescriptor(1004, instance=(200, 2), role="holder", validated=True)
    guardian = RetainedDescriptor(1005, instance=(201, 3), role="guardian", validated=True)
    payload = RetainedDescriptor(1006, instance=(202, 4), role="payload", validated=True)
    owner.custody.roles.update(holder=holder, guardian=guardian, payload=payload)
    hf = final_body(owner, "holder", holder.instance, guardian.instance)
    gf = final_body(owner, "guardian", guardian.instance, payload.instance)
    if fault == "missing_guardian":
        hf = final_body(
            owner, "holder", holder.instance, guardian.instance, adopted=[payload.instance]
        )
        hf.update(success=False, child_status=-9, operation_errors=["guardian lost"])
    if fault == "late_cleanup":
        now[0] = 120
    channels = [socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) for _ in range(2)]
    try:
        owner.peers = [
            ProbePeer(channels[0], record=holder, final=None if fault == "missing_holder" else hf),
            ProbePeer(
                channels[1], record=guardian, final=None if fault == "missing_guardian" else gf
            ),
        ]
        monkeypatch.setattr(
            RetainedDescriptor,
            "exited",
            lambda value: not (fault == "live_payload" and value is payload),
        )
        monkeypatch.setattr(RetainedDescriptor, "bound_descriptor", lambda value: value.descriptor)
        waits = []

        def wait_owned_root(kind, descriptor, flags):
            waits.append((kind, descriptor, flags))
            status = 1 if fault in ("root_wait", "missing_guardian") else 0
            return SimpleNamespace(si_code=os.CLD_EXITED, si_status=status)

        monkeypatch.setattr(owner_module.os, "waitid", wait_owned_root)
        physical = owner.reconcile()
        assert physical is (fault not in ("missing_holder", "live_payload"))
        assert owner.operation_succeeded() is (fault == "none")
        if physical:
            assert len(waits) == 1 and waits[0][1] == owner.holder_pin.descriptor
        else:
            assert not waits
        if fault in ("missing_guardian", "root_wait", "late_cleanup"):
            assert owner.custody.failures
    finally:
        for channel in channels:
            channel.close()
