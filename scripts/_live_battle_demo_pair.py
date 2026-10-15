"""Diagnostic paired advancement and retained cleanup, not native qualification.

Hooks during a quantum see its START tick. Only observed frame increments
advance managed time afterwards; input remains owned by PyBoy's event queue.
Cooperative cleanup deadlines cannot preempt a nonreturning native operation.
"""

from __future__ import annotations

import sys


def required_hash(value, label):
    """Diagnostic callers require a complete expected hash, never None."""
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(c not in "0123456789abcdefABCDEF" for c in value)
    ):
        raise ValueError(f"diagnostic {label} pin must be exactly 40 hexadecimal characters")
    return value.lower()


def required_version(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("diagnostic PyBoy version pin must be a nonempty string")
    return value.strip()


class DemoResources:
    """Keep real handles and failed attempt evidence through unwinding.

    A subsequent explicit cleanup call uses the same provider/Sessions. It is
    not an automatic retry and never launches its own stop worker.
    """

    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.provider = None
        self.attempts = []
        self.primary = None
        self.unresolved = []

    def cleanup(self, *, active_error=None):
        self.primary = active_error
        failures = []
        operations = []
        if self.provider is not None:
            operations.append(("pair detach", self.provider))
        operations.extend(
            (f"session {label}", session)
            for label, session in self.sessions if session is not None
        )
        # Resolve each operation inside its own BaseException boundary, too:
        # a broken attribute access must not suppress another owned cleanup.
        for label, owner in operations:
            record = {"label": label, "owner": owner, "status": "attempting"}
            self.attempts.append(record)
            try:
                if owner is self.provider and label == "pair detach":
                    owner.detach_all()
                else:
                    owner.close()
                record["status"] = "returned"
            except BaseException as exc:  # noqa: BLE001 - independent owned cleanup
                record.update(status="unresolved", error=exc)
                # Session failure may precede publication of an attempt.
                try:
                    record["stop_attempt"] = getattr(owner, "_stop_attempt", None)
                except BaseException as observation:  # noqa: BLE001
                    record["observation_error"] = observation
                    exc.add_note(f"cleanup attempt observation failed: {observation!r}")
                self.unresolved.append(record)
                exc.add_note(f"diagnostic cleanup failed for {label}")
                failures.append(exc)
        # Diagnostic output is secondary and cannot suppress an owned close.
        for record in self.attempts[-len(operations):] if operations else ():
            if record["status"] != "unresolved":
                continue
            error = record["error"]
            try:
                print(
                    f"[diagnostic cleanup] failed for {record['label']}: "
                    f"{type(error).__name__}: {error}", file=sys.stderr, flush=True,
                )
            except BaseException as diagnostic_error:  # noqa: BLE001
                diagnostic_error.add_note("diagnostic cleanup output also failed")
                failures.append(diagnostic_error)
        if failures:
            group = BaseExceptionGroup("diagnostic cleanup failed", failures)
            group.cleanup_owner = self
            if active_error is not None:
                raise group from active_error
            raise group


class PairProgress:
    """One synchronous pair owner with boundary-state and per-peer evidence."""

    def __init__(self, provider, a, b, *, view=False):
        self.provider = provider
        self.sessions = (a, b)
        self.view = view
        self.requested = 0
        self.completed = [0, 0]
        self.last_evidence = None
        self.failed = False

    @staticmethod
    def _counter(endpoint):
        value = endpoint.frame_count
        if type(value) is not int or value < 0:
            raise RuntimeError("diagnostic endpoint frame counter is invalid")
        return value

    @staticmethod
    def _state(endpoint):
        state = (endpoint.paused, endpoint.quitting)
        if any(type(flag) is not bool for flag in state):
            raise RuntimeError("diagnostic endpoint pause/quit state is unsupported")
        return state

    def advance(self, frames):
        from pokered_harness.session import locked_sessions

        if type(frames) is not int or frames <= 0:
            raise ValueError("diagnostic frames must be a positive integer")
        if self.failed:
            raise RuntimeError("diagnostic pair progress is unresolved; no replay")
        self.requested += frames
        try:
            with locked_sessions(*self.sessions, timeout_s=2.0):
                endpoints = tuple(s._pyboy for s in self.sessions)
                members = self.provider.attached
                if len(members) != 2 or any(a is not b for a, b in zip(members, endpoints)):
                    raise RuntimeError("diagnostic provider attachment mismatch")
                for session in self.sessions:
                    session._ensure_open()
                    if session._timed_endpoint is not None:
                        raise RuntimeError("diagnostic paired progress cannot own timed execution")
                for _ in range(frames):
                    self._quantum(endpoints)
        except BaseException:  # noqa: BLE001 - latch unknown/partial execution
            self.failed = True
            raise

    def _quantum(self, endpoints):
        before = []
        states = []
        admission_errors = []
        for endpoint in endpoints:
            try:
                before.append(self._counter(endpoint))
                states.append(self._state(endpoint))
            except BaseException as exc:  # noqa: BLE001
                admission_errors.append(exc)
        if admission_errors:
            raise admission_errors[0]
        if any(any(state) for state in states):
            raise RuntimeError("diagnostic endpoint is paused or quitting before progression")
        ticks = tuple(session.current_tick() for session in self.sessions)
        evidence = {"before": tuple(before), "ticks_before": ticks, "states_before": states,
                    "after": [None, None], "states_after": [None, None],
                    "deltas": [None, None], "accounted": [False, False], "errors": []}
        self.last_evidence = evidence
        primary = traceback = None
        try:
            self.provider.step_interleaved(1, render=self.view)
        except BaseException as exc:  # noqa: BLE001 - preserve original operation
            primary, traceback = exc, exc.__traceback__
        # Each observation and reconciliation is independent of the other peer.
        for i, (endpoint, session) in enumerate(zip(endpoints, self.sessions)):
            try:
                evidence["after"][i] = self._counter(endpoint)
                delta = evidence["after"][i] - before[i]
                evidence["deltas"][i] = delta
                if delta not in (0, 1):
                    raise RuntimeError("diagnostic frame accounting is unknown")
                self.completed[i] += delta
                session._advance_tick(delta)
                evidence["accounted"][i] = True
            except BaseException as exc:  # noqa: BLE001
                evidence["errors"].append(exc)
            try:
                state = self._state(endpoint)
                evidence["states_after"][i] = state
                if any(state):
                    raise RuntimeError("diagnostic endpoint paused or quit during progression")
            except BaseException as exc:  # noqa: BLE001
                evidence["errors"].append(exc)
        if tuple(evidence["deltas"]) != (1, 1):
            evidence["errors"].append(RuntimeError("diagnostic pair made partial or no progress"))
        if primary is None and evidence["errors"]:
            primary = evidence["errors"][0]
            traceback = primary.__traceback__
        if primary is not None:
            primary.pair_progress = self
            primary.pair_secondary_errors = tuple(e for e in evidence["errors"] if e is not primary)
            for error in primary.pair_secondary_errors:
                primary.add_note(f"diagnostic pair secondary: {error!r}")
            raise primary.with_traceback(traceback)
