"""Durable intent/completion journal for acceptance pair operations."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path


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


class JournalPair:
    """Record driver-level operations; internal RPCs remain the pair's contract.

    This is not an emulator hook or memory access. A missing completion retains
    the pending operation on failure and is never silently repaired.
    """

    def __init__(self, pair, stream):
        self._pair = pair
        self._stream = stream
        self._sequence = 0

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
            result = await member(*args, **kwargs)
            self._write(
                {
                    "sequence": sequence,
                    "event": "completion",
                    "operation": name,
                    "result": redacted(result),
                }
            )
            return result

        return recorded

    def _write(self, row):
        self._stream.write(json.dumps(row) + "\n")
        self._stream.flush()

    async def release_buttons(self):
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
