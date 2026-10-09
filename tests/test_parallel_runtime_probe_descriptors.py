"""Authored NOTRUN controls for real local SCM_RIGHTS retention.

These use only this test process's own pidfd or test-created pipe descriptors;
they do not launch probes, establish owner-loop containment, or qualify producers.
Explicit finally cleanup is a separate test-resource release, never custody proof.
"""

import array
import hashlib
import hmac
import os
import socket
import time

import pytest

from scripts import production_gate_parallel_probe_descriptors as descriptors
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from scripts.production_gate_parallel_probe_protocol import (
    ProbeContext,
    ProbeProtocolError,
    canonical_probe_frame,
)


def registration_case():
    now = time.monotonic()
    budget = MeasurementBudget(now + 10, now + 12)
    context = ProbeContext(
        "local-own-fd", "n" * 64, b"k" * 32, "a" * 64, "b" * 64, now + 10, now + 12
    )
    stamp, parent_pid = descriptors.kernel_instance(os.getpid(), budget)
    parent_start, _ = descriptors.kernel_instance(parent_pid, budget)
    body = {
        "scope": context.scope,
        "nonce": context.nonce,
        "source_sha256": context.source_sha256,
        "program_sha256": context.program_sha256,
        "phase": "register",
        "role": "payload",
        "pid": os.getpid(),
        "start": stamp,
        "sequence": 2,
        "parent_pid": parent_pid,
        "parent_start": parent_start,
        "guard": 9,
        "subreaper": False,
        "job_deadline": context.job_deadline,
        "cleanup_deadline": context.cleanup_deadline,
    }
    custody = descriptors.ProbeDescriptorCustody(context, budget)
    return custody, body, (parent_pid, parent_start)


def signed(body, context):
    mac = hmac.new(context.secret, canonical_probe_frame(body), hashlib.sha256).hexdigest()
    return canonical_probe_frame(dict(body, mac=mac))


def send_registration(sender, custody, body, fds):
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
    sender.sendmsg([signed(body, custody.context)], ancillary)


def independent_test_fd_cleanup(custody, original_fstat):
    for record in custody.records:
        if not record.closed:
            stat = original_fstat(record.descriptor)
            if record.binding is not None:
                assert (stat.st_dev, stat.st_ino) == record.binding
            # Only duplicates of this test's own descriptors were ever sent.
            os.close(record.descriptor)


@pytest.mark.parametrize("fault", ["mac", "start", "parent", "uid", "replay", "fstat", "budget"])
def test_failed_registration_retains_actual_received_descriptor_and_refuses_more(
    monkeypatch, fault
):
    custody, body, parent = registration_case()
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    source = os.pidfd_open(os.getpid())
    original_fstat = os.fstat
    kwargs = {"parent_instance": parent, "expected_role": "payload", "expected_uid": os.getuid()}
    try:
        if fault == "mac":
            sender.sendmsg(
                [canonical_probe_frame(dict(body, mac="0" * 64))],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source]))],
            )
        else:
            if fault == "start":
                body["start"] += 1
            if fault == "parent":
                kwargs["parent_instance"] = (parent[0], parent[1] + 1)
            if fault == "uid":
                kwargs["expected_uid"] += 1
            if fault == "replay":
                kwargs["previous_sequence"] = 2
            send_registration(sender, custody, body, [source])
        if fault == "fstat":

            def fail_fstat(_fd):
                raise OSError("controlled retained-fstat refusal")

            monkeypatch.setattr(descriptors.os, "fstat", fail_fstat)
        if fault == "budget":
            calls = []

            def expire_after_receive():
                calls.append(True)
                if len(calls) >= 2:
                    raise IdentityDeadline("controlled original-budget refusal")

            custody.budget = MeasurementBudget(
                custody.context.job_deadline,
                custody.context.cleanup_deadline,
                service=expire_after_receive,
            )
        with pytest.raises((ProbeProtocolError, OSError, IdentityDeadline)):
            custody.receive_registration(receiver, **kwargs)
        assert len(custody.records) == 1 and custody.failures
        record = custody.records[0]
        assert not record.closed
        assert original_fstat(record.descriptor).st_ino == original_fstat(source).st_ino
        with pytest.raises(ProbeProtocolError, match="unresolved custody"):
            custody.receive_registration(receiver, **kwargs)
    finally:
        independent_test_fd_cleanup(custody, original_fstat)
        os.close(source)
        receiver.close()
        sender.close()


@pytest.mark.parametrize("count", [0, 2, 9])
def test_missing_extra_or_truncated_rights_cannot_be_acknowledged(count):
    custody, body, parent = registration_case()
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    source = os.pidfd_open(os.getpid())
    try:
        send_registration(sender, custody, body, [source] * count)
        with pytest.raises(ProbeProtocolError):
            custody.receive_registration(
                receiver, parent_instance=parent, expected_role="payload", expected_uid=os.getuid()
            )
        assert custody.failures and not custody.roles
        assert all(not record.closed for record in custody.records)
        assert len(custody.records) <= descriptors.MAX_RIGHTS
    finally:
        independent_test_fd_cleanup(custody, os.fstat)
        os.close(source)
        receiver.close()
        sender.close()


def test_non_pidfd_right_is_retained_but_never_validated_or_signalled():
    custody, body, parent = registration_case()
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    read_fd, write_fd = os.pipe()
    try:
        send_registration(sender, custody, body, [read_fd])
        with pytest.raises(ProbeProtocolError, match="not the pinned peer pidfd"):
            custody.receive_registration(
                receiver, parent_instance=parent, expected_role="payload", expected_uid=os.getuid()
            )
        record = custody.records[0]
        assert not record.validated and not record.closed
        with pytest.raises(ProbeProtocolError, match="unvalidated"):
            record.signal(15)
    finally:
        independent_test_fd_cleanup(custody, os.fstat)
        os.close(read_fd)
        os.close(write_fd)
        receiver.close()
        sender.close()


def test_live_validated_self_pidfd_cannot_be_released_on_a_wait_claim():
    custody, body, parent = registration_case()
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    source = os.pidfd_open(os.getpid())
    try:
        send_registration(sender, custody, body, [source])
        decoded, record = custody.receive_registration(
            receiver, parent_instance=parent, expected_role="payload", expected_uid=os.getuid()
        )
        assert decoded == body and record.validated and not record.exited()
        with pytest.raises(ProbeProtocolError, match="remains unresolved"):
            custody.release_after_exit(record, reaping_proven=True)
        assert not record.closed and custody.failures
        assert os.fstat(record.descriptor).st_ino == os.fstat(source).st_ino
    finally:
        independent_test_fd_cleanup(custody, os.fstat)
        os.close(source)
        receiver.close()
        sender.close()
