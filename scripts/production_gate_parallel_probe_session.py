"""Checked CLI root ownership spanning pre-dispatch probes and job measurements.

Source under integration, not execution-admitted. One process-local owner retains
adoption, its self pidfd, private endpoint root and the continuing broker until
all registered scopes retire. Missing physical proof keeps this owner active;
it cannot be replaced with another session or a renewed cleanup allowance.
"""

from __future__ import annotations

import contextlib
import os
import signal
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Self

from scripts.production_gate_parallel_measurement_budget import (
    IdentityDeadline,
    MeasurementBudget,
    pre_dispatch_budget,
)
from scripts.production_gate_parallel_probe_broker import RootProbeBroker
from scripts.production_gate_parallel_probe_descriptors import RetainedDescriptor, validate_pidfd
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError
from scripts.production_gate_parallel_processes import OrchestratorAdoption
from scripts.production_gate_parallel_root_context import RootContext

_LIVE_SESSIONS: list[ProbeSession] = []


class ProbeSession:
    """CLI owns this object before any measurement or mutable-input allocation."""

    def __init__(self) -> None:
        self.adoption = OrchestratorAdoption()
        self.root_context = RootContext()
        self.root_pin: RetainedDescriptor | None = None
        self.directory: Path | None = None
        self.directory_binding: tuple[int, int] | None = None
        self.broker: RootProbeBroker | None = None
        self.budget = pre_dispatch_budget()
        self.closed = False
        self.enter_attempted = False
        self.setup_complete = False
        self.disposition_started = False
        self.setup_failure = ""
        self.setup_error: BaseException | None = None
        self.raw_root_pidfd: int | None = None
        self.root_pin_state = "none"
        self.raw_directory: str | None = None
        self.directory_state = "none"
        self.interrupt = threading.Event()
        self.temporary_inputs: list[
            tuple[tempfile.TemporaryDirectory[str], tuple[int, int] | None]
        ] = []

    @contextlib.contextmanager
    def cancellation_signals(self) -> Iterator[None]:
        """Concurrent root lifetime only; signal cancellation never unwinds custody."""

        previous = {}
        try:
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[number] = signal.signal(
                    number, lambda _signum, _frame: self.interrupt.set()
                )
            yield
        finally:
            for number, handler in reversed(tuple(previous.items())):
                signal.signal(number, handler)

    def __enter__(self) -> Self:
        if self.enter_attempted or self.closed:
            raise ProbeProtocolError("root probe setup cannot be re-entered")
        if _LIVE_SESSIONS:
            raise ProbeProtocolError("previous root probe owner remains unresolved")
        _LIVE_SESSIONS.append(self)  # before adoption mutation or any descriptor allocation
        self.enter_attempted = True
        try:
            self.root_context.claim(self.adoption, lambda: self in _LIVE_SESSIONS)
            self.adoption.__enter__()
            if not self.adoption.active or not self.adoption.verify():
                raise ProbeProtocolError("checked root subreaper admission failed")
            self.root_pin_state = "allocating"
            self.raw_root_pidfd = os.pidfd_open(os.getpid())
            self.root_pin_state = "raw"
            self.root_pin_state = "wrapping"
            self.root_pin = RetainedDescriptor(self.raw_root_pidfd)
            self.root_pin_state = "wrapped"
            self.raw_root_pidfd = None
            self.root_pin_state = "validating"
            info = os.fstat(self.root_pin.descriptor)
            self.root_pin.binding = (info.st_dev, info.st_ino)
            start, _parent = validate_pidfd(self.root_pin, os.getpid(), self.budget)
            self.root_pin.instance = (os.getpid(), start)
            self.root_pin.validated = True
            self.root_pin_state = "validated"
            self.directory_state = "allocating"
            self.raw_directory = tempfile.mkdtemp(prefix="pokered-probe-")
            self.directory_state = "raw"
            self.directory = Path(self.raw_directory)
            self.directory_state = "unbound"
            info = self.directory.lstat()
            self.directory_binding = (info.st_dev, info.st_ino)
            self.directory_state = "bound"
            control = Path("/usr/bin/python3").resolve(strict=True)
            if os.access(control, os.W_OK):
                raise ProbeProtocolError("control interpreter is writable by the executing UID")
            self.broker = RootProbeBroker(
                root_pin=self.root_pin,
                control_python=control,
                directory=self.directory,
                resource_owner=self,
            )
            self.root_context.probe_service = self.broker.service
            self.budget = replace(
                self.budget,
                service=self.checkpoint,
                probe_runner=self.broker.run,
                temporary_owner=self.retain_temporary,
            )
            self.setup_complete = True
            return self
        except BaseException as error:
            if self.setup_error is None:
                self.setup_error = error
                self.setup_failure = type(error).__name__
            raise

    def attach(self, budget: MeasurementBudget) -> MeasurementBudget:
        """Keep job success/cleanup bounds; never replace them with pre-dispatch bounds."""

        if (
            not self.setup_complete
            or self.setup_failure
            or self.disposition_started
            or self.root_context.disposing
            or self.root_context.failure
            or self.root_pin_state != "validated"
            or self.raw_root_pidfd is not None
            or self.root_pin is None
            or not self.root_pin.validated
            or self.root_pin.closed
            or self.root_pin.binding is None
            or self.root_pin.instance is None
            or self.directory_state != "bound"
            or self.raw_directory is None
            or self.directory is None
            or self.directory_binding is None
            or self.broker is None
            or self.broker.root_pin is not self.root_pin
            or self.broker.directory is not self.directory
            or self.broker.resource_owner is not self
            or self.closed
            or not self.adoption.verify()
        ):
            raise ProbeProtocolError("continuing root probe owner is unavailable")
        return replace(
            budget.with_checkpoint(self.checkpoint),
            probe_runner=self.broker.run,
            temporary_owner=self.retain_temporary,
        )

    def retain_temporary(self, directory: tempfile.TemporaryDirectory[str]) -> None:
        self.temporary_inputs.append((directory, None))
        info = Path(directory.name).lstat()
        self.temporary_inputs[-1] = (directory, (info.st_dev, info.st_ino))

    def service(self) -> None:
        if self.root_context.claimed and (
            self.root_context._servicing or self.root_context.closed or self.root_context.disposing
        ):
            self.root_context.service()  # guard BEFORE any nested scope use
            return
        if self.broker is not None:
            if self.interrupt.is_set():
                for scope in self.broker.scopes:
                    scope.refuse("CLI cancellation; success permanently refused")
        if self.root_context.claimed:
            self.root_context.service()
        elif self.broker is not None:
            self.broker.service()

    def checkpoint(self) -> None:
        self.service()
        if self.interrupt.is_set():
            raise IdentityDeadline("CLI measurement cancelled; continuing custody retained")

    def children_quiescent(self) -> bool:
        return self.root_context.children_quiescent() and not (
            self.broker is not None and self.broker.pending()
        )

    def pending(self) -> bool:
        """Own root disposition/restoration remains pending after children settle."""
        return not self.closed or not self.children_quiescent()

    def continue_custody(self) -> None:
        """Post-refusal custody has no new success window or new child admission.

        A missing holder final or unclassified adoption currently stays UNPROVEN:
        role-loss adoption and pre-ACK failure retirement remain unfinished. This
        loop keeps the actual owner alive, rather than treating a Python reference
        as a substitute for continuing custody. No scope is relaunched.
        """

        # One finite pass. Unknown jobs are NOT sent through finalize/finish again.
        first = None
        try:
            self.service()
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - finish finite siblings
            first = error
        if self.broker is not None:
            for scope in tuple(self.broker.scopes):
                try:
                    scope.refuse("CLI measurement ended; continuing physical custody only")
                    self.broker.retire(scope)  # same original exact owner and bounds
                except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - retain primary
                    if first is None:
                        first = error
                    elif first is not error:
                        first.add_note(f"secondary custody continuation: {type(error).__name__}: {error}")
        if first is not None:
            raise first

    def close(self) -> None:
        if self.closed:
            return
        if self not in _LIVE_SESSIONS:
            self.closed = True  # a refused new owner never acquired the old owner's obligations
            return
        if not self.children_quiescent():
            raise ProbeProtocolError(
                "root probe custody remains unresolved; retain inputs and adoption"
            )
        # Unknown or interrupted ownership is not repaired by repeating allocation,
        # rebinding a pathname, or guessing whether a numeric descriptor was closed.
        pin_none = (
            self.root_pin_state == "none" and self.root_pin is None and self.raw_root_pidfd is None
        )
        pin_valid = (
            self.root_pin_state == "validated"
            and self.root_pin is not None
            and self.root_pin.validated
            and not self.root_pin.closed
            and self.raw_root_pidfd is None
        )
        pin_closed = (
            self.root_pin_state == "closed"
            and self.root_pin is not None
            and self.root_pin.closed
            and self.raw_root_pidfd is None
        )
        path_none = (
            self.directory_state == "none"
            and self.raw_directory is None
            and self.directory is None
            and self.directory_binding is None
        )
        path_bound = (
            self.directory_state == "bound"
            and self.raw_directory is not None
            and self.directory is not None
            and self.directory_binding is not None
        )
        path_removed = (
            self.directory_state == "removed"
            and self.raw_directory is not None
            and self.directory is None
            and self.directory_binding is not None
        )
        if not (pin_none or pin_valid or pin_closed) or not (
            path_none or path_bound or path_removed
        ):
            raise ProbeProtocolError("root setup or disposition ownership remains unresolved")
        self.disposition_started = True  # monotone: never issue another attached runner
        self.root_context.disposing = True
        for directory, binding in tuple(self.temporary_inputs):
            info = Path(directory.name).lstat()
            if (info.st_dev, info.st_ino) != binding:
                raise ProbeProtocolError(
                    "retained temporary input directory changed before release"
                )
            directory.cleanup()
            self.temporary_inputs.remove((directory, binding))
        if path_bound:
            self.directory_state = "removing"
            try:
                info = self.directory.lstat()
                if (info.st_dev, info.st_ino) != self.directory_binding:
                    raise ProbeProtocolError("root endpoint directory changed before release")
                self.directory.rmdir()
            except BaseException:
                self.directory_state = "unknown"
                raise
            self.directory = None
            self.directory_state = "removed"
        if pin_valid:
            self.root_pin_state = "closing"
            try:
                os.close(self.root_pin.bound_descriptor())
            except BaseException:
                self.root_pin_state = "unknown"
                raise
            self.root_pin.closed = True
            self.root_pin_state = "closed"
        if self.root_context.claimed:
            self.root_context.close_adoption()
        else:
            self.adoption.__exit__(None, None, None)  # no entered context in legacy fixtures
        if self.adoption.restoration_pending:
            raise ProbeProtocolError("root adoption restoration remains unresolved")
        self.closed = True
        _LIVE_SESSIONS.remove(self)
