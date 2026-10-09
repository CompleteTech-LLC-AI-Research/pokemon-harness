"""Job-scoped loopback port reservation held until the listener binds (#107).

Concurrent runtime jobs get disjoint port ranges from the orchestrator through
``POKERED_PARALLEL_PORT_BASE/COUNT`` and a private lock directory.  ``reserve_port``
hands out one port of the job's range while holding an exclusive ``flock`` on it for
the life of the allocating process, so the reservation survives until the real
listener binds and two allocators in the same job can never return the same port.
Without the variables nothing changes: callers keep their sequential behaviour.
"""

from __future__ import annotations

import fcntl
import os
import socket
import time
from pathlib import Path

PORT_BASE_ENV = "POKERED_PARALLEL_PORT_BASE"
PORT_COUNT_ENV = "POKERED_PARALLEL_PORT_COUNT"
PORT_LOCK_ENV = "POKERED_PARALLEL_PORT_LOCK_DIR"
HANDOFF_ENV = "POKERED_LISTENER_HANDOFF"
REUSE_AFTER_SECONDS = 60.0
_HELD: dict[int, tuple[int, float]] = {}


class PortLeaseError(RuntimeError):
    """A parallel runtime job asked for a port it cannot hold through its owner."""


def handoff_environment() -> dict[str, str]:
    """The variable a spawned server needs in order to adopt its leased listening socket."""

    value = os.environ.get(HANDOFF_ENV)
    return {HANDOFF_ENV: value} if value else {}


def leased_port() -> int | None:
    """A port whose bound, listening socket the owner keeps until the listener adopts it.

    Outside a parallel job this returns None and callers keep their sequential behaviour.  Inside
    one the owner is required: a number-only reservation (flock, or bind-and-close) cannot stop a
    competing binder in the window before the real listener binds, so there is no such fallback.
    """

    if os.environ.get(HANDOFF_ENV):
        from pokered_harness.link.network_listener_handoff import lease_port

        return lease_port()
    if parallel_range() is not None:
        raise PortLeaseError(
            "a parallel runtime job has no listener owner; refusing a bare port number"
        )
    return None


def parallel_range() -> tuple[int, int, Path] | None:
    base, count, lock = (os.environ.get(n) for n in (PORT_BASE_ENV, PORT_COUNT_ENV, PORT_LOCK_ENV))
    if not (base and count and lock):
        return None
    return int(base), int(count), Path(lock)


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def release_port(port: int) -> None:
    held = _HELD.pop(port, None)
    if held is not None:
        fcntl.flock(held[0], fcntl.LOCK_UN)
        os.close(held[0])


def reserve_port() -> int | None:
    """Return a reserved loopback port of this job's range, or None outside a parallel job."""

    configured = parallel_range()
    if configured is None:
        return None
    base, count, lock_dir = configured
    lock_dir.mkdir(parents=True, exist_ok=True)
    for port in range(base, base + count):
        held = _HELD.get(port)
        if held is not None:
            if time.monotonic() - held[1] < REUSE_AFTER_SECONDS or not _bindable(port):
                continue
            release_port(port)
        descriptor = os.open(lock_dir / f"port-{port}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            continue
        if _bindable(port):
            _HELD[port] = (descriptor, time.monotonic())
            return port
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    raise RuntimeError("no loopback port is left in this job's reserved range")
