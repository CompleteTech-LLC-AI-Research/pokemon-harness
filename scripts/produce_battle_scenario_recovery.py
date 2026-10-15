"""Private on-disk prior-report recovery for the scenario producer.

This protocol protects prior bytes across Python exceptions/process interruption,
not power loss or uncooperative writes to the destination namespace. Callers
must serialize report replacement and rollback with other writers. In particular,
an inode probe is not an atomic conditional unlink/replace (issue #610 R3-01).
Recovery directories are deliberately retained, including after successful report
replacement or restoration; deleting them is an explicit operator decision.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from scripts.produce_battle_scenario_model import ScenarioRefusal, _note
from scripts.produce_battle_scenario_transaction import normal_guard, resource_scope


@dataclass(frozen=True)
class _ReportRecovery:
    """A named, reopenable recovery entry; no prior report bytes live here."""

    manifest: Path
    target: Path
    size: int
    sha256: str

    @property
    def path(self) -> Path:
        return self.manifest.parent / "prior-report"

    def metadata(self) -> dict[str, Any]:
        """Stable recovery evidence for rollback consumers (not report schema)."""
        return {
            "manifest": str(self.manifest),
            "path": str(self.path),
            "target": str(self.target),
            "size": self.size,
            "sha256": self.sha256,
            "durability": "process-interruption; no power-loss guarantee",
        }

    def describe(self) -> str:
        return (
            f"prior report recovery retained at {self.path}; manifest={self.manifest}; "
            f"size={self.size}; sha256={self.sha256}; reopen and verify the retained "
            "entry before restoring it to the target under exclusive writer access"
        )

    def read_verified(self) -> bytes:
        """Read the retained file afresh and refuse changed/incomplete bytes."""
        normal_guard("recovery verification")
        payload = self.path.read_bytes()
        normal_guard("recovery verification")
        if len(payload) != self.size or hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ScenarioRefusal(
                f"prior report recovery failed size/hash verification: {self.path}"
            )
        return payload

    @classmethod
    def reopen(cls, manifest: str | Path) -> _ReportRecovery:
        """Reconstruct recovery evidence from disk without a live publication."""
        path = Path(manifest).absolute()
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("schema") != 1
            or data.get("entry") != "prior-report"
            or type(data.get("size")) is not int
            or data["size"] < 0
            or not isinstance(data.get("target"), str)
            or not Path(data["target"]).is_absolute()
            or not isinstance(data.get("sha256"), str)
            or len(data["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in data["sha256"])
        ):
            raise ScenarioRefusal(f"invalid prior report recovery manifest: {path}")
        recovery = cls(path, Path(data["target"]), data["size"], data["sha256"])
        recovery.read_verified()
        return recovery


@dataclass(frozen=True)
class _WithdrawalResult:
    """Internal disposition with a str-or-None adapter for existing callers."""

    disposition: Literal["removed", "restored", "foreign-preserved", "unresolved"]
    recovery: _ReportRecovery | None = None
    error: str | None = None
    observation: str | None = None
    exception_type: str | None = None
    cleanup: tuple[dict[str, Any], ...] = ()

    def as_note(self) -> str | None:
        return self.error


@contextmanager
def _recovery_stream(path: Path, flags: int, mode: str, *, encoding=None):
    """Own the returned descriptor until fdopen succeeds, then own its stream.

    The scope claims each close before invoking it. A body/fdopen failure stays
    primary even if close raises; with no earlier failure, close prevents success.
    """
    with resource_scope() as scope:
        descriptor = os.open(path, flags, 0o600)
        raw = scope.action(object(), path, "recovery descriptor close",
                           lambda: os.close(descriptor))
        stream = os.fdopen(descriptor, mode, encoding=encoding)
        scope.disarm(raw)  # fdopen has returned successfully; ownership transferred
        wrapped = scope.action(object(), path, "recovery stream close", stream.close)
        yield stream
        scope.perform(wrapped)


def _sync_recovery_directory(directory: Path) -> None:
    """Require directory fsync support; preserve its failure across close."""
    normal_guard("recovery directory sync")
    with resource_scope() as scope:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        close = scope.action(object(), directory, "recovery directory close",
                             lambda: os.close(descriptor))
        os.fsync(descriptor)
        scope.perform(close)
    normal_guard("recovery directory sync")


def _report_stamp(entry: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        entry.st_ino,
        entry.st_dev,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    )


def _secure_prior_report(target: Path) -> _ReportRecovery | None:
    """Secure readable regular prior bytes before any destructive replacement.

    Only an absent directory entry means no prior report. A dangling symlink,
    unreadable file, changed read, or unsupported entry type refuses publication.
    The metadata checks detect observed changes; they do not lock the namespace
    or establish an atomic relationship with a later report replacement.

    A 0700 sibling directory contains the 0600 prior copy and a 0600 manifest.
    Both files are flushed/fsynced and closed, then the recovery directory and
    its parent are fsynced before returning. Unsupported directory sync refuses
    replacement. Entries are never automatically removed. This is not power-loss
    certification: subsequent replacement/rollback is not a synced journal.
    """
    normal_guard("prior report inspection")
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(before.st_mode):
        raise IsADirectoryError(errno.EISDIR, "report target is a directory", str(target))
    if not stat.S_ISREG(before.st_mode):
        raise ScenarioRefusal(f"refusing to replace a non-regular prior report: {target}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory = None
    try:
        with _recovery_stream(target, flags, "rb") as source:
            if _report_stamp(os.fstat(source.fileno())) != _report_stamp(before):
                raise ScenarioRefusal(f"prior report changed before recovery read: {target}")
            directory = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.recovery-", dir=target.parent)
            ).absolute()
            manifest = directory / "manifest.json"
            prior = directory / "prior-report"
            digest = hashlib.sha256()
            size = 0
            with _recovery_stream(prior, os.O_CREAT | os.O_EXCL | os.O_WRONLY, "wb") as backup:
                while True:
                    normal_guard("prior report copy")
                    chunk = source.read(1024 * 1024)
                    normal_guard("prior report copy")
                    if not chunk:
                        break
                    backup.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    normal_guard("prior report copy")
                backup.flush()
                normal_guard("prior report payload sync")
                os.fsync(backup.fileno())
                normal_guard("prior report payload sync")
            if (
                _report_stamp(os.fstat(source.fileno())) != _report_stamp(before)
                or _report_stamp(os.lstat(target)) != _report_stamp(before)
                or size != before.st_size
            ):
                raise ScenarioRefusal(f"prior report changed during recovery read: {target}")
            recovery = _ReportRecovery(manifest, target.absolute(), size, digest.hexdigest())
            data = {
                **recovery.metadata(),
                "schema": 1,
                "entry": "prior-report",
                "source_stamp": list(_report_stamp(before)),
                "original_kind": "regular-file",
                "original_identity": {"inode": before.st_ino, "device": before.st_dev},
                "snapshot_semantics": (
                    "observed independent byte copy; actual prior report requires exclusive writers"
                ),
                "transaction_id": directory.name,
                "phase": "snapshot-prepared; subsequent publication outcome unknown",
                "sync_protocol": ["payload", "manifest", "recovery-directory", "parent"],
                "sync_status": "pre-directory-sync manifest; not a completion receipt",
            }
            with _recovery_stream(
                manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, "w", encoding="utf-8"
            ) as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                normal_guard("prior report manifest sync")
                os.fsync(stream.fileno())
                normal_guard("prior report manifest sync")
            # Verify persisted bytes before permitting replacement, not merely
            # the digest of the source stream used to create the copy.
            recovery.read_verified()
            _sync_recovery_directory(directory)
            _sync_recovery_directory(target.parent)
            return recovery
    except BaseException as exc:
        if directory is not None:
            _note(
                exc,
                f"prior report recovery preparation incomplete at {directory}; "
                f"original target was not replaced; inspect {directory / 'manifest.json'} and prior-report",
            )
        raise
