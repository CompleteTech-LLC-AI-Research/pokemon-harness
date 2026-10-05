"""Policy and accounting tests for the #106 matrix-concurrency benchmark.

The selection logic is the part that can silently do the wrong thing: a
benchmark that drops failed rows from its denominator will recommend a faster
configuration precisely because it did less work.  These tests pin that
behaviour down, along with the comparability rule and the fresh-directory
requirement that keeps two arms from colliding.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_benchmark():
    """Import the benchmark module by path, like other script-level tests."""

    path = PROJECT_ROOT / "scripts" / "benchmark_matrix_concurrency.py"
    spec = importlib.util.spec_from_file_location("benchmark_matrix_concurrency", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_benchmark()


def _plan(runtime: str = "source", workers: int = 1, tiers=("trade", "battle")):
    return bench.ExperimentPlan(
        runtime=runtime,
        requested_workers=workers,
        tiers=tuple(tiers),
        python_executable=None,
    )


def _arm(
    *,
    runtime: str = "source",
    workers: int = 1,
    passing: int = 62,
    failed: int = 0,
    incomplete: int = 0,
    not_started: int = 0,
    interrupted: int = 0,
    wall: float = 100.0,
    effective: int | None = None,
    gate_passed: bool = True,
    tiers=("trade", "battle"),
):
    plan = _plan(runtime, workers, tiers)
    # A real arm records what each tier ran, because that is what makes two
    # arms comparable.  Model that here so fixtures are not incomparable by
    # omission; tests that care about the row identity override it.
    tier_rows = {"trade": 43, "battle": 19}
    nodeids = {
        tier: frozenset(f"tests/t.py::test[{tier}-{index}]" for index in range(count))
        for tier, count in tier_rows.items()
    }
    return bench.ArmResult(
        plan=plan,
        completed_passing=passing,
        failed=failed,
        incomplete=incomplete,
        interrupted=interrupted,
        not_started=not_started,
        effective_workers=workers if effective is None else effective,
        tier_row_totals={tier: tier_rows[tier] for tier in plan.tiers if tier in tier_rows},
        tier_rows={tier: nodeids[tier] for tier in plan.tiers if tier in nodeids},
        wall_seconds=wall,
        gate_passed=gate_passed,
    )


def _tier(name: str, *, rows: int, with_cases: bool = True):
    """Build one gate tier payload with a known declared row count."""

    tier = {
        "name": name,
        "status": "PASS",
        "counts": {"passed": rows, "failed": 0},
        "selected_nodeids": [f"tests/t.py::test[{name}-{index}]" for index in range(rows)],
    }
    if with_cases:
        tier["case_results"] = [
            {"nodeid": nodeid, "status": "PASS", "counts": {"passed": 1, "failed": 0}}
            for nodeid in tier["selected_nodeids"]
        ]
    return tier


def _full_set(*, source_walls, native_walls, passing: int = 62):
    """Return complete arms for both runtimes at workers 1/2/4.

    ``passing`` defaults to the full declared matrix (43 trade + 19 battle) so
    a positive selection fixture describes an arm that actually produced an
    outcome for every row it declared.
    """

    arms = []
    for runtime, walls in (("source", source_walls), ("native", native_walls)):
        for workers, wall in zip((1, 2, 4), walls, strict=True):
            arms.append(_arm(runtime=runtime, workers=workers, passing=passing, wall=wall))
    return arms
