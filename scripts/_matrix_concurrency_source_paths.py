from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

if __package__:
    from ._matrix_concurrency_constants import (
        ARM_TIMEOUT_MARGIN,
        DEFAULT_ARM_TIMEOUT_SECONDS_FLOOR,
        REQUIRED_SELECTION_TIERS,
    )
else:
    from _matrix_concurrency_constants import (
        ARM_TIMEOUT_MARGIN,
        DEFAULT_ARM_TIMEOUT_SECONDS_FLOOR,
        REQUIRED_SELECTION_TIERS,
    )


def default_arm_timeout_seconds(project_root: Path) -> float:
    """Return an arm bound that cannot kill a legitimate workers=1 baseline.

    The gate may spend each declared row's full per-row budget before any
    failure, so the bound is derived from the same two authoritative sources
    the gate itself reads: the declared matrix rows in ``tests/_tier_config``
    and the per-tier row timeouts in ``scripts.production_gate_model``.  A
    floor is applied so the bound stays conservative if either source cannot
    be read.
    """

    total = DEFAULT_ARM_TIMEOUT_SECONDS_FLOOR
    worst = _declared_matrix_worst_case_seconds(project_root)
    if worst > 0:
        # The floor is a floor, not a cap: a grown matrix must raise the bound
        # rather than quietly fall back to a value too small for it.
        total = max(total, worst * ARM_TIMEOUT_MARGIN)
    return total


def _declared_matrix_worst_case_seconds(project_root: Path) -> float:
    """Return the seconds a serial (workers=1) arm could legitimately spend.

    ``0.0`` means "the declared matrix could not be read", which leaves the
    caller's conservative floor in place.  Each row is charged its full
    per-row budget because the gate may spend that much on one row before it
    finally reports the failure, making this a true upper bound rather than an
    estimate.

    Both sources are loaded by file path under ``project_root`` rather than by
    module name.  An ``import_module("tests._tier_config")`` would return
    whichever copy was already imported -- the benchmark's own checkout, or a
    previously imported one -- regardless of the root it was asked about, so a
    benchmark pointed at a different tree would silently budget against another
    tree's matrix.  Reading the exact files keeps the bound tied to the tree
    whose rows the child gate will actually run.
    """

    root = Path(project_root)
    tier_config = _load_module_from_root(root, Path("tests") / "_tier_config.py")
    gate_model = _load_module_from_root(root, Path("scripts") / "production_gate_model.py")
    if tier_config is None or gate_model is None:
        return 0.0
    manifest = getattr(tier_config, "TIER_REQUIRED_NODEIDS", None)
    timeouts = getattr(gate_model, "MATRIX_CASE_TIMEOUT_SECONDS", None)
    if not isinstance(manifest, dict) or not isinstance(timeouts, dict):
        return 0.0
    worst = 0.0
    for tier in REQUIRED_SELECTION_TIERS:
        nodeids = manifest.get(tier)
        per_row = timeouts.get(tier)
        if not isinstance(nodeids, (set, frozenset, list, tuple)):
            return 0.0
        if isinstance(per_row, bool) or not isinstance(per_row, (int, float)):
            return 0.0
        worst += len(nodeids) * float(per_row)
    return worst


def _load_module_from_root(project_root: Path, relative_path: Path):
    """Load one source file from ``project_root`` without touching ``sys.path``.

    Returns ``None`` for any file that is absent, unreadable, or fails to
    execute.  A caller must treat that as "unknown" and fall back to its
    conservative default; it must never substitute a module imported from
    somewhere else.
    """

    path = project_root / relative_path
    name = f"_benchmark_matrix_{relative_path.stem}"
    previous = sys.modules.get(name)
    try:
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # ``dataclass`` resolves string annotations through
        # ``sys.modules[cls.__module__]``, so the module must be registered
        # while it executes.  The name is private and removed again below, so
        # this never shadows the real ``tests`` or ``scripts`` packages.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    except Exception:  # noqa: BLE001 - an unreadable source is not evidence
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        return None
    return module
