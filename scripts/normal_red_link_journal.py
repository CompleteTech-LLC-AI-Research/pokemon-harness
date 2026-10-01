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

    Diagnostic only: it never feeds a decision. Durations are clamped to be
    nonnegative. Aggregates are keyed by operation name, so size stays bounded;
    phase marks are capped. An operation that raised or was cancelled (for
    example by the row deadline) is reported under ``interrupted``.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._start = clock()
        self._operations = {}
        self._phases = []
        self._phases_dropped = 0
        self._interrupted = None

    def now(self):
        return self._clock()

    def elapsed(self):
        return max(0.0, self._clock() - self._start)

    def add(self, bucket, seconds):
        row = self._operations.setdefault(bucket, [0, 0.0, 0.0])
        seconds = max(0.0, seconds)
        row[0] += 1
        row[1] += seconds
        row[2] = max(row[2], seconds)

    def mark(self, name):
        if len(self._phases) >= MAX_PHASE_MARKS:
            self._phases_dropped += 1
            return
        self._phases.append({"phase": name, "seconds_since_start": self.elapsed()})

    def interrupted(self, bucket, seconds, error):
        self._interrupted = {
            "operation": bucket,
            "seconds": max(0.0, seconds),
            "error": type(error).__name__,
            "seconds_since_start": self.elapsed(),
        }

    def summary(self):
        return {
            "schema": TIMING_SCHEMA,
            "clock": "monotonic",
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

    def _timed(self, call, *args):
        started = self._ledger.now()
        try:
            return call(*args)
        finally:
            self._ledger.add(self._bucket, self._ledger.now() - started)

    def write(self, text):
        return self._timed(self._stream.write, text)

    def flush(self):
        return self._timed(self._stream.flush)


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
        """Add an optional phase marker; no effect without a timing ledger."""
        if self._timing is None:
            return
        self._timing.mark(name)
        self._write(
            {
                "sequence": self._sequence,
                "event": "phase",
                "phase": name,
                "seconds_since_start": self._timing.elapsed(),
            }
        )

    def _begin(self):
        return None if self._timing is None else self._timing.now()

    def _finish(self, bucket, started, error=None):
        """Return the nonnegative duration, or None when untimed."""
        if started is None:
            return None
        seconds = max(0.0, self._timing.now() - started)
        self._timing.add(bucket, seconds)
        if error is not None:
            self._timing.interrupted(bucket, seconds, error)
        return seconds

    def __getattr__(self, name):
        member = getattr(self._pair, name)
        if not inspect.iscoroutinefunction(member):
            return member

        async def recorded(*args, **kwargs):
            self._sequence += 1
            sequence = self._sequence
            self._write(
                {
                    "sequence": sequence,
                    "event": "intent",
                    "operation": name,
                    "args": redacted(args),
                    "kwargs": redacted(kwargs),
                }
            )
            started = self._begin()
            try:
                result = await member(*args, **kwargs)
            except BaseException as exc:
                self._finish(name, started, exc)
                raise
            row = {
                "sequence": sequence,
                "event": "completion",
                "operation": name,
                "result": redacted(result),
            }
            seconds = self._finish(name, started)
            if seconds is not None:
                row["seconds"] = seconds
            self._write(row)
            return result

        return recorded

    def _write(self, row):
        started = self._begin()
        self._stream.write(json.dumps(row) + "\n")
        self._stream.flush()
        if started is not None:
            self._timing.add("operation_journal_io", self._timing.now() - started)

    async def release_buttons(self):
        started = self._begin()
        try:
            await self._release_buttons()
        except BaseException as exc:
            self._finish("release_buttons", started, exc)
            raise
        self._finish("release_buttons", started)

    async def _release_buttons(self):
        """Release ordinary held buttons through advertised public owner tools."""
        clients = (
            [self._pair.client] if self._pair.transport == "local_pair" else self._pair.clients
        )
        for owner in range(2):
            client = clients[0] if len(clients) == 1 else clients[owner]
            name = "link_peer_release" if len(clients) == 1 and owner == 1 else "release"
            listed = await client.request("tools/list", {})
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
                    }
                )
                result = await client.tool(name, {"button": button})
                self._write(
                    {
                        "sequence": sequence,
                        "event": "completion",
                        "operation": "release",
                        "result": result,
                    }
                )
