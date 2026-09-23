"""Timed-owner policy, status and task helpers.

Split from ``pokered_harness.mcp_server`` for issue #125 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on ``pokered_harness.mcp_server`` stay visible.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from pokered_harness.mcp_server_model import LinkState, McpHarnessError
from pokered_harness.mcp_server_remote_contract import (
    _optional_rom_version,
    _positive_port,
    _require_primary_rom_version,
    _validate_remote_host,
    _validate_rom_version,
    _validate_timeout,
)
from pokered_harness.mcp_server_tools import _dispatch_session_tool
from pokered_harness.mcp_timed_owner import TimedOwner, TimedOwnerPolicy
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import Session

# -- server wiring -----------------------------------------------------------


_TIMED_INTEGER_FIELDS = (
    "rearm_budget",
    "rearm_instruction_cap",
    "max_edge_lateness",
    "quantum_cycles",
    "max_wait_attempts",
    "inbound_capacity",
    "queue_capacity",
)


_TIMED_TIMEOUT_FIELDS = (
    "operation_timeout",
    "request_timeout",
    "lock_timeout",
    "close_timeout",
)


def load_timed_policy_from_env() -> TimedOwnerPolicy | None:
    """Select timed transport explicitly; no policy value has an implicit default.

    Set POKERED_LINK_TRANSPORT=timed and POKERED_TIMED_<FIELD> for every
    TimedOwnerPolicy field. Cycle/count fields are integers; timeout fields
    are finite positive seconds. Legacy remains the default transport.
    """
    mode = os.environ.get("POKERED_LINK_TRANSPORT", "legacy").strip().lower()
    if mode not in {"legacy", "timed"}:
        raise McpHarnessError(
            "invalid_timed_configuration",
            "POKERED_LINK_TRANSPORT must be legacy or timed",
        )
    fields = _TIMED_INTEGER_FIELDS + _TIMED_TIMEOUT_FIELDS
    values = {field: os.environ.get(f"POKERED_TIMED_{field.upper()}") for field in fields}
    if mode == "legacy":
        if any(value is not None for value in values.values()):
            raise McpHarnessError(
                "invalid_timed_configuration",
                "POKERED_TIMED_* policy requires POKERED_LINK_TRANSPORT=timed",
            )
        return None
    missing = [
        f"POKERED_TIMED_{field.upper()}"
        for field, value in values.items()
        if value is None or not value.strip()
    ]
    if missing:
        raise McpHarnessError(
            "invalid_timed_configuration", "missing explicit timed policy: " + ", ".join(missing)
        )
    try:
        parsed = {
            field: (int(value) if field in _TIMED_INTEGER_FIELDS else float(value))
            for field, value in values.items()
        }
        return TimedOwnerPolicy(**parsed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise McpHarnessError("invalid_timed_configuration", str(exc)) from exc


def _timed_status_payload(snapshot: Any) -> dict[str, Any]:
    # The owner snapshot is immutable and contains no foreign-thread reads.
    def thaw(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [thaw(item) for item in value]
        return to_jsonable(value)

    payload = thaw(snapshot)
    payload["transport"] = "timed"
    payload["mode"] = payload.get("state", "unknown")
    payload["remote_mode"] = payload["mode"]
    payload["primary_tick"] = payload.get("tick")
    payload["peer_tick"] = None
    payload["paired"] = False
    payload["link_backend"] = "timed"
    payload["remote_bind_port"] = payload.get("port")
    payload["remote_peer_rom_version"] = payload.get("peer_rom_version")
    payload["remote_error"] = payload.get("error")
    payload["remote_role"] = payload.get("role")
    return payload


def _timed_status(owner: TimedOwner) -> dict[str, Any]:
    return _timed_status_payload(owner.status())


def _timed_tool_request(
    owner: TimedOwner,
    session: Session,
    name: str,
    args: dict[str, Any],
    link: LinkState | None,
) -> Any:
    if owner.session is not session:
        raise McpHarnessError(
            "invalid_timed_configuration", "timed owner belongs to another session"
        )
    if name in {"link_listen", "link_connect"}:
        version = _validate_rom_version(
            args.get("rom_version", link.primary_version if link else "red")
        )
        if link is not None:
            _require_primary_rom_version(link, version)
        peer_version = _optional_rom_version(args.get("peer_rom_version"))
        deadline = None
        if "timeout_s" in args:
            deadline = time.monotonic() + _validate_timeout(args["timeout_s"])
        host = _validate_remote_host(args.get("host", "127.0.0.1"))
        if host == "localhost":
            host = "127.0.0.1"
        port = _positive_port(args["port"])
        operation = owner.listen if name == "link_listen" else owner.connect
        return operation(host, port, version, deadline=deadline, peer_rom_version=peer_version)
    if name == "link_disconnect":
        return owner.disconnect()
    if name == "link_step":

        def step(owned: Session) -> Any:
            if owner.status()["state"] != "connected":
                raise McpHarnessError(
                    "timed_not_connected", "link_step requires a timed connection"
                )
            result = _dispatch_session_tool(owned, "step", args)
            return {"primary_tick": result["tick"], "peer_tick": None}

        return owner.submit(step)
    if name.startswith("link_"):
        raise McpHarnessError(
            "timed_unsupported_tool", f"{name} is not supported by timed remote transport"
        )
    return owner.submit(lambda owned: _dispatch_session_tool(owned, name, args))


async def _await_owner_request(request: Any, *, ready: bool = False) -> Any:
    """Isolate asyncio cancellation while the owner retains the queue outcome."""
    deadline = asyncio.timeout(max(0.0, request.deadline - time.monotonic()))
    try:
        async with deadline:
            return await (request.wait_ready() if ready else request.wait())
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        request.cancel()
        # Deadline cancellation can race the owner publishing a terminal
        # protocol failure. The shielded canonical future retains that reason;
        # preserve its identity instead of replacing it with a wrapper timeout.
        if request.future.done() and not request.future.cancelled():
            terminal = request.future.exception()
            if terminal is not None and getattr(terminal, "code", None) != "timed_cancelled":
                raise terminal
        raise McpHarnessError("timed_deadline", "timed owner request deadline expired") from exc
    except asyncio.CancelledError:
        request.cancel()
        raise


async def _shutdown_timed_owner(
    owner: TimedOwner, policy: TimedOwnerPolicy, tasks: _McpTaskRegistry
) -> None:
    """Cancel execution out of band; detach only through the owner queue."""
    deadline = time.monotonic() + policy.close_timeout
    owner.cancel()
    owner.disconnect()

    def join_owner() -> bool:
        remaining = deadline - time.monotonic()
        return remaining > 0 and owner.close(timeout=remaining)

    # Only the bounded join uses the executor. Session access, cancellation
    # recovery, endpoint detach and Session.close remain on the native owner.
    worker = tasks.start(join_owner)
    try:
        closed = await asyncio.wait_for(
            asyncio.shield(worker), max(0.0, deadline - time.monotonic())
        )
    except TimeoutError:
        closed = False
    if not closed:
        raise McpHarnessError(
            "timed_cleanup_pending", "timed owner retained pending cleanup; session remains owned"
        )


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve a late task outcome after its owner timed out or cancelled."""
    if task.cancelled():
        return
    task.exception()


class _McpTaskRegistry:
    """Track thread-backed request work until it has actually finished.

    Cancelling an asyncio task does not stop the executor thread created by
    :func:`asyncio.to_thread`.  Keeping those tasks in a server-owned registry
    gives the stdio shutdown path a final opportunity to wake remote work and
    wait for the emulator operation before the owning session is closed.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def start(self, operation: Callable[[], Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(asyncio.to_thread(operation))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        _consume_task_exception(task)

    async def wait_until(self, deadline: float) -> bool:
        """Wait for all tracked work without cancelling executor tasks."""
        while True:
            pending = tuple(task for task in self._tasks if not task.done())
            if not pending:
                return True
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            if remaining <= 0.0:
                return False
            await asyncio.wait(pending, timeout=remaining)


async def _wait_task_until(
    task: asyncio.Task[Any],
    deadline: float,
) -> bool:
    """Wait for a task without cancelling it, bounded by ``deadline``."""
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    if remaining <= 0.0:
        task.add_done_callback(_consume_task_exception)
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except TimeoutError:
        # ``shield`` deliberately leaves the thread-backed task alive. It
        # still owns the emulator operation and must be allowed to settle;
        # consume its eventual result so a late exception is not reported as
        # an unhandled asyncio task failure.
        task.add_done_callback(_consume_task_exception)
        return False
    except BaseException:  # noqa: BLE001 - cancellation cleanup is best effort
        return True
    return True


async def _await_blocking_task(
    task: asyncio.Task[Any],
    *,
    task_registry: _McpTaskRegistry,
    cancel_cleanup: Callable[[], Any] | None,
) -> Any:
    """Await a thread-backed request while retaining cancellation ownership."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cleanup_deadline = asyncio.get_running_loop().time() + _entry._DEFAULT_CLEANUP_TIMEOUT_S
        if cancel_cleanup is not None:
            cleanup = task_registry.start(cancel_cleanup)
            await _wait_task_until(cleanup, cleanup_deadline)
        await _wait_task_until(task, cleanup_deadline)
        raise


def _close_sessions_independently(
    peer_session: Session | None,
    session: Session | None,
) -> list[tuple[str, Exception]]:
    """Close each owned session even when another close attempt fails."""
    errors: list[tuple[str, Exception]] = []
    for role, target in (
        ("peer", peer_session),
        ("primary", session),
    ):
        if target is None:
            continue
        try:
            target.close(timeout_s=_entry._DEFAULT_CLEANUP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - preserve independent cleanup
            errors.append((role, exc))
    return errors


# Call-time indirection so facade-level monkeypatches stay visible here.
import pokered_harness.mcp_server as _entry
