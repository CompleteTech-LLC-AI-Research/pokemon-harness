"""Retained job allocation records; no independent process recovery authority."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_plan import ParallelPlan

# Strong, process-local roots. An unresolved run refuses later dispatch. This is
# local custody, not the shared session/adoption architecture or a recovery driver.
_RETAINED_RUNS: list[JobLaunchLedger] = []


@dataclass
class _Running:
    job_id: str
    phase: str
    process: Any
    started: float
    deadline: float
    tree: dict[str, Path]
    token: str
    owned: Any
    launched_wall: Any
    receipt: Path
    secret: bytes
    handles: list[Any] = field(default_factory=list)
    cleanup_deadline: float | None = None
    stop_requested: bool = False


@dataclass
class JobLaunch:
    """Published before allocations; raw fields confer no signal/wait authority."""

    job_id: str
    phase: str = ""
    process: Any = None
    secret: bytes = b""
    pipes: dict[str, int] = field(default_factory=dict)
    pipe_states: dict[str, str] = field(default_factory=dict)
    pipe_os: Any = None
    handles: list[Any] = field(default_factory=list)
    handle_states: dict[int, str] = field(default_factory=dict)
    status: Any = None
    status_state: str = "raw"
    holder: Any = None
    owned: Any = None
    raw_pidfd: int | None = None
    pin_state: str = "unopened"
    initialized: bool = False
    spawn_attempted: bool = False
    setup_complete: bool = False
    item: Any = None
    tree: dict[str, Path] = field(default_factory=dict)
    path_states: dict[Path, str] = field(default_factory=dict)
    created_paths: list[Path] = field(default_factory=list)
    created_files: list[Path] = field(default_factory=list)
    cgroup: Any = None
    cgroup_path: Path | None = None
    cgroup_state: str = "none"
    broker: Any = None
    registered: bool = False
    launched_monotonic: float | None = None
    launched_wall: Any = None
    deadline: float | None = None
    cleanup_cap: float | None = None
    clock: Any = None
    failure: str = ""
    errors: list[str] = field(default_factory=list)
    local_disposed: bool = False
    capacity_state: str = "pending"
    custody_use: str = "active"
    custody_state: str = "holding"
    custody_eligibility: Any = None
    custody_dispositions: dict[Any, Any] = field(default_factory=dict)
    custody_entries: Any = None
    pending_outcome: Any = None
    root_context: Any = None
    final_wait_attempted: bool = False
    final_wait_status: Any = None
    final_wait_complete: bool = False

    def fail(self, error: BaseException) -> None:
        self.failure = self.failure or f"{type(error).__name__}: {str(error)[:160]}"

    @property
    def no_spawn_eligible(self) -> bool:
        # Eligibility starts disposition; it does not falsely claim it completed.
        return not self.spawn_attempted and self.process is None

    def close_pipe(self, name: str, os_module: Any = None) -> None:
        if name not in self.pipes or self.pipe_states.get(name) != "raw":
            return
        os_module = os_module or self.pipe_os or os
        self.pipe_states[name] = "closing"
        try:
            os_module.close(self.pipes[name])
        except BaseException:
            self.pipe_states[name] = "unknown"
            raise
        self.pipe_states[name] = "closed"

    def close_parent_ends(self, os_module: Any) -> BaseException | None:
        first = None
        for name in ("secret_write", "secret_read", "status_write"):
            try:
                self.close_pipe(name, os_module)
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.errors.append(f"{name} close unproven: {type(error).__name__}")
                first = first or error
        return first

    def pipe_pair(self, prefix: str, os_module: Any) -> None:
        # Retain the complete normal-return pair before another allocation.
        pair = os_module.pipe()
        self.pipes.update({prefix + "_read": pair[0], prefix + "_write": pair[1]})
        self.pipe_states.update({prefix + "_read": "raw", prefix + "_write": "raw"})

    def file_intent(self, path: Path) -> None:
        self.path_states[path] = "intent"

    def file_created(self, path: Path) -> None:
        self.created_files.append(path)
        self.path_states[path] = "created"

    def directory_created(self, path: Path) -> None:
        self.created_paths.append(path)
        self.path_states[path] = "created"

    @property
    def current_cleanup_cap(self) -> float | None:
        bound = getattr(self.item, "cleanup_deadline", None)
        if self.cleanup_cap is None:
            return bound
        return self.cleanup_cap if bound is None else min(self.cleanup_cap, bound)

    def release_eligible(self, original_proof: bool) -> bool:
        if self.no_spawn_eligible:
            return self.local_disposed
        return bool(
            original_proof
            and self.initialized
            and self.setup_complete
            and not self.failure
            and self.custody_state == "disposed"
            and self.pin_state == "tree"
            and self.raw_pidfd is None
            and not self.errors
            and all(self.handle_states.get(id(h)) == "closed" for h in self.handles)
            and self.status is not None
            and self.status.fd < 0
            and self.status.close_state == "closed"
            and all(s in ("closed", "status") for s in self.pipe_states.values())
        )

    def dispose_no_spawn(self, handoffs: Any, hub: Any) -> bool:
        """Only positive no-attempt state permits local resource disposal."""
        if not self.no_spawn_eligible:
            return False
        for name in tuple(self.pipes):
            try:
                self.close_pipe(name)
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.errors.append(f"pipe close: {type(error).__name__}")
        for handle in self.handles:
            key = id(handle)
            if self.handle_states.get(key, "open") != "open":
                continue
            self.handle_states[key] = "closing"
            try:
                handle.close()
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.handle_states[key] = "unknown"
                self.errors.append(f"handle close: {type(error).__name__}")
            else:
                self.handle_states[key] = "closed"
        broker_ok = True
        if self.broker is not None:
            try:
                handoffs.close(self.job_id)
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.errors.append(f"handoff close: {type(error).__name__}")
            broker_ok = bool(self.broker.startup_disposed)
        if self.registered:
            try:
                hub.unregister(self.job_id)
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.errors.append(f"unregister: {type(error).__name__}")
            else:
                self.registered = False
        if self.cgroup_state == "created":
            self.cgroup_state = "disposing"
            try:
                self.cgroup_path.rmdir()
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.cgroup_state = "unknown"
                self.errors.append(f"cgroup remove: {type(error).__name__}")
            else:
                self.cgroup_state = "disposed"
        # No recursive deletion: only positively created entries may be removed.
        for path in [*reversed(self.created_files), *reversed(self.created_paths)]:
            if self.path_states.get(path) != "created":
                continue
            self.path_states[path] = "disposing"
            try:
                path.unlink() if path in self.created_files else path.rmdir()
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.path_states[path] = "unknown"
                self.errors.append(f"path disposition: {type(error).__name__}")
            else:
                self.path_states[path] = "disposed"
        self.local_disposed = bool(
            not self.errors
            and broker_ok
            and not self.registered
            and self.cgroup_state in ("none", "disposed")
            and all(s == "closed" for s in self.pipe_states.values())
            and all(self.handle_states.get(id(h)) == "closed" for h in self.handles)
            and all(s == "disposed" for s in self.path_states.values())
        )
        return self.local_disposed


def allocate_process(
    launch: JobLaunch,
    *,
    command: Any,
    popen: Any,
    os_module: Any,
    status_type: Any,
    holder_factory: Any = None,
    popen_kwargs: dict[str, Any],
) -> tuple[Any, bytes]:
    """Both real helper callers supply an already retained slot, never a default."""
    if launch.spawn_attempted or launch.pipes:
        raise RuntimeError("job launch allocation cannot be retried")
    original = None
    launch.pipe_os = os_module
    try:
        launch.secret = os_module.urandom(32)
        launch.pipe_pair("secret", os_module)
        launch.pipe_pair("status", os_module)
        os_module.write(launch.pipes["secret_write"], launch.secret.hex().encode())
        launch.close_pipe("secret_write", os_module)
        argv = command(launch)
        launch.spawn_attempted = True
        launch.process = popen(
            argv,
            pass_fds=(launch.pipes["secret_read"], launch.pipes["status_write"]),
            **popen_kwargs,
        )
        if launch.root_context is not None:
            launch.root_context.partition.publish_original(launch, launch.process)
    except BaseException as error:
        original = error
        launch.fail(error)
        raise
    finally:
        close_error = launch.close_parent_ends(os_module)
        if close_error is not None and original is None:
            launch.fail(close_error)
            raise close_error
    if holder_factory is not None:
        launch.holder = holder_factory(launch)
    launch.status = status_type.prepare(holder=launch.holder)
    launch.status.configure(launch.pipes["status_read"])
    launch.status_state = "status"
    launch.pipe_states["status_read"] = "status"
    launch.process.guardian_status = launch.status
    return launch.process, launch.secret


class JobLaunchLedger:
    """Run-owned roots survive setup exceptions and removal from running."""

    def __init__(self, budget: Any) -> None:
        self.budget = budget
        self.live: Any = None
        self.hub: Any = None
        self.handoffs: Any = None
        self.adoption: Any = None
        self.owns_adoption = False
        self.root_context: Any = None
        self.terminal_state = "none"
        self.slots: dict[str, JobLaunch] = {}
        self.errors: list[str] = []
        self.cleanup: dict[str, Any] = {}
        self.failure = ""
        self.retained = False
        self.setup_attempted = False
        self.clock: Any = None
        self.setup_paths: dict[Path, str] = {}
        self.plan_raw_fd: int | None = None
        self.plan_handle: Any = None
        self.plan_fd_state = "none"

    def retain(self) -> None:
        if self.retained:
            return
        if _RETAINED_RUNS:
            raise RuntimeError("prior job allocation custody/restoration is unresolved")
        _RETAINED_RUNS.append(self)
        self.retained = True

    def is_retained(self) -> bool:
        return self in _RETAINED_RUNS

    def prepare(self, job_id: str, phase: str) -> JobLaunch:
        if job_id not in self.slots:
            self.slots[job_id] = JobLaunch(job_id, phase)
            if self.root_context is not None:
                self.root_context.prepare_job(self, self.slots[job_id])
        return self.slots[job_id]

    @property
    def attempted(self) -> set[str]:
        return {s.job_id for s in self.slots.values() if s.spawn_attempted}

    def tighten_startup(self, job_id: str) -> None:
        slot = self.slots.get(job_id)
        if slot is None or slot.broker is None or slot.current_cleanup_cap is None:
            return
        old = getattr(slot.broker, "_startup_deadline", None)
        bound = slot.current_cleanup_cap
        slot.broker._startup_deadline = min(old, bound) if old is not None else bound
        slot.broker._startup_clock = slot.clock

    def wait_owned_children(self) -> set[int]:
        # Only original unreaped Popen children; never poll here or retain a stale
        # reaped numeric identity as a new foreign-instance exclusion.
        originals = {
            s.process.pid
            for s in self.slots.values()
            if s.process is not None and s.process.returncode is None
        }
        if self.root_context is not None:
            originals |= self.root_context.partition.unreaped_originals()
        return originals

    def proof(self, job_id: str, original_proof: bool) -> bool:
        slot = self.slots.get(job_id)
        return original_proof if slot is None else slot.release_eligible(original_proof)

    def guard(self, action: Any, label: str) -> None:
        try:
            action()
        except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
            self.errors.append(f"{label}: {type(error).__name__}: {str(error)[:160]}")

    def dispose_unstarted(self) -> None:
        for slot in tuple(self.slots.values()):
            if slot.no_spawn_eligible:
                self.guard(
                    lambda slot=slot: slot.dispose_no_spawn(self.handoffs, self.hub),
                    "local disposition",
                )

    def release_capacity(self, job_id: str, original_proof: bool) -> bool:
        if not self.proof(job_id, original_proof):
            return False
        slot = self.slots.get(job_id)
        if slot is not None:
            if slot.capacity_state != "pending":
                return slot.capacity_state == "released"
            slot.capacity_state = "releasing"
        try:
            self.live.release(job_id)
        except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
            if slot is not None:
                slot.capacity_state = "unknown"
            self.errors.append(f"capacity release: {type(error).__name__}")
            return False
        if slot is not None:
            slot.capacity_state = "released"
        return True

    def close_plan(self, os_module: Any = os) -> None:
        state = self.plan_fd_state
        if state not in ("raw", "handle"):
            return
        self.plan_fd_state = "closing"
        try:
            if state == "raw":
                os_module.close(self.plan_raw_fd)
            else:
                self.plan_handle.close()
        except BaseException:
            self.plan_fd_state = "unknown"
            raise
        self.plan_fd_state = "closed"

    def finalize(self, cleanup: dict[str, Any], original: BaseException | None = None) -> bool:
        if self.terminal_state in ("attempting", "unknown", "published"):
            return self.terminal_state == "published"
        if self.terminal_state == "restoration_pending":
            return self.continue_restoration(original)
        self.dispose_unstarted()
        self.guard(self.close_plan, "plan descriptor disposition")
        eligible = (
            self.plan_fd_state in ("none", "closed")
            and all(state == "retained_artifact" for state in self.setup_paths.values())
            and all(self.proof(job_id, record_proof(cleanup, job_id)) for job_id in self.slots)
        )
        if eligible:
            for job_id in self.slots:
                self.tighten_startup(job_id)
                self.release_capacity(job_id, record_proof(cleanup, job_id))
            for label, owner, action in (
                ("handoffs", self.handoffs, "close_all"),
                ("hub", self.hub, "stop"),
            ):
                if owner is None:
                    continue
                caps = [
                    s.current_cleanup_cap
                    for s in self.slots.values()
                    if s.current_cleanup_cap is not None
                ]
                if caps:
                    bound = min(caps)
                    old = getattr(owner, "_startup_deadline", None)
                    owner._startup_deadline = min(old, bound) if old is not None else bound
                    owner._startup_clock = self.clock
                try:
                    getattr(owner, action)()
                except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                    self.errors.append(f"{label} disposition: {type(error).__name__}")
                if not owner.startup_disposed:
                    eligible = False
        eligible = (
            eligible
            and not self.errors
            and all(s.capacity_state == "released" for s in self.slots.values())
        )
        if eligible and self.root_context is not None:
            proof = tuple(
                (s.job_id, s.no_spawn_eligible, s.local_disposed, s.custody_state,
                 s.final_wait_complete, s.final_wait_status, s.capacity_state)
                for s in self.slots.values()
            )
            self.terminal_state = "attempting"
            try:
                self.root_context.settle_run(self, proof)
            except BaseException:
                self.terminal_state = "unknown"
                self.root_context.refuse("run terminal proof publication remains unknown")
                raise
            self.terminal_state = "publication_complete"
            if self.root_context.standalone:
                self.terminal_state = "restoration_pending"
                return self.continue_restoration(original)
        if (
            eligible
            and self.owns_adoption
            and self.adoption is not None
            and self.adoption.restoration_pending
        ):
            try:
                self.adoption.__exit__(
                    type(original) if original is not None else None,
                    original,
                    original.__traceback__ if original else None,
                )
            except BaseException as error:  # noqa: BLE001 - retain custody and the first failure
                self.errors.append(f"adoption restoration: {type(error).__name__}")
            # Restoration may suppress its error onto the original exception.
            if self.adoption.restoration_pending:
                eligible = False
        retired = bool(
            eligible
            and not self.errors
            and all(s.capacity_state == "released" for s in self.slots.values())
        )
        if retired and self.retained:
            try:
                _RETAINED_RUNS.remove(self)
            except BaseException:
                self.terminal_state = "unknown"
                if self.root_context is not None:
                    self.root_context.refuse("run terminal membership removal remains unknown")
                raise
            self.retained = False
            self.terminal_state = "published"
        return retired


    def continue_restoration(self, original: BaseException | None = None) -> bool:
        """Only the same original adoption owner may continue after publication."""
        context = self.root_context
        if context is None or not context.standalone or not context.children_quiescent():
            return False
        # No disposal, proof publication, wait, checkpoint or budget creation here.
        try:
            context.close_adoption(original)
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - retain same restore owner
            note = f"adoption restoration: {type(error).__name__}: {error}"
            if note not in self.errors:
                self.errors.append(note)
            if original is not None and error is not original:
                original.add_note(note)
            return False  # report incomplete; only same-owner restoration remains retryable
        if not context.closed:
            return False  # suppressed restoration error remains pending, not permanent poison
        if self.retained:
            try:
                _RETAINED_RUNS.remove(self)
            except BaseException:
                self.terminal_state = "unknown"
                context.refuse("run terminal membership removal remains unknown")
                raise
            self.retained = False
        self.terminal_state = "published"
        return True


def record_proof(cleanup: dict[str, Any], job_id: str) -> bool:
    return cleanup.get(job_id, {}).get("process_tree", {}).get("proof") is True


def start_allocation(
    slot: JobLaunch,
    job: Any,
    *,
    ledger: JobLaunchLedger,
    body: Any,
    plan: Any,
    run_root: Path,
    project_root: Path,
    base_env: Any,
    ranges: Any,
    adoption: Any,
    handoffs: Any,
    hub: Any,
    use_cgroup: bool,
    require_kernel_ownership: bool,
    grace_seconds: float,
    capacity_policy_text: str | None,
    clock: Any,
    wall_clock: Any,
    running: Any,
    checkpoint: Any,
    prepare_tree: Any,
    policy_writer: Any,
    worker_factory: Any,
    cgroup_type: Any,
    ownership_error: Any,
    launcher: Any,
    environment: Any,
    tree_type: Any,
    running_type: Any,
    token_factory: Any,
    stdin: Any,
) -> Any:
    """Actual scheduler start allocation; all authorities are explicit dependencies."""
    tree = prepare_tree(run_root, body["run_id"], slot.job_id, launch=slot)
    slot.tree = tree
    token = token_factory()
    receipt = tree["root"] / "guardian.json"
    if capacity_policy_text is not None:
        path = tree["root"] / "capacity-policy.json"
        slot.file_intent(path)
        policy_writer(path, capacity_policy_text, job["pairs"], job.get("policy_sha256"))
        slot.file_created(path)
    worker_argv = list(worker_factory(plan, slot.job_id, tree, project_root))
    for name in ("child.stdout", "child.stderr"):
        path = tree["root"] / name
        slot.file_intent(path)
        slot.handles.append(open(path, "wb"))  # noqa: SIM115 - retained across constructor failures
        slot.file_created(path)
    slot.clock = clock
    slot.launched_monotonic = clock()
    slot.launched_wall = wall_clock()
    slot.deadline = slot.launched_monotonic + float(job["deadline_seconds"])
    slot.cleanup_cap = slot.deadline + float(body["deadlines"]["cleanup_seconds"])
    checkpoint()
    handoff = handoffs.open(slot.job_id, *ranges[slot.job_id], launch=slot)
    slot.registered = True  # intent precedes register; partial callbacks retain the id
    arbiter_env = hub.register(slot.job_id, job["pairs"], job.get("policy_sha256", ""))
    if use_cgroup:
        slot.cgroup = cgroup_type.create(
            f"pokered-{body['run_id'][:12]}-{slot.job_id}", launch=slot
        )
    else:
        slot.cgroup = cgroup_type(None, "disabled by the caller")
    if require_kernel_ownership and not slot.cgroup.available:
        raise ownership_error(f"kernel ownership is required: {slot.cgroup.reason}")
    checkpoint()
    launcher(
        receipt,
        grace_seconds,
        worker_argv,
        launch=slot,
        cgroup=("!" if require_kernel_ownership else "")
        + (str(slot.cgroup.path) if slot.cgroup.available else ""),
        cwd=project_root,
        env=environment(
            tree,
            base_env,
            project_root=project_root,
            token=token,
            port_range=ranges[slot.job_id],
            deadline_epoch=slot.launched_wall.timestamp() + float(job["deadline_seconds"]),
            job_deadline_monotonic=slot.deadline,
            cleanup_deadline_monotonic=slot.cleanup_cap,
            handoff=handoff,
            arbiter_env=arbiter_env,
        ),
        stdin=stdin,
        stdout=slot.handles[0],
        stderr=slot.handles[1],
        start_new_session=True,
    )
    slot.owned = tree_type.prepare(
        slot.process.pid,
        token,
        clock=clock,
        adoption=adoption,
        status=slot.status,
        known_children=lambda: (
            ledger.wait_owned_children()
            | {
                pid
                for i in running
                for pid in i.owned.tracked
                if all(s.process is None or pid != s.process.pid for s in ledger.slots.values())
            }
        ),
        cgroup=slot.cgroup,
    )
    slot.owned._custody_slot = slot  # root partition visible before first pin
    slot.owned.initialize_original(slot.process, slot)
    slot.item = running_type(
        slot.job_id,
        slot.phase,
        slot.process,
        slot.launched_monotonic,
        slot.deadline,
        tree,
        token,
        slot.owned,
        slot.launched_wall,
        receipt,
        slot.secret,
        slot.handles,
        cleanup_deadline=slot.cleanup_cap,
    )
    slot.item.launch = slot
    slot.owned._custody_slot = slot
    slot.status._custody_slot = slot
    # Complete initialized identity before any callback can enter owner poll paths.
    running.append(slot.item)
    slot.setup_complete = True
    checkpoint()
    return slot.item


def prepare_owned_tree(
    self: Any,
    root_pid: int,
    token: str,
    *,
    clock: Any,
    observe_seconds: float,
    adoption: Any = None,
    status: Any = None,
    known_children: Any = set,
    cgroup: Any = None,
) -> None:
    """Complete plain state before callbacks, queries or descriptor operations."""
    self.root_pid = root_pid
    self.cgroup = cgroup
    self.cgroup_joined = False
    self._known_children = known_children
    self.adoption = adoption
    self.status = status
    self.holder_mode = (
        getattr(status, "holder", None) is not None
    )  # root is a holder, not a guardian
    self.launch_adopting = False
    self.orphans_pinned = 0
    self.unattributed: list[int] = []
    self.unknown_live: list[int] = []
    self._orphans: set[int] = set()
    self.token = token  # attestation only; never used to find or signal a process
    self._clock = clock
    self._observe_seconds = observe_seconds
    self._last = float("-inf")
    self._entries: dict[int, tuple[int, int]] = {}
    self.ports: set[int] = set()
    self.root_start: int | None = None
    self.adopted_total = 0
    self.contained = True
    self.reason = ""


def start_listener(
    owner: Any, socket_factory: Any, thread_factory: Any, target: Any, name: str
) -> Any:
    """Publish startup objects before every fallible configure/start operation."""
    import socket

    if owner._startup_state != "new":
        raise RuntimeError("listener startup cannot be retried")
    owner._startup_state = "allocating"
    owner._server_state = "allocating"
    owner._server = socket_factory(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    owner._server_state = "open"
    owner._server.bind("\0" + owner.name)
    owner._server.listen(16)
    owner._server.settimeout(0.2)
    owner._thread = thread_factory(target=target, name=name, daemon=True)
    owner._thread_state = "attempted"
    try:
        owner._thread.start()
    except BaseException:
        owner._thread_state = "uncertain"
        raise
    owner._thread_state = "started"
    owner._startup_state = "started"
    return owner


def dispose_listener(owner: Any, stopping: Any) -> bool:
    """A close or start exception is not a proved thread/socket disposition."""
    stopping.set()
    if owner._server is not None and owner._server_state == "open":
        owner._server_state = "closing"
        try:
            owner._server.close()
        except BaseException:
            owner._server_state = "unknown"
            raise
        owner._server_state = "closed"
    if owner._thread is not None and owner._thread_state != "quiescent":
        if owner._thread_dispose_attempted:
            return False
        owner._thread_dispose_attempted = True
        if owner._thread_state == "uncertain" and owner._thread.ident is None:
            return False
        timeout = 2.0
        bound = getattr(owner, "_startup_deadline", None)
        if bound is not None:
            timeout = min(timeout, max(0.0, bound - owner._startup_clock()))
        owner._thread.join(timeout=timeout)
        if owner._thread.is_alive():
            return False
        owner._thread_state = "quiescent"
    return owner._server_state in ("none", "closed") and owner._thread_state in (
        "none",
        "quiescent",
    )


WorkerArgvFactory = Callable[[ParallelPlan, str, Mapping[str, Path], Path], Sequence[str]]


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


def write_plan_allocations(
    ledger: JobLaunchLedger, run_root: Path, plan: Any, os_module: Any, loader: Any, error_type: Any
) -> Path:
    """Retain normal-return plan fd/wrapper and explicit setup mkdir results."""
    target = run_root / plan.run_id
    missing = []
    directory = target
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        ledger.setup_paths[directory] = "intent"
        directory.mkdir(mode=0o700, exist_ok=False)
        ledger.setup_paths[directory] = "retained_artifact"
    path = target / "plan.json"
    ledger.setup_paths[path] = "intent"
    ledger.plan_raw_fd = os_module.open(
        path, os_module.O_WRONLY | os_module.O_CREAT | os_module.O_EXCL, 0o600
    )
    ledger.plan_fd_state = "raw"
    ledger.setup_paths[path] = "retained_artifact"
    original = None
    try:
        # fdopen may fail after consuming/closing its argument: no guessed retry.
        ledger.plan_fd_state = "wrapping"
        ledger.plan_handle = os_module.fdopen(ledger.plan_raw_fd, "w", encoding="utf-8")
        ledger.plan_fd_state = "handle"
        ledger.plan_handle.write(plan.text)
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            ledger.close_plan(os_module)
        except BaseException as error:
            ledger.errors.append(f"plan close unproven: {type(error).__name__}")
            if original is None:
                raise
    if loader(path.read_text(encoding="utf-8")).digest != plan.digest:
        raise error_type("plan file read-back differs from the frozen plan")
    return path
