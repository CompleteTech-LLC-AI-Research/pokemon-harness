"""Actual finish/harvest AST with controlled failures, no process or kernel operations."""

import __future__

import ast
from pathlib import Path
from types import SimpleNamespace

from scripts import production_gate_parallel_safety as safety
from scripts.production_gate_parallel_capacity import PairBudget
from scripts.production_gate_parallel_ownership import (
    latch_holder_loss,
    record_cgroup_receipt,
    signal_guardians,
)
from scripts.production_gate_parallel_stopping import prepare_stops
from tests._parallel_runtime_holder_kernel import Clock


def finish_failure_case(point):
    clock, sent, cancellations = Clock(), [], []
    budget = PairBudget(2)  # deliberately no arbiter enrolment or independent capacity loss
    hub = SimpleNamespace(closed_reason="", expect_drop=lambda job: None)
    running = []

    def lose(reason):
        hub.closed_reason = hub.closed_reason or reason
        signal_guardians(running, [])

    hub.lose = lose

    def terminate(**kwargs):
        if point == "terminate":
            raise RuntimeError("controlled terminate failure")
        return {"proof": True}

    def reader(*args, **kwargs):
        if point == "terminate":
            return None, ["controlled missing guardian receipt"]
        raise RuntimeError("controlled receipt failure")

    for name in ("failed", "sibling"):
        running.append(
            SimpleNamespace(
                job_id=name,
                cleanup_deadline=None,
                stop_requested=False,
                process=SimpleNamespace(
                    pid=4242,
                    poll=(lambda: 0) if name == "failed" else (lambda: None),
                    guardian_status=SimpleNamespace(
                        drain=lambda: None, ready=True, eof=False, read_receipts=reader
                    ),
                ),
                owned=SimpleNamespace(
                    root_start=987654,
                    cgroup=None,
                    terminate=terminate,
                    signal_root=lambda sig, n=name: sent.append(n),
                ),
                receipt=Path("unused.json"),
                secret=b"unused",
            )
        )
    failed, sibling = running

    def cancel(reason):
        cancellations.append((reason, [item.job_id for item in running]))
        running.clear()

    namespace = {
        "_close_handles": lambda item: None,
        "holder_loss": lambda *args: "controlled terminal holder loss",
        "latch_holder_loss": latch_holder_loss,
        "record_cgroup_receipt": record_cgroup_receipt,
        "prepare_stops": prepare_stops,
        "budget": budget,
        "hub": hub,
        "body": {"deadlines": {"cleanup_seconds": 0.25}},
        "running": running,
        "grace_seconds": 0.01,
        "clock": clock.monotonic,
        "sleeper": clock.sleep,
        "read_guardian_receipt": reader,
        "safety": safety,
        "outcome_record": lambda plan, job, state, **fields: dict(state=state, **fields),
        "plan": None,
        "outcomes": {},
        "cleanup": {},
        "cancel": cancel,
    }
    source = Path(__file__).resolve().parents[1] / "scripts/production_gate_parallel.py"
    outer = next(
        n for n in ast.parse(source.read_text()).body if getattr(n, "name", "") == "_run_plan"
    )
    nodes = [
        n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name in ("finish", "harvest")
    ]
    assert len(nodes) == 2
    exec(  # noqa: S102 - actual repository AST with process operations replaced by controlled doubles
        compile(
            ast.Module(body=nodes, type_ignores=[]),
            "actual-finish-and-harvest",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    namespace["harvest"]()
    return namespace, budget, hub, failed, sibling, sent, cancellations
