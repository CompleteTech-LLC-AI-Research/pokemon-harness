"""Structured supplemental facts of the concurrent runtime gate."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_declaration import COVERAGE_DECLARATION, LIMITATIONS
from scripts.production_gate_parallel_plan import ParallelPlan


def _report_extras(
    plan: ParallelPlan,
    started: float,
    clock: Callable[[], float],
    cancel_reason: str,
    cleanup: Mapping[str, Any],
    problems: Sequence[str],
    *,
    observations: Sequence[Any] = (),
    serial: bool = False,
    load: tuple[Any, Any] = ((), ()),
) -> dict[str, Any]:
    body = plan.as_dict()
    return {
        "fail_fast": body["fail_fast"],
        "elapsed_seconds": round(clock() - started, 3),
        "cancellation": cancel_reason,
        "cleanup": dict(cleanup),
        "orchestration_problems": list(problems),
        "capacity": body["capacity"],
        "capacity_observations": list(observations),
        "execution_mode": "serial" if serial else "concurrent",
        "plan": plan.text,
        "resource_trace": {
            "affinity_cpus": len(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else 0,
            "loadavg_start": list(load[0]),
            "loadavg_end": list(load[1]),
        },
        "row_identity_enforced": True,
        "sequential_comparison": {"status": "NOT_RUN"},
        "limitations": list(LIMITATIONS),
        "coverage_declaration": COVERAGE_DECLARATION,
        **({"overall": "FAIL"} if problems else {}),
    }


def _write_progress(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass
