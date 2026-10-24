"""Root-owned holder allocation and bounded admission/output pulses.

The continuing root registers a ProbeLaunch before calling allocate(). This
module never installs adoption, owns a background loop, releases job capacity,
or retries a refused launch. Scheduler/worker-request integration is unfinished;
this source is not an execution grant. Partial allocations remain in the caller's
registry, including a Popen whose first pidfd could not be validated.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import signal
import socket
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.production_gate_parallel_adoption import read_subreaper
from scripts.production_gate_parallel_measurement_budget import MeasurementBudget
from scripts.production_gate_parallel_probe_bootstrap import CONTROL_ENVIRONMENT, PROBE_NODE_SOURCE
from scripts.production_gate_parallel_probe_descriptors import RetainedDescriptor, validate_pidfd
from scripts.production_gate_parallel_probe_owner import RootProbeOwner
from scripts.production_gate_parallel_probe_protocol import (
    MAX_PROBE_FRAME,
    ProbeContext,
    ProbeProtocolError,
    canonical_probe_frame,
    decode_probe_frame,
)

OUTPUT_LIMIT = 8 * 1024 * 1024
OUTPUT_PULSE_BYTES = 65536


class ProbeLaunch:
    """A registered scope's allocation, parent admission and output obligations.

    Root's already held self pin and checked adoption context outlive every
    launch. The registry must service this object even after a caller's bounded
    measurement fails; unresolved ownership prevents resource/capacity release.
    All I/O in pulse() is nonblocking and bounded. No Popen.poll()/wait() competes
    with RootProbeOwner's pidfd wait; the explicit owner supplies its wait result.
    """

    def __init__(
        self,
        context: ProbeContext,
        budget: MeasurementBudget,
        *,
        root_pin: RetainedDescriptor,
        control_python: Path,
        directory: Path,
        command: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path,
        grace_seconds: float = 1.0,
    ) -> None:
        self.context, self.budget = context, budget
        self.root_pin, self.control_python = root_pin, control_python
        self.directory, self.cwd = directory, cwd
        self.command, self.environment = list(command), dict(environment)
        self.grace_seconds = grace_seconds
        # Set before invoking Popen: exceptions from Popen are uncertain spawn,
        # never evidence of a zero-child allocation.
        self.root_context = None
        self.spawn_attempted = False
        self.directory_created = False
        self.process: subprocess.Popen[bytes] | None = None
        self.holder_pin: RetainedDescriptor | None = None
        # Raw acquisition is retained before any wrapper can raise. Only the
        # descriptor owner may close after transfer; raw/partial state is UNPROVEN.
        self.raw_holder_pidfd: int | None = None
        self.holder_pidfd_owner = "unopened"
        self.owner: RootProbeOwner | None = None
        self.parent: socket.socket | None = None
        self.child: socket.socket | None = None
        self.directory_binding: tuple[int, int] | None = None
        self.pending_initial: bytes | None = None
        self.pending_ack: bytes | None = None
        self.ready = False
        self.acknowledged = False
        # Latch BEFORE send: even a short/raising ACK send forfeits no-fork proof.
        self.parent_ack_attempted = False
        self.unadmitted_abort = False
        self.allocated = False
        self.setup_complete = False
        self.released = False
        self.terminal_publication_state = "none"
        self.servicing = False
        self.failures: list[str] = []
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        self.output_hashes = {name: hashlib.sha256() for name in self.output}
        self.output_counts = dict.fromkeys(self.output, 0)
        self.output_eof = dict.fromkeys(self.output, False)

    def refuse(self, reason: str) -> None:
        if reason not in self.failures and len(self.failures) < 64:
            self.failures.append(reason[:240])
        if self.owner is not None:
            self.owner.custody.refuse(reason)

    def allocate(self) -> None:
        """Call only after registry retention, before sending any admission bytes."""

        if self.allocated or self.failures:
            raise ProbeProtocolError("a probe allocation cannot be retried")
        self.allocated = True  # a failure cannot restart this allocation
        try:
            self.budget.checkpoint()
            if read_subreaper(ctypes.CDLL(None, use_errno=True)) != 1:
                raise ProbeProtocolError("root checked subreaper admission is unavailable")
            if (
                not self.root_pin.validated
                or self.root_pin.instance is None
                or self.root_pin.instance[0] != os.getpid()
                or self.root_pin.exited()
                or not self.control_python.is_absolute()
                or not self.cwd.is_absolute()
                or not self.command
                or any(not isinstance(v, str) or "\0" in v for v in self.command)
                or any(
                    not isinstance(k, str)
                    or not isinstance(v, str)
                    or not k
                    or "=" in k
                    or "\0" in k + v
                    for k, v in self.environment.items()
                )
            ):
                raise ProbeProtocolError("root pin or measured program admission is invalid")
            self.root_pin.bound_descriptor()
            program = {
                "command": self.command,
                "environment": self.environment,
                "cwd": str(self.cwd),
            }
            if (
                hashlib.sha256(canonical_probe_frame(program)).hexdigest()
                != self.context.program_sha256
            ):
                raise ProbeProtocolError("measured program differs from its frozen binding")
            if len(self.context.secret) != 32 or any(
                self.context.secret.hex() in value for value in self.environment.values()
            ):
                raise ProbeProtocolError("private probe key admission is invalid")
            if (
                self.context.source_sha256 != hashlib.sha256(PROBE_NODE_SOURCE.encode()).hexdigest()
                or self.context.job_deadline != self.budget.deadline
                or self.context.cleanup_deadline != self.budget.cleanup_bound()
            ):
                raise ProbeProtocolError("source or original budget binding differs")
            if len(os.fsencode(self.directory / "root.sock")) >= 108:
                raise ProbeProtocolError("private endpoint exceeds the AF_UNIX path bound")
            self.directory.mkdir(mode=0o700)  # refuse reuse of another scope's directory
            self.directory_created = True
            info = self.directory.lstat()
            self.directory_binding = (info.st_dev, info.st_ino)
            self.parent, self.child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.parent.setblocking(False)
            self.child.setblocking(False)
            self.budget.checkpoint()
            self.spawn_attempted = True
            self.process = subprocess.Popen(
                [
                    str(self.control_python),
                    "-I",
                    "-S",
                    "-c",
                    PROBE_NODE_SOURCE,
                    "holder",
                    str(self.child.fileno()),
                    *(str(v) for v in self.root_pin.instance),
                    repr(self.context.job_deadline),
                    repr(self.context.cleanup_deadline),
                ],
                pass_fds=(self.child.fileno(),),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=CONTROL_ENVIRONMENT,
            )
            if self.root_context is not None:
                self.root_context.partition.publish_original(self, self.process)
            # Keep the original unreaped Popen even if the first pin fails. No
            # initial config or ACK has been sent; trusted H cannot fork/exec.
            self.raw_holder_pidfd = os.pidfd_open(self.process.pid)
            self.holder_pidfd_owner = "raw"
            self.holder_pin = RetainedDescriptor(self.raw_holder_pidfd)
            self.holder_pidfd_owner = "descriptor"
            self.raw_holder_pidfd = None  # transfer, never a second close obligation
            held = self.holder_pin
            self.owner = RootProbeOwner.prepare(
                self.context,
                self.budget,
                root_pin=self.root_pin,
                holder_pin=held,
                control_python=self.control_python,
                endpoint=self.directory / "root.sock",
                grace_seconds=self.grace_seconds,
            )  # complete resource-free owner published before validation/callbacks
            self.owner.root_context = self.root_context
            self.owner.wait_scope = self
            info = os.fstat(held.descriptor)
            held.binding = (info.st_dev, info.st_ino)
            start, parent = validate_pidfd(held, self.process.pid, self.budget)
            if parent != self.root_pin.instance[0]:
                raise ProbeProtocolError("allocated holder is not root's original child")
            held.instance, held.parent, held.role = (
                (self.process.pid, start),
                self.root_pin.instance,
                "holder",
            )
            held.validated = True
            if self.root_context is not None:
                self.root_context.partition.bind_holder(self, held)
            self.child.close()
            self.child = None
            for stream in (self.process.stdout, self.process.stderr):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
            self.owner.open_endpoint()
            config = dict(
                program,
                role="holder",
                scope=self.context.scope,
                nonce=self.context.nonce,
                secret=self.context.secret.hex(),
                source_sha256=self.context.source_sha256,
                program_sha256=self.context.program_sha256,
                job_deadline=self.context.job_deadline,
                cleanup_deadline=self.context.cleanup_deadline,
                root_pid=self.root_pin.instance[0],
                root_start=self.root_pin.instance[1],
                root_endpoint=str(self.owner.endpoint),
                grace_seconds=self.grace_seconds,
            )
            self.pending_initial = canonical_probe_frame(config)
            if len(self.pending_initial) > MAX_PROBE_FRAME:
                raise ProbeProtocolError("initial configuration exceeds its declared bound")
            self.budget.checkpoint()
            self.setup_complete = True
        except BaseException as error:
            self.refuse(type(error).__name__ + ": " + str(error))
            # No ACK on partial setup; retain process, pin, sockets and paths.
            raise

    def _send_pending(self, attribute: str) -> None:
        raw = getattr(self, attribute)
        if raw is None:
            return
        self.budget.checkpoint()
        assert self.parent is not None
        if attribute == "pending_ack":
            self.parent_ack_attempted = True
        if self.parent.send(raw) != len(raw):
            raise ProbeProtocolError("partial root-parent admission frame")
        setattr(self, attribute, None)
        if attribute == "pending_ack":
            self.acknowledged = True

    def _admit(self) -> None:
        assert self.owner is not None and self.holder_pin is not None
        if (
            not self.setup_complete
            or self.failures
            or self.owner.custody.failures
            or self.acknowledged
        ):
            return
        if self.pending_initial is not None:
            self._send_pending("pending_initial")
            return
        if not self.ready:
            self.budget.checkpoint()
            raw, rights = self.owner.custody.receive_packet(self.parent)
            if rights:
                raise ProbeProtocolError(
                    "unexpected parent-ready rights retained; admission refused"
                )
            body = decode_probe_frame(raw, self.context)
            if (
                body["phase"] != "ready"
                or body["role"] != "holder"
                or (body["pid"], body["start"]) != self.holder_pin.instance
                or body["sequence"] != 1
                or body.get("guard") != signal.SIGTERM
                or body.get("subreaper") is not True
            ):
                raise ProbeProtocolError("original holder readiness differs")
            self.holder_pin.bound_descriptor()
            if self.holder_pin.exited():
                raise ProbeProtocolError("holder exited before original parent ACK")
            self.ready = True
            self.pending_ack = self.owner._frame(
                self.holder_pin,
                "parent_ack",
                retained=True,
                parent_pid=self.root_pin.instance[0],
                parent_start=self.root_pin.instance[1],
            )
        self._send_pending("pending_ack")

    def _read_output(self) -> None:
        if self.process is None:
            return
        for name in self.output:
            stream = getattr(self.process, name)
            if stream is None or self.output_eof[name]:
                continue
            # Partial pidfd/setup refusal can occur before allocate configured
            # these streams. A continuing pulse must never read a blocking pipe.
            os.set_blocking(stream.fileno(), False)
            try:
                raw = os.read(stream.fileno(), OUTPUT_PULSE_BYTES)
            except BlockingIOError:
                continue
            if not raw:
                self.output_eof[name] = True
                stream.close()
                continue
            self.output_counts[name] += len(raw)
            self.output_hashes[name].update(raw)
            remaining = max(0, OUTPUT_LIMIT - len(self.output[name]))
            self.output[name].extend(raw[:remaining])
            if self.output_counts[name] > OUTPUT_LIMIT:
                self.refuse(name + " exceeded bounded capture; full output is unavailable")

    def pulse(self) -> None:
        """Service one bounded admission/output/owner step; never release capacity."""

        if self.servicing:
            return
        self.servicing = True
        try:
            try:
                self._read_output()
                if self.owner is not None:
                    self._admit()
            except BlockingIOError:
                pass
            except Exception as error:  # noqa: BLE001 - retain every partial allocation on refusal
                self.refuse(type(error).__name__ + ": " + str(error))
            if self.owner is not None:
                if self._begin_unadmitted_abort():
                    # This owner alone signals/waits; never admit another peer.
                    self.owner.service_unadmitted_abort()
                else:
                    self.owner.service()
        finally:
            self.servicing = False

    def _begin_unadmitted_abort(self) -> bool:
        """Positive pre-parent-ACK proof; uncertainty never creates this branch.

        The original Popen is retained and never polled/waited by this launcher.
        Its exclusive wait authority belongs to this owner, including on abort.
        No replacement pin, process lookup or ACK retry is allowed here.
        """

        if self.unadmitted_abort:
            return True
        owner, process, held = self.owner, self.process, self.holder_pin
        if (
            not (self.failures or (owner is not None and owner.custody.failures))
            or not self.spawn_attempted
            or process is None
            or process.returncode is not None
            or self.parent_ack_attempted
            or self.acknowledged
            or owner is None
            or held is None
            or self.raw_holder_pidfd is not None
            or self.holder_pidfd_owner == "raw"
            or not held.validated
            or held.closed
            or held.instance is None
            or held.instance[0] != process.pid
            or held.parent != self.root_pin.instance
            or owner.holder_pin is not held
            or owner.root_pin is not self.root_pin
            or owner.peers
            or owner.custody.records
            or owner.custody.roles
            or owner.holder_wait_status is not None
            or owner.physical_cleanup_verified_at is not None
        ):
            return False
        held.bound_descriptor()
        self.unadmitted_abort = True
        owner.unadmitted_process = process
        owner.custody.refuse("original parent ACK never attempted; no-fork abort")
        return True

    def never_spawned(self) -> bool:
        """Positive phase evidence, never an inference from owner=None."""

        return (
            self.allocated
            and not self.spawn_attempted
            and self.process is None
            and self.holder_pin is None
            and self.raw_holder_pidfd is None
            and self.holder_pidfd_owner == "unopened"
            and self.owner is None
            and self.pending_initial is None
            and not self.acknowledged
        )

    def reconcile(self) -> bool:
        """Physical owner proof and output EOF; failure is never successful measurement."""

        if self.raw_holder_pidfd is not None or self.holder_pidfd_owner == "raw":
            return False  # no wrapper/validated capability, not a closeable no-spawn path
        if self.never_spawned():
            return True  # no holder final or wait obligation can exist in this phase
        if self.owner is None or not self.owner.reconcile():
            return False
        assert self.process is not None
        self.process.returncode = self.owner.holder_wait_status
        return all(self.output_eof.values())

    def release_confirmed_resources(self) -> bool:
        """Caller still retains job leases; retire only reconciled owned resources."""

        if self.released:
            return True
        if not self.reconcile():
            return False
        if not self.never_spawned():
            assert self.owner is not None
            if not self.owner.release_confirmed_resources():
                return False
        try:
            for attribute in ("parent", "child"):
                channel = getattr(self, attribute)
                if channel is not None:
                    channel.close()
                    setattr(self, attribute, None)
            if self.directory_created:
                if self.directory_binding is None:
                    raise ProbeProtocolError("created directory binding remains unknown")
                info = self.directory.lstat()
                if (info.st_dev, info.st_ino) != self.directory_binding:
                    raise ProbeProtocolError("owned probe directory changed before release")
                self.directory.rmdir()  # unexpected files remain unresolved
                self.directory_created = False
                self.directory_binding = None
            self.released = True
            return True
        except Exception as error:  # noqa: BLE001 - partial release retains registry/lease obligation
            self.refuse(type(error).__name__ + ": " + str(error))
            return False
