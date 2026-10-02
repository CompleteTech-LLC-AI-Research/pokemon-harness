"""Durable intent/completion journal for acceptance pair operations."""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from pathlib import Path

TIMING_SCHEMA = "normal-red-timing-v1"
MAX_PHASE_MARKS = 64


def redacted(value):
    if isinstance(value, bytes):
        return {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: redacted(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redacted(item) for item in value]
    return value


def mark_phase(pair, name):
    """Record a phase marker when the pair is timed; otherwise do nothing."""
    mark = getattr(pair, "mark", None)
    if mark is not None:
        mark(name)


class TimingLedger:
    """Bounded monotonic timing aggregates for one qualification row.

    Diagnostic only: it never feeds a decision, and it shows where wall time
    accrues, not why. Durations are clamped to be nonnegative. Buckets are
    exclusive for sequential wrapped stages. Concurrent overlapping stages are
    detected and set ``buckets_additive=False``; do not sum those buckets:

    - one bucket per wrapped pair operation: only the awaited underlying call;
    - ``operation_journal_io``: building/redacting a journal row, serializing
      it, and writing and flushing it;
    - ``release_rpc``: the release sequence's own public tool calls;
    - ``observation_stream_write_flush``: stream write/flush only (the caller's
      own serialization is not included).

    ``interrupted`` is the last exception caught inside a wrapped stage (an
    operation, or a journal write/flush with its ``stage``). A deadline that
    fires outside a wrapped stage, or during synchronous I/O, leaves it null;
    it is not a universal indicator of the operation in flight.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._start = clock()
        self._operations = {}
        self._phases = []
        self._phases_dropped = 0
        self._interrupted = None
        self._active_intervals = 0
        self._overlap_detected = False

    def now(self):
        return self._clock()

    def begin_interval(self):
        self._active_intervals += 1
        self._overlap_detected |= self._active_intervals > 1
        return self.now()

    def end_interval(self):
        self._active_intervals -= 1

    def elapsed(self):
        return max(0.0, self._clock() - self._start)

    def add(self, bucket, seconds):
        row = self._operations.setdefault(bucket, [0, 0.0, 0.0])
        seconds = max(0.0, seconds)
        row[0] += 1
        row[1] += seconds
        row[2] = max(row[2], seconds)

    def mark(self, name):
        """Record a phase; return False once the cap has dropped it."""
        if len(self._phases) >= MAX_PHASE_MARKS:
            self._phases_dropped += 1
            return False
        self._phases.append({"phase": name, "seconds_since_start": self.elapsed()})
        return True

    def interrupted(self, bucket, seconds, error, stage=None):
        self._interrupted = {
            "operation": bucket,
            "stage": stage,
            "seconds": max(0.0, seconds),
            "error": type(error).__name__,
            "seconds_since_start": self.elapsed(),
        }

    def summary(self):
        return {
            "schema": TIMING_SCHEMA,
            "clock": "monotonic",
            "buckets_additive": not self._overlap_detected,
            "overlap_detected": self._overlap_detected,
            "elapsed_seconds": self.elapsed(),
            "operations": {
                name: {"count": count, "total_seconds": total, "max_seconds": longest}
                for name, (count, total, longest) in self._operations.items()
            },
            "phases": list(self._phases),
            "phases_dropped": self._phases_dropped,
            "interrupted": self._interrupted,
        }


class TimedStream:
    """Time write/flush of an existing stream without changing what is written."""

    def __init__(self, stream, ledger, bucket):
        self._stream = stream
        self._ledger = ledger
        self._bucket = bucket

    def _timed(self, stage, call, *args):
        started = self._ledger.begin_interval()
        try:
            result = call(*args)
        except BaseException as exc:
            seconds = max(0.0, self._ledger.now() - started)
            self._ledger.end_interval()
            self._ledger.add(self._bucket, seconds)
            self._ledger.interrupted(self._bucket, seconds, exc, stage)
            raise
        self._ledger.end_interval()
        self._ledger.add(self._bucket, self._ledger.now() - started)
        return result

    def write(self, text):
        return self._timed("write", self._stream.write, text)

    def flush(self):
        return self._timed("flush", self._stream.flush)


class JournalPair:
    """Record driver-level operations; internal RPCs remain the pair's contract.

    This is not an emulator hook or memory access. A missing completion retains
    the pending operation on failure and is never silently repaired.
    """

    def __init__(self, pair, stream, timing=None):
        self._pair = pair
        self._stream = stream
        self._sequence = 0
        self._timing = timing

    def mark(self, name):
        """Retain bounded phases in the receipt without changing journal events."""
        if self._timing is not None:
            self._timing.mark(name)

    def _begin(self):
        return None if self._timing is None else self._timing.begin_interval()

    def _finish(self, bucket, started, error=None, stage=None):
        """Return the nonnegative duration, or None when untimed."""
        if started is None:
            return None
        seconds = max(0.0, self._timing.now() - started)
        self._timing.end_interval()
        self._timing.add(bucket, seconds)
        if error is not None:
            self._timing.interrupted(bucket, seconds, error, stage)
        return seconds

    async def _call(self, bucket, awaitable):
        """Await one underlying call, timing only that await."""
        started = self._begin()
        try:
            result = await awaitable
        except BaseException as exc:
            self._finish(bucket, started, exc)
            raise
        return result, self._finish(bucket, started)

    def __getattr__(self, name):
        member = getattr(self._pair, name)
        if not inspect.iscoroutinefunction(member):
            return member

        async def recorded(*args, **kwargs):
            self._sequence += 1
            sequence = self._sequence
            self._write(
                lambda: {
                    "sequence": sequence,
                    "event": "intent",
                    "operation": name,
                    "args": redacted(args),
                    "kwargs": redacted(kwargs),
                },
                "intent",
            )
            result, seconds = await self._call(name, member(*args, **kwargs))

            def completion():
                row = {
                    "sequence": sequence,
                    "event": "completion",
                    "operation": name,
                    "result": redacted(result),
                }
                if seconds is not None:
                    row["seconds"] = seconds
                return row

            self._write(completion, "completion")
            return result

        return recorded

    def _write(self, build, stage):
        """Build (if lazy), serialize, write and flush one row; time all as journal I/O."""
        started = self._begin()
        try:
            self._stream.write(json.dumps(build() if callable(build) else build) + "\n")
            self._stream.flush()
        except BaseException as exc:
            self._finish("operation_journal_io", started, exc, stage)
            raise
        self._finish("operation_journal_io", started)

    async def release_buttons(self):
        """Release ordinary held buttons through advertised public owner tools."""
        clients = (
            [self._pair.client] if self._pair.transport == "local_pair" else self._pair.clients
        )
        for owner in range(2):
            client = clients[0] if len(clients) == 1 else clients[owner]
            name = "link_peer_release" if len(clients) == 1 and owner == 1 else "release"
            listed, _ = await self._call("release_rpc", client.request("tools/list", {}))
            if name not in {row["name"] for row in listed["tools"]}:
                raise ValueError(f"owner {owner} does not advertise {name}")
            for button in ("a", "b", "up", "down"):
                self._sequence += 1
                sequence = self._sequence
                self._write(
                    {
                        "sequence": sequence,
                        "event": "intent",
                        "operation": "release",
                        "owner": owner,
                        "button": button,
                        "public_tool": name,
                    },
                    "release_intent",
                )
                result, _ = await self._call("release_rpc", client.tool(name, {"button": button}))
                self._write(
                    {
                        "sequence": sequence,
                        "event": "completion",
                        "operation": "release",
                        "result": result,
                    },
                    "release_completion",
                )
