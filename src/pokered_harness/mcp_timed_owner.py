"""Persistent native owner for explicitly configured timed MCP execution.

There are three persistent threads: native owner, deadline supervisor, and
cancellation worker. Blocking cancellation never occupies the supervisor.
No request allocates a thread. Status is an immutable, possibly stale observation;
reading it never enters the emulator. Callables receive the owned Session.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import select
import socket
import threading
import time
from collections import deque
from concurrent.futures import Future, InvalidStateError
from dataclasses import asdict, dataclass, is_dataclass
from types import MappingProxyType

from .link.timed_remote import TimedRemoteEndpoint
from .link.timed_wire import ProtocolError


class TimedOwnerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TimedOwnerPolicy:
    rearm_budget: int
    rearm_instruction_cap: int
    max_edge_lateness: int
    quantum_cycles: int
    operation_timeout: float
    max_wait_attempts: int
    inbound_capacity: int
    queue_capacity: int
    request_timeout: float
    lock_timeout: float
    close_timeout: float

    def __post_init__(self):
        for name in (
            "rearm_budget",
            "rearm_instruction_cap",
            "max_edge_lateness",
            "quantum_cycles",
            "max_wait_attempts",
            "inbound_capacity",
            "queue_capacity",
        ):
            value = getattr(self, name)
            minimum = 0 if name in ("rearm_budget", "max_edge_lateness") else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name in ("operation_timeout", "request_timeout", "lock_timeout", "close_timeout"):
            _positive(getattr(self, name), name)

    def endpoint_options(self):
        return {
            name: getattr(self, name)
            for name in (
                "rearm_budget",
                "rearm_instruction_cap",
                "max_edge_lateness",
                "quantum_cycles",
                "operation_timeout",
                "max_wait_attempts",
                "inbound_capacity",
            )
        }


def _positive(value, name):
    if (
        type(value) not in (float, int)
        or not math.isfinite(value)
        or not 0 < value <= threading.TIMEOUT_MAX
    ):
        raise ValueError(f"{name} must be finite, positive and <= threading.TIMEOUT_MAX")
    return float(value)


def _freeze(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _freeze(asdict(value))
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _settle(future, value=None, error=None):
    try:
        if error is None:
            future.set_result(value)
        else:
            future.set_exception(error)
    except InvalidStateError:
        pass
    except BaseException:  # A Future callback must not kill the owner.
        if not future.done():
            raise


class OwnerRequest:
    """A bounded request; cancelling a queued request never cancels an epoch."""

    def __init__(self, owner, operation, args, kwargs, deadline, generation, kind="call"):
        self.owner = owner
        self.operation, self.args, self.kwargs = operation, args, kwargs
        self.deadline, self.generation, self.kind = deadline, generation, kind
        self.future: Future = Future()
        self.ready: Future = Future()
        self.cancel_event = threading.Event()
        self.endpoint = None
        self.phase = "queued"
        self.future.add_done_callback(self._future_done)

    def _future_done(self, future):
        if future.cancelled():
            self.cancel()

    def cancel(self):
        return self.owner._cancel_request(self, "timed_cancelled")

    def result(self, timeout=None):
        budget = max(0.0, self.deadline - time.monotonic())
        if timeout is not None:
            budget = min(budget, _positive(timeout, "timeout"))
        try:
            return self.future.result(timeout=budget)
        except TimeoutError:
            self.owner._cancel_request(self, "timed_deadline")
            if self.future.done():
                return self.future.result()
            raise TimedOwnerError("timed_deadline", "request deadline expired") from None

    async def _wait(self, future):
        if future.done():
            return future.result()
        wrapped = asyncio.wrap_future(future)
        # asyncio.wait leaves this wrapper alive on timeout/caller cancellation.
        # Consume its eventual exception even if no caller remains to await it.
        wrapped.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        try:
            done, _ = await asyncio.wait(
                (wrapped,), timeout=max(0.0, self.deadline - time.monotonic())
            )
            if done:
                return wrapped.result()
            self.owner._cancel_request(self, "timed_deadline")
            if future.done():
                return future.result()
            raise self.owner._first_reason(
                self.endpoint, TimedOwnerError("timed_deadline", "request deadline expired")
            )
        except asyncio.CancelledError:
            self.cancel()
            raise

    async def wait(self):
        return await self._wait(self.future)

    async def wait_ready(self):
        return await self._wait(self.ready)

    def __await__(self):
        return self.wait().__await__()


class TimedOwner:
    """Own all native access until close succeeds, including retained cleanup.

    ``disconnect`` stops admission until successful owner cleanup, then permits
    ordinary requests and a new epoch. ``close`` additionally closes the Session
    on the owner and stops all three threads. A False close result retains ownership:
    callers must retry, never close or detach the Session from another thread.
    """

    def __init__(self, session, policy: TimedOwnerPolicy):
        if not isinstance(policy, TimedOwnerPolicy):
            raise TypeError("an explicit TimedOwnerPolicy is required")
        self.session, self.policy = session, policy
        self._condition = threading.Condition()
        self._queue = deque()
        self._active = None
        self._endpoint = None
        self._epoch_event = None
        self._cancel_pending = deque()
        self._cancel_active = None
        self._cancel_completed = None
        self._cancel_idle = threading.Event()
        self._cancel_idle.set()
        self._setup_pending = False
        self._bound = False
        self._generation = 0
        self._admitting = True
        self._cleanup = None
        self._closing = False
        self._stopped = False
        self._cache = _freeze(
            {
                "state": "idle",
                "generation": 0,
                "queued": 0,
                "active": False,
                "admitting": True,
                "cleanup_pending": False,
                "error": None,
                "host": None,
                "port": None,
                "peer_rom_version": None,
                "epoch": None,
                "tick": None,
                "current_tick": None,
                "accounting": None,
                "transport": "timed",
                "mode": "idle",
                "observed_at": time.monotonic(),
                "native_observed_at": None,
                "last_error": None,
                "cancellation_pending": False,
                "cancellation_active": False,
                "cancellation_worker_alive": True,
                "cancellation_error": None,
            }
        )
        self._thread = threading.Thread(target=self._work, name="timed-native-owner", daemon=True)
        self._supervisor = threading.Thread(
            target=self._supervise, name="timed-deadlines", daemon=True
        )
        self._canceller = threading.Thread(
            target=self._cancel_work, name="timed-cancellation", daemon=True
        )
        self._canceller.start()
        self._thread.start()
        self._supervisor.start()

    def status(self):
        return self._cache

    @property
    def generation(self):
        return self._cache["generation"]

    def _publish(self, **changes):
        # Called under the short state lock; never reads native state.
        changes["observed_at"] = time.monotonic()
        if changes.get("error") is not None and not self._cache.get("last_error"):
            changes["last_error"] = changes["error"]
        if "state" in changes:
            changes["mode"] = changes["state"]
        if "tick" in changes:
            changes["current_tick"] = changes["tick"]
        self._cache = _freeze(
            {
                **self._cache,
                "generation": self._generation,
                "queued": len(self._queue),
                "active": self._active is not None,
                "admitting": self._admitting,
                "cancellation_pending": bool(self._cancel_pending),
                "cancellation_active": self._cancel_active is not None,
                **changes,
            }
        )

    def _deadline(self, deadline):
        now = time.monotonic()
        if deadline is None:
            return now + self.policy.request_timeout
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("deadline must be a finite absolute monotonic time")
        return min(deadline, now + self.policy.request_timeout)

    def submit(self, operation, *args, deadline=None, generation=None, **kwargs):
        if not callable(operation) and not isinstance(operation, str):
            raise TypeError("operation must be callable or a Session method name")
        return self._submit(operation, args, kwargs, deadline, generation, "call")

    def _submit(self, operation, args, kwargs, deadline, generation, kind):
        deadline = self._deadline(deadline)
        with self._condition:
            if not self._admitting or self._stopped:
                raise TimedOwnerError("timed_owner_closed", "owner is not admitting requests")
            if generation is not None and (
                type(generation) is not int or generation != self._generation
            ):
                raise TimedOwnerError(
                    "timed_stale_generation", "request belongs to a stale generation"
                )
            if len(self._queue) >= self.policy.queue_capacity:
                raise TimedOwnerError("timed_queue_full", "owner request queue is full")
            if kind in ("listen", "connect"):
                if self._setup_pending or self._endpoint is not None:
                    raise TimedOwnerError(
                        "timed_busy", "an endpoint or setup already belongs to this owner"
                    )
                self._setup_pending = True
            request = OwnerRequest(self, operation, args, kwargs, deadline, self._generation, kind)
            self._queue.append(request)
            self._publish()
            self._condition.notify_all()
        return request

    def listen(self, host, port, rom_version, *, deadline=None, peer_rom_version=None):
        return self._network("listen", host, port, rom_version, deadline, peer_rom_version)

    def connect(self, host, port, rom_version, *, deadline=None, peer_rom_version=None):
        return self._network("connect", host, port, rom_version, deadline, peer_rom_version)

    def _network(self, kind, host, port, rom_version, deadline, peer_rom_version):
        if not isinstance(host, str):
            raise TypeError("host must be a numeric loopback address")
        address = ipaddress.ip_address(host)
        if not address.is_loopback or not isinstance(host, str) or "%" in host:
            raise ValueError("host must be a numeric loopback address")
        minimum = 0 if kind == "listen" else 1
        if type(port) is not int or not minimum <= port <= 65535:
            raise ValueError(f"port must be an integer in {minimum}..65535")
        if rom_version not in ("red", "blue", "yellow"):
            raise ValueError("rom_version must be red, blue, or yellow")
        if peer_rom_version is not None and peer_rom_version not in ("red", "blue", "yellow"):
            raise ValueError("peer_rom_version must be red, blue, or yellow")
        return self._submit(
            None,
            (str(address), port, rom_version),
            {"peer_rom_version": peer_rom_version},
            deadline,
            None,
            kind,
        )

    def _cancel_request(self, request, code):
        endpoint = None
        with self._condition:
            if request.phase == "done" or request.cancel_event.is_set():
                return False
            request.cancel_event.set()
            if request is self._active and request.kind != "cleanup":
                endpoint = request.endpoint
                if self._epoch_event is not None:
                    self._epoch_event.set()
                self._admitting = False
                self._schedule_cancel(endpoint)
                self._publish(state="cancelling", cleanup_pending=True)
            elif request in self._queue:
                self._queue.remove(request)
                if request.kind in ("listen", "connect"):
                    self._setup_pending = False
                request.phase = "done"
                self._publish()
            self._condition.notify_all()
        error = TimedOwnerError(
            code, "request cancelled" if code == "timed_cancelled" else "request deadline expired"
        )
        error = self._first_reason(endpoint, error)
        _settle(request.future, error=error)
        _settle(request.ready, error=error)
        return True

    def _first_reason(self, endpoint, fallback):
        # TimedWireChannel publishes _error once and never replaces the object.
        # Read that reference directly: its public property acquires a lock
        # that blocked cancellation may hold. No native state is read here.
        reason = endpoint.channel._error if endpoint is not None else None
        error = reason if isinstance(reason, ProtocolError) else fallback
        with self._condition:
            if endpoint is not None and endpoint is self._endpoint:
                self._publish(error=f"{type(error).__name__}: {error}")
        return error

    def _schedule_cancel(self, endpoint):
        with self._condition:
            if (
                endpoint is not None
                and endpoint is self._endpoint
                and (endpoint is not self._cancel_active and endpoint is not self._cancel_completed)
                and not any(item is endpoint for item in self._cancel_pending)
            ):
                # Admission stays closed through cleanup, so only the retained
                # epoch can occupy this single pending slot or the active slot.
                self._cancel_pending.append(endpoint)
                self._cancel_idle.clear()
                self._publish()
            self._condition.notify_all()

    def _cancel_work(self):
        while True:
            with self._condition:
                while not self._cancel_pending and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    self._publish(cancellation_worker_alive=False)
                    return
                endpoint = self._cancel_pending.popleft()
                self._cancel_active = endpoint
                self._publish()
            try:
                self._cancel_endpoint(endpoint)
            finally:
                with self._condition:
                    self._cancel_active = None
                    self._cancel_completed = endpoint
                    self._cancel_idle.set()
                    self._publish()
                    self._condition.notify_all()

    def _cancel_endpoint(self, endpoint):
        try:
            endpoint.cancel()
        except BaseException as exc:  # noqa: BLE001 - retain cleanup after native failures
            with self._condition:
                if endpoint is self._endpoint:
                    self._publish(
                        error=f"{type(exc).__name__}: {exc}",
                        cleanup_pending=True,
                        cancellation_error=f"{type(exc).__name__}: {exc}",
                    )

    def cancel(self):
        """Cancel the current epoch without queue admission or a Session lock."""
        return self.disconnect()

    def disconnect(self):
        with self._condition:
            if self._stopped:
                request = OwnerRequest(
                    self,
                    None,
                    (),
                    {},
                    time.monotonic() + self.policy.close_timeout,
                    self._generation,
                    "cleanup",
                )
                stopped = True
                pending, active, endpoint = [], None, None
            else:
                stopped = False
                self._admitting = False
                pending = list(self._queue)
                self._queue.clear()
                active, endpoint = self._active, self._endpoint
                if self._epoch_event is not None:
                    self._epoch_event.set()
                self._schedule_cancel(endpoint)
                self._setup_pending = active is not None and active.kind in ("listen", "connect")
                if active is not None and active.kind != "cleanup":
                    active.cancel_event.set()
                if active is not None and active.kind == "cleanup":
                    request = active
                elif self._cleanup is None or self._cleanup.phase == "done":
                    self._cleanup = OwnerRequest(
                        self,
                        None,
                        (),
                        {},
                        time.monotonic() + self.policy.close_timeout,
                        self._generation,
                        "cleanup",
                    )
                    request = self._cleanup
                else:
                    request = self._cleanup
                self._publish(state="disconnecting", cleanup_pending=True)
                self._condition.notify_all()
        error = TimedOwnerError("timed_cancelled", "owner disconnect requested")
        for item in pending + ([active] if active is not None and active.kind != "cleanup" else []):
            reason = self._first_reason(endpoint, error) if item is active else error
            _settle(item.future, error=reason)
            _settle(item.ready, error=reason)
        if stopped:
            _settle(request.future, self.status())
        return request

    def close(self, timeout=None):
        budget = self.policy.close_timeout if timeout is None else _positive(timeout, "timeout")
        deadline = time.monotonic() + budget
        with self._condition:
            self._closing = True
        self.disconnect()
        if threading.current_thread() in (self._thread, self._supervisor, self._canceller):
            return False
        self._thread.join(max(0, deadline - time.monotonic()))
        self._supervisor.join(max(0, deadline - time.monotonic()))
        self._canceller.join(max(0, deadline - time.monotonic()))
        return not any(
            thread.is_alive() for thread in (self._thread, self._supervisor, self._canceller)
        )

    def _supervise(self):
        while True:
            with self._condition:
                if self._stopped:
                    return
                candidates = list(self._queue)
                if self._cleanup is not None:
                    candidates.append(self._cleanup)
                if self._active is not None:
                    candidates.append(self._active)
                expired = [
                    item
                    for item in candidates
                    if not item.cancel_event.is_set() and item.deadline <= time.monotonic()
                ]
                if not expired:
                    self._condition.wait(0.01)
                    continue
            for item in expired:
                self._cancel_request(item, "timed_deadline")

    def _remaining(self, request):
        if request.cancel_event.is_set():
            raise TimedOwnerError("timed_cancelled", "request cancelled")
        remaining = request.deadline - time.monotonic()
        if remaining <= 0:
            raise TimedOwnerError("timed_deadline", "request deadline expired")
        return min(remaining, self.policy.lock_timeout)

    def _open(self, request):
        if self._endpoint is not None:
            raise TimedOwnerError("timed_busy", "an endpoint already belongs to this owner")
        host, port, rom_version = request.args
        options = dict(
            rom_version=rom_version,
            deadline=request.deadline,
            cancel_event=request.cancel_event,
            **self.policy.endpoint_options(),
        )
        with self._condition:
            self._epoch_event = request.cancel_event
            self._publish(state="connecting", host=host, port=port, error=None)
        if request.kind == "listen":
            family = socket.AF_INET if ipaddress.ip_address(host).version == 4 else socket.AF_INET6
            with socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP) as listener:
                listener.setblocking(False)
                listener.bind((host, port))
                listener.listen(1)
                self._remaining(request)
                with self._condition:
                    self._publish(state="listening", port=listener.getsockname()[1])
                _settle(request.ready, self.status())
                while True:
                    budget = self._remaining(request)
                    if not select.select([listener], [], [], min(0.01, budget))[0]:
                        continue
                    try:
                        connected, _ = listener.accept()
                        break
                    except (BlockingIOError, InterruptedError):
                        continue
            endpoint = TimedRemoteEndpoint.from_connected_socket(
                connected, side="listener", **options
            )
        else:
            endpoint = TimedRemoteEndpoint.connect(host, port, **options)
        # Publish ownership before attach: failure must retain a cleanup handle.
        with self._condition:
            self._endpoint = endpoint
            request.endpoint = endpoint
        expected = request.kwargs["peer_rom_version"]
        if expected is not None and endpoint.peer_rom_version != expected:
            raise TimedOwnerError(
                "timed_peer_mismatch", "peer ROM version does not match expectation"
            )
        with self.session.locked(timeout_s=self._remaining(request)):
            endpoint.attach(self.session._pyboy, deadline=request.deadline)
            self._remaining(request)
            self.session.bind_timed_execution(endpoint, timeout_s=self._remaining(request))
            self._bound = True
            self._observe()
        with self._condition:
            self._publish(
                state="connected",
                peer_rom_version=endpoint.peer_rom_version,
                epoch=endpoint.epoch.hex(),
            )
        return self.status()

    def _clean(self, request):
        endpoint = self._endpoint
        if endpoint is not None:
            # Do not race native detach against blocked channel/native cancel.
            # Event.wait holds no queue, state, Session, or transport lock.
            if not self._cancel_idle.wait(max(0.0, request.deadline - time.monotonic())):
                raise TimedOwnerError(
                    "timed_cleanup_failed", "cancellation worker is still running"
                )
            with self.session.locked(timeout_s=self.policy.lock_timeout, allow_closed=True):
                if self._bound:
                    self.session.unbind_timed_execution(
                        endpoint, timeout_s=self.policy.lock_timeout
                    )
                    self._bound = False
                else:
                    endpoint.close()
            with self._condition:
                self._endpoint = None
                self._epoch_event = None
                self._cancel_completed = None
        if self._closing:
            self.session.close(timeout_s=self.policy.close_timeout)
        with self._condition:
            self._generation += 1
            self._admitting = not self._closing
            self._stopped = self._closing
            self._publish(
                state="closed" if self._closing else "idle",
                cleanup_pending=False,
                error=None,
                host=None,
                port=None,
                peer_rom_version=None,
                epoch=None,
                accounting=None,
            )
            self._condition.notify_all()
        return self.status()

    def _execute(self, request):
        if request.kind == "cleanup":
            return self._clean(request)
        self._remaining(request)
        if request.kind in ("listen", "connect"):
            return self._open(request)
        with self.session.locked(timeout_s=self._remaining(request)):
            self._remaining(request)
            try:
                if callable(request.operation):
                    result = request.operation(self.session, *request.args, **request.kwargs)
                else:
                    result = getattr(self.session, request.operation)(
                        *request.args, **request.kwargs
                    )
                self._remaining(request)
            finally:
                self._observe()
        return result

    def _observe(self):
        """Refresh native evidence on the owner, including partial failed calls."""
        changes = {}
        try:
            changes["tick"] = self.session.current_tick()
            changes["native_observed_at"] = time.monotonic()
            if self._endpoint is not None:
                changes["accounting"] = _freeze(self._endpoint.snapshot())
        except BaseException as exc:  # noqa: BLE001 - observation cannot erase an action outcome
            changes["snapshot_error"] = f"{type(exc).__name__}: {exc}"
        with self._condition:
            self._publish(**changes)

    def _work(self):
        while True:
            with self._condition:
                while not self._stopped and self._cleanup is None and not self._queue:
                    self._condition.wait(0.01)
                    if (
                        self._cleanup is None
                        and self._endpoint is not None
                        and self._endpoint.channel.closed
                        and self._cache["state"] != "cleanup_failed"
                    ):
                        self._admitting = False
                        self._cleanup = OwnerRequest(
                            self,
                            None,
                            (),
                            {},
                            time.monotonic() + self.policy.close_timeout,
                            self._generation,
                            "cleanup",
                        )
                        self._publish(
                            state="disconnecting",
                            cleanup_pending=True,
                            error="peer transport closed",
                        )
                if self._stopped:
                    return
                request = self._cleanup if self._cleanup is not None else self._queue.popleft()
                self._cleanup = None
                self._active = request
                request.phase = "running"
                request.endpoint = self._endpoint
                self._publish()
            result = error = None
            try:
                if request.kind != "cleanup" and request.generation != self._generation:
                    raise TimedOwnerError(
                        "timed_stale_generation", "request belongs to a stale generation"
                    )
                result = self._execute(request)
            except BaseException as exc:  # noqa: BLE001 - worker must survive all native failures
                error = exc
            endpoint_terminal = self._endpoint is not None and (
                self._endpoint.channel.closed
                or self._endpoint.session._terminal.is_set()
                or self._endpoint._cancel.is_set()
            )
            with self._condition:
                request.phase = "done"
                if request.kind in ("listen", "connect"):
                    self._setup_pending = False
                self._active = None
                terminal = request.kind != "cleanup" and (
                    request.cancel_event.is_set()
                    or (
                        error is not None
                        and (endpoint_terminal or request.kind in ("listen", "connect"))
                    )
                )
                if terminal:
                    self._admitting = False
                    self._publish(state="cancelling", cleanup_pending=True)
                if error is not None:
                    self._publish(error=f"{type(error).__name__}: {error}")
                    if request.kind == "cleanup":
                        self._admitting = False
                        self._publish(state="cleanup_failed", cleanup_pending=True)
                self._publish()
                self._condition.notify_all()
            _settle(request.future, result, error)
            _settle(request.ready, result, error)
            if terminal:
                self.disconnect()


__all__ = ["OwnerRequest", "TimedOwner", "TimedOwnerError", "TimedOwnerPolicy"]
