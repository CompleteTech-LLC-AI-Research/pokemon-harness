"""Python-side ownership shared by raw providers and managed Sessions.

Native PyBoy objects need not support attributes, weak references, or hashing.
Weak registry values are Python owner records; live Sessions/providers/scopes
retain their records, which in turn keep emulator identities alive.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager


class EmulatorOwnershipError(RuntimeError):
    """An emulator cannot safely enter the requested ownership scope."""


_registry_lock = threading.Lock()
_owners: weakref.WeakValueDictionary[int, EmulatorOwner] = weakref.WeakValueDictionary()
_thread_owners = threading.local()


def assert_no_emulator_scope(operation: str) -> None:
    """Reject reverse entry into an outer operation lock before waiting."""
    if getattr(_thread_owners, "held", ()):
        raise EmulatorOwnershipError(
            f"{operation} must be called outside an emulator ownership scope"
        )


class EmulatorOwner:
    """The Python identity, lock, and lifecycle claims for one emulator."""

    def __init__(self, pyboy: object) -> None:
        self.pyboy = pyboy
        self.order = id(pyboy)
        self.lock = threading.RLock()
        self.execution = threading.local()
        self.closed = False
        self._closed_error: type[Exception] = EmulatorOwnershipError
        self._session: weakref.ReferenceType | None = None
        self._provider: weakref.ReferenceType | None = None
        self._metadata_lock = threading.Lock()

    def assert_lock_order(self) -> None:
        """Reject a reverse single-owner acquisition before it can deadlock."""
        if getattr(self.execution, "depth", 0):
            return
        held = getattr(_thread_owners, "held", ())
        if any(owner.order > self.order for owner in held):
            raise EmulatorOwnershipError(
                "emulator locks must be acquired in canonical order; "
                "use the ordered group scope"
            )

    @contextmanager
    def access(self, *, timeout: float | None = None) -> Iterator[None]:
        """Acquire this emulator's canonical lock and mark execution depth.

        ``timeout`` is used by bounded Session teardown/compound operations.
        The default deliberately keeps the lock's ordinary context-manager
        behavior so existing instrumented test locks remain observable.
        """
        self.assert_lock_order()
        if timeout is None:
            # Preserve the context-manager protocol used by instrumented
            # provider/session locks; some test and adapter locks intentionally
            # expose only ``__enter__``/``__exit__``.
            with self.lock, self._mark_scope():
                yield
            return
        acquire = getattr(self.lock, "acquire", None)
        if not callable(acquire):
            # A context-manager-only lock cannot honor a deadline.  Entering
            # it here would turn a bounded owner scope into an unbounded
            # wait, which can strand link teardown or a compound MCP call.
            raise EmulatorOwnershipError(
                "emulator ownership lock does not support bounded acquisition"
            )
        acquired = acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError("emulator ownership lock acquisition timed out")
        try:
            with self._mark_scope():
                yield
        finally:
            self.lock.release()

    @contextmanager
    def _mark_scope(self) -> Iterator[None]:
        """Track one already-acquired owner lock on the current thread."""
        depth = getattr(self.execution, "depth", 0)
        held = getattr(_thread_owners, "held", ())
        self.execution.depth = depth + 1
        if not depth:
            _thread_owners.held = held + (self,)
        try:
            yield
        finally:
            self.execution.depth = depth
            if not depth:
                _thread_owners.held = held

    def ensure_open(self) -> None:
        if self.closed or getattr(self.pyboy, "stopped", False) is True:
            raise self._closed_error("emulator session is closed")

    def bind_session(self, session: object, closed_error: type[Exception]) -> None:
        """Claim the managed Session wrapper, exactly once per emulator."""
        # Do not introduce a new wrapper halfway through a raw owner callback.
        if getattr(self.execution, "depth", 0):
            raise EmulatorOwnershipError(
                "cannot create a Session during emulator execution"
            )
        with self.access():
            if self._session is not None and self._session() is not None:
                raise EmulatorOwnershipError("emulator already has a live Session owner")
            self.ensure_open()
            self._session = weakref.ref(session)
            self._closed_error = closed_error

    def claim_provider(self, provider: object) -> None:
        """Claim the raw provider slot without entering emulator execution."""
        # Metadata-only lock: no emulator wait or network operation under it.
        with self._metadata_lock:
            current = self._provider() if self._provider is not None else None
            if current is not None and current is not provider:
                raise EmulatorOwnershipError(
                    "emulator is already attached to another provider"
                )
            self._provider = weakref.ref(provider)

    def release_provider(self, provider: object) -> None:
        with self._metadata_lock:
            if self._provider is not None and self._provider() is provider:
                self._provider = None


def owner_for(pyboy: object) -> EmulatorOwner:
    """Return the live identity association without inspecting native layout."""
    key = id(pyboy)
    with _registry_lock:
        owner = _owners.get(key)
        if owner is None:
            owner = EmulatorOwner(pyboy)
            _owners[key] = owner
        elif owner.pyboy is not pyboy:  # identity reuse must never alias owners
            raise EmulatorOwnershipError("emulator ownership identity mismatch")
        return owner


@contextmanager
def owner_group(
    owners: Iterable[EmulatorOwner],
    *,
    allow_closed: bool = False,
    timeout: float | None = None,
) -> Iterator[None]:
    """Acquire a canonical group before any provider operation lock."""
    ordered = sorted(
        {owner.order: owner for owner in owners}.values(),
        key=lambda item: item.order,
    )
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    with ExitStack() as stack:
        for owner in ordered:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            stack.enter_context(owner.access(timeout=remaining))
        if not allow_closed:
            for owner in ordered:
                owner.ensure_open()
        yield
