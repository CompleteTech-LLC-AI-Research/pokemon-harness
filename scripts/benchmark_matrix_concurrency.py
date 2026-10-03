"""Reproducible matrix-concurrency benchmark and worker-policy selector.

Issue #106.  This module runs the declared trade/battle matrices at a set of
worker counts in both runtimes, reports throughput, latency and resource cost,
and then refuses to select a policy unless complete terminal results at every
declared worker count support it.

Constraints that are deliberate and load-bearing:

* Selection is a *report*, never a silent default change.  ``select_policy``
  returns ``"unselected"`` unless every candidate has a complete, passing,
  comparable run.  A smaller measured optimum is a valid result, and "no policy
  is justified" is also a valid result.
* Failed, incomplete, interrupted and unstarted rows stay in the denominator.
  Dropping them would let a slower configuration look faster by doing less
  work, which is the failure this benchmark exists to detect.
* One total pair/CPU budget applies across all runtime jobs.  A worker count is
  an upper bound; the capacity policy's admitted ceiling clamps it, and the
  effective value is reported per arm so a clamp is never mistaken for a
  worker-count effect.
* No deadline, capacity threshold, or assertion is relaxed anywhere here.

Every arm records the commit, the interpreter for each runtime, the input
roots, the declared tiers, and wall clock, so results from two machine loads
are never merged into one synthetic clean gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = 1
DEFAULT_WORKER_COUNTS = (1, 2, 4)
RUNTIMES = ("source", "native")
# A worker policy governs the declared trade/battle matrix as a whole.  A run
# that measured only one of them has no evidence about the other, so it may
# report measurements but must not select a policy.
REQUIRED_SELECTION_TIERS = ("battle", "trade")
# The slowest legitimate arm must not be killed by this harness.  At workers=1
# the gate may spend every declared row's full per-row budget before any
# failure, so an arm bound below that would interrupt a valid slow baseline and
# report an unsupported "unselected".  The bound is derived from the two
# sources the gate itself uses -- the declared matrix rows and the per-tier row
# timeouts -- rather than hardcoded, so it tracks the live matrix.
#
# This is a ceiling on our own child process.  It relaxes no per-row deadline
# the gate enforces.
DEFAULT_ARM_TIMEOUT_SECONDS_FLOOR = 108000.0
# Headroom above the derived worst case so process start-up, report writing and
# the final aggregate flush cannot consume the margin and report a legitimate
# arm as interrupted.
ARM_TIMEOUT_MARGIN = 1.25


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


# ``production_gate.py`` spells the compiled runtime ``cython`` (see
# ``production_gate_model.RUNTIME_MODES``).  This benchmark reports it as
# ``native`` because that is the name used throughout the issue and the
# release documentation, so the label is translated at the process boundary
# instead of being passed through as an invalid runtime mode.
GATE_RUNTIME_MODES = {"source": "source", "native": "cython"}
SECONDS_PER_HOUR = 3600.0


def _hash_text(text: str) -> str:
    """Return a stable digest of ``text`` for evidence identity."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_int(value: Any) -> int:
    """Return ``value`` as a non-negative int, treating anything else as zero."""

    return value if type(value) is int and value >= 0 else 0


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Return a nearest-rank percentile, or ``None`` for an empty sample.

    Nearest-rank is deliberate: the reported p95 is always an observed
    measurement, never a synthetic value interpolated between two samples.
    """

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), -(-int(fraction * len(ordered) * 1000) // 1000)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class ExperimentPlan:
    """One (runtime, worker-count) comparison arm."""

    runtime: str
    requested_workers: int
    tiers: tuple[str, ...]
    python_executable: Path | None = None

    def key(self) -> str:
        """Return the stable arm identifier used in reports and comparisons."""

        return f"{self.runtime}/workers-{self.requested_workers}"

    def validate(self) -> list[str]:
        """Return explicit problems for this arm; never guess a value."""

        problems: list[str] = []
        if self.runtime not in RUNTIMES:
            problems.append(f"unknown runtime {self.runtime!r}; expected one of {RUNTIMES}")
        if self.requested_workers <= 0:
            problems.append("workers must be positive")
        if not self.tiers:
            problems.append("at least one tier must be declared")
        return problems


@dataclass
class ArmResult:
    """Terminal outcome and cost of one comparison arm."""

    plan: ExperimentPlan
    completed_passing: int = 0
    failed: int = 0
    incomplete: int = 0
    interrupted: int = 0
    not_started: int = 0
    effective_workers: int | None = None
    # Per-tier counts of the rows the arm actually reported, captured from the
    # gate's own report.  Arms run sequentially, so the manifest can change
    # between two of them; these are what make that difference visible instead
    # of letting a combined total hide a trade/battle shift.
    tier_row_totals: dict[str, int] = field(default_factory=dict)
    tier_effective_workers: dict[str, int] = field(default_factory=dict)
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    per_case_seconds: list[float] = field(default_factory=list)
    deadline_headroom_seconds: float | None = None
    pressure_avg300: float | None = None
    returncode: int | None = None
    gate_passed: bool = False
    report_path: str | None = None
    error: str = ""

    @property
    def required_rows(self) -> int:
        """Return every row this arm was required to produce."""

        return (
            self.completed_passing
            + self.failed
            + self.incomplete
            + self.interrupted
            + self.not_started
        )

    @property
    def complete(self) -> bool:
        """Return whether this arm reached a terminal verdict for every row."""

        return self.incomplete == 0 and self.interrupted == 0 and self.not_started == 0

    @property
    def clean_pass(self) -> bool:
        """Return whether this arm passed with no failed or missing row."""

        return self.complete and self.failed == 0 and self.completed_passing > 0

    @property
    def passing_per_hour(self) -> float:
        """Return throughput over the arm's whole declared work.

        The numerator is every row the arm was required to produce, not only
        the passing ones, so an arm that skipped or failed work to finish
        sooner scores *lower* rather than higher.  Dividing passing rows by
        wall time alone would report an arm that completed 18 of 19 rows in
        ten seconds as roughly ten times faster than one that completed all
        nineteen in a hundred seconds.

        ``clean_passing_per_hour`` remains available for reporting the raw
        passing-row rate, but it is never used to rank arms or select a policy.
        """

        if self.wall_seconds <= 0:
            return 0.0
        if self.required_rows <= 0:
            return 0.0
        return self.required_rows / self.wall_seconds * SECONDS_PER_HOUR

    @property
    def clean_passing_per_hour(self) -> float:
        """Return the passing-row rate, for reporting only."""

        if self.wall_seconds <= 0:
            return 0.0
        return self.completed_passing / self.wall_seconds * SECONDS_PER_HOUR

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable record for this arm."""

        return {
            "arm": self.plan.key(),
            "runtime": self.plan.runtime,
            "requested_workers": self.plan.requested_workers,
            "effective_workers": self.effective_workers,
            "tiers": list(self.plan.tiers),
            "tier_rows": dict(sorted(self.tier_row_totals.items())),
            "tier_effective_workers": dict(sorted(self.tier_effective_workers.items())),
            "rows": {
                "completed_passing": self.completed_passing,
                "failed": self.failed,
                "incomplete": self.incomplete,
                "interrupted": self.interrupted,
                "not_started": self.not_started,
                "required_total": self.required_rows,
            },
            "wall_seconds": round(self.wall_seconds, 6),
            "cpu_seconds": round(self.cpu_seconds, 6),
            "passing_per_hour": round(self.passing_per_hour, 6),
            "per_case_seconds": {
                "count": len(self.per_case_seconds),
                "min": min(self.per_case_seconds) if self.per_case_seconds else None,
                "max": max(self.per_case_seconds) if self.per_case_seconds else None,
                "p95": percentile(self.per_case_seconds, 0.95),
                "median": percentile(self.per_case_seconds, 0.50),
            },
            "deadline_headroom_seconds": self.deadline_headroom_seconds,
            "pressure_avg300": self.pressure_avg300,
            "returncode": self.returncode,
            "report_path": self.report_path,
            "complete": self.complete,
            "clean_pass": self.clean_pass,
            "error": self.error,
        }


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


def build_arm_command(
    plan: ExperimentPlan,
    *,
    project_root: Path,
    evidence_dir: Path,
    capacity_policy: Path | None = None,
    rom_root: Path | None = None,
    fixture_root: Path | None = None,
) -> list[str]:
    """Return the exact ``production_gate.py`` argv for one arm.

    Each arm gets its own fresh evidence and raw-output directories so two arms
    can never collide on a report file or a port, and the interpreter is passed
    explicitly so each runtime is measured with its declared interpreter
    rather than whatever is on ``PATH``.
    """

    command = [
        sys.executable,
        str(project_root / "scripts" / "production_gate.py"),
        "--runtime-mode",
        GATE_RUNTIME_MODES[plan.runtime],
        "--repo-root",
        str(project_root),
        "--evidence-dir",
        str(evidence_dir),
        "--raw-output-dir",
        # The gate rejects a --raw-output-dir nested inside --evidence-dir, so
        # each arm's raw capture is a sibling directory rather than a child.
        # Both still live under the same fresh arm root, so two arms still
        # cannot collide on a report file or a port.
        str(evidence_dir.with_name(f"{evidence_dir.name}-raw")),
        "--matrix-workers",
        str(plan.requested_workers),
        "--keep-going",
        "--format",
        "json",
    ]
    for tier in plan.tiers:
        command.extend(("--tier", tier))
    if rom_root is not None:
        command.extend(("--rom-root", str(rom_root)))
    if fixture_root is not None:
        command.extend(("--fixture-root", str(fixture_root)))
    # The gate only accepts --cython-python together with --runtime-mode both,
    # so a single compiled arm is selected with --runtime-mode cython and its
    # interpreter is supplied through --python, which the gate applies to the
    # one runtime it was asked to execute.
    if plan.python_executable is not None:
        command.extend(("--python", str(plan.python_executable)))
    if capacity_policy is not None:
        command.extend(("--capacity-policy", str(capacity_policy)))
    return command


def _process_cpu_seconds() -> float:
    """Return reaped child CPU seconds, or 0.0 when unavailable."""

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (ImportError, OSError, ValueError):
        return 0.0
    return float(usage.ru_utime + usage.ru_stime)


def run_arm(
    plan: ExperimentPlan,
    *,
    project_root: Path,
    evidence_root: Path,
    timeout_seconds: float,
    capacity_policy: Path | None = None,
    rom_root: Path | None = None,
    fixture_root: Path | None = None,
    environment: dict[str, str] | None = None,
) -> ArmResult:
    """Run one arm and return its terminal, non-dropping result.

    A missing, unreadable, or unparseable report yields ``incomplete`` with a
    non-zero returncode.  It never yields a passing result, and never an empty
    one that would read as instant success.
    """

    result = ArmResult(plan=plan)
    arm_dir = evidence_root / plan.runtime / f"workers-{plan.requested_workers}"
    try:
        arm_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        result.error = f"evidence directory is not fresh: {type(exc).__name__}"
        return result
    # The arm's raw-output directory is deliberately not created here: the gate
    # requires
    # --raw-output-dir to name a directory that does not yet exist, and it
    # refuses a pre-existing one with FileExistsError.  Leaving it absent also
    # keeps the freshness guarantee for a re-run of the same arm key.

    command = build_arm_command(
        plan,
        project_root=project_root,
        evidence_dir=arm_dir,
        capacity_policy=capacity_policy,
        rom_root=rom_root,
        fixture_root=fixture_root,
    )
    env = dict(os.environ if environment is None else environment)
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    stdout_path = arm_dir / "benchmark-stdout.json"

    before = _process_cpu_seconds()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        result.wall_seconds = time.monotonic() - started
        result.cpu_seconds = max(0.0, _process_cpu_seconds() - before)
        result.interrupted = 1
        result.error = f"arm exceeded its {timeout_seconds}s bound"
        result.report_path = str(stdout_path)
        return result
    except OSError as exc:
        result.wall_seconds = time.monotonic() - started
        result.error = f"arm could not start: {type(exc).__name__}: {exc}"
        return result

    result.wall_seconds = time.monotonic() - started
    result.cpu_seconds = max(0.0, _process_cpu_seconds() - before)
    result.returncode = completed.returncode
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    (arm_dir / "benchmark-stderr.txt").write_text(completed.stderr, encoding="utf-8")
    result.report_path = str(stdout_path)

    try:
        report = json.loads(completed.stdout)
    except (ValueError, TypeError):
        result.error = "gate produced no parseable JSON report"
        result.incomplete = 1
        return result
    if not isinstance(report, dict):
        result.error = "gate report was not a JSON object"
        result.incomplete = 1
        return result

    counts = tier_counts(report, plan.tiers)
    result.completed_passing = counts["completed_passing"]
    result.failed = counts["failed"]
    result.incomplete = counts["incomplete"]
    result.not_started = counts["not_started"]
    result.interrupted = counts["interrupted"]
    result.effective_workers = effective_workers(report, plan.requested_workers, plan.tiers)
    # Record what each tier actually ran so a manifest change between sequential
    # arms cannot hide behind an unchanged combined total.
    result.tier_row_totals = {
        tier: rows
        for tier, rows in tier_row_counts(report, plan.tiers).items()
        if isinstance(rows, int) and rows > 0
    }
    per_tier_workers = tier_effective_workers(report, plan.requested_workers, plan.tiers)
    if per_tier_workers is not None:
        result.tier_effective_workers = per_tier_workers
    result.per_case_seconds, result.deadline_headroom_seconds = case_durations(report, plan.tiers)
    result.pressure_avg300 = report_pressure(report)
    # The gate's exit status is authoritative: production_gate.py returns 0 if
    # and only if its overall verdict is PASS.  Counting rows alone would accept
    # an arm whose runtime, collection, evidence, or capacity prerequisite was
    # rejected while some rows still happened to pass.
    result.gate_passed = result.returncode == 0
    if not result.gate_passed and not result.failed and not result.incomplete:
        # Preserve the row counts, but the arm is not a clean pass.
        result.incomplete = max(result.incomplete, 1)
    return result


def comparable(results: Sequence[ArmResult]) -> bool:
    """Return whether these arms may be compared against each other.

    Two arms are comparable only when they ran the same declared work: a shared
    runtime, the same set of declared tiers, and the same number of rows in
    *each* tier.

    The effective worker count deliberately varies across the arms being
    compared -- that difference is the experiment.  What must not vary is the
    *amount and shape of declared work*.  Comparing per tier rather than on a
    combined total matters because arms run sequentially: if the manifest
    changes between them, a shift from 43 trade and 19 battle rows to 42 and 20
    keeps the total at 62 while trading a cheap row for an expensive one.  The
    timing difference would then be attributed to concurrency.

    A capacity-policy clamp is detected separately, by comparing each arm's
    effective worker count against the worker count it requested.  An arm that
    was clamped still belongs in the report with its effective value, and it
    is never silently read as a worker-count effect.
    """

    if len(results) < 2:
        return True
    identity = {
        (
            r.plan.runtime,
            frozenset(r.plan.tiers),
            r.required_rows,
            tuple(sorted(r.tier_row_totals.items())),
        )
        for r in results
    }
    if len(identity) != 1:
        return False
    # An arm that never recorded per-tier rows cannot be shown to have run the
    # same work as its peers, so it is not comparable by assumption.
    return all(r.tier_row_totals for r in results)


def select_policy(
    results: Sequence[ArmResult],
    *,
    worker_counts: Sequence[int] = DEFAULT_WORKER_COUNTS,
) -> dict[str, Any]:
    """Return a worker policy only when complete results support one.

    Refuses to select when a declared worker count is missing an arm, when any
    arm is incomplete, when any arm failed a row, when arms are not comparable,
    or when nothing beat the workers=1 reference.

    The workers=1 arm is required whenever a policy is selected.  The current
    shipped default is 1, so a recommendation is only evidence if it is
    measured against that default; comparing 4 against 2 would show an
    improvement over nothing in particular.
    """

    declared = list(dict.fromkeys(int(w) for w in worker_counts))
    reasons: list[str] = []
    reference_workers = min(declared)
    if reference_workers != 1:
        reasons.append(
            f"the workers=1 reference is required to select a policy; declared "
            f"worker counts start at {reference_workers}"
        )
    # Every arm must cover the whole matrix on its own.  A union across arms is
    # not evidence: an arm that measured only trade has no battle throughput to
    # compare, and a worker policy that governs both tiers cannot be justified
    # by a benchmark in which different worker counts measured different tiers.
    required_tiers = set(REQUIRED_SELECTION_TIERS)
    for result in results:
        missing_tiers = sorted(required_tiers - set(result.plan.tiers))
        if missing_tiers:
            reasons.append(
                f"{result.plan.key()} did not measure the full "
                f"{list(REQUIRED_SELECTION_TIERS)} matrix; not measured: {missing_tiers}"
            )
    for runtime in RUNTIMES:
        for workers in declared:
            if not any(
                r.plan.runtime == runtime and r.plan.requested_workers == workers for r in results
            ):
                reasons.append(f"no result for {runtime} workers={workers}")
    # An arm at a worker count that was never declared is not part of this
    # experiment.  Leaving such arms eligible would let the ranking below pick
    # a count the run never set out to measure, and would compare it against
    # declared counts that were never measured at that concurrency.
    for result in results:
        if result.plan.requested_workers not in declared:
            reasons.append(
                f"{result.plan.key()} ran at workers={result.plan.requested_workers}, "
                f"which is not a declared worker count {declared}"
            )

    for result in results:
        if not result.complete:
            reasons.append(
                f"{result.plan.key()} is incomplete (incomplete={result.incomplete} "
                f"interrupted={result.interrupted} not_started={result.not_started})"
            )
        if result.failed:
            reasons.append(f"{result.plan.key()} failed {result.failed} required row(s)")
        if result.completed_passing <= 0:
            reasons.append(f"{result.plan.key()} produced no passing row")
        declared_rows = sum(result.tier_row_totals.values())
        if declared_rows and result.required_rows < declared_rows:
            # Every declared row must have produced a terminal outcome.  Without
            # this, an arm declaring 62 rows but recording one passing row passes
            # every other check: no row failed, none was skipped, and the
            # positive-passing test is satisfied by that single row.  The gap
            # between declared and recorded work is unaccounted-for work, and an
            # arm that ran less than it declared cannot be compared against one
            # that ran all of it.
            reasons.append(
                f"{result.plan.key()} recorded {result.required_rows} outcome(s) for "
                f"{declared_rows} declared row(s); "
                f"{declared_rows - result.required_rows} row(s) produced no outcome"
            )
        if (
            result.effective_workers is not None
            and result.effective_workers != result.plan.requested_workers
        ):
            # A clamp is reported, never folded into the worker-count result:
            # the arm ran under the policy's ceiling, not the requested count.
            reasons.append(
                f"{result.plan.key()} ran at effective workers="
                f"{result.effective_workers} after the capacity policy clamped the "
                f"requested {result.plan.requested_workers}"
            )
        if result.effective_workers is None:
            # Without an admitted ceiling there is no capacity evidence, so a
            # worker count cannot be justified against a CPU budget.
            reasons.append(
                f"{result.plan.key()} has no admitted capacity ceiling; supply "
                "--capacity-policy so the worker count is measured against a budget"
            )
        if not result.gate_passed:
            reasons.append(
                f"{result.plan.key()} gate verdict is not PASS (returncode={result.returncode})"
            )

    by_runtime: dict[str, list[ArmResult]] = {}
    for result in results:
        by_runtime.setdefault(result.plan.runtime, []).append(result)
    for runtime, arms in by_runtime.items():
        if not comparable(arms):
            reasons.append(
                f"{runtime} arms are not comparable; their declared tiers or "
                f"per-tier row counts differ: "
                f"{ {a.plan.key(): a.tier_row_totals for a in arms} }"
            )

    # The runtimes must also have measured the same work.  They run
    # sequentially, so a manifest change between the source block and the
    # native block would otherwise be invisible: each runtime is internally
    # consistent, yet a single worker policy would then be selected from two
    # different matrices.
    if len(by_runtime) > 1:
        runtime_shapes = {
            runtime: frozenset(
                (tier, rows) for arm in arms for tier, rows in arm.tier_row_totals.items()
            )
            for runtime, arms in by_runtime.items()
        }
        if len(set(runtime_shapes.values())) > 1:
            reasons.append(
                "runtimes did not measure the same matrix; per-tier row counts "
                f"differ between them: "
                f"{ {rt: sorted(shape) for rt, shape in runtime_shapes.items()} }"
            )

    if reasons:
        return {
            "outcome": "unselected",
            "policy": None,
            "reasons": reasons,
            "note": (
                "No worker policy is justified. This is a valid benchmark result, "
                "not a defect, and no default was changed."
            ),
        }

    per_runtime: dict[str, dict[str, Any]] = {}
    for runtime, arms in by_runtime.items():
        reference = next((a for a in arms if a.plan.requested_workers == min(declared)), None)
        ranked = sorted(arms, key=lambda a: (-a.passing_per_hour, a.plan.requested_workers))
        best = ranked[0]
        per_runtime[runtime] = {
            "reference_workers": reference.plan.requested_workers if reference else None,
            "reference_passing_per_hour": reference.passing_per_hour if reference else None,
            "best_workers": best.plan.requested_workers,
            "best_passing_per_hour": best.passing_per_hour,
            "improves_on_reference": bool(
                reference and best.passing_per_hour > reference.passing_per_hour
            ),
        }

    improving = [r for r, v in per_runtime.items() if v["improves_on_reference"]]
    if not improving:
        return {
            "outcome": "unselected",
            "policy": None,
            "reasons": ["no worker count beat the workers=1 reference in any runtime"],
            "note": (
                "Concurrency did not improve measured throughput on this runner. "
                "Retaining the workers=1 default is the measured result."
            ),
            "per_runtime": per_runtime,
        }

    # One total budget governs every runtime, so a single worker count is only
    # selectable when it is the best count in *every* runtime that has a
    # complete result.  Restricting the vote to the improving runtimes would
    # let a runtime whose best count differs pass unnoticed, because a runtime
    # that merely matches its own reference never reaches ``improving``.
    winners = {per_runtime[r]["best_workers"] for r in per_runtime}
    if len(winners) > 1:
        return {
            "outcome": "unselected",
            "policy": None,
            "reasons": [
                (
                    f"runtimes disagree on the best worker count ({sorted(winners)}); "
                    "one total budget cannot select both"
                )
            ],
            "note": "One total pair/CPU budget applies across all runtime jobs.",
            "per_runtime": per_runtime,
        }

    return {
        "outcome": "selected",
        "policy": {
            "matrix_workers": winners.pop(),
            "basis": "measured complete results in every declared arm of every runtime",
            "per_runtime": per_runtime,
        },
        "reasons": [],
        "per_runtime": per_runtime,
    }


def _read_head(project_root: Path) -> str:
    """Return the tested commit identity, or ``unavailable``."""

    try:
        completed = subprocess.run(
            ("git", "-C", str(project_root), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return completed.stdout.strip() or "unavailable"


def build_report(
    *,
    project_root: Path,
    plans: Sequence[ExperimentPlan],
    results: Sequence[ArmResult],
    selection: dict[str, Any],
    rom_root: str,
    fixture_root: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Return the complete benchmark report."""

    head = _read_head(project_root)
    identity = _hash_text(
        json.dumps(
            {
                "head": head,
                "rom_root": rom_root,
                "fixture_root": fixture_root,
                "plans": sorted(p.key() for p in plans),
            },
            sort_keys=True,
        )
    )
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_utc": datetime.now(UTC).isoformat(),
        "identity": {
            "head": head,
            "sha256": identity,
            "rom_root": rom_root,
            "fixture_root": fixture_root,
            "arm_bound_seconds": timeout_seconds,
        },
        "arms": [r.as_dict() for r in results],
        "selection": selection,
        "release_status": "PARTIAL",
        "note": (
            "A benchmark result is evidence, not a release gate. No deadline, "
            "capacity threshold, or assertion is relaxed by this module, and "
            "non-passing rows are retained in every denominator."
        ),
    }


def render_text(report: dict[str, Any]) -> str:
    """Render the benchmark report for a terminal transcript."""

    identity = report.get("identity", {})
    lines = [
        "matrix-concurrency benchmark",
        f"  head: {identity.get('head')}",
        f"  report-sha256: {identity.get('sha256')}",
    ]
    for arm in report.get("arms", []):
        rows = arm.get("rows", {})
        lines.append(
            f"  [{arm.get('arm')}] effective_workers={arm.get('effective_workers')} "
            f"passing={rows.get('completed_passing')} failed={rows.get('failed')} "
            f"not_started={rows.get('not_started')} wall={arm.get('wall_seconds')}s "
            f"passing/hour={arm.get('passing_per_hour')}"
        )
    selection = report.get("selection", {})
    lines.append(f"  selection: {selection.get('outcome')}")
    for reason in selection.get("reasons", []):
        lines.append(f"    reason: {reason}")
    policy = selection.get("policy")
    if isinstance(policy, dict):
        lines.append(f"    matrix_workers: {policy.get('matrix_workers')}")
    return "\n".join(lines)


def _parse_counts(raw: str) -> list[int]:
    """Return positive worker counts from a comma-separated argument."""

    counts: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError as exc:
            raise ValueError(f"worker count {piece!r} is not an integer") from exc
        if value <= 0:
            raise ValueError("worker counts must be positive")
        counts.append(value)
    if not counts:
        raise ValueError("at least one worker count is required")
    return list(dict.fromkeys(counts))


def build_parser() -> argparse.ArgumentParser:
    """Return the declared command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--rom-root", default=None)
    parser.add_argument("--fixture-root", default=None)
    parser.add_argument("--capacity-policy", default=None)
    parser.add_argument("--source-python", default=None)
    parser.add_argument("--native-python", default=None)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--worker-counts", default=",".join(str(w) for w in DEFAULT_WORKER_COUNTS))
    parser.add_argument("--tiers", default="trade,battle")
    parser.add_argument(
        "--arm-timeout-seconds",
        type=float,
        default=None,
        help=(
            "wall-clock ceiling for one arm; defaults to a bound derived from the "
            "declared matrix so a legitimate workers=1 baseline is not interrupted"
        ),
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark and write its report."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        worker_counts = _parse_counts(args.worker_counts)
    except ValueError as exc:
        parser.error(str(exc))
    if args.arm_timeout_seconds is not None and args.arm_timeout_seconds <= 0:
        parser.error("--arm-timeout-seconds must be positive")

    project_root = Path(args.project_root).expanduser().resolve()
    arm_timeout_seconds = (
        args.arm_timeout_seconds
        if args.arm_timeout_seconds is not None
        else default_arm_timeout_seconds(project_root)
    )
    evidence_root = Path(args.evidence_dir).expanduser().resolve()
    tiers = tuple(t.strip() for t in args.tiers.split(",") if t.strip())
    if not tiers:
        parser.error("--tiers must name at least one tier")

    interpreters = {
        "source": Path(args.source_python).expanduser() if args.source_python else None,
        "native": Path(args.native_python).expanduser() if args.native_python else None,
    }
    plans = [
        ExperimentPlan(
            runtime=runtime,
            requested_workers=workers,
            tiers=tiers,
            python_executable=interpreters[runtime],
        )
        for runtime in RUNTIMES
        for workers in worker_counts
    ]
    problems = [problem for plan in plans for problem in plan.validate()]
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    try:
        evidence_root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(
            f"error: --evidence-dir requires a new writable directory: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    capacity_policy = (
        Path(args.capacity_policy).expanduser().resolve() if args.capacity_policy else None
    )
    rom_root = Path(args.rom_root).expanduser().resolve() if args.rom_root else None
    fixture_root = Path(args.fixture_root).expanduser().resolve() if args.fixture_root else None
    environment = dict(os.environ)

    results: list[ArmResult] = []
    for plan in plans:
        result = run_arm(
            plan,
            project_root=project_root,
            evidence_root=evidence_root,
            timeout_seconds=arm_timeout_seconds,
            capacity_policy=capacity_policy,
            rom_root=rom_root,
            fixture_root=fixture_root,
            environment=environment,
        )
        results.append(result)
        summary = (
            f"error={result.error}"
            if result.error
            else (
                f"passing={result.completed_passing} failed={result.failed} "
                f"wall={result.wall_seconds:.3f}s"
            )
        )
        print(f"arm {plan.key()}: {summary}", file=sys.stderr)

    selection = select_policy(results, worker_counts=worker_counts)
    report = build_report(
        project_root=project_root,
        plans=plans,
        results=results,
        selection=selection,
        rom_root=args.rom_root or "unavailable",
        fixture_root=args.fixture_root or "unavailable",
        timeout_seconds=arm_timeout_seconds,
    )
    report_path = evidence_root / "benchmark-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
        print(f"  report: {report_path}")

    # A benchmark that could not select a policy is a legitimate result rather
    # than a failed command, so only an argument or environment fault exits
    # non-zero here.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
