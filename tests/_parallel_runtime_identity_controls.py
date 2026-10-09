"""Real file hashing with actual scheduler checkpoints; process signals are controlled."""

import hashlib
from pathlib import Path

import pytest

from scripts.production_gate_parallel_identity import (
    CHUNK_BYTES,
    IdentityDeadline,
    sha256_file,
    tree_digest,
)
from tests._parallel_runtime_deadline_controls import watched_jobs


def hashing_deadline_case(tmp_path, monkeypatch, kind):
    """A sibling is notified while hashing and own expiry closes the open file."""

    payload = b"deadline-control" * (CHUNK_BYTES // 16 + 1) * 6
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    assert sha256_file(source) == hashlib.sha256(payload).hexdigest()
    baseline = tree_digest(tmp_path)
    assert baseline[1] == 1
    watch, clock, jobs, sent = watched_jobs((10.0, 0.5))
    handles = []
    original_open = Path.open

    def observed_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == source:
            handles.append(handle)
        return handle

    def checkpoint():
        clock.now += 0.1
        watch.pulse()
        if clock.now >= 1.0:
            raise IdentityDeadline("controlled hash budget exhausted")

    monkeypatch.setattr(Path, "open", observed_open)
    with pytest.raises(IdentityDeadline, match="controlled hash budget"):
        if kind == "file":
            sha256_file(source, checkpoint=checkpoint)
        else:
            tree_digest(tmp_path, checkpoint=checkpoint)
    assert handles and all(handle.closed for handle in handles)
    assert len(sent) == 1 and sent[0][0] == "second"
    assert 0.5 <= sent[0][1] < 0.7
    assert jobs[1].stop_requested and jobs[1].cleanup_deadline == 0.75
    assert not jobs[0].stop_requested
    # Removing the expired budget restores the exact byte identity, not a cached partial hash.
    assert sha256_file(source) == hashlib.sha256(payload).hexdigest()
    assert tree_digest(tmp_path) == baseline
