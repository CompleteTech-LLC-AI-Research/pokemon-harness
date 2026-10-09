"""Opt-in concurrent runtime orchestration for the production gate (#107).

``--parallel-runtimes`` runs the source and Cython gates as separate owned child
processes under one immutable plan and ONE live capacity authority.  The sequential
gate remains the default; nothing in this module changes it.  Jobs run from a private
immutable snapshot, in phases (both additional smokes finish before any selected-tier
job starts), each in its own session with an owned process tree that is tracked while
it runs and proven gone before budget is released.  A PASS is derived only from an
observed child exit status plus a terminal artifact whose independently measured
identity, structured rows and timestamps match the frozen plan.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_artifacts import (
    TERMINAL_ARTIFACT_NAME,
    aggregate_outcomes,
    outcome_record,
    read_terminal_artifact,
    validate_terminal_artifact,
)
from scripts.production_gate_parallel_capacity import (
    CapacityError,
    LiveCapacity,
    PairBudget,
    affinity_pairs,
    capacity_loss_probe,
    require_parallel_capacity,
)
from scripts.production_gate_parallel_identity import (
    expected_identity,
    identity_differences,
    measure_commit,
    measure_identity,
)
from scripts.production_gate_parallel_isolation import (
    IsolationError,
    assign_port_ranges,
    build_snapshot,
    diff_snapshots,
    distinct_tree_problems,
    isolation_environment,
    port_range_problems,
    snapshot_tree,
    thaw_and_remove,
)
from scripts.production_gate_parallel_phases import (
    barrier_decision,
    jobs_in_phase,
    may_cancel_siblings,
    phase_names,
)
from scripts.production_gate_parallel_plan import ParallelPlan, PlanError, load_plan
from scripts.production_gate_parallel_processes import JOB_TOKEN_ENV, OwnedTree

__all__ = [
    "CapacityError",
    "PairBudget",
    "affinity_pairs",
    "capacity_loss_probe",
    "require_parallel_capacity",
]

TREE_DIRECTORIES = ("out", "tmp", "cache", "build", "evidence", "raw", "ports")
BASE_ENVIRONMENT_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "VIRTUAL_ENV")
THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_POLL_SECONDS = 0.05
CLEANUP_SECONDS = 60.0
LIMITATIONS = (
    "authored controls do not close the real-ROM dual-runtime comparison; release stays PARTIAL",
    (
        "the sequential comparison is NOT_RUN unless a serial bundle produced by this "
        "orchestrator for the same immutable plan is supplied and re-validated"
    ),
    (
        "ports are reserved only for helpers that use the job port allocator; helpers outside "
        "the authorized paths still release a port before the listener binds"
    ),
    "none of this source has been executed: new and updated controls await a Sol-admitted batch",
)


COVERAGE_DECLARATION = {
    "implemented": [
        "opt-in --parallel-runtimes with sequential execution as the unchanged default",
        (
            "immutable frozen plan: run/commit/per-runtime build/input/asset identities, tiers, "
            "case ids, timing repetitions, derived deadlines, fail_fast and one capacity budget"
        ),
        "private immutable snapshot of the checkout and ROM/fixtures with a real write-denial probe",
        "phases: both additional smokes complete before any selected-tier job; both retained",
        (
            "structured row records, independently measured identities and per-launch tokens "
            "validated against the plan; validators are total over decoded JSON"
        ),
        (
            "live capacity admission (CapacitySession + one budget) before dispatch and on every "
            "poll; owned process-tree tracking with proof before budget release"
        ),
        "completed failures are harvested before any cancellation; diagnostics are preserved",
        "production comparison of two complete re-validated bundles (concurrent vs serial)",
    ],
    "exclusions": [
        "full real-ROM dual-runtime run over the six tiers (#107.5) is NOT done",
        "elapsed-time comparison against sequential execution on equivalent conditions is NOT done",
        "operator allocation (#85/#86), #106 policy and the 18 orientation fixtures are unmet",
        "nothing in this candidate has been executed; authored controls are not gameplay evidence",
        "release status remains PARTIAL",
    ],
}


def _wall_now() -> datetime:
    return datetime.now(UTC)


def prepare_job_tree(run_root: Path, run_id: str, job_id: str) -> dict[str, Path]:
    """Create the owned private trees for one job; refuse symlinks and existing trees."""

    if run_root.is_symlink() or not run_root.is_dir():
        raise OSError("run root must be an existing real directory")
    base = run_root / run_id / job_id
    if not base.resolve().is_relative_to(run_root.resolve()):
        raise OSError("job tree escapes the run root")
    base.mkdir(mode=0o700, parents=True, exist_ok=False)
    tree = {"root": base}
    for name in TREE_DIRECTORIES:
        path = base / name
        path.mkdir(mode=0o700, exist_ok=False)
        tree[name] = path
    return tree


def job_environment(
    tree: Mapping[str, Path],
    base_env: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
    token: str = "",
    port_range: tuple[int, int] | None = None,
) -> dict[str, str]:
    """Build an isolated environment: private writers, one thread per library, job token."""

    source = os.environ if base_env is None else base_env
    env = {key: source[key] for key in BASE_ENVIRONMENT_KEYS if key in source}
    env.update(isolation_environment(tree, port_range))
    env.update({name: "1" for name in THREAD_VARIABLES})
    if project_root is not None:
        env["PYTHONPATH"] = str(project_root)
    if token:
        env[JOB_TOKEN_ENV] = token
    return env


def terminate_owned_group(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Stop a process group this module started: TERM, bounded wait, then KILL."""

    group = process.pid  # start_new_session=True makes the child its own group leader
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    limit = clock() + grace_seconds
    while process.poll() is None and clock() < limit:
        sleeper(0.02)
    if process.poll() is None:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(grace_seconds, 1.0))
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(group, signal.SIGKILL)  # stragglers that outlived the leader
    except ProcessLookupError:
        pass


def _manifest(directory: Path) -> dict[str, tuple[int, int]]:
    entries: dict[str, tuple[int, int]] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            entries[str(path.relative_to(directory))] = (info.st_size, info.st_mtime_ns)
    return entries


@dataclass
class _Running:
    job_id: str
    phase: str
    process: subprocess.Popen[Any]
    started: float
    deadline: float
    tree: dict[str, Path]
    token: str
    owned: OwnedTree
    launched_wall: datetime
    handles: list[Any] = field(default_factory=list)


WorkerArgvFactory = Callable[[ParallelPlan, str, Mapping[str, Path], Path], Sequence[str]]
Measurer = Callable[[str], tuple[dict[str, Any], list[str]]]


def default_worker_argv(
    gate_arguments: Sequence[str],
    interpreters: Mapping[str, str] | None = None,
    *,
    git_root: Path | None = None,
    rom_root: Path | None = None,
    fixture_root: Path | None = None,
) -> WorkerArgvFactory:
    """Return the factory that launches the real per-runtime worker module."""

    def factory(
        plan: ParallelPlan, job_id: str, tree: Mapping[str, Path], project_root: Path
    ) -> list[str]:
        mode = plan.job(job_id)["mode"]
        interpreter = (interpreters or {}).get(mode, sys.executable)
        argv = [
            interpreter,
            "-m",
            "scripts.production_gate_parallel_worker",
            f"--plan={tree['root'].parent / 'plan.json'}",
            f"--job-id={job_id}",
            f"--job-root={tree['root']}",
            f"--project-root={project_root}",
            f"--python={interpreter}",
        ]
        for flag, value in (
            ("--git-root", git_root),
            ("--rom-root", rom_root),
            ("--fixture-root", fixture_root),
        ):
            if value is not None:
                argv.append(f"{flag}={value}")
        argv.extend(f"--gate-arg={item}" for item in gate_arguments)
        return argv

    return factory


def _write_plan_file(run_root: Path, plan: ParallelPlan) -> Path:
    directory = run_root / plan.run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "plan.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(plan.text)
    if load_plan(path.read_text(encoding="utf-8")).digest != plan.digest:
        raise PlanError("plan file read-back differs from the frozen plan")
    return path


def _finish_job(
    plan: ParallelPlan,
    running: _Running,
    exit_status: int,
    clock: Callable[[], float],
    wall_clock: Callable[[], datetime] = _wall_now,
    measure: Measurer | None = None,
) -> dict[str, Any]:
    artifact, read_problems = read_terminal_artifact(running.tree["root"])
    problems = list(read_problems)
    if artifact is not None:
        problems.extend(
            validate_terminal_artifact(
                artifact,
                plan,
                running.job_id,
                child_exit_status=exit_status,
                child_pid=running.process.pid,
                expected_token=running.token,
                window=(running.launched_wall, wall_clock()),
            )
        )
    if measure is not None:
        measured, measure_problems = measure(running.job_id)
        mode = plan.job(running.job_id)["mode"]
        problems.extend(measure_problems)
        problems.extend(identity_differences(measured, expected_identity(plan.as_dict(), mode)))
    elapsed = clock() - running.started
    record = outcome_record(
        plan,
        running.job_id,
        "failed" if problems else "passed",
        child_exit_status=exit_status,
        artifact=artifact,
        artifact_problems=problems,
        reason="; ".join(problems[:1]),
        elapsed_seconds=round(elapsed, 3),
    )
    record["launch_token"] = running.token
    return record


def _close_handles(running: _Running) -> None:
    for handle in running.handles:
        try:
            handle.close()
        except OSError:
            pass


def _write_progress(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def run_parallel_plan(
    plan: ParallelPlan,
    *,
    run_root: Path,
    project_root: Path,
    budget: PairBudget,
    worker_argv_factory: WorkerArgvFactory,
    base_env: Mapping[str, str] | None = None,
    capacity: LiveCapacity | None = None,
    capacity_probe: Callable[[], object] | None = None,
    interrupt: threading.Event | None = None,
    serial: bool = False,
    measure: Measurer | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], datetime] = _wall_now,
) -> dict[str, Any]:
    """Dispatch every planned job (phase by phase) and return the retained aggregate report."""

    body = plan.as_dict()
    live = capacity or LiveCapacity(budget, probe=capacity_probe)
    started = clock()
    outcomes: dict[str, dict[str, Any]] = {}
    running: list[_Running] = []
    cleanup: dict[str, dict[str, Any]] = {}
    out_manifests: dict[str, dict[str, tuple[int, int]]] = {}
    trees: dict[str, dict[str, Path]] = {}
    ports_seen: dict[str, set[int]] = {}
    problems: list[str] = []
    phase_of = {job["job_id"]: job["phase"] for job in body["jobs"]}
    ranges = assign_port_ranges(list(phase_of))
    problems.extend(port_range_problems(ranges))
    cancel_reason = ""
    progress_path = run_root / body["run_id"] / "progress.json"
    excluded = (
        (".git", run_root.name)
        if run_root.resolve().is_relative_to(project_root.resolve())
        else (".git",)
    )
    shared_before = snapshot_tree(project_root, excluded) if project_root.is_dir() else {}
    load_before = os.getloadavg() if hasattr(os, "getloadavg") else ()

    def progress() -> None:
        states = {
            job_id: outcomes[job_id]["state"]
            if job_id in outcomes
            else ("running" if any(i.job_id == job_id for i in running) else "pending")
            for job_id in phase_of
        }
        _write_progress(
            progress_path,
            {
                "updated_utc": wall_clock().isoformat(),
                "cancellation": cancel_reason,
                "jobs": states,
            },
        )

    def settle(job_id: str, state: str, reason: str, status: int | None = None) -> None:
        outcomes[job_id] = outcome_record(
            plan,
            job_id,
            state,
            child_exit_status=status,
            artifact=None,
            artifact_problems=[reason],
            reason=reason,
        )
        progress()

    def finish(item: _Running, status: int, *, allow_cancel: bool) -> None:
        _close_handles(item)
        receipt = item.owned.terminate(grace_seconds=grace_seconds, clock=clock, sleeper=sleeper)
        cleanup[item.job_id] = {"process_tree": receipt}
        ports_seen[item.job_id] = set(item.owned.ports)
        outcome = _finish_job(plan, item, status, clock, wall_clock, measure)
        if receipt["terminated"] or not receipt["proof"]:
            outcome["state"] = "failed"
            outcome["artifact_problems"].append(
                "owned descendants outlived the worker or could not be proven gone"
            )
        outcomes[item.job_id] = outcome
        out_manifests[item.job_id] = _manifest(item.tree["out"])
        running.remove(item)
        progress()
        if (
            allow_cancel
            and outcome["state"] != "passed"
            and running
            and may_cancel_siblings(item.phase, body["fail_fast"])
        ):
            cancel(f"fail_fast: {item.job_id} {outcome['state']}")

    def harvest(*, allow_cancel: bool = True) -> None:
        for item in running[:]:
            status = item.process.poll()
            if status is not None and item in running:
                finish(item, status, allow_cancel=allow_cancel)

    def cancel(reason: str) -> None:
        nonlocal cancel_reason
        cancel_reason = cancel_reason or reason
        harvest(allow_cancel=False)  # completed failures are kept before any signal is sent
        for item in list(running):
            receipt = item.owned.terminate(
                grace_seconds=grace_seconds, clock=clock, sleeper=sleeper
            )
            cleanup[item.job_id] = {"process_tree": receipt}
            ports_seen[item.job_id] = set(item.owned.ports)
            status = item.process.poll()
            _close_handles(item)
            settle(item.job_id, "interrupted", f"cancelled: {reason}", status)
            running.remove(item)

    def start(job: Mapping[str, Any]) -> None:
        job_id = job["job_id"]
        handles: list[Any] = []
        try:
            tree = prepare_job_tree(run_root, body["run_id"], job_id)
            trees[job_id] = tree
            token = f"{body['run_id']}:{job_id}:{uuid.uuid4().hex}"
            argv = list(worker_argv_factory(plan, job_id, tree, project_root))
            handles.append(open(tree["root"] / "child.stdout", "wb"))  # noqa: SIM115
            handles.append(open(tree["root"] / "child.stderr", "wb"))  # noqa: SIM115
            launched = wall_clock()
            process = subprocess.Popen(
                argv,
                cwd=project_root,
                env=job_environment(
                    tree,
                    base_env,
                    project_root=project_root,
                    token=token,
                    port_range=ranges[job_id],
                ),
                stdin=subprocess.DEVNULL,
                stdout=handles[0],
                stderr=handles[1],
                start_new_session=True,
            )
        except OSError as exc:
            for handle in handles:
                handle.close()
            settle(job_id, "failed", f"could not start: {type(exc).__name__}")
            if body["fail_fast"] and may_cancel_siblings(phase_of[job_id], True):
                cancel(f"fail_fast: {job_id} could not start")
            return
        running.append(
            _Running(
                job_id,
                phase_of[job_id],
                process,
                clock(),
                clock() + float(job["deadline_seconds"]),
                tree,
                token,
                OwnedTree(process.pid, token, clock=clock),
                launched,
                handles,
            )
        )
        progress()

    def poll() -> None:
        while running:
            harvest()
            if not running:
                break
            if interrupt is not None and interrupt.is_set():
                cancel("external interrupt")
                break
            reason = live.check()
            if reason:
                budget.mark_lost(reason)
                cancel(f"capacity loss: {reason}")
                break
            for item in list(running):
                item.owned.observe()
                if item in running and clock() > item.deadline:
                    receipt = item.owned.terminate(
                        grace_seconds=grace_seconds, clock=clock, sleeper=sleeper
                    )
                    cleanup[item.job_id] = {"process_tree": receipt}
                    ports_seen[item.job_id] = set(item.owned.ports)
                    status = item.process.poll()
                    _close_handles(item)
                    settle(item.job_id, "timeout", "deadline exceeded", status)
                    running.remove(item)
                    if may_cancel_siblings(item.phase, body["fail_fast"]) and running:
                        cancel(f"fail_fast: {item.job_id} timed out")
            if running:
                sleeper(poll_seconds)

    def admit(batch: Sequence[Mapping[str, Any]]) -> bool:
        nonlocal cancel_reason
        try:
            require_parallel_capacity(plan, budget)
            for job in batch:
                live.admit(job["job_id"], job["pairs"])
        except CapacityError as exc:
            for job in batch:
                live.release(job["job_id"])
                settle(job["job_id"], "not_started", f"refused before dispatch: {exc}")
            cancel_reason = cancel_reason or f"capacity refused: {exc}"
            return False
        return True

    try:
        _write_plan_file(run_root, plan)
        previous: list[dict[str, Any]] = []
        for index, phase in enumerate(phase_names(body)):
            jobs = jobs_in_phase(body, phase)
            if not cancel_reason and index:
                go, why = barrier_decision(previous, fail_fast=body["fail_fast"])
                if not go:
                    cancel_reason = why
            if cancel_reason:
                for job in jobs:
                    settle(job["job_id"], "not_started", f"not started: {cancel_reason}")
                continue
            for batch in [[job] for job in jobs] if serial else [jobs]:
                if cancel_reason:
                    for job in batch:
                        settle(job["job_id"], "not_started", f"not started: {cancel_reason}")
                    continue
                if not admit(batch):
                    continue
                for job in batch:
                    if not cancel_reason:
                        start(job)
                poll()
                for job in batch:
                    job_id = job["job_id"]
                    if job_id not in outcomes:
                        settle(
                            job_id, "not_started", f"not started: {cancel_reason or 'no dispatch'}"
                        )
                    proof = cleanup.get(job_id, {}).get("process_tree", {}).get("proof", True)
                    if proof:
                        live.release(job_id)
                    else:
                        problems.append(f"{job_id}: cleanup unproven; reservation retained")
                    if job_id in trees:
                        shutil.rmtree(trees[job_id]["tmp"], ignore_errors=True)
            previous = [outcomes[job["job_id"]] for job in jobs if job["job_id"] in outcomes]
    except (PlanError, OSError) as exc:
        cancel_reason = cancel_reason or f"refused before dispatch: {type(exc).__name__}"
        problems.append(f"orchestration refused: {exc}")
    except KeyboardInterrupt:
        cancel("external interrupt")
    finally:
        if running:
            cancel(cancel_reason or "orchestrator exit")
        for job_id in phase_of:
            if job_id not in outcomes:
                settle(job_id, "not_started", f"not started: {cancel_reason or 'no dispatch'}")
            proof = cleanup.get(job_id, {}).get("process_tree", {}).get("proof", True)
            if proof:
                live.release(job_id)
        for job_id, tree in trees.items():
            shutil.rmtree(tree["tmp"], ignore_errors=True)
            cleanup.setdefault(job_id, {}).update(
                {
                    "tmp_removed": not tree["tmp"].exists(),
                    "cache_retained": tree["cache"].exists(),
                    "child_exit_status": outcomes[job_id].get("child_exit_status"),
                }
            )
    for job_id, tree in trees.items():
        if job_id in out_manifests and _manifest(tree["out"]) != out_manifests[job_id]:
            problems.append(
                f"{job_id}: output tree changed after the job finished (cross-job mutation)"
            )
        base, count = ranges[job_id]
        outside = sorted(p for p in ports_seen.get(job_id, set()) if not base <= p < base + count)
        if outside:
            problems.append(f"{job_id}: bound ports outside its reserved range {outside[:5]}")
    names = sorted(ports_seen)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            shared = ports_seen[first] & ports_seen[second]
            if shared:
                problems.append(f"{first} and {second} bound the same ports {sorted(shared)[:5]}")
    problems.extend(distinct_tree_problems(trees))
    if project_root.is_dir():
        problems.extend(diff_snapshots(shared_before, snapshot_tree(project_root, excluded)))
    ordered = [outcomes[job["job_id"]] for job in body["jobs"]]
    report = aggregate_outcomes(plan, ordered)
    report.update(
        _report_extras(
            plan,
            started,
            clock,
            cancel_reason,
            cleanup,
            problems,
            observations=live.observations,
            serial=serial,
            load=(load_before, os.getloadavg() if hasattr(os, "getloadavg") else ()),
        )
    )
    return report


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


def write_report(run_root: Path, report: Mapping[str, Any]) -> Path:
    """Write the single published aggregate next to the plan, never overwriting."""

    path = run_root / str(report["run_id"]) / "parallel-report.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_report_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"parallel runtime gate run {report.get('run_id')} plan {str(report.get('plan_sha256'))[:12]}",
        (
            f"commit {report.get('commit')} elapsed {report.get('elapsed_seconds')}s "
            f"mode {report.get('execution_mode')}"
        ),
    ]
    for job in report.get("jobs", []):
        lines.append(
            f"  {job.get('job_id')} ({job.get('mode')}): {job.get('state')} "
            f"exit={job.get('child_exit_status')}"
        )
        lines.extend(f"    {item}" for item in job.get("artifact_problems", [])[:3])
    lines.extend(f"problem: {item}" for item in report.get("problems", []))
    lines.extend(f"orchestration: {item}" for item in report.get("orchestration_problems", []))
    lines.extend(f"limitation: {item}" for item in report.get("limitations", []))
    lines.append(f"overall: {report.get('overall')}")
    return "\n".join(lines)


def terminal_artifact_path(tree: Mapping[str, Path]) -> Path:
    return tree["out"] / TERMINAL_ARTIFACT_NAME


INPUT_FILES = (
    "VERSIONS.md",
    "tests/_tier_config.py",
    "scripts/production_gate.py",
    "scripts/production_gate_model.py",
    "scripts/production_gate_runtime_gates.py",
    "scripts/production_gate_runtime.py",
    "scripts/production_gate_execution.py",
    "scripts/production_gate_tiers.py",
    "scripts/production_gate_matrix.py",
    "scripts/production_gate_evidence.py",
    "scripts/production_gate_parallel.py",
    "scripts/production_gate_parallel_plan.py",
    "scripts/production_gate_parallel_artifacts.py",
    "scripts/production_gate_parallel_worker.py",
    "scripts/production_gate_parallel_identity.py",
    "scripts/production_gate_parallel_records.py",
    "scripts/production_gate_parallel_capacity.py",
    "scripts/production_gate_parallel_processes.py",
    "scripts/production_gate_parallel_isolation.py",
    "scripts/production_gate_parallel_phases.py",
    "scripts/production_gate_parallel_validation.py",
    "scripts/production_gate_parallel_comparison.py",
    "tree:.",
)


def _deadlines(args: Any, required: Mapping[str, Sequence[str]], pairs: int) -> dict[str, float]:
    from scripts.production_gate_model import (
        COLLECTION_TIMEOUT_SECONDS,
        DEFAULT_TIMEOUT_SECONDS,
        MATRIX_AGGREGATE_GRACE_SECONDS,
        MATRIX_CASE_TIMEOUT_SECONDS,
    )

    tier = (
        float(args.timeout_seconds)
        if args.timeout_seconds
        else max(DEFAULT_TIMEOUT_SECONDS.values())
    )
    row = (
        float(args.matrix_timeout_seconds)
        if args.matrix_timeout_seconds
        else max(MATRIX_CASE_TIMEOUT_SECONDS.values())
    )
    largest = max((len(items) for items in required.values()), default=0)
    matrix = row * math.ceil(max(largest, 1) / max(pairs, 1)) + MATRIX_AGGREGATE_GRACE_SECONDS
    return {
        "preflight_seconds": float(COLLECTION_TIMEOUT_SECONDS),
        "row_seconds": row,
        "tier_seconds": max(tier, matrix),
        "repetition_seconds": tier,
        "cleanup_seconds": CLEANUP_SECONDS,
    }


def run_parallel_cli(
    *,
    args: Any,
    parser: Any,
    project_root: Path,
    python_executable: Path,
    cython_python_executable: Path | None,
    assets: Sequence[Any],
    required_nodeids_by_tier: Mapping[str, Sequence[str]],
    configuration_problems: Sequence[str],
    capacity_policy: Any,
    capacity_policy_path: Path | None,
    selected: Sequence[str],
    early_smoke: bool,
    fail_fast: bool,
    rom_root: Path | None = None,
    fixture_root: Path | None = None,
    capacity_session: Any | None = None,
    explicit_matrix_workers: bool = False,
) -> int:
    """Run the opt-in concurrent mode; every refusal is explicit and non-zero."""

    from scripts.production_gate_parallel_comparison import compare_bundles
    from scripts.production_gate_parallel_plan import build_parallel_plan

    if args.runtime_mode != "both":
        parser.error("--parallel-runtimes requires --runtime-mode both")
    if capacity_policy is None or capacity_policy_path is None:
        parser.error("--parallel-runtimes requires a valid --capacity-policy")
    if args.parallel_run_root is None:
        parser.error("--parallel-runtimes requires --parallel-run-root")
    problems = list(configuration_problems)
    _head, _clean, commit_problems = measure_commit(project_root)
    problems.extend(commit_problems)
    run_root = args.parallel_run_root.expanduser().resolve()
    if run_root.is_relative_to(project_root):
        problems.append("--parallel-run-root must be outside the checkout")
    if rom_root is None or fixture_root is None:
        problems.append("the ROM and fixture roots must be resolved explicitly")
    total = getattr(capacity_policy, "max_concurrent_pairs", None)
    run_id = uuid.uuid4().hex
    cython = cython_python_executable or python_executable
    interpreters = {"source": str(python_executable), "cython": str(cython)}
    snapshot: dict[str, Any] = {}
    plan = None
    measure: Measurer | None = None
    if not problems:
        run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            snapshot = build_snapshot(
                project_root,
                Path(str(rom_root)),
                Path(str(fixture_root)),
                run_root / run_id / "snapshot",
            )
        except (IsolationError, OSError) as exc:
            problems.append(f"private immutable snapshot refused: {exc}")
    if not problems:
        snap_root, snap_rom, snap_fixture = (
            Path(snapshot["checkout"]),
            Path(snapshot["rom_root"]),
            Path(snapshot["fixture_root"]),
        )

        def measure(job_id: str) -> tuple[dict[str, Any], list[str]]:
            mode = "cython" if job_id.startswith("cython") else "source"
            return measure_identity(
                mode=mode,
                interpreter=interpreters[mode],
                project_root=snap_root,
                input_names=INPUT_FILES,
                rom_root=snap_rom,
                fixture_root=snap_fixture,
                git_root=project_root,
            )

        measured = {mode: measure(f"{mode}-job") for mode in ("source", "cython")}
        for _values, mode_problems in measured.values():
            problems.extend(mode_problems)
        first, second = measured["source"][0], measured["cython"][0]
        if (first["commit"], first["input_digests"], first["asset_digest"]) != (
            second["commit"],
            second["input_digests"],
            second["asset_digest"],
        ):
            problems.append("the two runtimes measured different commits, inputs or assets")
        if not problems:
            pairs = (total if type(total) is int else 0) // 2
            try:
                plan = build_parallel_plan(
                    commit=first["commit"],
                    build_identity={mode: measured[mode][0]["build_identity"] for mode in measured},
                    input_digests=first["input_digests"],
                    asset_digest=first["asset_digest"],
                    games=("red", "blue", "yellow"),
                    roles=("listener", "connector"),
                    modes=("source", "cython"),
                    tiers=tuple(selected),
                    required_nodeids_by_tier=required_nodeids_by_tier,
                    repetitions=args.repeat_timing,
                    deadlines=_deadlines(args, required_nodeids_by_tier, pairs),
                    total_pairs=total if type(total) is int else 0,
                    policy_digest=file_sha256(capacity_policy_path),
                    early_smoke=early_smoke,
                    fail_fast=fail_fast,
                    matrix_workers=args.matrix_workers if explicit_matrix_workers else None,
                    run_id=run_id,
                )
            except PlanError as exc:
                problems.append(f"plan refused: {exc}")
    if problems or plan is None:
        if snapshot:
            thaw_and_remove(Path(snapshot["root"]))
        print("parallel runtime gate refused:\n" + "\n".join(f"  {item}" for item in problems))
        return 2
    gate_arguments = [
        "--fail-fast" if fail_fast else "--keep-going",
        f"--repeat-timing={args.repeat_timing}",
        *([f"--timeout-seconds={args.timeout_seconds}"] if args.timeout_seconds else []),
        *(
            [f"--matrix-timeout-seconds={args.matrix_timeout_seconds}"]
            if args.matrix_timeout_seconds
            else []
        ),
    ]
    factory = default_worker_argv(
        gate_arguments,
        interpreters,
        git_root=project_root,
        rom_root=Path(snapshot["rom_root"]),
        fixture_root=Path(snapshot["fixture_root"]),
    )
    budget = PairBudget(total)
    live = LiveCapacity(
        budget,
        session=capacity_session,
        probe=capacity_loss_probe(sum(job["pairs"] for job in plan.as_dict()["jobs"][:2])),
    )
    try:
        report = run_parallel_plan(
            plan,
            run_root=run_root,
            project_root=Path(snapshot["checkout"]),
            budget=budget,
            worker_argv_factory=factory,
            capacity=live,
            serial=bool(getattr(args, "parallel_serial", False)),
            measure=measure,
        )
    finally:
        thaw_and_remove(Path(snapshot["root"]))
    compare_to = getattr(args, "parallel_compare_to", None)
    if compare_to is not None:
        report["sequential_comparison"] = compare_bundles(
            report, json.loads(Path(compare_to).read_text(encoding="utf-8"))
        )
    path = write_report(run_root, report)
    print(
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else render_report_text(report)
    )
    print(f"report: {path}", file=sys.stderr)
    return 0 if report["overall"] == "PASS" else 1
