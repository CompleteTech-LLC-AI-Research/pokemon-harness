"""Process-local continuing ownership after bounded fixture cleanup refusal.

Strong references retain the exact fixture and its pidfds, adoption obligation,
receiver and runtime directory. There is no background loop or automatic retry.
The same test-process owner must explicitly retry; process death is not cleanup
proof. New nested fixtures are refused while these obligations remain pending.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

_PENDING: dict[int, Any] = {}


def pending_nested_cleanup() -> tuple[Any, ...]:
    return tuple(_PENDING.values())


def unresolved_resources(owner: Any) -> bool:
    try:
        if owner.fds or (owner.thread is not None and owner.thread.is_alive()):
            return True
        if owner.listener is not None and owner.listener.fileno() >= 0:
            return True
        return owner.directory is not None and Path(owner.directory.name).exists()
    except Exception:  # noqa: BLE001 - an unreadable resource is unresolved
        return True


def finish_nested_cleanup(owner: Any, original: BaseException | None = None) -> None:
    if threading.current_thread() is not owner.owner_thread:
        raise RuntimeError("nested cleanup must remain on its original owner thread")
    # Register before any teardown operation can fail. This is retention by the
    # same owner, not an unacknowledged transfer to an imagined worker.
    _PENDING[id(owner)] = owner
    failures: list[BaseException] = []
    try:
        owner._cleanup()
    except BaseException as error:  # noqa: BLE001 - interrupts must not discard allocated ownership
        failures.append(error)
    if unresolved_resources(owner):
        failures.append(AssertionError("outer fixture ownership retained; cleanup is unproven"))
    else:
        try:
            owner.adoption.__exit__()
        except BaseException as error:  # noqa: BLE001 - retain both restoration and original failures
            failures.append(error)
        if not owner.adoption.restoration_pending:
            del _PENDING[id(owner)]
    if failures:
        if original is not None:
            for error in failures:
                original.add_note(f"outer cleanup also failed: {type(error).__name__}: {error}")
            return
        first, *others = failures
        for error in others:
            first.add_note(f"additional cleanup failure: {type(error).__name__}: {error}")
        raise first
