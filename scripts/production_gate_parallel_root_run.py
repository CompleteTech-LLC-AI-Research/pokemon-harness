"""Public scheduler run ownership; standalone mode allocates no probe endpoint."""

from __future__ import annotations

from typing import Any

from scripts.production_gate_parallel_root_context import RootContext


def run_owned(
    plan: Any, *, adoption_type: Any, ledger_type: Any, runner: Any,
    root_context: RootContext | None = None, adopt_orphans: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    ledger = ledger_type(kwargs["budget"])
    ledger.retain()  # original process-local root before adoption/context mutation
    original = None
    try:
        if root_context is None:
            root_context = RootContext()
            ledger.adoption = adoption_type(enabled=adopt_orphans)
            root_context.claim(ledger.adoption, ledger.is_retained, standalone=True)
            root_context.attach_run(ledger, (j["job_id"] for j in plan.as_dict()["jobs"]))
            adoption = ledger.adoption.__enter__()
        else:
            if not adopt_orphans:
                raise RuntimeError("disabled adoption cannot borrow a qualifying root context")
            root_context.attach_run(ledger, (j["job_id"] for j in plan.as_dict()["jobs"]))
            adoption = root_context.adoption
        return runner(plan, adoption=adoption, ledger=ledger, **kwargs)
    except BaseException as error:
        original = error
        ledger.failure = ledger.failure or f"{type(error).__name__}: {error}"
        raise
    finally:
        ledger.guard(lambda: ledger.finalize(ledger.cleanup, original), "run finalization")
