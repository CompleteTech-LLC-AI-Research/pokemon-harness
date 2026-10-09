"""Retained root custody for private probe registration descriptors.

This source is not wired to a launcher. Receipt of SCM_RIGHTS precedes parsing,
authentication and kernel validation: refusal cannot discard a possibly live
instance. The eventual continuing root owner must retain this object and service
its unresolved records; keeping a Python reference alone proves no containment.
No numeric signalling, foreign process discovery or automatic recovery is used.
"""

from __future__ import annotations

import array
import os
import select
import signal
import socket
import struct
from dataclasses import dataclass
from typing import Any

from scripts.production_gate_parallel_measurement_budget import MeasurementBudget
from scripts.production_gate_parallel_probe_protocol import (
    MAX_PROBE_FRAME,
    ProbeContext,
    ProbeProtocolError,
    decode_probe_frame,
    registration_problems,
)

MAX_RIGHTS = 8
MAX_RETAINED = 24
_INTEGER_BYTES = array.array("i").itemsize


@dataclass
class RetainedDescriptor:
    descriptor: int
    binding: tuple[int, int] | None = None
    instance: tuple[int, int] | None = None
    parent: tuple[int, int] | None = None
    role: str | None = None
    sequence: int = 0
    validated: bool = False
    closed: bool = False

    def bound_descriptor(self) -> int:
        if self.closed or self.binding is None:
            raise ProbeProtocolError("retained descriptor binding is unresolved")
        stat = os.fstat(self.descriptor)
        if (stat.st_dev, stat.st_ino) != self.binding:
            raise ProbeProtocolError("retained descriptor binding changed")
        return self.descriptor

    def exited(self) -> bool:
        if not self.validated:
            raise ProbeProtocolError("retained pidfd was not kernel validated")
        poller = select.poll()
        poller.register(self.bound_descriptor(), select.POLLIN)
        events = poller.poll(0)
        if any(bits & (select.POLLERR | select.POLLNVAL) for _, bits in events):
            raise ProbeProtocolError("retained pidfd exit state is unknown")
        return any(bits & select.POLLIN for _, bits in events)

    def signal(self, number: int) -> None:
        if not self.validated or number not in (signal.SIGTERM, signal.SIGKILL):
            raise ProbeProtocolError("unvalidated instance or undeclared signal")
        if not self.exited():
            signal.pidfd_send_signal(self.bound_descriptor(), number)


def bounded_proc_bytes(path: str, budget: MeasurementBudget, *, limit: int = 16384) -> bytes:
    budget.checkpoint()
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    budget.checkpoint()
    if len(raw) > limit:
        raise ProbeProtocolError("kernel instance evidence exceeds its bound")
    return raw


def kernel_instance(pid: int, budget: MeasurementBudget) -> tuple[int, int]:
    """Return independently observed start/parent, never a sender's assertion."""

    raw = bounded_proc_bytes(f"/proc/{pid}/stat", budget)
    try:
        fields = raw.rsplit(b")", 1)[1].split()
        return int(fields[19]), int(fields[1])
    except (ValueError, IndexError) as error:
        raise ProbeProtocolError("kernel instance evidence is malformed") from error


def validate_pidfd(
    record: RetainedDescriptor, expected_pid: int, budget: MeasurementBudget
) -> tuple[int, int]:
    """Bind a received fd to the live instance using fdinfo and double readback."""

    first = kernel_instance(expected_pid, budget)
    descriptor = record.bound_descriptor()
    budget.checkpoint()
    link = os.readlink(f"/proc/self/fd/{descriptor}")
    raw = bounded_proc_bytes(f"/proc/self/fdinfo/{descriptor}", budget)
    fields = [
        line.split(b":", 1)[1].strip() for line in raw.splitlines() if line.startswith(b"Pid:")
    ]
    if link != "anon_inode:[pidfd]" or fields != [str(expected_pid).encode("ascii")]:
        raise ProbeProtocolError("received descriptor is not the pinned peer pidfd")
    again = kernel_instance(expected_pid, budget)
    if first != again:
        raise ProbeProtocolError("peer instance changed during pidfd validation")
    record.bound_descriptor()
    budget.checkpoint()
    return first


class ProbeDescriptorCustody:
    """One scope's descriptor ledger, retained by its continuing root owner.

    Parent instances must already be held and independently validated by that
    owner. This component neither proves the parent's custody nor sends ACKs.
    A failed ledger refuses further receives; explicit owner reconciliation must
    preserve or transfer every unresolved descriptor before another scope starts.
    """

    def __init__(self, context: ProbeContext, budget: MeasurementBudget) -> None:
        self.context = context
        self.budget = budget
        self.records: list[RetainedDescriptor] = []
        self.failures: list[str] = []
        self.roles: dict[str, RetainedDescriptor] = {}

    def refuse(self, reason: str) -> None:
        if reason not in self.failures and len(self.failures) < 64:
            self.failures.append(reason[:240])

    def receive_packet(self, channel: socket.socket) -> tuple[bytes, list[RetainedDescriptor]]:
        """Retain rights from registration AND diagnostic packets before refusal."""

        if len(self.records) > MAX_RETAINED - MAX_RIGHTS:
            raise ProbeProtocolError("scope descriptor custody bound is exhausted")
        if channel.getblocking() or channel.type & 0xF != socket.SOCK_SEQPACKET:
            raise ProbeProtocolError(
                "packet receipt requires a nonblocking sequenced-packet socket"
            )
        raw, ancillary, flags, _address = channel.recvmsg(
            MAX_PROBE_FRAME + 1,
            socket.CMSG_SPACE(MAX_RIGHTS * _INTEGER_BYTES),
            socket.MSG_CMSG_CLOEXEC,
        )
        incoming: list[RetainedDescriptor] = []
        malformed = False
        try:
            for level, kind, data in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    malformed = True
                    continue
                values = array.array("i")
                complete = len(data) - len(data) % _INTEGER_BYTES
                values.frombytes(data[:complete])
                malformed = malformed or complete != len(data)
                for descriptor in values:
                    record = RetainedDescriptor(descriptor)
                    self.records.append(record)
                    incoming.append(record)
            for record in incoming:
                stat = os.fstat(record.descriptor)
                record.binding = (stat.st_dev, stat.st_ino)
            if malformed or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise ProbeProtocolError("registration ancillary/frame data is incomplete")
            return raw, incoming
        except BaseException as error:
            self.refuse(type(error).__name__ + ": " + str(error))
            raise

    def receive_registration(
        self,
        channel: socket.socket,
        *,
        parent_instance: tuple[int, int],
        expected_role: str,
        expected_uid: int,
        previous_sequence: int = 0,
    ) -> tuple[dict[str, Any], RetainedDescriptor]:
        if self.failures or len(self.records) > MAX_RETAINED - MAX_RIGHTS:
            raise ProbeProtocolError("scope refuses new admission with unresolved custody")
        self.budget.checkpoint()
        if channel.getblocking() or channel.type & 0xF != socket.SOCK_SEQPACKET:
            raise ProbeProtocolError("registration requires a nonblocking sequenced-packet socket")
        raw, incoming = self.receive_packet(channel)
        try:
            if len(incoming) != 1:
                raise ProbeProtocolError("registration must supply exactly one retained pidfd")
            self.budget.checkpoint()
            pid, uid, _gid = struct.unpack(
                "3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != expected_uid or pid <= 0:
                raise ProbeProtocolError("registration peer credentials differ")
            body = decode_probe_frame(raw, self.context)
            if body["role"] != expected_role or expected_role in self.roles:
                raise ProbeProtocolError("registration role is unexpected or already retained")
            record = incoming[0]
            start, parent_pid = validate_pidfd(record, pid, self.budget)
            parent_start, _grandparent = kernel_instance(parent_pid, self.budget)
            if (parent_pid, parent_start) != parent_instance:
                raise ProbeProtocolError("registration kernel parent differs from retained parent")
            problems = registration_problems(
                body,
                self.context,
                peer_instance=(pid, start),
                parent_instance=parent_instance,
                previous_sequence=previous_sequence,
                cleanup_bound=self.budget.cleanup_bound(),
            )
            if problems:
                raise ProbeProtocolError("; ".join(problems))
            if kernel_instance(pid, self.budget) != (start, parent_pid):
                raise ProbeProtocolError("registration instance/parent changed before retention")
            record.instance, record.parent = (pid, start), parent_instance
            record.role, record.sequence = expected_role, body["sequence"]
            record.validated = True
            if record.exited():
                raise ProbeProtocolError("registration peer exited before owner admission")
            self.budget.checkpoint()
            self.roles[expected_role] = record
            return body, record  # Only the continuing owner may now attempt an ACK.
        except BaseException as error:
            self.refuse(type(error).__name__ + ": " + str(error))
            raise

    def release_after_exit(self, record: RetainedDescriptor, *, reaping_proven: bool) -> None:
        """Caller supplies separately verified parent/root wait reconciliation.

        This deliberately has no close-all/destructor. Refusal preserves the
        descriptor; success requires independently polled exit and resolved wait
        obligations. Missing supervisor evidence cannot be replaced by EOF.
        """

        if not any(value is record for value in self.records):
            raise ProbeProtocolError("descriptor is not owned by this scope")
        try:
            if reaping_proven is not True or not record.exited():
                raise ProbeProtocolError("instance exit or reaping remains unresolved")
            os.close(record.bound_descriptor())
            record.closed = True
        except BaseException as error:
            self.refuse(type(error).__name__ + ": " + str(error))
            raise
