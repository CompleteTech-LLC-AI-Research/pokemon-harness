"""Checked CLI root ownership spanning pre-dispatch probes and job measurements.

Source under integration, not execution-admitted. One process-local owner retains
adoption, its self pidfd, private endpoint root and the continuing broker until
all registered scopes retire. Missing physical proof keeps this owner active;
it cannot be replaced with another session or a renewed cleanup allowance.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
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

_LIVE_SESSIONS: list[ProbeSession] = []


class ProbeSession:
    """CLI owns this object before any measurement or mutable-input allocation."""

    def __init__(self) -> None:
        self.adoption = OrchestratorAdoption()
        self.root_pin: RetainedDescriptor | None = None
        self.directory: Path | None = None
        self.directory_binding: tuple[int, int] | None = None
        self.broker: RootProbeBroker | None = None
        self.budget = pre_dispatch_budget()
        self.closed = False
        self.interrupt = threading.Event()
        self.temporary_inputs: list[
            tuple[tempfile.TemporaryDirectory[str], tuple[int, int] | None]
        ] = []

    def __enter__(self) -> Self:
        if _LIVE_SESSIONS:
            raise ProbeProtocolError("previous root probe owner remains unresolved")
        _LIVE_SESSIONS.append(self)  # before adoption mutation or any descriptor allocation
        self.adoption.__enter__()
        if not self.adoption.active or not self.adoption.verify():
            raise ProbeProtocolError("checked root subreaper admission failed")
        self.root_pin = RetainedDescriptor(os.pidfd_open(os.getpid()))
        info = os.fstat(self.root_pin.descriptor)
        self.root_pin.binding = (info.st_dev, info.st_ino)
        start, _parent = validate_pidfd(self.root_pin, os.getpid(), self.budget)
        self.root_pin.instance = (os.getpid(), start)
        self.root_pin.validated = True
        self.directory = Path(tempfile.mkdtemp(prefix="pokered-probe-"))
        info = self.directory.lstat()
        self.directory_binding = (info.st_dev, info.st_ino)
        control = Path("/usr/bin/python3").resolve(strict=True)
        if os.access(control, os.W_OK):
            raise ProbeProtocolError("control interpreter is writable by the executing UID")
        self.broker = RootProbeBroker(
            root_pin=self.root_pin,
            control_python=control,
            directory=self.directory,
            resource_owner=self,
        )
        self.budget = replace(
            self.budget,
            service=self.checkpoint,
            probe_runner=self.broker.run,
            temporary_owner=self.retain_temporary,
        )
        return self

    def attach(self, budget: MeasurementBudget) -> MeasurementBudget:
        """Keep job success/cleanup bounds; never replace them with pre-dispatch bounds."""

        if self.broker is None or self.closed or not self.adoption.verify():
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
        if self.broker is not None:
            if self.interrupt.is_set():
                for scope in self.broker.scopes:
                    scope.refuse("CLI cancellation; success permanently refused")
            self.broker.service()

    def checkpoint(self) -> None:
        self.service()
        if self.interrupt.is_set():
            raise IdentityDeadline("CLI measurement cancelled; continuing custody retained")

    def pending(self) -> bool:
        return self.broker is not None and self.broker.pending()

    def continue_custody(self) -> None:
        """Post-refusal custody has no new success window or new child admission.

        A missing holder final or unclassified adoption currently stays UNPROVEN:
        role-loss adoption and pre-ACK failure retirement remain unfinished. This
        loop keeps the actual owner alive, rather than treating a Python reference
        as a substitute for continuing custody. No scope is relaunched.
        """

        while self.pending():
            assert self.broker is not None
            self.service()
            for scope in tuple(self.broker.scopes):
                scope.refuse("CLI measurement ended; continuing physical custody only")
                try:
                    self.broker.retire(scope)
                except Exception as error:  # noqa: BLE001 - retain unknown physical evidence
                    scope.refuse("custody reconciliation remains unproven: " + type(error).__name__)
            if self.pending():
                time.sleep(0.01)

    def close(self) -> None:
        if self.closed:
            return
        if self not in _LIVE_SESSIONS:
            self.closed = True  # a refused new owner never acquired the old owner's obligations
            return
        if self.pending():
            raise ProbeProtocolError(
                "root probe custody remains unresolved; retain inputs and adoption"
            )
        for directory, binding in tuple(self.temporary_inputs):
            info = Path(directory.name).lstat()
            if (info.st_dev, info.st_ino) != binding:
                raise ProbeProtocolError(
                    "retained temporary input directory changed before release"
                )
            directory.cleanup()
            self.temporary_inputs.remove((directory, binding))
        if self.directory is not None:
            info = self.directory.lstat()
            if (info.st_dev, info.st_ino) != self.directory_binding:
                raise ProbeProtocolError("root endpoint directory changed before release")
            self.directory.rmdir()
            self.directory = None
        if self.root_pin is not None and not self.root_pin.closed:
            if not self.root_pin.validated:
                raise ProbeProtocolError("root self descriptor remains unresolved")
            os.close(self.root_pin.bound_descriptor())
            self.root_pin.closed = True
        self.adoption.__exit__(None, None, None)  # checked restoration, retained on refusal
        self.closed = True
        _LIVE_SESSIONS.remove(self)
