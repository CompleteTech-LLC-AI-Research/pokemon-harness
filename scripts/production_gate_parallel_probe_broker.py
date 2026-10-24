"""Continuing root registry for private measurement probes.

This provides allocation-before-admission and bounded service of retained scopes.
The scheduler, CLI pre-dispatch owner and worker request channel still need to
use the same registry and keep their resource leases through unresolved probes.
No execution grant, standalone recovery loop or automatic owner replacement is
provided. Caller-owned checked adoption and its retained self pidfd are mandatory.
"""

from __future__ import annotations

import hashlib
import secrets
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.production_gate_parallel_measurement_budget import MeasurementBudget
from scripts.production_gate_parallel_probe_bootstrap import PROBE_NODE_SOURCE
from scripts.production_gate_parallel_probe_descriptors import RetainedDescriptor
from scripts.production_gate_parallel_probe_launch import ProbeLaunch
from scripts.production_gate_parallel_probe_protocol import (
    ProbeContext,
    ProbeProtocolError,
    canonical_probe_frame,
)

MAX_ACTIVE_PROBES = 4
MAX_RETIRED_PROBES = 4096


class RootProbeBroker:
    """Root registry retained through every measurement and cleanup failure.

    ``resource_owner`` is the existing scheduler/pre-dispatch resource owner,
    never a substitute lease: integration must also prohibit that owner's release
    while this registry has pending scopes. A reference alone is not enforcement.
    """

    def __init__(
        self,
        *,
        root_pin: RetainedDescriptor,
        control_python: Path,
        directory: Path,
        resource_owner: object,
    ) -> None:
        self.root_pin, self.control_python = root_pin, control_python
        self.directory, self.resource_owner = directory, resource_owner
        self.scopes: list[ProbeLaunch] = []
        self.receipts: list[dict] = []
        self.servicing = False

    def begin(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        cwd: Path,
        budget: MeasurementBudget,
    ) -> ProbeLaunch:
        """Register before allocation; failed startup stays available to service()."""

        if len(self.receipts) >= MAX_RETIRED_PROBES:
            raise ProbeProtocolError("root probe receipt quota reached")
        if len(self.scopes) >= MAX_ACTIVE_PROBES or any(
            scope.failures or (scope.owner is not None and scope.owner.custody.failures)
            for scope in self.scopes
        ):
            raise ProbeProtocolError("root probe admission withheld by unresolved prior custody")
        command, environment = list(command), dict(environment)
        program = {"command": command, "environment": environment, "cwd": str(cwd)}
        context = ProbeContext(
            scope=secrets.token_hex(16),
            nonce=secrets.token_hex(16),
            secret=secrets.token_bytes(32),
            source_sha256=hashlib.sha256(PROBE_NODE_SOURCE.encode()).hexdigest(),
            program_sha256=hashlib.sha256(canonical_probe_frame(program)).hexdigest(),
            job_deadline=budget.deadline,
            cleanup_deadline=budget.cleanup_bound(),
        )
        scope = ProbeLaunch(
            context,
            budget.with_checkpoint(self.service),
            root_pin=self.root_pin,
            control_python=self.control_python,
            directory=self.directory / context.scope,
            command=command,
            environment=environment,
            cwd=cwd,
        )
        self.scopes.append(scope)  # allocation errors cannot lose the scope
        scope.allocate()
        return scope

    def service(self) -> None:
        """One bounded pulse per owned scope, with recursive checkpoints suppressed."""

        if self.servicing:
            return
        self.servicing = True
        try:
            for scope in tuple(self.scopes):
                scope.pulse()
        finally:
            self.servicing = False

    def pending(self) -> bool:
        """Do not release adoption, runtimes, endpoint root or job leases if true."""

        return bool(self.scopes)

    def retire(self, scope: ProbeLaunch) -> bool:
        """Release a physically reconciled scope and keep its failed disposition."""

        if scope not in self.scopes or not scope.reconcile():
            return False
        assert scope.owner is not None
        if not scope.release_confirmed_resources():
            return False
        self.receipts.append(
            {
                "scope": scope.context.scope,
                "source_sha256": scope.context.source_sha256,
                "program_sha256": scope.context.program_sha256,
                "job_deadline": scope.context.job_deadline,
                "cleanup_deadline": scope.context.cleanup_deadline,
                "physical_cleanup_verified_at": scope.owner.physical_cleanup_verified_at,
                "holder_wait_status": scope.owner.holder_wait_status,
                "failures": scope.failures,
                "custody_failures": scope.owner.custody.failures,
                "finals": {
                    peer.record.role: peer.final
                    for peer in scope.owner.peers
                    if peer.record is not None
                },
                "output_counts": scope.output_counts,
                "output_sha256": {
                    name: digest.hexdigest() for name, digest in scope.output_hashes.items()
                },
                "released": True,
            }
        )
        self.scopes.remove(scope)
        return True

    def run(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        cwd: Path,
        budget: MeasurementBudget,
    ) -> subprocess.CompletedProcess[bytes]:
        """Await bounded measurement while the caller services all sibling owners.

        Expiry raises with the original scope still retained; the continuing
        scheduler must keep pulsing it. No cleanup allowance starts here. An
        authenticated nonzero target result remains nonzero, never proof of a
        successful operation. Missing guardian evidence withholds a result.
        """

        scope = self.begin(command, environment=environment, cwd=cwd, budget=budget)
        while True:
            budget.checkpoint()  # scheduler callback, original deadline, latched cleanup
            self.service()
            if scope.failures:
                raise ProbeProtocolError("probe output/admission failed; retained custody")
            if scope.reconcile():
                assert scope.owner is not None
                finals = {
                    peer.record.role: peer.final
                    for peer in scope.owner.peers
                    if peer.record is not None
                }
                guardian = finals.get("guardian")
                if guardian is None or scope.owner.custody.failures:
                    raise ProbeProtocolError("physical cleanup cannot establish measured result")
                status = guardian["child_status"]
                if type(status) is not int or (
                    status == 0 and not scope.owner.operation_succeeded()
                ):
                    raise ProbeProtocolError("measurement success is unproven")
                result = subprocess.CompletedProcess(
                    list(command),
                    status,
                    bytes(scope.output["stdout"]),
                    bytes(scope.output["stderr"]),
                )
                budget.checkpoint()  # never return success after copying crosses a bound
                if not self.retire(scope):
                    raise ProbeProtocolError("probe resources remain unresolved")
                try:
                    budget.checkpoint()
                except Exception as error:
                    scope.refuse(
                        "measurement expired during resource release: " + type(error).__name__
                    )
                    raise
                return result
            remaining = min(budget.deadline, budget.cleanup_bound()) - budget.now()
            if remaining > 0:
                time.sleep(min(0.01, remaining))  # bounded wait, not a new allowance
