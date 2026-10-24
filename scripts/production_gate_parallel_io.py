"""Bounded evidence reads with cooperative checkpoints and a digest of the same bytes."""

import hashlib
import os
import shutil
import stat
from collections.abc import Callable, Iterator
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


def directory_entries(
    root: Path, *, checkpoint: Callable[[], None] = noop_checkpoint, ignore_errors: bool = False
) -> Iterator[Path]:
    """Stream entries without first materializing an entire directory."""

    checkpoint()
    try:
        scanner = os.scandir(root)
    except OSError:
        if ignore_errors:
            return
        raise
    with scanner as entries:
        while True:
            checkpoint()
            try:
                entry = next(entries)
            except StopIteration:
                break
            except OSError:
                if ignore_errors:
                    break
                raise
            checkpoint()
            yield Path(entry.path)
    checkpoint()


def tree_entries(
    root: Path,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
    exclude_dirs: frozenset[str] = frozenset(),
) -> Iterator[Path]:
    """Postorder entries, excluding root, never following directory symlinks."""

    for path in directory_entries(root, checkpoint=checkpoint):
        checkpoint()
        if path.is_dir():
            if path.name in exclude_dirs:
                continue
            if not path.is_symlink():
                yield from tree_entries(path, checkpoint=checkpoint, exclude_dirs=exclude_dirs)
        checkpoint()
        yield path


def copy_file_bytes(
    source: Path, target: Path, *, checkpoint: Callable[[], None] = noop_checkpoint
) -> None:
    """Chunked copyfile bytes; callers retain metadata policy and partial targets.

    No callback failure is swallowed or followed by automatic target deletion.
    This is cooperative servicing, not a bound on arbitrary filesystem syscalls.
    """

    checkpoint()
    if target.exists() and os.path.samefile(source, target):
        raise shutil.SameFileError(source, target)
    for path in (source, target):
        checkpoint()
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISFIFO(mode):
            raise shutil.SpecialFileError(f"{path} is a named pipe")
    with source.open("rb") as reader, target.open("wb") as writer:
        while True:
            checkpoint()
            chunk = reader.read(CHUNK_BYTES)
            checkpoint()
            if not chunk:
                break
            if writer.write(chunk) != len(chunk):
                raise OSError("private copy write was incomplete")
            checkpoint()
    checkpoint()


def read_file_data(
    path: Path, *, text: bool = False, checkpoint: Callable[[], None] = noop_checkpoint
) -> bytes | str:
    """Chunked input for rewrites; full content is still retained in memory."""

    pieces = []
    checkpoint()
    with path.open("r" if text else "rb", **({"encoding": "utf-8"} if text else {})) as handle:
        while True:
            checkpoint()
            chunk = handle.read(CHUNK_BYTES)
            checkpoint()
            if not chunk:
                break
            pieces.append(chunk)
    checkpoint()
    result = ("" if text else b"").join(pieces)
    checkpoint()
    return result


def replace_file_data(data, old, new, *, checkpoint: Callable[[], None] = noop_checkpoint):
    """Match normal nonoverlapping replace, retaining cross-chunk matches."""

    if not old:
        raise ValueError("rewrite source spelling must not be empty")
    pieces, carry = [], data[:0]
    for offset in range(0, len(data), CHUNK_BYTES):
        checkpoint()
        carry += data[offset : offset + CHUNK_BYTES]
        boundary = max(0, len(carry) - len(old) + 1)
        position = carry.find(old)
        while 0 <= position < boundary:
            boundary = max(boundary, position + len(old))
            position = carry.find(old, position + len(old))
        pieces.append(carry[:boundary].replace(old, new))
        carry = carry[boundary:]
        checkpoint()
    pieces.append(carry.replace(old, new))
    checkpoint()
    result = data[:0].join(pieces)
    checkpoint()
    return result


def write_file_data(path: Path, data, *, checkpoint: Callable[[], None] = noop_checkpoint) -> None:
    """Rewrite an existing inode in chunks; failure retains partial private bytes."""

    checkpoint()
    text = isinstance(data, str)
    with path.open("w" if text else "wb", **({"encoding": "utf-8"} if text else {})) as handle:
        for offset in range(0, len(data), CHUNK_BYTES):
            checkpoint()
            chunk = data[offset : offset + CHUNK_BYTES]
            if handle.write(chunk) != len(chunk):
                raise OSError("private rewrite was incomplete")
            checkpoint()
    checkpoint()
