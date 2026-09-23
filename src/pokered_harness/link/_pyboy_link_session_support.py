"""Private support types and helpers for :mod:`pyboy_link_session`.

Extracted verbatim from ``pyboy_link_session.py`` (#136): the ``_PyBoyLike``
protocol, the ``PairedSessionOperationError`` type, the bounded provider-lock
and timeout helpers, and the ``_serialized_local_operation`` decorator.
"""

from __future__ import annotations

import inspect
import time
from contextlib import contextmanager
from functools import wraps
from typing import Protocol, runtime_checkable

from pokered_harness.ownership import EmulatorOwnershipError, owner_for, owner_group


@runtime_checkable
class _PyBoyLike(Protocol):  # noqa: PYI046 - protocol shared across the link-session mixins
    """Minimal duck-type the session needs from a PyBoy instance.

    Real :class:`pyboy.PyBoy` satisfies this. A test fake need only
    expose a ``mb`` with a ``serial`` attribute (carrying a
    settable ``backend``) and a ``tick`` method.
    """

    mb: object

    def tick(self, count: int = 1, render: bool = True, sound: bool = False) -> bool: ...


def _validate_positive_int(value: int, name: str) -> int:
    """Validate a public count argument before doing any work."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class PairedSessionOperationError(RuntimeError):
    code = "paired_session_operation"


@contextmanager
def _bounded_provider_lock(lock, deadline):
    if deadline is None:
        with lock:
            yield
        return
    if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("provider lock did not become available before cleanup deadline")
    try:
        yield
    finally:
        lock.release()


def _call_with_timeout(function, timeout_s: float):
    """Call a lifecycle hook only when it accepts a bounded deadline.

    Network teardown is a safety boundary: a no-argument ``stop`` or
    ``detach_local_core`` cannot prove that cleanup will return.  The native
    NetworkBackend exposes ``timeout_s`` on both hooks, and adapters must
    implement that same bounded contract before an emulator is attached.
    """
    if not _accepts_timeout(function):
        raise TypeError("network lifecycle hook must expose timeout_s")
    return function(timeout_s=timeout_s)


def _accepts_timeout(function) -> bool:
    try:
        # Bind the exact call shape: a positional-only timeout parameter or
        # another required argument is not a usable bounded lifecycle hook.
        inspect.signature(function).bind(timeout_s=0.0)
    except (TypeError, ValueError):
        return False
    return True


def _serialized_local_operation(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        # Snapshot membership without taking the provider lock first. Every
        # native access uses canonical emulator owners BEFORE provider state.
        members = tuple(self._pyboys)
        endpoints = members
        if function.__name__ == "attach":
            endpoint = args[0] if args else kwargs["pyboy"]
            endpoints += (endpoint,)
        cleanup = function.__name__ in {"detach", "detach_all"}
        previous_deadline = getattr(self._cleanup_state, "deadline", None)
        deadline = previous_deadline
        if cleanup and deadline is None:
            deadline = time.monotonic() + 2.0
        network_generation = (
            self._network_cancellation_generation() if self._network_backend is not None else None
        )
        previous_network_cancelled = getattr(self._cleanup_state, "network_cancelled", False)
        try:
            if cleanup:
                self._cleanup_state.deadline = deadline
                if self._network_backend is not None:
                    # Validate before publishing transport cancellation.  If
                    # an adapter was replaced or lost its bounded detach
                    # hook, teardown must leave both transport and emulator
                    # references untouched for a later repair/retry.
                    self._require_network_lifecycle_contract()
                if self._network_backend is not None and not previous_network_cancelled:
                    # Transport cancellation is deliberately outside the
                    # emulator owner group.  A raw network tick can be
                    # blocked in native serial code while holding that
                    # owner; stopping the transport is the wake-up that lets
                    # it unwind before backend/core restoration.
                    self._cancel_network_transport(deadline)
                    self._cleanup_state.network_cancelled = True
            with (
                owner_group(
                    (owner_for(endpoint) for endpoint in endpoints),
                    allow_closed=cleanup,
                    timeout=None if deadline is None else max(0.0, deadline - time.monotonic()),
                ),
                _bounded_provider_lock(self._operation_lock, deadline),
            ):
                if len(members) != len(self._pyboys) or any(
                    before is not after for before, after in zip(members, self._pyboys)
                ):
                    raise EmulatorOwnershipError("provider membership changed; retry the operation")
                result = function(self, *args, **kwargs)
                if (
                    network_generation is not None
                    and function.__name__ in {"step", "step_interleaved"}
                    and network_generation != self._network_cancellation_generation()
                ):
                    raise EmulatorOwnershipError(
                        "network emulator operation was cancelled by cleanup"
                    )
                return result
        finally:
            if cleanup:
                self._cleanup_state.deadline = previous_deadline
                self._cleanup_state.network_cancelled = previous_network_cancelled

    return call
