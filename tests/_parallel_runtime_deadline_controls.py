"""Controlled clocks and processes for actual timeout scheduling; no kernel operations."""

from types import SimpleNamespace

from scripts.production_gate_parallel_stopping import DeadlineWatch
from tests._parallel_runtime_holder_kernel import Clock


def watched_jobs(expiry, *, fail_fast=False):
    clock, sent, jobs = Clock(), [], []
    for name, deadline in zip(("first", "second"), expiry, strict=True):
        jobs.append(
            SimpleNamespace(
                job_id=name,
                deadline=deadline,
                cleanup_deadline=None,
                stop_requested=False,
                process=SimpleNamespace(
                    poll=lambda: None,
                    guardian_status=SimpleNamespace(drain=lambda: None, ready=True, eof=False),
                ),
                owned=SimpleNamespace(signal_root=lambda sig, n=name: sent.append((n, clock.now))),
            )
        )
    watch = DeadlineWatch(
        jobs,
        SimpleNamespace(expect_drop=lambda job: None),
        0.25,
        lambda item: fail_fast,
        grace_seconds=0.01,
        clock=clock.monotonic,
        sleeper=clock.sleep,
        problems=[],
    )
    return watch, clock, jobs, sent
