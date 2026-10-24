"""Root-local listener/offer custody; no authority over exported recipient copies."""

from __future__ import annotations

import math
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self


class Refused(OSError):
    """A known refusal without an ambiguous effect; not a retry authorization."""


@dataclass
class Operation:
    number: int
    cap: float | None = None
    unknown: str = ""


@dataclass
class Lease:
    number: int
    candidate: int
    port: int | None = None
    sock: Any = None
    state: str = "intent"
    close_state: str = "none"
    actor: int | None = None
    unknown: str = ""
    note: str = ""
    exported: bool = False
    sent: Any = None


class HandoffView:
    """The SAME accepted record and closer, with handoff-only sendmsg capability."""

    def __init__(self, pool: Any, record: Any, book: Any, operation: Operation) -> None:
        self.pool, self.record, self.book, self.operation = pool, record, book, operation

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            self.pool.close(self.record, handler=True)
        except BaseException as close_error:
            if error is None:
                raise
            error.add_note("handoff connection close unproven: " + type(close_error).__name__)
        return False

    def __getattr__(self, name: str) -> Any:
        if name not in ("settimeout", "getsockopt", "recv", "send", "sendmsg"):
            raise AttributeError(name)

        def effect(*args: Any, **kwargs: Any) -> Any:
            self.pool.require_socket_use(self.record)
            remaining = self.book.remaining(self.operation)
            sock = self.record.connection
            if name == "settimeout":
                return sock.settimeout(min(args[0], remaining))
            sock.settimeout(remaining)
            return getattr(sock, name)(*args, **kwargs)

        return effect


class HandoffBook:
    """One lock with the accepted start/stop claim; all effects happen outside it."""

    def __init__(
        self, pool: Any, limit: int, clock: Any, bound: Any, loss: Any, timeout: float
    ) -> None:
        if type(limit) is not int or limit < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("finite positive handoff limits are required")
        self.pool, self.lock, self.limit = pool, pool.lock, limit
        self.clock, self.bound, self.loss, self.timeout = clock, bound, loss, timeout
        self.operations: dict[int, Operation] = {}
        self.leases: dict[int, Lease] = {}
        self.available: dict[int, Lease] = {}
        self.candidates: set[int] = set()
        self.history: deque[dict[str, Any]] = deque(maxlen=limit)
        self.ordinal = 0
        self.first_reason = ""

    def lose(self, reason: str) -> bool:
        with self.lock:
            first = not self.first_reason
            self.first_reason = self.first_reason or reason
            self.pool.closing = True
        return first

    def fault(
        self,
        operation: Operation,
        reason: str,
        lease: Lease | None = None,
        *,
        primary: BaseException | None = None,
    ) -> None:
        with self.lock:
            operation.unknown = operation.unknown or reason
            if lease is not None:
                lease.unknown = lease.unknown or reason
        self.notify(reason, primary)

    def notify(self, reason: str, primary: BaseException | None = None) -> None:
        try:
            self.loss(reason)
        except BaseException as secondary:
            if primary is None:
                raise
            primary.add_note("handoff notification also escaped: " + type(secondary).__name__)

    @contextmanager
    def operation(self, existing: Operation | None = None, *, cap: float | None = None) -> Any:
        if existing is not None:
            with self.lock:
                valid = self.operations.get(existing.number) is existing and not existing.unknown
            if not valid:
                raise Refused("handoff operation is no longer usable")
            self.remaining(existing)
            yield existing
            return
        with self.lock:
            refused = self.pool.closing
            full = len(self.operations) >= self.pool.limit
            if not refused and not full:
                self.ordinal += 1
                operation = Operation(self.ordinal)
                self.operations[operation.number] = operation
        if refused or full:
            if full:
                self.loss("handoff operation quota exhausted")
            raise Refused("handoff admission is closed")
        try:
            now, bound = self.clock(), self.bound()
            original = now + self.timeout if cap is None else min(cap, now + self.timeout)
            operation.cap = original if bound is None else min(original, bound)
            self.remaining(operation)
            yield operation
        except Refused:
            raise
        except BaseException as error:
            self.fault(
                operation, "handoff operation unproven: " + type(error).__name__, primary=error
            )
            raise
        finally:
            with self.lock:
                if not operation.unknown:
                    del self.operations[operation.number]

    def remaining(self, operation: Operation) -> float:
        now, bound = self.clock(), self.bound()
        with self.lock:
            if self.operations.get(operation.number) is not operation or operation.unknown:
                raise Refused("handoff operation is no longer usable")
            cap = operation.cap
            if bound is not None:
                cap = bound if cap is None else min(cap, bound)
            if self.pool.stop_cap is not None:
                cap = self.pool.stop_cap if cap is None else min(cap, self.pool.stop_cap)
            operation.cap = cap
        if cap is None or now >= cap:
            raise TimeoutError("the original handoff transaction bound expired")
        return cap - now

    def reserve(self, operation: Operation, candidate: int) -> Lease | None:
        with self.lock:
            closed = self.pool.closing
            full = len(self.leases) >= self.limit
            duplicate = candidate != 0 and (
                candidate in self.candidates or candidate in self.available
            )
            if not closed and not full and not duplicate:
                self.ordinal += 1
                lease = Lease(self.ordinal, candidate, actor=operation.number)
                self.leases[lease.number] = lease
                if candidate:
                    self.candidates.add(candidate)
        if closed:
            raise Refused("handoff lease admission is closed")
        if full:
            self.loss("handoff lease quota exhausted")
            raise Refused("handoff lease quota exhausted")
        return None if duplicate else lease

    def publish(self, lease: Lease, port: int) -> bool:
        with self.lock:
            lease.port = port
            self.candidates.discard(lease.candidate)
            if self.pool.closing:
                lease.state = "cancelled"
                return False
            lease.state, lease.actor = "leased", None
            self.available[port] = lease
            return True

    def claim(self, port: object, operation: Operation, state: str) -> Lease | None:
        with self.lock:
            lease = self.available.get(port) if type(port) is int else None
            if self.pool.closing or lease is None or lease.state != "leased":
                return None
            del self.available[port]
            lease.state, lease.actor = state, operation.number
            return lease

    def mark(self, lease: Lease, state: str) -> None:
        with self.lock:
            lease.state = state

    def close_lease(self, lease: Lease, operation: Operation | None = None) -> None:
        if operation is not None:
            self.remaining(operation)
        with self.lock:
            if lease.close_state != "open":
                return
            permitted = (
                lease.actor == operation.number
                if operation is not None
                else not self.operations and not self.pool.records and self.pool.closing
            )
            permitted = permitted and not lease.unknown
            if operation is not None:
                permitted = permitted and not operation.unknown
            if not permitted:
                return
            self.available.pop(lease.port, None)
            self.candidates.discard(lease.candidate)
            lease.close_state = "closing"
        try:
            # Last min-only check precedes the close effect, outside the custody lock.
            if operation is not None:
                self.remaining(operation)
            else:
                now, bound = self.clock(), self.bound()
                caps = [x for x in (bound, self.pool.stop_cap) if x is not None]
                if caps and now >= min(caps):
                    raise TimeoutError("original listener disposal bound expired")
            lease.sock.close()
        except BaseException as error:
            with self.lock:
                lease.close_state = "unknown"
                lease.unknown = lease.unknown or "root listener close unproven"
            self.notify("root listener close unproven", error)
            raise
        with self.lock:
            lease.close_state = "closed"

    def retire(self, lease: Lease) -> None:
        with self.lock:
            if lease.close_state == "closed" and not lease.unknown:
                self.history.append(self.metadata(lease))
                self.leases.pop(lease.number)

    @staticmethod
    def metadata(lease: Lease) -> dict[str, Any]:
        return {
            "port": lease.port,
            "state": lease.state,
            "note": lease.note,
            "close_state": lease.close_state,
            "unknown": lease.unknown,
            "exported": lease.exported,
        }

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.history] + [
                self.metadata(x) for x in self.leases.values()
            ]

    def state(self, port: int) -> str | None:
        rows = self.snapshot()
        return next(
            (
                "unacknowledged" if row["unknown"] and row["exported"] else str(row["state"])
                for row in reversed(rows)
                if row["port"] == port
            ),
            None,
        )

    def dispose_idle(self, broker: Any) -> None:
        with self.lock:
            eligible = (
                self.pool.closing
                and not self.pool.records
                and not self.operations
                and broker._thread_state in ("none", "quiescent")
                and broker._server_state in ("none", "closed")
                and not self.pool.accept_unknown
            )
            leases = tuple(self.leases.values()) if eligible else ()
        for lease in leases:
            if self.pool.stop_cap is not None and self.clock() >= self.pool.stop_cap:
                return
            self.close_lease(lease)
            self.retire(lease)

    def reason(self) -> str:
        with self.lock:
            return self.first_reason

    def disposed(self) -> bool:
        with self.lock:
            return not self.leases and not self.operations
