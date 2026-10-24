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

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import production_gate_parallel_job_retirement as retirement
from scripts import production_gate_parallel_safety as safety
from scripts.production_gate_parallel_arbiter import ArbiterHub
from scripts.production_gate_parallel_artifacts import (
    TERMINAL_ARTIFACT_NAME,
    collect_partial_evidence,
    outcome_record,
    read_terminal_artifact,
    total_outcome,
)
from scripts.production_gate_parallel_capacity import (
    CapacityError,
    LiveCapacity,
    PairBudget,
    affinity_pairs,
    capacity_loss_probe,
    child_capacity_loss,
    require_parallel_capacity,
    write_pinned_policy,
)
from scripts.production_gate_parallel_cgroup import (
    JobCgroup,
    OwnershipError,
    kernel_ownership,
)
from scripts.production_gate_parallel_finishing import finish_job as _finish_job
from scripts.production_gate_parallel_holder import launch_holder
from scripts.production_gate_parallel_identity import Measurer
from scripts.production_gate_parallel_isolation import (
    assign_port_ranges,
    create_job_tree,
    diff_snapshots,
    distinct_tree_problems,
    isolation_environment,
    port_range_problems,
    snapshot_tree,
)
from scripts.production_gate_parallel_job_lifecycle import (
    JobLaunchLedger,
    WorkerArgvFactory,
    _Running,
    default_worker_argv,  # noqa: F401 - preserve the existing public factory facade
    start_allocation,
    write_plan_allocations,
)
from scripts.production_gate_parallel_ownership import (
    holder_loss,
    latch_holder_loss,
    receipt_loss,
    record_cgroup_receipt,
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
from scripts.production_gate_parallel_reporting import completed_report, write_running_progress
from scripts.production_gate_parallel_root_run import run_owned
from scripts.production_gate_parallel_service import OwnerService
from scripts.production_gate_parallel_stopping import DeadlineWatch, prepare_stops, stop_process

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


def _wall_now() -> datetime:
    return datetime.now(UTC)


def prepare_job_tree(
    run_root: Path, run_id: str, job_id: str, *, launch: Any = None
) -> dict[str, Path]:
    """Create private writers through the isolation module, retaining the public entrypoint."""

    return create_job_tree(run_root, run_id, job_id, TREE_DIRECTORIES, launch=launch)


def job_environment(
    tree: Mapping[str, Path],
    base_env: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
    token: str = "",
    port_range: tuple[int, int] | None = None,
    deadline_epoch: float | None = None,
    job_deadline_monotonic: float | None = None,
    cleanup_deadline_monotonic: float | None = None,
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
    if job_deadline_monotonic is not None:
        env["POKERED_PARALLEL_JOB_DEADLINE_MONOTONIC"] = repr(job_deadline_monotonic)
    if cleanup_deadline_monotonic is not None:
        env["POKERED_PARALLEL_CLEANUP_DEADLINE_MONOTONIC"] = repr(cleanup_deadline_monotonic)
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


def _write_plan_file(
    run_root: Path, plan: ParallelPlan, *, ledger: JobLaunchLedger | None = None
) -> Path:
    if ledger is not None:
        return write_plan_allocations(ledger, run_root, plan, os, load_plan, PlanError)
    directory = run_root / plan.run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "plan.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(plan.text)
    if load_plan(path.read_text(encoding="utf-8")).digest != plan.digest:
        raise PlanError("plan file read-back differs from the frozen plan")
    return path


def _close_handles(running: _Running) -> None:
    slot = getattr(running, "launch", None)
    for handle in running.handles:
        if slot is not None:
            if slot.handle_states.get(id(handle), "open") != "open":
                continue
            slot.handle_states[id(handle)] = "closing"
        try:
            handle.close()
        except OSError:
            if slot is not None:
                slot.handle_states[id(handle)] = "unknown"
                slot.errors.append("output handle close unproven")
        else:
            if slot is not None:
                slot.handle_states[id(handle)] = "closed"


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
    root_context: Any = None,
    adopt_orphans: bool = True,
    use_cgroup: bool = True,
    require_kernel_ownership: bool = False,
) -> dict[str, Any]:
    """Dispatch under one retained adoption owner, standalone or explicitly borrowed."""
    return run_owned(
        plan, adoption_type=OrchestratorAdoption, ledger_type=JobLaunchLedger,
        runner=_run_plan, root_context=root_context, adopt_orphans=adopt_orphans,
        run_root=run_root, project_root=project_root, budget=budget,
        worker_argv_factory=worker_argv_factory, base_env=base_env, capacity=capacity,
        capacity_probe=capacity_probe, interrupt=interrupt, serial=serial, measure=measure,
        capacity_policy_text=capacity_policy_text, poll_seconds=poll_seconds,
        grace_seconds=grace_seconds, clock=clock, sleeper=sleeper, wall_clock=wall_clock,
        use_cgroup=use_cgroup, require_kernel_ownership=require_kernel_ownership,
    )


def _run_plan(
    plan: ParallelPlan,
    *,
    adoption: OrchestratorAdoption,
    ledger: JobLaunchLedger | None = None,
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
    if ledger is None:
        ledger = JobLaunchLedger(budget)
        ledger.adoption = adoption
    ledger.retain()
    if ledger.setup_attempted or ledger.adoption is not adoption:
        raise RuntimeError("run allocation setup cannot be retried or change adoption owner")
    ledger.setup_attempted = True
    ledger.clock = clock
    body = plan.as_dict()
    ledger.live = capacity or LiveCapacity(budget, probe=capacity_probe)
    live = ledger.live
    started = clock()
    outcomes: dict[str, dict[str, Any]] = {}
    running: list[_Running] = []
    cleanup: dict[str, dict[str, Any]] = {}
    ledger.cleanup = cleanup
    launched_jobs: set[str] = set()
    out_manifests: dict[str, dict[str, tuple[int, int]]] = {}
    trees: dict[str, dict[str, Path]] = {}
    ports_seen: dict[str, set[int]] = {}
    problems: list[str] = []
    phase_of = {job["job_id"]: job["phase"] for job in body["jobs"]}
    ranges = assign_port_ranges(list(phase_of))
    problems.extend(port_range_problems(ranges))
    ledger.handoffs = HandoffRegistry(job_ids=(j["job_id"] for j in body["jobs"]))
    handoffs = ledger.handoffs

    ledger.loss_notice = retirement.HubLossNotice()
    # Background loss only notifies; guardian delivery waits for an owner checkpoint.
    ledger.hub = ArbiterHub(
        budget.total_pairs,
        on_loss=ledger.loss_notice.notify,
        handler_limit=max(4, 2 * budget.total_pairs + 2 * len(body["jobs"])),
    )
    hub = ledger.hub
    handoffs.bind_loss(hub.lose)
    deadlines = DeadlineWatch(
        running,
        hub,
        body["deadlines"]["cleanup_seconds"],
        lambda item: may_cancel_siblings(item.phase, body["fail_fast"]),
        grace_seconds=grace_seconds,
        clock=clock,
        sleeper=sleeper,
        problems=problems,
    )
    sleeper = deadlines.sleep
    ownership = kernel_ownership(require_kernel_ownership)

    def close_handoff(job_id: str) -> dict[str, Any] | None:
        slot = ledger.slots.get(job_id)
        if slot is not None and slot.spawn_attempted and not slot.setup_complete:
            return None
        ledger.tighten_startup(job_id)
        record = handoffs.close(job_id)
        if record and record["unacknowledged"]:
            problems.append(
                f"{job_id}: a leased listener was transferred but never acknowledged "
                f"{record['unacknowledged'][:5]}"
            )
        return record

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
        write_running_progress(
            progress_path, phase_of, outcomes, running, cancel_reason, wall_clock
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

        return safety.recover_evidence(read_terminal_artifact, collect_partial_evidence, item.tree)

    def owned_finish(item: _Running, status: int, worker_pid: Any) -> Any:
        def measure_owned() -> Any:
            return _finish_job(
                plan, item, status, clock, wall_clock, measure, worker_pid,
                checkpoint=lambda: owner_pulse(skip=item),
                cancel_reason=lambda: owner_service.cancel_reason,
            )
        if ledger.root_context is not None:
            return ledger.root_context.during_finish(item, measure_owned)
        return measure_owned()

    def finish(item: _Running, status: int, *, allow_cancel: bool) -> None:
        if not retirement.enter_finish(item, status, running, outcomes):
            return
        _close_handles(item)
        view = item.process.guardian_status  # type: ignore[attr-defined]
        item.cleanup_deadline = min(
            item.cleanup_deadline if item.cleanup_deadline is not None else float("inf"),
            item.deadline + body["deadlines"]["cleanup_seconds"],
            clock() + body["deadlines"]["cleanup_seconds"],
        )
        early_loss = holder_loss(view, {"pid": item.process.pid, "start": item.owned.root_start})
        if early_loss and allow_cancel:
            latch_holder_loss(budget, hub, item.job_id, early_loss)
            prepare_stops(
                [other for other in running if other is not item],
                hub,
                item.cleanup_deadline,
                grace_seconds=grace_seconds,
                clock=clock,
                sleeper=sleeper,
            )
        guardian, guardian_problems = view.read_receipts(
            item.receipt,
            item.secret,
            item.process.pid,
            item.owned.root_start,
            reader=read_guardian_receipt,
        )
        record_cgroup_receipt(item.owned, guardian, guardian_problems)
        receipt = retirement.sweep_owned(
            item,
            grace_seconds=grace_seconds,
            clock=clock,
            sleeper=sleeper,
            deadline=item.cleanup_deadline,
        )
        proven = cleanup_proven(receipt, guardian, view)
        ledger = hub.unregister(item.job_id, cleanup_proven=proven)
        cleanup[item.job_id] = {
            "process_tree": receipt,
            "guardian": guardian or {},
            "listener_handoff": close_handoff(item.job_id),
            "arbiter": ledger,
        }
        ports_seen[item.job_id] = set(item.owned.ports)
        worker_pid = guardian.get("worker_pid") if guardian else None
        outcome = total_outcome(
            plan,
            item.job_id,
            lambda: owned_finish(item, status, worker_pid),
            exit_status=status,
            launched_utc=item.launched_wall.isoformat(),
            finished_utc=wall_clock().isoformat(),
            token=item.token,
        )
        if guardian and guardian.get("worker_status") != status and status >= 0:
            guardian_problems.append("the guardian saw a different worker exit status")
        if guardian_problems or receipt["terminated"] or not proven:
            outcome["state"] = "failed"
            outcome["artifact_problems"].extend(guardian_problems)
            outcome["artifact_problems"].append(
                "owned descendants outlived the worker or could not be proven gone"
            )
        if guardian and guardian.get("start_error"):
            outcome["reason"] = f"could not start: {guardian['start_error']}"
        if not proven:
            receipt["proof"] = False
        retirement.close_legacy_status(item)
        lost, malformed = safety.child_capacity_loss_total(
            child_capacity_loss, outcome.get("capacity")
        )
        if malformed:
            outcome["state"] = "failed"
            outcome["artifact_problems"].append(malformed)
        if not lost:
            lost = receipt_loss(view, receipt, guardian)
        if (
            not lost
            and guardian
            and guardian.get("contained") is False
            and require_kernel_ownership
        ):
            lost = "kernel ownership could not be established: " + str(
                guardian.get("reason") or guardian.get("cgroup_error") or "no reason recorded"
            )
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
            latch_holder_loss(budget, hub, item.job_id, lost)
        retirement.finish_custody(item, receipt, proven, outcome, running, owner_service)
        outcomes[item.job_id] = outcome
        out_manifests[item.job_id] = _manifest(item.tree["out"])
        retirement.remove_active(item, running)
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
        for item in retirement.active_snapshot(running, outcomes):
            if item not in running or not retirement.active_item(item):
                continue
            status = retirement.poll_item(item)
            if status is not None and item in running and not item.stop_requested:
                try:
                    finish(item, status, allow_cancel=allow_cancel)
                except Exception as exc:  # noqa: BLE001 - an aggregation error fails its own job only
                    safety.fail_finish(
                        outcome_record,
                        plan,
                        item,
                        status,
                        exc,
                        outcomes,
                        cleanup,
                        running,
                        deadline=min(
                            item.cleanup_deadline
                            if item.cleanup_deadline is not None
                            else float("inf"),
                            item.deadline + body["deadlines"]["cleanup_seconds"],
                            clock() + body["deadlines"]["cleanup_seconds"],
                        ),
                        clock=clock,
                        sleeper=sleeper,
                    )
                    if allow_cancel and hub.closed_reason and running:
                        cancel(f"containment lost before finish failed: {hub.closed_reason}")

    def stop(item: _Running) -> None:
        retirement.stop_job(
            item,
            running=running,
            hub=hub,
            cleanup=cleanup,
            ports_seen=ports_seen,
            close_handoff=close_handoff,
            cleanup_proven=cleanup_proven,
            stop_process=stop_process,
            reader=read_guardian_receipt,
            record_cgroup_receipt=record_cgroup_receipt,
            clock=clock,
            sleeper=sleeper,
            grace_seconds=grace_seconds,
            cleanup_seconds=body["deadlines"]["cleanup_seconds"],
            owner_service=owner_service,
        )

    def cancel(reason: str) -> None:
        nonlocal cancel_reason
        cancel_reason = cancel_reason or reason
        deadline = min(
            [clock() + body["deadlines"]["cleanup_seconds"]]
            + [item.cleanup_deadline for item in running if item.cleanup_deadline is not None]
        )
        prepare_stops(
            running, hub, deadline, grace_seconds=grace_seconds, clock=clock, sleeper=sleeper
        )
        harvest(
            allow_cancel=False
        )  # completed jobs were never signalled; failures remain their own rows
        for item in retirement.active_snapshot(running, outcomes):
            stop_error = safety.stop_safely(item, stop, cleanup, clock=clock, sleeper=sleeper)
            recovered, partial = recover(item)
            status = retirement.final_poll(item)
            _close_handles(item)
            settle(
                item.job_id,
                "timeout" if item.job_id in deadlines.expired else "interrupted",
                f"cancelled: {reason}" + (f"; {stop_error}" if stop_error else ""),
                status,
                artifact=recovered,
                partial=partial,
                item=item,
            )
            retirement.remove_active(item, running)

    def setup_checkpoint() -> None:
        owner_pulse()
        if owner_service.cancel_reason:
            raise CapacityError(owner_service.cancel_reason)

    def start(job: Mapping[str, Any]) -> None:
        nonlocal cancel_reason
        job_id = job["job_id"]
        slot = ledger.prepare(job_id, phase_of[job_id])
        try:
            start_allocation(
                slot,
                job,
                ledger=ledger,
                body=body,
                plan=plan,
                run_root=run_root,
                project_root=project_root,
                base_env=base_env,
                ranges=ranges,
                adoption=adoption,
                handoffs=handoffs,
                hub=hub,
                use_cgroup=use_cgroup,
                require_kernel_ownership=require_kernel_ownership,
                grace_seconds=grace_seconds,
                capacity_policy_text=capacity_policy_text,
                clock=clock,
                wall_clock=wall_clock,
                running=running,
                checkpoint=setup_checkpoint,
                prepare_tree=prepare_job_tree,
                policy_writer=write_pinned_policy,
                worker_factory=worker_argv_factory,
                cgroup_type=JobCgroup,
                ownership_error=OwnershipError,
                launcher=launch_holder if adoption.enabled else launch_guardian,
                environment=job_environment,
                tree_type=OwnedTree,
                running_type=_Running,
                token_factory=lambda: f"{body['run_id']}:{job_id}:{uuid.uuid4().hex}",
                stdin=subprocess.DEVNULL,
            )
            launched_jobs.add(job_id)
            progress()
            setup_checkpoint()
        except BaseException as exc:
            slot.fail(exc)
            if slot.spawn_attempted:
                # Unknown/no returned process is never discovered by a numeric PID.
                cancel_reason = cancel_reason or f"job setup unproven: {job_id}: {slot.failure}"
                ledger.failure = ledger.failure or cancel_reason
            else:
                if not slot.dispose_no_spawn(handoffs, hub):
                    cancel_reason = cancel_reason or f"local setup disposition unproven: {job_id}"
                    ledger.failure = ledger.failure or cancel_reason
            if isinstance(exc, (OSError, CapacityError, OwnershipError)):
                detail = str(exc) if isinstance(exc, OwnershipError) else type(exc).__name__
                settle(job_id, "failed", f"could not start: {detail}")
                if isinstance(exc, OwnershipError):
                    hub.lose(f"{job_id}: {detail}")
                if body["fail_fast"] and may_cancel_siblings(phase_of[job_id], True):
                    cancel(f"fail_fast: {job_id} could not start")
            else:
                cancel_reason = cancel_reason or f"job setup failed: {job_id}: {slot.failure}"
                raise
        finally:
            if slot.tree:
                trees[job_id] = slot.tree

    owner_service = OwnerService(
        running,
        hub,
        budget,
        live,
        deadlines,
        plan,
        interrupt,
        cleanup_seconds=body["deadlines"]["cleanup_seconds"],
        clock=clock,
        loss_notice=ledger.loss_notice,
        handoffs=handoffs,
    )
    if ledger.root_context is not None:
        ledger.root_context.attach_service(ledger, owner_service)
    owner_pulse = (
        ledger.root_context.service if ledger.root_context is not None else owner_service.pulse
    )
    deadlines.owner_loss_delivery = lambda: owner_service.consume_hub_loss(deadlines_current=True)

    def consume_service_cancel() -> bool:
        nonlocal cancel_reason
        owner_service.consume_hub_loss()
        if not owner_service.cancel_reason:
            return False
        cancel_reason = cancel_reason or owner_service.cancel_reason
        note = f"owner service cancelled: {owner_service.cancel_reason}"
        if note not in problems:
            problems.append(note)  # aggregate FAIL even if every row was already finalized
        return True

    def poll() -> None:
        while running:
            owner_service.preflight()
            if consume_service_cancel():
                cancel(cancel_reason)
                break
            harvest()
            if consume_service_cancel():
                cancel(cancel_reason)
                break
            if not running:
                break
            owner_service.pulse(preflight=False)
            if consume_service_cancel():
                cancel(cancel_reason)
                break
            for item in retirement.active_snapshot(running, outcomes):
                if deadlines.fail_fast_reason:
                    cancel(deadlines.fail_fast_reason)
                    break
                if item in running and item.job_id in deadlines.expired:
                    stop_error = safety.stop_safely(
                        item, stop, cleanup, clock=clock, sleeper=sleeper
                    )
                    recovered, partial = recover(item)
                    status = retirement.final_poll(item)
                    _close_handles(item)
                    settle(
                        item.job_id,
                        "timeout",
                        "deadline exceeded" + (f"; {stop_error}" if stop_error else ""),
                        status,
                        artifact=recovered,
                        partial=partial,
                        item=item,
                    )
                    retirement.remove_active(item, running)
                    if may_cancel_siblings(item.phase, body["fail_fast"]) and running:
                        cancel(f"fail_fast: {item.job_id} timed out")
            if running:
                sleeper(poll_seconds)

    def admit(batch: Sequence[Mapping[str, Any]]) -> bool:
        nonlocal cancel_reason
        if consume_service_cancel() or ledger.errors:
            cancel_reason = cancel_reason or "job allocation disposition failed"
            for job in batch:
                settle(job["job_id"], "not_started", f"not started: {cancel_reason}")
            return False
        if hub.closed_reason:
            budget.mark_lost(hub.closed_reason)  # a closed arbiter closes dispatch as well
        try:
            require_parallel_capacity(plan, budget)
            for job in batch:
                ledger.prepare(job["job_id"], phase_of[job["job_id"]])
                live.admit(job["job_id"], job["pairs"])
        except CapacityError as exc:
            for job in batch:
                ledger.dispose_unstarted()
                ledger.release_capacity(job["job_id"], True)
                settle(job["job_id"], "not_started", f"refused before dispatch: {exc}")
            cancel_reason = cancel_reason or f"capacity refused: {exc}"
            problems.append(f"capacity refused before dispatch: {exc}")
            return False
        return True

    try:
        _write_plan_file(run_root, plan, ledger=ledger)
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
                    if hub.closed_reason and not cancel_reason:
                        budget.mark_lost(hub.closed_reason)
                        cancel_reason = f"live arbiter closed admission: {hub.closed_reason}"
                    if not cancel_reason:
                        start(job)
                poll()
                for job in batch:
                    job_id = job["job_id"]
                    if job_id not in outcomes:
                        settle(
                            job_id,
                            "failed" if job_id in ledger.attempted else "not_started",
                            f"setup unproven: {cancel_reason}"
                            if job_id in ledger.attempted
                            else f"not started: {cancel_reason or 'no dispatch'}",
                        )
                    proof = safety.cleanup_proven_for(launched_jobs, cleanup, job_id, ledger=ledger)
                    if proof:
                        ledger.release_capacity(job_id, proof)
                    else:
                        problems.append(f"{job_id}: cleanup unproven; reservation retained")
                        cancel_reason = cancel_reason or f"{job_id}: cleanup remains unproven"
                    if job_id in trees and proof and outcomes[job_id]["state"] == "passed":
                        shutil.rmtree(trees[job_id]["tmp"], ignore_errors=True)
            previous = [outcomes[job["job_id"]] for job in jobs if job["job_id"] in outcomes]
    except (PlanError, OSError) as exc:
        cancel_reason = cancel_reason or f"refused before dispatch: {type(exc).__name__}"
        problems.append(f"orchestration refused: {exc}")
    except KeyboardInterrupt:
        cancel("external interrupt")
    finally:
        ledger.guard(consume_service_cancel, "consume cancellation")
        if running:
            ledger.guard(lambda: cancel(cancel_reason or "orchestrator exit"), "cancel siblings")
        ledger.guard(lambda: owner_service.pulse(), "service siblings on exit")
        ledger.guard(consume_service_cancel, "consume exit cancellation")
        ledger.guard(ledger.dispose_unstarted, "dispose unstarted")
        ledger.guard(
            lambda: retirement.restore_pending_outcomes(ledger, outcomes), "pending outcomes"
        )
        for job_id in phase_of:
            if job_id not in outcomes:
                ledger.guard(
                    lambda job_id=job_id: settle(
                        job_id,
                        "failed" if job_id in ledger.attempted else "not_started",
                        f"setup unproven: {cancel_reason}"
                        if job_id in ledger.attempted
                        else f"not started: {cancel_reason or 'no dispatch'}",
                    ),
                    "missing outcome",
                )
            if safety.cleanup_proven_for(launched_jobs, cleanup, job_id, ledger=ledger):
                ledger.release_capacity(job_id, True)
        for job_id, tree in trees.items():
            try:
                if not all(key in tree for key in ("tmp", "cache", "out")):
                    cleanup.setdefault(job_id, {}).update(
                        process_tree={"proof": False}, partial_tree=True
                    )
                    continue
                proven = safety.cleanup_proven_for(launched_jobs, cleanup, job_id, ledger=ledger)
                if proven and outcomes[job_id]["state"] == "passed":
                    shutil.rmtree(tree["tmp"], ignore_errors=True)
                cleanup.setdefault(job_id, {}).update(
                    {
                        "tmp_removed": not tree["tmp"].exists(),
                        "cache_retained": tree["cache"].exists(),
                        "child_exit_status": outcomes[job_id].get("child_exit_status"),
                    }
                )
            except BaseException as error:  # noqa: BLE001 - preserve the original caller failure
                ledger.errors.append(f"cleanup metadata: {job_id}: {type(error).__name__}")

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
    complete = safety.note_unproven_cleanup(launched_jobs, cleanup, problems, ledger=ledger)
    if not ledger.finalize(cleanup):
        complete = False
        problems.append("job allocation/startup custody or restoration remains unproven")
    ordered = [outcomes[job["job_id"]] for job in body["jobs"]]
    report = completed_report(
        plan,
        ordered,
        started,
        clock,
        cancel_reason,
        cleanup,
        problems,
        ownership,
        launched_jobs,
        complete,
        observations=live.observations,
        serial=serial,
        load=(load_before, os.getloadavg() if hasattr(os, "getloadavg") else ()),
    )
    report["allocation_publication"] = {
        "attempted": sorted(ledger.attempted),
        "returned": sorted(s.job_id for s in ledger.slots.values() if s.process is not None),
        "pending": ledger.retained,
        "errors": list(ledger.errors),
    }
    retirement.seal_report(report, hub)
    return report


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
