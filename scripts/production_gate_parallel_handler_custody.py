"""Bounded custody of Hub accepted transports, not process/ancillary authority.

The accept thread publishes normal returns; the claimed handler alone uses and
closes its socket. Owner housekeeping only joins completed, normally started
handlers. Ambiguous transfers, starts, joins and closes remain retained.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self


@dataclass
class ConnectionRecord:
    number: int
    raw_result: Any = None
    descriptor: int | None = None
    accept_state: str = "attempted"
    transfer: str = "none"
    connection: Any = None
    thread: Any = None
    start_attempted: bool = False
    start_committed: bool = False
    start_state: str = "none"
    body_complete: bool = False
    done: Any = field(default_factory=threading.Event)
    join_state: str = "none"
    close_state: str = "none"
    unknown: str = ""
    body_error: str = ""


class _AcceptedView:
    """Keep the real _serve context-manager oracle; only this route owns close."""

    def __init__(self, custody: ConnectionCustody, record: ConnectionRecord) -> None:
        self.custody, self.record = custody, record

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            self.custody.close(self.record, handler=True)
        except BaseException as close_error:
            if error is None:
                raise
            error.add_note("accepted transport close unproven: " + type(close_error).__name__)
        return False

    def __getattr__(self, name: str) -> Any:
        if name not in ("settimeout", "getsockopt", "recv", "send"):
            raise AttributeError(name)
        self.custody.require_socket_use(self.record)
        return getattr(self.record.connection, name)


class ConnectionCustody:
    def __init__(self, limit: int, on_loss: Any) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("handler_limit must be a positive integer")
        self.limit, self.on_loss = limit, on_loss
        self.lock = threading.Lock()
        self.records: dict[int, ConnectionRecord] = {}
        self.history: deque[tuple[int, str]] = deque(maxlen=limit)
        self.retired = self.next_number = 0
        self.closing = self.accept_done = self.accept_unknown = False
        self.stop_cap: float | None = None

    def fail(self, record: ConnectionRecord, reason: str, *, accept: bool = False) -> None:
        with self.lock:
            record.unknown = record.unknown or reason
            if accept:
                record.accept_state = "unknown"
                self.accept_unknown = True
        self.on_loss(reason)  # never under the custody lock

    def reserve(self) -> ConnectionRecord | None:
        record = ConnectionRecord(0)  # synchronization construction is outside the lock
        with self.lock:
            if self.closing:
                return None
            full = len(self.records) >= self.limit
            if not full:
                self.next_number += 1
                record.number = self.next_number
                self.records[record.number] = record
        if full:
            self.on_loss("the Hub accepted-handler custody quota is exhausted")
            return None
        return record

    def unreadable(self, record: ConnectionRecord) -> None:
        # Only ordinary readiness timeout/EAGAIN has this no-result branch.
        with self.lock:
            record.accept_state = "empty"
            del self.records[record.number]

    def accepted(self, record: ConnectionRecord) -> None:
        try:
            descriptor, _address = record.raw_result
            if type(descriptor) is not int or descriptor < 0:
                raise ValueError("invalid normally returned accepted descriptor")
            record.descriptor = descriptor
            with self.lock:
                record.accept_state, record.transfer, record.close_state = "returned", "raw", "open"
        except BaseException:
            self.fail(record, "the Hub accept result publication is unproven", accept=True)
            raise

    def wrap(self, record: ConnectionRecord, factory: Any, server: Any) -> None:
        with self.lock:
            record.transfer = "wrapping"
        try:
            record.connection = factory(
                server.family, server.type, server.proto, fileno=record.descriptor
            )
        except BaseException:
            self.fail(record, "the Hub accepted-socket transfer is unproven")
            raise
        with self.lock:
            record.transfer = "wrapped"  # the raw integer is metadata, never a second closer

    def dispatch(self, record: ConnectionRecord, factory: Any, serve: Any) -> None:
        try:
            record.thread = factory(
                target=lambda: self.run(record, serve),
                name=f"arbiter-handler-{record.number}",
                daemon=True,
            )
        except BaseException:
            self.close(record, handler=False)
            raise
        with self.lock:
            permitted = not self.closing
            if permitted:
                # One winner against begin_stop; physical start is outside this lock.
                record.start_attempted, record.start_state = True, "claimed"
        if not permitted:
            self.close(record, handler=False)
            return
        try:
            record.thread.start()
        except BaseException:
            with self.lock:
                record.start_state = "unknown"
            self.fail(record, "the Hub accepted-handler start is unproven")
            raise
        with self.lock:
            record.start_committed = True
            if record.start_state == "claimed":
                record.start_state = "started"
            # Do not overwrite a fast handler's completion/close or unknown facts.

    def require_socket_use(self, record: ConnectionRecord) -> None:
        with self.lock:
            permitted = (
                record.start_attempted
                and record.transfer == "wrapped"
                and record.close_state == "open"
                and not record.body_complete
            )
        if not permitted:
            raise RuntimeError("accepted transport has no active handler capability")

    def run(self, record: ConnectionRecord, serve: Any) -> None:
        primary = None
        try:
            serve(_AcceptedView(self, record))
        except BaseException as error:
            primary = error
            with self.lock:
                record.body_error = type(error).__name__
            raise
        finally:
            try:
                self.close(record, handler=True)
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note("accepted transport close unproven: " + type(error).__name__)
            finally:
                with self.lock:
                    record.body_complete = True
                record.done.set()  # notification only, never thread-quiescence proof

    def close(self, record: ConnectionRecord, *, handler: bool) -> None:
        with self.lock:
            if record.close_state != "open":
                return  # closing/unknown never receives another effect
            permitted = (
                record.transfer in ("raw", "wrapped")
                and (record.start_attempted if handler else not record.start_attempted)
                and (record.transfer == "wrapped" if handler else not record.unknown)
            )
            if permitted:
                record.close_state = "closing"
        if not permitted:
            return
        try:
            if record.transfer == "wrapped":
                record.connection.close()
            else:
                os.close(record.descriptor)
        except BaseException:
            with self.lock:
                record.close_state = "unknown"
            self.fail(record, "the Hub accepted-transport close is unproven")
            raise
        with self.lock:
            record.close_state = "closed"
            if not record.start_attempted and not record.unknown:
                self._retire_locked(record, "never_started")

    def _retire_locked(self, record: ConnectionRecord, reason: str) -> None:
        self.records.pop(record.number)
        self.retired += 1
        self.history.append((record.number, reason))

    def begin_stop(self, now: float, bound: float | None) -> float:
        with self.lock:
            self.closing = True
            if self.stop_cap is None:
                self.stop_cap = now + 2.0
            if bound is not None:
                self.stop_cap = min(self.stop_cap, bound)
            return self.stop_cap

    def complete_accept(self) -> None:
        with self.lock:
            self.accept_done = True

    def accept_clear(self) -> bool:
        with self.lock:
            return not self.accept_unknown and not (
                self.accept_done
                and any(r.accept_state == "attempted" for r in self.records.values())
            )

    def disposed(self) -> bool:
        with self.lock:
            return self.closing and not self.accept_unknown and not self.records

    def housekeep(self, *, clock: Any = None, cap: float | None = None) -> None:
        with self.lock:
            records = tuple(self.records.values())
        for record in records:
            done = record.done.is_set()
            with self.lock:
                permitted = (
                    done
                    and record.body_complete
                    and record.start_committed
                    and record.start_state == "started"
                    and record.close_state == "closed"
                    and not record.unknown
                    and record.join_state in ("none", "pending")
                )
                if permitted:
                    record.join_state = "joining"
            if not permitted:
                continue
            try:
                timeout = 0.0 if cap is None else max(0.0, cap - clock())
                record.thread.join(timeout=timeout)
                alive = record.thread.is_alive()
            except BaseException:
                with self.lock:
                    record.join_state = "unknown"
                self.fail(record, "the Hub accepted-handler join is unproven")
                raise
            with self.lock:
                record.join_state = "pending" if alive else "quiescent"
                if not alive:
                    self._retire_locked(record, "joined")


def stop_hub(hub: Any, dispose_listener: Any) -> None:
    """Join the exact accept Thread before listener close; no renewed stop cap."""
    custody = hub._connections
    clock = getattr(hub, "_startup_clock", hub._clock)
    cap = custody.begin_stop(clock(), getattr(hub, "_startup_deadline", None))
    hub._stopping.set()
    primary = None
    try:
        if not custody.accept_clear():
            return
        if hub._thread is None and hub._thread_state == "none":
            dispose_listener(hub, hub._stopping)  # positive never-started: no join
            return
        if hub._thread_state != "quiescent":
            if hub._thread_state != "started" or hub._thread_dispose_attempted:
                return
            hub._thread_dispose_attempted = True
            try:
                hub._thread.join(timeout=max(0.0, cap - clock()))
                alive = hub._thread.is_alive()
                with custody.lock:
                    complete = (
                        custody.accept_done
                        and not custody.accept_unknown
                        and all(
                            r.accept_state not in ("attempted", "unknown")
                            for r in custody.records.values()
                        )
                    )
            except BaseException:
                hub._thread_state = "uncertain"
                hub.lose("the Hub accept-thread join is unproven")
                raise
            if alive or not complete:
                return
            hub._thread_state = "quiescent"
        dispose_listener(hub, hub._stopping)  # sees quiescent: closes server, no second join
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            custody.housekeep(clock=clock, cap=cap)
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f"Hub handler housekeeping also failed: {error}")
