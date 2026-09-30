"""Opt-in peer stack diagnostics for the TCP trade peer.

Extracted from ``tests/_tcp_trade_peer.py`` (#127): the warning sink and the
one-shot faulthandler watchdog that ``main`` arms around ``_run_peer``.
"""

from __future__ import annotations

import faulthandler
import math
import os
import sys
import tempfile
import time
from pathlib import Path


def _trace_warning(message: str) -> None:
    """Diagnostics must never replace a peer result or its original exception."""
    try:
        print(f"[peer trace] {message}", file=sys.stderr, flush=True)
    except BaseException:  # noqa: BLE001, S110 - Failed logging must not recurse or mask peer errors.
        pass


class _PeerTraceWatchdog:
    """Opt-in Python stack capture, with at most two one-shot schedules.

    This owns the process-wide faulthandler timer. It does not terminate the
    peer, inspect emulator state, or promise native C stack frames. Stderr is
    best effort: parent filtering/tail limits may discard frames. An explicit
    existing POKERED_PEER_TRACE_DIR retains a private, unique PID artifact.
    Two dump attempts bound output frequency, not an exact byte quota.
    """

    def __init__(self, delay, destination, *, owned=False):
        self.delay = delay
        self.destination = destination
        self.owned = owned
        self.attempts = 0
        self.initial_due = None
        self.closed = False

    @classmethod
    def from_env(cls):
        raw = os.environ.get("POKERED_PEER_TRACE_AFTER_SECONDS")
        if raw is None:
            return None
        try:
            delay = float(raw)
            if not math.isfinite(delay) or delay <= 0:
                raise ValueError("trace delay must be finite and positive")
        except BaseException as exc:  # noqa: BLE001
            _trace_warning(f"invalid trace configuration ({type(exc).__name__}); disabled")
            return None
        trace = cls(delay, sys.stderr)
        directory = os.environ.get("POKERED_PEER_TRACE_DIR")
        if directory is not None:
            try:
                if not directory or not Path(directory).is_dir():
                    raise ValueError("trace directory must already exist")
                # mkstemp uses exclusive creation; never mkdir or
                # overwrite an existing artifact, even for repeated same-PID runs.
                trace.destination, artifact = tempfile.mkstemp(
                    prefix=f"peer-trace-{os.getpid()}-",
                    suffix=".log",
                    dir=directory,
                )
                trace.owned = True
                _trace_warning(f"stack artifact: {artifact}")
            except BaseException as exc:  # noqa: BLE001
                _trace_warning(f"trace artifact open failed ({type(exc).__name__}); using stderr")
        return trace

    def _arm(self, delay):
        if self.closed or self.attempts >= 2:
            return
        # Failed scheduling calls count too; never retry indefinitely.
        self.attempts += 1
        faulthandler.dump_traceback_later(
            delay,
            repeat=False,
            file=self.destination,
            exit=False,
        )

    def start(self):
        if self.closed or self.attempts:
            return
        try:
            self.initial_due = time.monotonic() + self.delay
            self._arm(self.delay)
        except BaseException as exc:  # noqa: BLE001
            _trace_warning(f"trace arming failed ({type(exc).__name__})")

    def cleanup(self, deadline):
        try:
            if self.closed or self.attempts != 1 or self.initial_due is None:
                return
            now = time.monotonic()
            if now < self.initial_due:
                return  # Preserve a pending initial dump; do not postpone it.
            # Elapsed is not observed firing: faulthandler has no fired callback.
            # A delayed initial watchdog may race this replacement. At most two
            # schedules remain possible, including failed scheduling attempts.
            delay = min(self.delay, max(0.000001, deadline - now))
            self._arm(delay)
        except BaseException as exc:  # noqa: BLE001
            _trace_warning(f"cleanup trace arming failed ({type(exc).__name__})")

    def close(self):
        if self.closed:
            return
        try:
            if self.attempts:
                faulthandler.cancel_dump_traceback_later()
        except BaseException as exc:  # noqa: BLE001
            # Raw descriptors have no GC closer: keep it valid until process
            # exit if cancellation was not confirmed, rather than risk reuse.
            _trace_warning(
                f"trace cancellation failed ({type(exc).__name__}); retaining destination"
            )
            return
        self.closed = True
        if self.owned:
            try:
                os.close(self.destination)
            except BaseException as exc:  # noqa: BLE001
                _trace_warning(f"trace destination close failed ({type(exc).__name__})")
