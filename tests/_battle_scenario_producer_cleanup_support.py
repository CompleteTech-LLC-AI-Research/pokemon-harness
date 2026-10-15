"""Supporting cleanup controls; test nodes remain in their original module."""

from __future__ import annotations

import os
from pathlib import Path

from scripts import produce_battle_scenario_capture as capture


def _token(path):
    entry = path.lstat()
    return capture._OwnedPublication(path, entry.st_ino, entry.st_dev)


def _recovery_handle_faults(
    monkeypatch, report, *, close_roles=(), fdopen_role=None, close_type=SystemExit
):
    """Real handles, with a shim that closes once then reports an injected failure."""
    real_open, real_fdopen, real_close = os.open, os.fdopen, os.close
    handles, attempts = {}, []
    wrapping_failure = OSError("fdopen failed before ownership transfer")

    def role(path):
        path = Path(path)
        if path == report:
            return "source"
        if path.name == "prior-report":
            return "backup"
        if path.name == "manifest.json":
            return "manifest"
        if ".recovery-" in path.name and path.is_dir():
            return "directory"
        return "other"

    def opening(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        handles[descriptor] = role(path)
        return descriptor

    class Stream:
        def __init__(self, wrapped, name):
            self.wrapped, self.name = wrapped, name

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def close(self):
            attempts.append((self.name, "stream"))
            self.wrapped.close()
            if self.name in close_roles:
                error = close_type(f"{self.name} close failed")
                handles.setdefault("close_failures", []).append(error)
                raise error

    def wrapping(descriptor, *args, **kwargs):
        name = handles.get(descriptor, "other")
        if name == fdopen_role:
            raise wrapping_failure
        stream = real_fdopen(descriptor, *args, **kwargs)
        return Stream(stream, name) if name in {"source", "backup", "manifest"} else stream

    def closing(descriptor):
        name = handles.get(descriptor, "other")
        attempts.append((name, "descriptor"))
        real_close(descriptor)
        if name in close_roles:
            error = close_type(f"{name} descriptor close failed")
            handles.setdefault("close_failures", []).append(error)
            raise error

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "fdopen", wrapping)
    monkeypatch.setattr(os, "close", closing)
    return handles, attempts, wrapping_failure
