"""Bounded evidence reads with cooperative checkpoints and a digest of the same bytes."""

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_identity import CHUNK_BYTES, noop_checkpoint
from scripts.production_gate_parallel_validation import loads_strict

MAX_JSON_EVIDENCE_BYTES = 64 * 1024 * 1024


def read_evidence_bytes(
    path: Path,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
    max_bytes: int = MAX_JSON_EVIDENCE_BYTES,
    regular_nofollow: bool = False,
) -> tuple[bytes, str]:
    """Read/hash the same bounded bytes; no partial result is returned after expiry."""

    checkpoint()
    digest, pieces, size = hashlib.sha256(), [], 0
    if regular_nofollow:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("evidence is not a regular file")
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
    else:
        handle = path.open("rb")
    with handle:
        while True:
            checkpoint()
            chunk = handle.read(CHUNK_BYTES)
            checkpoint()
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("JSON evidence exceeds the byte bound")
            digest.update(chunk)
            pieces.append(chunk)
    checkpoint()
    raw = b"".join(pieces)
    checkpoint()
    return raw, digest.hexdigest()


def read_json_evidence(
    path: Path,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
    max_bytes: int = MAX_JSON_EVIDENCE_BYTES,
) -> tuple[Any, int, str]:
    """No partial document/digest is returned after expiry, oversize, or malformed JSON."""

    raw, digest = read_evidence_bytes(path, checkpoint=checkpoint, max_bytes=max_bytes)
    document = loads_strict(raw.decode("utf-8"))
    checkpoint()
    return document, len(raw), digest
