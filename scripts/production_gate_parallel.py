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

import json
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

from scripts.production_gate_parallel_arbiter import ArbiterHub
from scripts.production_gate_parallel_artifacts import (
    TERMINAL_ARTIFACT_NAME,
    aggregate_outcomes,
    collect_partial_evidence,
    outcome_record,
    read_terminal_artifact,
    validate_terminal_artifact,
    verify_gate_report,
)
from scripts.production_gate_parallel_capacity import (
    CapacityError,
    LiveCapacity,
    PairBudget,
    affinity_pairs,
    capacity_loss_probe,
    child_capacity_loss,
    policy_file_problem,
    require_parallel_capacity,
    write_pinned_policy,
)
from scripts.production_gate_parallel_cgroup import (
    JobCgroup,
    OwnershipError,
    kernel_ownership,
    qualification_after,
)
from scripts.production_gate_parallel_declaration import COVERAGE_DECLARATION
from scripts.production_gate_parallel_identity import (
    expected_identity,
    identity_differences,
)
from scripts.production_gate_parallel_isolation import (
    assign_port_ranges,
    diff_snapshots,
    distinct_tree_problems,
    isolation_environment,
    port_range_problems,
    snapshot_tree,
)
from scripts.production_gate_parallel_phases import (
    barrier_decision,
    jobs_in_phase,
    may_cancel_siblings,
    phase_names,
)
from scripts.production_gate_parallel_plan import ParallelPlan, PlanError, load_plan
from scripts.production_gate_parallel_ports import HANDOFF_ENV, HandoffRegistry
from scripts.production_gate_parallel_processes import (
    JOB_TOKEN_ENV,
    OrchestratorAdoption,
    OwnedTree,
    cleanup_proven,
    launch_guardian,
    read_guardian_receipt,
)

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
    deadline_epoch: float | None = None,
    handoff: str | None = None,
    arbiter_env: Mapping[str, str] | None = None,
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
    if deadline_epoch is not None:
        env["POKERED_PARALLEL_DEADLINE_EPOCH"] = repr(deadline_epoch)
    if handoff:
        env[HANDOFF_ENV] = handoff
    env.update(arbiter_env or {})
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
    receipt: Path
    secret: bytes
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
    capacity_policy: bool = False,
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
        if capacity_policy:
            argv.append(f"--capacity-policy={tree['root'] / 'capacity-policy.json'}")
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
    worker_pid: int | None = None,
) -> dict[str, Any]:
    artifact, read_problems = read_terminal_artifact(running.tree["root"])
    problems = list(read_problems)
    child_capacity = None
    if artifact is not None:
        report_problems, gate_report = verify_gate_report(
            running.tree["root"], artifact, plan.as_dict(), plan.job(running.job_id)
        )
        problems.extend(report_problems)
        if gate_report is not None and isinstance(gate_report.get("capacity"), dict):
            child_capacity = gate_report["capacity"]
        problems.extend(
            validate_terminal_artifact(
                artifact,
                plan,
                running.job_id,
                child_exit_status=exit_status,
                child_pid=worker_pid if worker_pid is not None else running.process.pid,
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
        launched_utc=running.launched_wall.isoformat(),
        finished_utc=wall_clock().isoformat(),
    )
    record["launch_token"] = running.token
    record["capacity"] = child_capacity
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
    capacity_policy_text: str | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], datetime] = _wall_now,
    adopt_orphans: bool = True,
    use_cgroup: bool = True,
    require_kernel_ownership: bool = False,
) -> dict[str, Any]:
    """Dispatch every planned job (phase by phase) and return the retained aggregate report."""

    with OrchestratorAdoption(enabled=adopt_orphans) as adoption:
        return _run_plan(
            plan,
            adoption=adoption,
            use_cgroup=use_cgroup,
            require_kernel_ownership=require_kernel_ownership,
            run_root=run_root,
            project_root=project_root,
            budget=budget,
            worker_argv_factory=worker_argv_factory,
            base_env=base_env,
            capacity=capacity,
            capacity_probe=capacity_probe,
            interrupt=interrupt,
            serial=serial,
            measure=measure,
            capacity_policy_text=capacity_policy_text,
            poll_seconds=poll_seconds,
            grace_seconds=grace_seconds,
            clock=clock,
            sleeper=sleeper,
            wall_clock=wall_clock,
        )


def _run_plan(
    plan: ParallelPlan,
    *,
    adoption: OrchestratorAdoption,
    use_cgroup: bool,
    require_kernel_ownership: bool,
    run_root: Path,
    project_root: Path,
    budget: PairBudget,
    worker_argv_factory: WorkerArgvFactory,
    base_env: Mapping[str, str] | None,
    capacity: LiveCapacity | None,
    capacity_probe: Callable[[], object] | None,
    interrupt: threading.Event | None,
    serial: bool,
    measure: Measurer | None,
    capacity_policy_text: str | None,
    poll_seconds: float,
    grace_seconds: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    wall_clock: Callable[[], datetime],
) -> dict[str, Any]:
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
    handoffs = HandoffRegistry()

    def signal_guardians(_reason: str) -> None:
        for item in tuple(running):  # the live arbiter lost: every owned sibling is told at once
            item.owned.signal_root(signal.SIGTERM)

    hub = ArbiterHub(budget.total_pairs, on_loss=signal_guardians)
    ownership = kernel_ownership(require_kernel_ownership)

    def close_handoff(job_id: str) -> dict[str, Any] | None:
        ledger = handoffs.close(job_id)
        if ledger and ledger["unacknowledged"]:
            problems.append(
                f"{job_id}: a leased listener was transferred but never acknowledged "
                f"{ledger['unacknowledged'][:5]}"
            )
        return ledger

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

    def settle(
        job_id: str,
        state: str,
        reason: str,
        status: int | None = None,
        *,
        artifact: Mapping[str, Any] | None = None,
        partial: Mapping[str, Any] | None = None,
        item: _Running | None = None,
    ) -> None:
        outcomes[job_id] = outcome_record(
            plan,
            job_id,
            state,
            child_exit_status=status,
            artifact=artifact,
            artifact_problems=[reason],
            reason=reason,
            launched_utc=item.launched_wall.isoformat() if item is not None else None,
            finished_utc=wall_clock().isoformat() if item is not None else None,
            partial_evidence=partial,
        )
        progress()

    def recover(item: _Running) -> tuple[Mapping[str, Any] | None, Mapping[str, Any]]:
        """Whatever the cancelled job already produced; kept beside, never as, a PASS."""

        artifact, _problems = read_terminal_artifact(item.tree["root"])
        return artifact, collect_partial_evidence(item.tree)

    def finish(item: _Running, status: int, *, allow_cancel: bool) -> None:
        _close_handles(item)
        receipt = item.owned.terminate(grace_seconds=grace_seconds, clock=clock, sleeper=sleeper)
        guardian, guardian_problems = read_guardian_receipt(
            item.receipt, item.secret, item.process.pid, item.owned.root_start
        )
        ledger = hub.unregister(item.job_id)
        cleanup[item.job_id] = {
            "process_tree": receipt,
            "guardian": guardian or {},
            "listener_handoff": close_handoff(item.job_id),
            "arbiter": ledger,
        }
        ports_seen[item.job_id] = set(item.owned.ports)
        worker_pid = guardian.get("worker_pid") if guardian else None
        outcome = _finish_job(plan, item, status, clock, wall_clock, measure, worker_pid)
        if guardian and guardian.get("worker_status") != status and status >= 0:
            guardian_problems.append("the guardian saw a different worker exit status")
        if guardian_problems or receipt["terminated"] or not receipt["proof"]:
            outcome["state"] = "failed"
            outcome["artifact_problems"].extend(guardian_problems)
            outcome["artifact_problems"].append(
                "owned descendants outlived the worker or could not be proven gone"
            )
        if guardian and guardian.get("start_error"):
            outcome["reason"] = f"could not start: {guardian['start_error']}"
        view = item.process.guardian_status  # type: ignore[attr-defined]
        if not cleanup_proven(receipt, guardian, view):
            receipt["proof"] = False
        view.close()
        lost = child_capacity_loss(outcome.get("capacity"))
        if not lost and not (guardian and guardian.get("phase") == "final"):
            lost = "the job's guardian was lost"
        if (
            capacity_policy_text is not None
            and outcome["state"] == "passed"
            and ledger
            and (not ledger["hello"] or ledger["unreleased"])
        ):
            outcome["state"] = "failed"
            outcome["artifact_problems"].append(
                "the child had no live arbiter session or ended holding reservations"
            )
        if lost:
            outcome["state"] = "failed"
            outcome["artifact_problems"].append(f"the child reported a loss: {lost}")
            budget.mark_lost(f"{item.job_id}: {lost}")
            hub.lose(f"{item.job_id}: {lost}")
        outcomes[item.job_id] = outcome
        out_manifests[item.job_id] = _manifest(item.tree["out"])
        running.remove(item)
        progress()
        if allow_cancel and running and lost:
            cancel(f"capacity loss reported by {item.job_id}: {lost}")
        elif (
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

    def stop(item: _Running) -> None:
        """Ask the guardian to contain its subtree first, then prove it from outside."""

        view = item.process.guardian_status  # type: ignore[attr-defined]
        # The guardian installs its handlers before it reports readiness over the status pipe; a
        # stop signal sent earlier would kill it with the default action and leave no receipt.
        ready_limit = clock() + grace_seconds + 2.0
        while item.process.poll() is None and clock() < ready_limit:
            view.drain()
            if view.ready or view.eof:
                break
            sleeper(0.01)
        item.owned.signal_root(signal.SIGTERM)
        limit = clock() + 3 * grace_seconds + 3.0  # the guardian's own worst case is ~3x grace + 1
        while item.process.poll() is None and clock() < limit:
            sleeper(0.02)
        receipt = item.owned.terminate(grace_seconds=grace_seconds, clock=clock, sleeper=sleeper)
        guardian, _problems = read_guardian_receipt(
            item.receipt, item.secret, item.process.pid, item.owned.root_start
        )
        if not cleanup_proven(receipt, guardian, view):
            receipt["proof"] = False
        view.close()
        cleanup[item.job_id] = {
            "process_tree": receipt,
            "guardian": guardian or {},
            "listener_handoff": close_handoff(item.job_id),
            "arbiter": hub.unregister(item.job_id),
        }
        ports_seen[item.job_id] = set(item.owned.ports)

    def cancel(reason: str) -> None:
        nonlocal cancel_reason
        cancel_reason = cancel_reason or reason
        harvest(allow_cancel=False)  # completed failures are kept before any signal is sent
        for item in list(running):
            stop(item)
            recovered, partial = recover(item)
            status = item.process.poll()
            _close_handles(item)
            settle(
                item.job_id,
                "interrupted",
                f"cancelled: {reason}",
                status,
                artifact=recovered,
                partial=partial,
                item=item,
            )
            running.remove(item)

    def start(job: Mapping[str, Any]) -> None:
        job_id = job["job_id"]
        handles: list[Any] = []
        job_cgroup = JobCgroup(None, "not created")
        try:
            tree = prepare_job_tree(run_root, body["run_id"], job_id)
            trees[job_id] = tree
            token = f"{body['run_id']}:{job_id}:{uuid.uuid4().hex}"
            receipt_path = tree["root"] / "guardian.json"
            if capacity_policy_text is not None:
                write_pinned_policy(
                    tree["root"] / "capacity-policy.json",
                    capacity_policy_text,
                    job["pairs"],
                    job.get("policy_sha256"),
                )
            worker_argv = list(worker_argv_factory(plan, job_id, tree, project_root))
            handles.append(open(tree["root"] / "child.stdout", "wb"))  # noqa: SIM115
            handles.append(open(tree["root"] / "child.stderr", "wb"))  # noqa: SIM115
            launched = wall_clock()
            handoff = handoffs.open(job_id, *ranges[job_id])
            arbiter_env = hub.register(job_id, job["pairs"], job.get("policy_sha256", ""))
            job_cgroup = (
                JobCgroup.create(f"pokered-{body['run_id'][:12]}-{job_id}")
                if use_cgroup
                else JobCgroup(None, "disabled by the caller")
            )
            if require_kernel_ownership and not job_cgroup.available:
                raise OwnershipError(f"kernel ownership is required: {job_cgroup.reason}")
            process, secret = launch_guardian(
                receipt_path,
                grace_seconds,
                worker_argv,
                cgroup=("!" if require_kernel_ownership else "")
                + (str(job_cgroup.path) if job_cgroup.available else ""),
                cwd=project_root,
                env=job_environment(
                    tree,
                    base_env,
                    project_root=project_root,
                    token=token,
                    port_range=ranges[job_id],
                    deadline_epoch=launched.timestamp() + float(job["deadline_seconds"]),
                    handoff=handoff,
                    arbiter_env=arbiter_env,
                ),
                stdin=subprocess.DEVNULL,
                stdout=handles[0],
                stderr=handles[1],
                start_new_session=True,
            )
        except (OSError, CapacityError, OwnershipError) as exc:
            close_handoff(job_id)
            hub.unregister(job_id)
            job_cgroup.remove()
            for handle in handles:
                handle.close()
            detail = str(exc) if isinstance(exc, OwnershipError) else type(exc).__name__
            settle(job_id, "failed", f"could not start: {detail}")
            if isinstance(exc, OwnershipError):
                hub.lose(f"{job_id}: {detail}")
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
                OwnedTree(
                    process.pid,
                    token,
                    clock=clock,
                    adoption=adoption,
                    status=process.guardian_status,  # type: ignore[attr-defined]
                    known_children=lambda: (
                        {i.process.pid for i in running}
                        | {pid for i in running for pid in i.owned.tracked}
                    ),
                    cgroup=job_cgroup,
                ),
                launched,
                receipt_path,
                secret,
                handles,
            )
        )
        progress()

    def poll() -> None:
        while running:
            hub.check_leases()
            for item in tuple(running):  # a policy that is no longer the plan-pinned one is a loss
                pin = plan.job(item.job_id).get("policy_sha256")
                if pin and policy_file_problem(item.tree["root"] / "capacity-policy.json", pin):
                    hub.lose(f"{item.job_id}: the child capacity policy changed during the run")
            if hub.closed_reason:
                budget.mark_lost(hub.closed_reason)
                cancel(f"live arbiter closed admission: {hub.closed_reason}")
                break
            harvest()
            if not running:
                break
            if interrupt is not None and interrupt.is_set():
                cancel("external interrupt")
                break
            reason = live.check()
            if reason:
                budget.mark_lost(reason)
                hub.lose(f"capacity loss: {reason}")
                cancel(f"capacity loss: {reason}")
                break
            for item in list(running):
                item.owned.observe()
                if item in running and clock() > item.deadline:
                    stop(item)
                    recovered, partial = recover(item)
                    status = item.process.poll()
                    _close_handles(item)
                    settle(
                        item.job_id,
                        "timeout",
                        "deadline exceeded",
                        status,
                        artifact=recovered,
                        partial=partial,
                        item=item,
                    )
                    running.remove(item)
                    if may_cancel_siblings(item.phase, body["fail_fast"]) and running:
                        cancel(f"fail_fast: {item.job_id} timed out")
            if running:
                sleeper(poll_seconds)

    def admit(batch: Sequence[Mapping[str, Any]]) -> bool:
        nonlocal cancel_reason
        if hub.closed_reason:
            budget.mark_lost(hub.closed_reason)  # a closed arbiter closes dispatch as well
        try:
            require_parallel_capacity(plan, budget)
            for job in batch:
                live.admit(job["job_id"], job["pairs"])
        except CapacityError as exc:
            for job in batch:
                live.release(job["job_id"])
                settle(job["job_id"], "not_started", f"refused before dispatch: {exc}")
            cancel_reason = cancel_reason or f"capacity refused: {exc}"
            problems.append(f"capacity refused before dispatch: {exc}")
            return False
        return True

    try:
        _write_plan_file(run_root, plan)
        if ownership["admitted"]:
            hub.start()
        else:
            cancel_reason = (
                f"refused before any child: kernel ownership is required ({ownership['reason']})"
            )
            problems.append(cancel_reason)
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
                    if job_id in trees and proof and outcomes[job_id]["state"] == "passed":
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
        handoffs.close_all()
        hub.stop()
        for job_id in phase_of:
            if job_id not in outcomes:
                settle(job_id, "not_started", f"not started: {cancel_reason or 'no dispatch'}")
            proof = cleanup.get(job_id, {}).get("process_tree", {}).get("proof", True)
            if proof:
                live.release(job_id)
        for job_id, tree in trees.items():
            proven = cleanup.get(job_id, {}).get("process_tree", {}).get("proof") is True
            if proven and outcomes[job_id]["state"] == "passed":
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
    report["kernel_ownership"] = qualification_after(ownership, cleanup)
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


def run_parallel_cli(**kwargs: Any) -> int:
    """Run the opt-in concurrent mode (implemented in ``production_gate_parallel_cli``)."""

    from scripts.production_gate_parallel_cli import run_parallel_cli as run

    return run(**kwargs)
