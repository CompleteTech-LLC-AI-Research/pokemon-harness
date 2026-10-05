from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

if __package__:
    from ._matrix_concurrency_model_values import (
        _as_int,
    )
    from ._matrix_concurrency_source_paths import (
        _load_module_from_root,
    )
else:
    from _matrix_concurrency_model_values import (
        _as_int,
    )
    from _matrix_concurrency_source_paths import (
        _load_module_from_root,
    )


def tier_counts(report: dict[str, Any], tiers: Iterable[str]) -> dict[str, int]:
    """Sum declared tier counts, preserving every non-passing row.

    Anything the gate could not classify counts as incomplete rather than being
    dropped, so an unparseable report degrades to "not comparable" instead of
    "fast".

    Matrix tiers carry their rows in ``case_results``.  Those rows are read
    directly because a ``NOT_STARTED`` or ``INTERRUPTED`` row contributes
    nothing to the aggregate ``counts``; reading only the aggregates would let
    an unrun row disappear from the denominator entirely.
    """

    wanted = set(tiers)
    totals = {
        "completed_passing": 0,
        "failed": 0,
        "incomplete": 0,
        "interrupted": 0,
        "not_started": 0,
    }
    payload = report.get("tiers")
    if not isinstance(payload, list):
        totals["incomplete"] += 1
        return totals
    seen: set[str] = set()
    for tier in payload:
        if not isinstance(tier, dict):
            totals["incomplete"] += 1
            continue
        name = tier.get("name")
        if name not in wanted:
            continue
        seen.add(name)
        counts = tier.get("counts")
        if not isinstance(counts, dict):
            totals["incomplete"] += 1
            continue
        case_results = tier.get("case_results")
        if isinstance(case_results, list) and case_results:
            for case in case_results:
                _accumulate_case(totals, case)
        elif tier.get("status") == "BLOCKED":
            # A tier the gate blocked before dispatch declares its rows in
            # ``selected_nodeids`` and produces no ``case_results`` at all.
            # Reading only the aggregates returns zero for it, which erases
            # every row it was required to run and makes the arm's denominator
            # describe work the gate never even attempted.  Each of those rows
            # had no admission decision and no lifecycle state, so it is
            # unattempted work, not a row that produced an outcome.
            declared = tier.get("selected_nodeids")
            if isinstance(declared, list) and declared:
                totals["not_started"] += len(declared)
            else:
                totals["incomplete"] += 1
        else:
            totals["completed_passing"] += _as_int(counts.get("passed"))
            totals["failed"] += _as_int(counts.get("failed")) + _as_int(counts.get("errors"))
            totals["not_started"] += _as_int(counts.get("skipped")) + _as_int(counts.get("xfailed"))
        if tier.get("status") not in ("PASS", "FAIL", "BLOCKED"):
            totals["incomplete"] += 1
    for missing in sorted(wanted - seen):
        # A declared tier the report never mentions is not a pass.
        totals["incomplete"] += 1
    return totals


def _accumulate_case(totals: dict[str, int], case: Any) -> None:
    """Fold one matrix row into the arm totals without dropping it."""

    if not isinstance(case, dict):
        totals["incomplete"] += 1
        return
    status = case.get("status")
    if status == "NOT_STARTED":
        totals["not_started"] += 1
        return
    if status == "INTERRUPTED":
        totals["interrupted"] += 1
        return
    if status == "TIMEOUT":
        # A row the gate killed at its per-row deadline is attempted work that
        # produced no passing outcome.  Reading only the pytest counts below
        # drops it from the denominator entirely: the gate records the
        # timeout with zero counts, so the arm would report one fewer required
        # row and could even read as complete and clean while the gate failed.
        totals["failed"] += 1
        return
    if status not in ("PASS", "FAIL"):
        # Any status this reader does not model is unclassified work, not a
        # passing row.
        totals["incomplete"] += 1
        return
    counts = case.get("counts")
    if not isinstance(counts, dict):
        totals["incomplete"] += 1
        return
    totals["completed_passing"] += _as_int(counts.get("passed"))
    totals["failed"] += _as_int(counts.get("failed")) + _as_int(counts.get("errors"))
    totals["not_started"] += _as_int(counts.get("skipped")) + _as_int(counts.get("xfailed"))


def tier_row_counts(report: dict[str, Any], tiers: Iterable[str]) -> dict[str, int | None]:
    """Return how many declared rows each requested tier actually ran.

    The gate schedules at most one worker per row of a tier
    (``max_workers = min(matrix_workers, len(nodeids))``), so a tier's row
    count is the ceiling on that tier's concurrency.  The count comes from the
    report itself -- ``case_results`` for a tier that ran, otherwise the
    declared ``selected_nodeids`` -- so it tracks the real matrix instead of a
    hardcoded row total.  ``None`` means the tier's row count could not be
    determined, which is not evidence of any worker count.
    """

    wanted = set(tiers)
    counts: dict[str, int | None] = {tier: None for tier in wanted}
    payload = report.get("tiers")
    if not isinstance(payload, list):
        return counts
    for tier in payload:
        if not isinstance(tier, dict) or tier.get("name") not in wanted:
            continue
        name = tier["name"]
        case_results = tier.get("case_results")
        if isinstance(case_results, list) and case_results:
            counts[name] = len(case_results)
            continue
        selected = tier.get("selected_nodeids")
        if isinstance(selected, list) and selected:
            counts[name] = len(selected)
    return counts


def effective_workers(report: dict[str, Any], requested: int, tiers: Iterable[str]) -> int | None:
    """Return the concurrency that governs this arm's whole matrix.

    The gate caps each tier independently at ``min(matrix_workers, rows,
    capacity ceiling)``, so a single arm genuinely uses different concurrency
    per tier when its tiers declare different row counts.  This returns the
    smallest of those per-tier values: the concurrency that governs the arm as
    a whole.

    That is deliberately the *smallest*.  A worker policy governs the matrix as
    one unit, so a count is only selectable when it is honoured everywhere.  A
    request of 44 against a 43-row trade tier and a 19-row battle tier is not a
    44-worker arm -- the gate cannot run 44 battle rows at once -- and reporting
    44 would claim a concurrency the matrix never reached.  The per-tier values
    are recorded separately in ``tier_effective_workers`` so the report does
    not present this governing value as if it were uniform.

    ``None`` returns when the capacity policy admitted no ceiling or when a
    tier's row count is unreadable.  An unknown effective count is never
    reported as a match for the requested count, because that would let an
    unsupported worker count look measured.
    """

    capacity = report.get("capacity")
    admission = capacity.get("admission") if isinstance(capacity, dict) else None
    if not isinstance(admission, dict):
        return None
    admitted = admission.get("max_concurrent_pairs")
    if type(admitted) is not int or admitted <= 0:
        return None
    ceiling = min(requested, admitted)
    row_counts = tier_row_counts(report, tiers)
    for tier in tiers:
        rows = row_counts.get(tier)
        if not isinstance(rows, int) or rows <= 0:
            return None
        ceiling = min(ceiling, rows)
    return max(1, ceiling)


def declared_tier_rows(project_root: Path, tiers: Iterable[str]) -> dict[str, int]:
    """Return the declared row count of each tier in ``project_root``.

    Read from the same manifest the gate selects its rows from, so a run that
    died before writing a report is still charged the matrix it was asked to
    run.  A tier that is not declared yields no entry rather than a guess.
    """

    wanted = tuple(tiers)
    tier_config = _load_module_from_root(project_root, Path("tests") / "_tier_config.py")
    if tier_config is None:
        return {}
    manifest = getattr(tier_config, "TIER_REQUIRED_NODEIDS", None)
    if not isinstance(manifest, dict):
        return {}
    rows: dict[str, int] = {}
    for tier in wanted:
        nodeids = manifest.get(tier)
        if isinstance(nodeids, (set, frozenset, list, tuple)) and nodeids:
            rows[tier] = len(nodeids)
    return rows


def tier_row_identity(report: dict[str, Any], tiers: Iterable[str]) -> dict[str, frozenset[str]]:
    """Return the exact declared row ids each requested tier carried.

    Counts alone cannot prove two arms ran the same work: if the manifest swaps
    one 43-row trade node id for another between two sequential arms, every
    count still reads 43/19 while a different set of tests was timed.  The gate
    reports the exact ids in ``selected_nodeids``, so that is what identity is
    built from.  A tier whose ids are unreadable maps to an empty set, which is
    not equal to any populated set and therefore blocks comparability.
    """

    wanted = set(tiers)
    identity: dict[str, frozenset[str]] = {tier: frozenset() for tier in wanted}
    payload = report.get("tiers")
    if not isinstance(payload, list):
        return identity
    for tier in payload:
        if not isinstance(tier, dict) or tier.get("name") not in wanted:
            continue
        selected = tier.get("selected_nodeids")
        if isinstance(selected, list):
            identity[tier["name"]] = frozenset(str(nodeid) for nodeid in selected)
    return identity


def tier_effective_workers(
    report: dict[str, Any], requested: int, tiers: Iterable[str]
) -> dict[str, int] | None:
    """Return the per-tier concurrency the gate actually used for this arm.

    Mirrors ``production_gate_matrix._run_matrix_tier``, which caps each tier
    at ``min(matrix_workers, len(nodeids))`` and then at the admitted pair
    ceiling.  Returning ``None`` means at least one declared tier's row count
    could not be read, so no per-tier value is asserted.
    """

    capacity = report.get("capacity")
    admission = capacity.get("admission") if isinstance(capacity, dict) else None
    if not isinstance(admission, dict):
        return None
    admitted = admission.get("max_concurrent_pairs")
    if type(admitted) is not int or admitted <= 0:
        return None
    row_counts = tier_row_counts(report, tiers)
    result: dict[str, int] = {}
    for tier in tiers:
        rows = row_counts.get(tier)
        if not isinstance(rows, int) or rows <= 0:
            return None
        result[tier] = max(1, min(requested, admitted, rows))
    return result


def case_durations(
    report: dict[str, Any], tiers: Iterable[str]
) -> tuple[list[float], float | None]:
    """Return observed per-row durations and the worst per-row deadline margin.

    Only matrix ``case_results`` rows are per-case samples.  A tier's aggregate
    ``duration_seconds`` is the sum of a whole tier's rows and is never a
    per-case latency, so it is not used here.

    Headroom is computed per row as ``deadline_seconds - duration_seconds`` and
    the minimum is reported.  Comparing one row's duration against a different
    row's deadline would report a false overrun: a 1000 s battle row under a
    1200 s deadline and a 100 s trade row under a 900 s deadline both pass,
    while min-deadline minus max-duration reports -100 s.
    """

    wanted = set(tiers)
    durations: list[float] = []
    margins: list[float] = []
    payload = report.get("tiers")
    if not isinstance(payload, list):
        return durations, None
    for tier in payload:
        if not isinstance(tier, dict) or tier.get("name") not in wanted:
            continue
        case_results = tier.get("case_results")
        if not isinstance(case_results, list):
            continue
        for case in case_results:
            if not isinstance(case, dict):
                continue
            value = case.get("duration_seconds")
            if isinstance(value, (int, float)) and value > 0:
                durations.append(float(value))
            deadline = case.get("deadline_seconds")
            if isinstance(deadline, (int, float)) and deadline > 0:
                margins.append(
                    float(deadline) - float(value if isinstance(value, (int, float)) else 0.0)
                )
    return durations, (min(margins) if margins else None)


def report_pressure(report: dict[str, Any]) -> float | None:
    """Return the recorded CPU pressure figure, if the gate captured one."""

    capacity = report.get("capacity")
    samples = capacity.get("samples") if isinstance(capacity, dict) else None
    if not isinstance(samples, list) or not samples:
        return None
    values: list[float] = []
    for entry in samples:
        if isinstance(entry, dict):
            for key in ("psi_cpu_some_avg300", "cgroup_cpu_some_avg300"):
                value = entry.get(key)
                if isinstance(value, (int, float)):
                    values.append(float(value))
    return max(values) if values else None
