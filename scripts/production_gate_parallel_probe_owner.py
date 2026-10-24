"""Independent root endpoint for one admitted identity-probe scope.

Unintegrated source: the scheduler must register this continuing owner BEFORE
holder admission, service it during every wait, retain capacity/runtime resources
while unresolved, and preserve it on bounded refusal. No destructor, background
thread, launch/recovery loop or numeric signalling is provided here. Constructing
an object or accepting authenticated frames does not prove kernel containment.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_measurement_budget import MeasurementBudget
from scripts.production_gate_parallel_probe_bootstrap import CONTROL_ENVIRONMENT, PROBE_NODE_SOURCE
from scripts.production_gate_parallel_probe_descriptors import (
    ProbeDescriptorCustody,
    RetainedDescriptor,
    bounded_proc_bytes,
    kernel_instance,
)
from scripts.production_gate_parallel_probe_protocol import (
    MAX_PROBE_FRAME,
    ProbeContext,
    ProbeProtocolError,
    canonical_probe_frame,
    decode_probe_frame,
    terminal_problems,
)
from scripts.production_gate_parallel_probe_stopping import ProbeStopState, advance_probe_stop

ROLES = ("holder", "guardian", "payload")
MAX_OWNER_CONNECTIONS = 4


@dataclass
class ProbePeer:
    channel: socket.socket
    record: RetainedDescriptor | None = None
    pending_ack: bytes | None = None
    eof: bool = False
    final: dict[str, Any] | None = None


class RootProbeOwner:
    """Created before any holder ACK, held by an already registered root owner.

    ``root_pin`` pins this process; ``holder_pin`` was acquired immediately after
    its owned Popen allocation and before parent ACK/configuration. Both are
    retained by the caller until this scope's complete reconciliation. The caller
    is responsible for checked root subreaper installation and wait ownership.
    """

    def __init__(
        self,
        context: ProbeContext,
        budget: MeasurementBudget,
        *,
        root_pin: RetainedDescriptor,
        holder_pin: RetainedDescriptor,
        control_python: Path,
        endpoint: Path,
        grace_seconds: float = 1.0,
    ) -> None:
        # The caller retains even a partially initialized owner on setup failure.
        self.context, self.budget = context, budget
        self.root_pin, self.holder_pin = root_pin, holder_pin
        self.control_python, self.endpoint = control_python, endpoint
        self.custody = ProbeDescriptorCustody(context, budget)
        self.listener: socket.socket | None = None
        self.endpoint_binding: tuple[int, int] | None = None
        self.peers: list[ProbePeer] = []
        self.sequence = 0
        self.servicing = False
        self.holder_wait_status: int | None = None
        self.physical_cleanup_verified_at: float | None = None
        self.closed = False
        self.stop_state = ProbeStopState(grace_seconds)

    def open_endpoint(self) -> None:
        """Caller registers this object before invoking this reversible setup step."""

        self.budget.checkpoint()
        if self.listener is not None or self.closed or self.custody.failures:
            raise ProbeProtocolError("probe endpoint cannot be reopened")
        if (
            not self.root_pin.validated
            or self.root_pin.instance is None
            or self.root_pin.instance[0] != os.getpid()
            or not self.holder_pin.validated
            or self.holder_pin.instance is None
            or self.holder_pin.parent != self.root_pin.instance
            or self.root_pin.exited()
            or self.holder_pin.exited()
        ):
            raise ProbeProtocolError("original root/holder custody is not established")
        self.root_pin.bound_descriptor()
        self.holder_pin.bound_descriptor()
        if hashlib.sha256(PROBE_NODE_SOURCE.encode()).hexdigest() != self.context.source_sha256:
            raise ProbeProtocolError("probe source differs from the frozen source")
        if (
            self.budget.deadline != self.context.job_deadline
            or self.budget.cleanup_bound() > self.context.cleanup_deadline
        ):
            raise ProbeProtocolError("owner budget differs from the original probe budget")
        parent = self.endpoint.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ProbeProtocolError("probe endpoint directory must be owner-private")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.setblocking(False)
        try:
            self.listener.bind(str(self.endpoint))
            info = self.endpoint.lstat()
            self.endpoint_binding = (info.st_dev, info.st_ino)
            os.chmod(self.endpoint, 0o600)
            self.listener.listen(MAX_OWNER_CONNECTIONS)
            self.budget.checkpoint()
        except BaseException as error:
            self.custody.refuse(type(error).__name__ + ": " + str(error))
            raise  # caller retains socket/path/identity pins, including partial setup

    def _parent_for(self, role: str) -> tuple[int, int]:
        parent = (
            self.root_pin
            if role == "holder"
            else self.custody.roles.get(ROLES[ROLES.index(role) - 1])
        )
        if parent is None or not parent.validated or parent.instance is None or parent.exited():
            raise ProbeProtocolError("required retained probe parent is missing or exited")
        parent.bound_descriptor()
        return parent.instance

    def _node_source_matches(self, record: RetainedDescriptor) -> None:
        assert record.instance is not None
        raw = bounded_proc_bytes(f"/proc/{record.instance[0]}/cmdline", self.budget, limit=131072)
        self.budget.checkpoint()
        actual_executable = os.readlink(f"/proc/{record.instance[0]}/exe")
        self.budget.checkpoint()
        if not self.control_python.is_absolute() or actual_executable != str(self.control_python):
            raise ProbeProtocolError("stage executable differs from the pinned control interpreter")
        environment_raw = bounded_proc_bytes(
            f"/proc/{record.instance[0]}/environ", self.budget, limit=65536
        )
        environment_fields = environment_raw.rstrip(b"\0").split(b"\0")
        expected_environment = {os.fsencode(k + "=" + v) for k, v in CONTROL_ENVIRONMENT.items()}
        if (
            len(environment_fields) != len(expected_environment)
            or set(environment_fields) != expected_environment
        ):
            raise ProbeProtocolError("stage inherited an undeclared control environment")
        fields = raw.rstrip(b"\0").split(b"\0")
        expected = [
            os.fsencode(self.control_python),
            b"-I",
            b"-S",
            b"-c",
            PROBE_NODE_SOURCE.encode(),
        ]
        if fields[:5] != expected or len(fields) != 11 or fields[5] != record.role.encode():
            raise ProbeProtocolError("registered stage is not the pinned isolated control source")
        try:
            declared = (int(fields[7]), int(fields[8]), float(fields[9]), float(fields[10]))
            control_fd = int(fields[6])
        except ValueError as error:
            raise ProbeProtocolError("registered stage arguments are malformed") from error
        if control_fd < 3 or declared != (
            *record.parent,
            self.context.job_deadline,
            self.context.cleanup_deadline,
        ):
            raise ProbeProtocolError(
                "registered stage arguments differ from original parent/budget"
            )
        stamp, parent_pid = kernel_instance(record.instance[0], self.budget)
        if stamp != record.instance[1] or parent_pid != record.parent[0]:
            raise ProbeProtocolError("registered stage instance changed during source readback")

    def _frame(self, record: RetainedDescriptor, phase: str, **extra: Any) -> bytes:
        if record.instance is None or record.role is None or self.root_pin.instance is None:
            raise ProbeProtocolError("owner cannot acknowledge unresolved instances")
        self.sequence += 1
        body = dict(
            extra,
            scope=self.context.scope,
            nonce=self.context.nonce,
            role=record.role,
            pid=record.instance[0],
            start=record.instance[1],
            source_sha256=self.context.source_sha256,
            program_sha256=self.context.program_sha256,
            sequence=self.sequence,
            phase=phase,
        )
        body["mac"] = hmac.new(
            self.context.secret, canonical_probe_frame(body), hashlib.sha256
        ).hexdigest()
        raw = canonical_probe_frame(body)
        if len(raw) > MAX_PROBE_FRAME:
            raise ProbeProtocolError("owner frame exceeds its original bound")
        return raw

    def _register(self, peer: ProbePeer) -> None:
        if len(self.custody.roles) >= len(ROLES):
            raise ProbeProtocolError("all declared stages are already registered")
        next_role = ROLES[len(self.custody.roles)]
        _body, record = self.custody.receive_registration(
            peer.channel,
            parent_instance=self._parent_for(next_role),
            expected_role=next_role,
            expected_uid=os.getuid(),
        )
        peer.record = record  # retain association before subsequent source/ACK failure
        if next_role == "holder" and record.instance != self.holder_pin.instance:
            raise ProbeProtocolError("holder differs from the original owned Popen instance")
        self._node_source_matches(record)
        peer.pending_ack = self._frame(
            record,
            "root_ack",
            retained=True,
            root_pid=self.root_pin.instance[0],
            root_start=self.root_pin.instance[1],
        )

    def _send_ack(self, peer: ProbePeer) -> None:
        self.budget.checkpoint()
        if peer.pending_ack is None:
            return
        if peer.channel.send(peer.pending_ack) != len(peer.pending_ack):
            raise ProbeProtocolError("partial owner ACK")
        peer.pending_ack = None

    def _read(self, peer: ProbePeer) -> None:
        assert peer.record is not None
        raw, incoming = self.custody.receive_packet(peer.channel)
        if incoming:
            raise ProbeProtocolError("supervisor frame supplied undeclared retained descriptors")
        if not raw:
            peer.eof = True
            if peer.record.role != "payload" and peer.final is None:
                raise ProbeProtocolError("required supervisor final was lost")
            return  # payload exec closes its control socket; this is NOT exit proof
        body = decode_probe_frame(raw, self.context)
        if body["role"] != peer.record.role or (body["pid"], body["start"]) != peer.record.instance:
            raise ProbeProtocolError("terminal channel instance differs from retained peer")
        if body["phase"] == "unproven":
            raise ProbeProtocolError("probe supervisor retained unproven custody")
        child_role = "guardian" if peer.record.role == "holder" else "payload"
        child = self.custody.roles.get(child_role)
        problems = terminal_problems(
            body,
            stage_instance=peer.record.instance,
            child_instance=child.instance if child is not None else None,
            previous_sequence=peer.record.sequence,
        )
        if peer.final is not None:
            problems.append("duplicate supervisor final")
        if body.get("job_deadline") != self.context.job_deadline:
            problems.append("supervisor changed the original success budget")
        bound = body.get("cleanup_deadline")
        if type(bound) not in (int, float) or not 0 < bound <= min(
            self.context.cleanup_deadline, self.budget.cleanup_bound()
        ):
            problems.append("supervisor cleanup bound differs or became unknown")
        now = self.budget.now()
        if problems:
            raise ProbeProtocolError("; ".join(problems))
        peer.record.sequence = body["sequence"]
        peer.final = body
        if not body["timely_cleanup"]:
            self.custody.refuse("supervisor physical cleanup completed after its original bound")
        if now >= self.budget.cleanup_bound() or (
            body.get("success") and now >= self.budget.deadline
        ):
            # Retain a valid physical-sweep record for continuing cleanup, while
            # permanently refusing a timely operation result. No retry/reset.
            self.custody.refuse("supervisor final arrived after its original bound")

    def service(self) -> None:
        """Bounded nonblocking work; caller also services sibling stop/escalation.

        Reentrant budget callbacks skip this scope while its outer service step
        owns it. No harvest/finalization callback is invoked recursively here.
        """

        if self.servicing or self.closed:
            return
        self.servicing = True
        try:
            if self.listener is not None and not self.custody.failures:
                for _ in range(MAX_OWNER_CONNECTIONS - len(self.peers)):
                    try:
                        channel, _address = self.listener.accept()
                    except BlockingIOError:
                        break
                    peer = ProbePeer(channel)
                    self.peers.append(peer)  # retain immediately, before configuration
                    channel.setblocking(False)
            for peer in tuple(self.peers):
                if peer.eof:
                    continue
                try:
                    if peer.record is None:
                        if not self.custody.failures:
                            self._register(peer)
                    elif peer.pending_ack is not None:
                        if not self.custody.failures:
                            self._send_ack(peer)
                    else:
                        self._read(peer)
                except BlockingIOError:
                    continue
                except Exception as error:  # noqa: BLE001 - retain all peers and pins on refusal
                    self.custody.refuse(type(error).__name__ + ": " + str(error))
            self._request_stop()
        except Exception as error:  # noqa: BLE001 - setup/accept failures cannot discard custody
            self.custody.refuse(type(error).__name__ + ": " + str(error))
        finally:
            self.servicing = False

    def _request_stop(self) -> None:
        advance_probe_stop(self)
        if not self.custody.failures and self.budget.now() < min(
            self.budget.deadline, self.budget.cleanup_bound()
        ):
            return
        for peer in self.peers:
            if peer.record is None or peer.eof:
                continue
            try:
                peer.channel.send(
                    self._frame(peer.record, "stop", cleanup_deadline=self.budget.cleanup_bound())
                )
            except (OSError, ProbeProtocolError):
                pass  # missing stop delivery is not cleanup or terminal proof

    def reconcile(self) -> bool:
        """Require the holder final, actual held exits and direct-root wait.

        This proves neither job success nor missing owner adoption on role loss.
        Failure leaves every pin/socket/path owned; the scheduler must continue
        custody and withhold new admission instead of releasing this scope.
        """

        if self.physical_cleanup_verified_at is not None:
            return True
        finals = {peer.record.role: peer.final for peer in self.peers if peer.record is not None}
        if finals.get("holder") is None:
            return False
        if "holder" not in self.custody.roles:
            return False
        if any(not record.validated and not record.closed for record in self.custody.records):
            return False
        if any(not record.exited() for record in self.custody.roles.values()):
            return False
        holder, guardian = finals["holder"], finals.get("guardian")
        swept = {tuple(pair) for pair in holder["reaped"]}
        for role, record in self.custody.roles.items():
            if (
                role != "holder"
                and record.instance not in swept
                and (
                    guardian is None
                    or record.instance not in {tuple(pair) for pair in guardian["reaped"]}
                )
            ):
                return False
        if guardian is None:
            self.custody.refuse("guardian final missing; operation failed, holder sweep only")
        elif holder["child_status"] != (0 if guardian["success"] else 1):
            self.custody.refuse("holder and guardian exit dispositions differ")
        if self.holder_wait_status is None:
            result = os.waitid(
                os.P_PIDFD, self.holder_pin.bound_descriptor(), os.WEXITED | os.WNOHANG
            )
            if result is None:
                return False
            self.holder_wait_status = (
                result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
            )
        if self.holder_wait_status != (0 if holder["success"] else 1):
            self.custody.refuse("root wait differs from holder final")
        self.physical_cleanup_verified_at = self.budget.now()
        if self.physical_cleanup_verified_at >= self.budget.cleanup_bound():
            self.custody.refuse("physical cleanup was observed after the original cleanup bound")
        return True  # caller still must resolve owners' resources and success separately

    def operation_succeeded(self) -> bool:
        """Full successful operation needs both finals, all stages and timely proof."""

        if self.custody.failures or self.budget.now() >= min(
            self.budget.deadline, self.budget.cleanup_bound()
        ):
            return False
        finals = {peer.record.role: peer.final for peer in self.peers if peer.record is not None}
        reconciled = bool(
            all(role in self.custody.roles for role in ROLES)
            and all(
                finals.get(role) is not None and finals[role]["success"]
                for role in ("holder", "guardian")
            )
            and self.reconcile()
            and not self.custody.failures
        )
        if not reconciled:
            return False
        if self.budget.now() >= min(self.budget.deadline, self.budget.cleanup_bound()):
            self.custody.refuse("operation expired during physical reconciliation")
            return False
        return True

    def release_confirmed_resources(self) -> bool:
        """Retire only the physically reconciled scope, retaining partial failures.

        Root's shared self pin is never released here. Failed or late operations
        remain failures even if continuing custody later proves physical cleanup.
        No unknown/unvalidated received descriptor is discarded by this method.
        """

        if self.closed:
            return True
        if not self.reconcile() or any(
            not record.closed and not record.validated for record in self.custody.records
        ):
            return False
        try:
            for record in self.custody.records:
                if not record.closed:
                    self.custody.release_after_exit(record, reaping_proven=True)
            if not self.holder_pin.closed:
                if not self.holder_pin.exited():
                    raise ProbeProtocolError("original holder pin does not confirm exit")
                os.close(self.holder_pin.bound_descriptor())
                self.holder_pin.closed = True
            for peer in self.peers:
                peer.channel.close()
            if self.listener is not None:
                self.listener.close()
            if self.endpoint_binding is not None:
                try:
                    info = self.endpoint.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (info.st_dev, info.st_ino) != self.endpoint_binding:
                        raise ProbeProtocolError("owned endpoint path changed before release")
                    self.endpoint.unlink()
            elif self.listener is not None:
                raise ProbeProtocolError("partially created endpoint path remains unresolved")
            self.closed = True
            return True
        except Exception as error:  # noqa: BLE001 - retain unresolved resources and failure history
            self.custody.refuse(type(error).__name__ + ": " + str(error))
            return False
