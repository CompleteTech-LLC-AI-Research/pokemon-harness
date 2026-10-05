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
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_temporary_import_context = __name__ == "__main__" or not bool(__package__)
if _temporary_import_context:
    import hashlib as _hashlib
    import importlib.machinery as _machinery
    import sys as _sys
    from pathlib import Path as _Path
    from types import ModuleType as _ModuleType

    _scripts_path = str(_Path(__file__).resolve().parent)
    _namespace_name = "_matrix_" + _hashlib.sha256(_scripts_path.encode()).hexdigest()
    _candidate = _ModuleType(_namespace_name)
    _candidate.__package__ = _namespace_name
    _candidate.__matrix_root__ = _scripts_path
    _candidate.__path__ = [_scripts_path]
    _candidate.__spec__ = _machinery.ModuleSpec(_namespace_name, None, is_package=True)
    _candidate.__spec__.submodule_search_locations = _candidate.__path__
    _namespace = _sys.modules.setdefault(_namespace_name, _candidate)
    _namespace_spec = getattr(_namespace, "__spec__", None)
    if (
        not isinstance(_namespace, _ModuleType)
        or getattr(_namespace, "__name__", None) != _namespace_name
        or getattr(_namespace, "__package__", None) != _namespace_name
        or getattr(_namespace, "__matrix_root__", None) != _scripts_path
        or list(getattr(_namespace, "__path__", ())) != [_scripts_path]
        or getattr(_namespace, "__file__", None) is not None
        or getattr(_namespace, "__loader__", None) is not None
        or _namespace_spec is None
        or getattr(_namespace_spec, "name", None) != _namespace_name
        or getattr(_namespace_spec, "loader", None) is not None
        or getattr(_namespace_spec, "origin", None) is not None
        or getattr(_namespace_spec, "parent", None) != _namespace_name
        or getattr(_namespace_spec, "submodule_search_locations", None) != [_scripts_path]
    ):
        raise ImportError("matrix helper namespace is not owned by this source root")
    _original_package = __package__
    _original_spec = __spec__
    __package__ = _namespace_name
    __spec__ = _namespace_spec

try:
    if _temporary_import_context:
        _bootstrap_key = f"{_namespace_name}._matrix_concurrency_import_bootstrap"
        _cached_bootstrap = _sys.modules.get(_bootstrap_key)
        _expected_bootstrap = _Path(
            _scripts_path, "_matrix_concurrency_import_bootstrap.py"
        ).resolve()
        _bootstrap_spec = getattr(_cached_bootstrap, "__spec__", None)
        if _cached_bootstrap is not None and (
            not isinstance(_cached_bootstrap, _ModuleType)
            or _Path(getattr(_cached_bootstrap, "__file__", "")).resolve() != _expected_bootstrap
            or _bootstrap_spec is None
            or _bootstrap_spec.name != _bootstrap_key
            or _Path(_bootstrap_spec.origin or "").resolve() != _expected_bootstrap
        ):
            raise ImportError("matrix import bootstrap is not owned by this source root")
        from ._matrix_concurrency_import_bootstrap import _validate_modules

        _validate_modules(_namespace_name, _scripts_path, require_all=False)
    from ._matrix_concurrency_accounting import (
        _accumulate_case,  # noqa: F401
        case_durations,
        declared_tier_rows,
        effective_workers,
        report_pressure,
        tier_counts,
        tier_effective_workers,
        tier_row_counts,
        tier_row_identity,
    )
    from ._matrix_concurrency_cli import _parse_counts
    from ._matrix_concurrency_constants import (
        ARM_TIMEOUT_MARGIN,  # noqa: F401
        DEFAULT_ARM_TIMEOUT_SECONDS_FLOOR,  # noqa: F401
        DEFAULT_WORKER_COUNTS,
        GATE_RUNTIME_MODES,
        REPORT_SCHEMA_VERSION,
        REQUIRED_SELECTION_TIERS,
        RUNTIMES,
        SECONDS_PER_HOUR,
    )
    from ._matrix_concurrency_model_values import (
        _as_int,  # noqa: F401
        _hash_text,
        percentile,
    )
    from ._matrix_concurrency_process import _process_cpu_seconds
    from ._matrix_concurrency_reporting import _read_head, render_text
    from ._matrix_concurrency_source_paths import (
        _declared_matrix_worst_case_seconds,  # noqa: F401
        _load_module_from_root,  # noqa: F401
        default_arm_timeout_seconds,
    )

    if _temporary_import_context:
        _validate_modules(_namespace_name, _scripts_path, require_all=True)
finally:
    if _temporary_import_context:
        __package__ = _original_package
        __spec__ = _original_spec


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
    tier_rows: dict[str, frozenset[str]] = field(default_factory=dict)
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
    def required_rows_per_hour(self) -> float:
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
            # Named for what it divides: required rows by wall time.  Calling
            # this "passing_per_hour" while it includes failed, skipped and
            # unstarted rows would misreport a run that did less work as one
            # that completed more.
            "required_rows_per_hour": round(self.required_rows_per_hour, 6),
            "clean_passing_per_hour": round(self.clean_passing_per_hour, 6),
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
        # The child was killed before it wrote a report, so no row produced a
        # terminal outcome.  Charging one interrupted row would report a 62-row
        # arm as having required one row and would let its denominator shrink
        # to nothing.  The whole declared matrix is charged instead.
        declared = declared_tier_rows(project_root, plan.tiers)
        result.tier_row_totals = declared
        result.not_started = max(0, sum(declared.values()) - 1)
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
    result.tier_rows = {
        tier: nodeids for tier, nodeids in tier_row_identity(report, plan.tiers).items() if nodeids
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
    # No synthetic "incomplete" row is added for a non-PASS verdict.  Such a
    # row is not a row the matrix declared, and adding one inflated the arm's
    # required-row total above the declaration -- which then made the arm look
    # like it had run more work than it declared.  ``gate_passed`` already
    # blocks selection on its own, so the real row counts are preserved as
    # reported.
    return result


def comparable(results: Sequence[ArmResult]) -> bool:
    """Return whether these arms may be compared against each other.

    Two arms are comparable only when they ran the same declared work: a shared
    runtime, the same set of declared tiers, the same number of rows in *each*
    tier, and the same row ids in each tier.

    The effective worker count deliberately varies across the arms being
    compared -- that difference is the experiment.  What must not vary is the
    *amount and shape of declared work*.  Comparing per tier rather than on a
    combined total matters because arms run sequentially: if the manifest
    changes between them, a shift from 43 trade and 19 battle rows to 42 and 20
    keeps the total at 62 while trading a cheap row for an expensive one.  The
    timing difference would then be attributed to concurrency.  Counts alone
    are still not enough: swapping one node id for another leaves every count
    identical while a different set of tests is timed, so the recorded row ids
    are compared directly.

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
            tuple(sorted((tier, frozenset(rows)) for tier, rows in r.tier_rows.items())),
        )
        for r in results
    }
    if len(identity) != 1:
        return False
    # Row ids are only meaningful when every declared tier actually reported
    # them; an arm that recorded none cannot be shown to have run the same work.
    if any(not all(r.tier_rows.get(tier) for tier in r.plan.tiers) for r in results):
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

    # One arm per (runtime, worker count).  Two runs at the same count are not a
    # worker-count effect: ranking them against each other and against the
    # workers=1 reference would report run-to-run variance as an improvement
    # caused by concurrency.
    seen_arms: set[tuple[str, int]] = set()
    for result in results:
        arm_key = (result.plan.runtime, result.plan.requested_workers)
        if arm_key in seen_arms:
            reasons.append(
                f"{result.plan.key()} is a duplicate arm; exactly one result per "
                "runtime and worker count is required"
            )
        seen_arms.add(arm_key)

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
        # The declared totals and the recorded ids must describe the same rows.
        # Checking only the sum against the outcomes lets an arm claim 43+19
        # rows while supplying a single id per tier; the identity check in
        # ``comparable`` cannot see it because every arm would be equally wrong.
        inconsistent_tiers = [
            tier
            for tier, total in result.tier_row_totals.items()
            if total and len(result.tier_rows.get(tier, ())) != total
        ]
        if inconsistent_tiers:
            recorded = {tier: len(result.tier_rows.get(tier, ())) for tier in inconsistent_tiers}
            reasons.append(
                f"{result.plan.key()} declares {result.tier_row_totals} but recorded "
                f"{recorded} row ids; inconsistent tiers: {inconsistent_tiers}"
            )
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
        if declared_rows and result.required_rows > declared_rows:
            # The mirror case: an arm claiming more outcomes than it declared
            # rows is internally inconsistent.  The gate's own PASS path guards
            # this, but a caller can construct one directly, and an inflated
            # denominator would inflate the throughput used to rank arms.
            reasons.append(
                f"{result.plan.key()} recorded {result.required_rows} outcome(s) for "
                f"{declared_rows} declared row(s); "
                f"{result.required_rows - declared_rows} outcome(s) exceed the declaration"
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
        clamped_tiers = {
            tier: workers
            for tier, workers in result.tier_effective_workers.items()
            if workers != result.plan.requested_workers
        }
        if clamped_tiers and result.effective_workers == result.plan.requested_workers:
            # The arm-level count can agree with the request while a tier's own
            # recorded concurrency does not.  Trusting only the arm-level value
            # would accept an arm whose own per-tier evidence says it ran below
            # the count it is being compared on.
            reasons.append(
                f"{result.plan.key()} reports effective workers="
                f"{result.effective_workers} but its own per-tier evidence "
                f"recorded {clamped_tiers} for the requested "
                f"{result.plan.requested_workers}"
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
                (tier, rows)
                for arm in arms
                for tier, rows in (
                    (name, frozenset(nodeids)) for name, nodeids in arm.tier_rows.items()
                )
            )
            for runtime, arms in by_runtime.items()
        }
        if len(set(runtime_shapes.values())) > 1:
            reasons.append(
                "runtimes did not measure the same matrix; their declared rows "
                f"differ: "
                f"{ {rt: sorted((t, len(rows)) for t, rows in shape) for rt, shape in runtime_shapes.items()} }"
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
        ranked = sorted(arms, key=lambda a: (-a.required_rows_per_hour, a.plan.requested_workers))
        best = ranked[0]
        per_runtime[runtime] = {
            "reference_workers": reference.plan.requested_workers if reference else None,
            "reference_required_rows_per_hour": (
                reference.required_rows_per_hour if reference else None
            ),
            "best_workers": best.plan.requested_workers,
            "best_required_rows_per_hour": best.required_rows_per_hour,
            "improves_on_reference": bool(
                reference and best.required_rows_per_hour > reference.required_rows_per_hour
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
