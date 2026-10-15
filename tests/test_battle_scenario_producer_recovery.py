"""ROM-free durable-prior-report controls; R3-01 namespace races remain open."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from scripts import produce_battle_scenario as producer
from scripts import produce_battle_scenario_capture as capture
from scripts.produce_battle_scenario_recovery import _ReportRecovery

PRIOR = b'{"prior": "must survive", "binary": "\\u0000"}\n'


def _reopen_prior(directory: Path, prior: bytes = PRIOR) -> _ReportRecovery:
    manifests = list(directory.glob(".report.json.recovery-*/manifest.json"))
    assert len(manifests) == 1, "exactly one named on-disk prior report is required"
    recovery = _ReportRecovery.reopen(manifests[0])
    assert recovery.path.read_bytes() == prior
    assert recovery.size == len(prior)
    assert recovery.sha256 == hashlib.sha256(prior).hexdigest()
    assert recovery.target == directory / "report.json"
    return recovery


@pytest.mark.parametrize("prior", [PRIOR, b""], ids=["nonempty", "empty"])
def test_report_recovery_reopens_after_success_and_owned_rollback(
    tmp_path: Path, prior: bytes
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(prior)
    publication = producer.write_report(report, {"new": True})
    recovery = _reopen_prior(tmp_path, prior)
    assert publication.recovery_metadata() == recovery.metadata()
    assert json.loads(report.read_bytes()) == {"new": True}
    if os.name == "posix":
        assert stat.S_IMODE(recovery.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(recovery.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(recovery.manifest.stat().st_mode) == 0o600
        assert stat.S_IMODE(report.stat().st_mode) == 0o600
    result = publication.withdraw_result()
    assert result.disposition == "restored" and result.as_note() is None
    assert result.recovery is not None
    assert report.read_bytes() == prior
    assert _ReportRecovery.reopen(recovery.manifest).path.read_bytes() == prior
    assert list(tmp_path.glob("*.partial-*")) == []


def test_report_without_prior_preserves_exclusive_owned_rollback(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    publication = producer.write_report(report, {"new": True})
    assert publication.recovery_metadata() is None
    assert list(tmp_path.glob("*.recovery-*")) == []
    assert publication.withdraw() is None
    assert not report.exists()


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_report_restore_failure_retains_new_and_exact_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = producer.write_report(report, {"new": True})
    new_bytes = report.read_bytes()

    def refuse_restore(*_args: Any) -> None:
        raise error_type("injected restore failure")

    monkeypatch.setattr(capture, "_write_bytes_atomically", refuse_restore)
    error = publication.withdraw()
    recovery = _reopen_prior(tmp_path)
    assert error is not None and "could not be restored" in error
    assert str(recovery.manifest) in error
    assert recovery.sha256 in error
    assert f"size={len(PRIOR)}" in error
    assert report.read_bytes() == new_bytes


def test_report_recovery_remains_when_target_is_foreign(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = producer.write_report(report, {"new": True})
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"competitor")
    os.replace(foreign, report)
    error = publication.withdraw()
    recovery = _reopen_prior(tmp_path)
    assert report.read_bytes() == b"competitor"
    assert error is not None and "ownership unresolved" in error
    assert str(recovery.manifest) in error


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_report_recovery_runs_after_an_earlier_rollback_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = producer.write_report(report, {"new": True})
    earlier = capture._OwnedPublication(tmp_path / "fixture", 123, 456)
    real_withdraw = capture._OwnedPublication.withdraw
    attempted: list[Path] = []

    def fail_earlier(self: capture._OwnedPublication) -> str | None:
        attempted.append(self.path)
        if self is earlier:
            raise error_type("earlier rollback failed")
        return real_withdraw(self)

    monkeypatch.setattr(capture._OwnedPublication, "withdraw", fail_earlier)
    primary = RuntimeError("primary publication failure")
    capture._withdraw_all(primary, earlier, publication)
    assert attempted == [earlier.path, report]
    assert report.read_bytes() == PRIOR
    assert any("earlier rollback failed" in note for note in primary.__notes__)
    _reopen_prior(tmp_path)


def test_report_restore_failure_after_earlier_failure_retains_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = producer.write_report(report, {"new": True})
    earlier = capture._OwnedPublication(tmp_path / "fixture", 123, 456)
    real_withdraw = capture._OwnedPublication.withdraw

    def fail_earlier(self: capture._OwnedPublication) -> str | None:
        if self is earlier:
            raise KeyboardInterrupt("earlier rollback interrupted")
        return real_withdraw(self)

    def fail_restore(*_args: Any) -> None:
        raise PermissionError("restore refused")

    monkeypatch.setattr(capture._OwnedPublication, "withdraw", fail_earlier)
    monkeypatch.setattr(capture, "_write_bytes_atomically", fail_restore)
    primary = RuntimeError("primary")
    capture._withdraw_all(primary, earlier, publication)
    recovery = _reopen_prior(tmp_path)
    assert json.loads(report.read_bytes()) == {"new": True}
    assert any("earlier rollback interrupted" in note for note in primary.__notes__)
    assert any(str(recovery.manifest) in note for note in primary.__notes__)


def test_interrupted_report_replace_recovers_prior_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    real_replace = os.replace
    replacements: list[Path] = []

    def replace_then_interrupt(source: Path, destination: Path) -> None:
        real_replace(source, destination)
        replacements.append(Path(destination))
        if len(replacements) == 1:
            raise KeyboardInterrupt("after real report replacement")

    monkeypatch.setattr(producer.os, "replace", replace_then_interrupt)
    with pytest.raises(KeyboardInterrupt, match="after real report replacement") as error:
        producer.write_report(report, {"new": True})
    assert replacements == [report, report]
    assert report.read_bytes() == PRIOR
    recovery = _reopen_prior(tmp_path)
    assert any(str(recovery.manifest) in note for note in error.value.__notes__)


def test_interrupted_replace_and_failed_restore_leave_reopenable_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    real_replace = os.replace
    replacements: list[Path] = []

    def replace_then_fail(source: Path, destination: Path) -> None:
        replacements.append(Path(destination))
        if len(replacements) == 1:
            real_replace(source, destination)
            raise KeyboardInterrupt("primary interruption")
        raise PermissionError("restoration refused")

    monkeypatch.setattr(producer.os, "replace", replace_then_fail)
    with pytest.raises(KeyboardInterrupt, match="primary interruption") as error:
        producer.write_report(report, {"new": True})
    assert replacements == [report, report]
    assert json.loads(report.read_bytes()) == {"new": True}
    recovery = _reopen_prior(tmp_path)
    assert any("restoration refused" in note for note in error.value.__notes__)
    assert any(str(recovery.path) in note for note in error.value.__notes__)
    assert list(tmp_path.glob("*.partial-*")) == []


def test_corrupt_recovery_refuses_restore_without_removing_current_report(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = producer.write_report(report, {"new": True})
    recovery = _reopen_prior(tmp_path)
    recovery.path.write_bytes(b"damaged")
    error = publication.withdraw()
    assert error is not None and "size/hash verification" in error
    assert json.loads(report.read_bytes()) == {"new": True}
    with pytest.raises(producer.ScenarioRefusal, match="size/hash verification"):
        _ReportRecovery.reopen(recovery.manifest)


def test_dangling_report_symlink_is_refused_before_replacement(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.symlink_to(tmp_path / "missing")
    with pytest.raises(producer.ScenarioRefusal, match="non-regular prior report"):
        producer.write_report(report, {"new": True})
    assert report.is_symlink()
    assert os.readlink(report) == str(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("fail_number", [1, 2, 3, 4])
def test_recovery_sync_failure_precedes_destructive_report_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_number: int
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    syncs: list[int] = []
    replacements: list[Path] = []
    real_sync = os.fsync

    def fail_sync(descriptor: int) -> None:
        syncs.append(descriptor)
        if len(syncs) == fail_number:
            raise OSError("recovery sync refused")
        real_sync(descriptor)

    def unexpected_replace(_source: Path, destination: Path) -> None:
        replacements.append(destination)
        raise AssertionError("report replacement must not start")

    monkeypatch.setattr(producer.os, "fsync", fail_sync)
    monkeypatch.setattr(producer.os, "replace", unexpected_replace)
    with pytest.raises(OSError, match="recovery sync refused") as error:
        producer.write_report(report, {"new": True})
    assert report.read_bytes() == PRIOR
    assert replacements == []
    assert any("recovery preparation incomplete" in note for note in error.value.__notes__)


class _UnsafeMemoryPublication:
    """Intentionally unsafe R3-03 baseline: memory-only prior and destructive error cleanup."""

    def __init__(self, report: Path) -> None:
        self.path = report
        self.prior = report.read_bytes()
        report.write_bytes(b'{"new": true}\n')

    def withdraw(self) -> str | None:
        try:
            capture._write_bytes_atomically(self.path, self.prior)
        except OSError:
            self.path.unlink()
            return "restore failed"
        return None


def _assert_restore_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, unsafe: bool
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = (
        _UnsafeMemoryPublication(report) if unsafe else producer.write_report(report, {"new": True})
    )

    def refuse_restore(*_args: Any) -> None:
        raise OSError("restore failure control")

    monkeypatch.setattr(capture, "_write_bytes_atomically", refuse_restore)
    assert publication.withdraw() is not None
    # The observable oracle is persisted bytes, not publication class identity.
    _reopen_prior(tmp_path)
    assert json.loads(report.read_bytes()) == {"new": True}


@pytest.mark.parametrize("unsafe", [False, True], ids=["candidate", "unsafe-baseline"])
def test_restore_recovery_control_rejects_memory_only_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: bool
) -> None:
    if unsafe:
        with pytest.raises(AssertionError, match="named on-disk prior report"):
            _assert_restore_recovery(tmp_path, monkeypatch, unsafe=True)
    else:
        _assert_restore_recovery(tmp_path, monkeypatch, unsafe=False)


def _assert_unreadable_prior_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, unsafe: bool
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    real_open = os.open
    real_read = Path.read_bytes

    def refuse_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if Path(path) == report:
            raise PermissionError("prior is unreadable")
        return real_open(path, *args, **kwargs)

    def refuse_read(path: Path) -> bytes:
        if path == report:
            raise PermissionError("prior is unreadable")
        return real_read(path)

    monkeypatch.setattr(producer.os, "open", refuse_open)
    monkeypatch.setattr(Path, "read_bytes", refuse_read)
    refused = False
    try:
        if unsafe:
            # Historical suppression treats unreadable prior bytes as absence.
            with contextlib.suppress(OSError):
                report.read_bytes()
            report.write_bytes(b'{"new": true}\n')
        else:
            producer.write_report(report, {"new": True})
    except PermissionError:
        refused = True
    assert refused, "unreadable prior report was replaced instead of refused"
    assert real_read(report) == PRIOR


@pytest.mark.parametrize("unsafe", [False, True], ids=["candidate", "unsafe-baseline"])
def test_unreadable_prior_control_rejects_suppression_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: bool
) -> None:
    if unsafe:
        with pytest.raises(AssertionError, match="replaced instead of refused"):
            _assert_unreadable_prior_refused(tmp_path, monkeypatch, unsafe=True)
    else:
        _assert_unreadable_prior_refused(tmp_path, monkeypatch, unsafe=False)


def _assert_definite_eexist_preserves_same_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, unsafe: bool
) -> None:
    output = tmp_path / "fixture"
    real_link = os.link

    def competitor_then_eexist(source: Path, destination: Path) -> None:
        real_link(source, destination)
        raise FileExistsError("competitor published first")

    monkeypatch.setattr(producer.os, "link", competitor_then_eexist)
    if unsafe:
        stage = tmp_path / ".fixture.partial-unsafe"
        stage.write_bytes(b"fixture bytes")
        publication = capture._OwnedPublication(output, *capture._entry_identity(stage))
        try:
            os.link(stage, output)
        except FileExistsError:
            publication.withdraw()
        finally:
            stage.unlink()
    else:
        with pytest.raises(producer.ScenarioRefusal, match="refusing to overwrite"):
            producer._write_fixture_exclusive(output, b"fixture bytes")
    assert output.exists(), "definite EEXIST must not withdraw a competitor's hard link"
    assert output.read_bytes() == b"fixture bytes"


@pytest.mark.parametrize("unsafe", [False, True], ids=["candidate", "unsafe-baseline"])
def test_definite_eexist_control_rejects_preconstructed_ownership_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: bool
) -> None:
    if unsafe:
        with pytest.raises(AssertionError, match="must not withdraw a competitor"):
            _assert_definite_eexist_preserves_same_inode(tmp_path, monkeypatch, unsafe=True)
    else:
        _assert_definite_eexist_preserves_same_inode(tmp_path, monkeypatch, unsafe=False)


def test_manifest_write_failure_refuses_report_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import produce_battle_scenario_recovery as recovery_module

    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)

    def refuse_manifest(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("manifest write refused")

    monkeypatch.setattr(recovery_module.json, "dump", refuse_manifest)
    with pytest.raises(OSError, match="manifest write refused") as error:
        producer.write_report(report, {"new": True})
    assert report.read_bytes() == PRIOR
    assert any("recovery preparation incomplete" in note for note in error.value.__notes__)
    retained = list(tmp_path.glob(".report.json.recovery-*/prior-report"))
    assert len(retained) == 1 and retained[0].read_bytes() == PRIOR


def test_backup_write_failure_refuses_report_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import produce_battle_scenario_recovery as recovery_module

    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    real_fdopen = os.fdopen

    @contextlib.contextmanager
    def fail_backup_stream(descriptor: int, mode: str, **kwargs: Any) -> Any:
        with real_fdopen(descriptor, mode, **kwargs) as stream:
            if mode == "wb":

                class BrokenBackup:
                    def write(self, _payload: bytes) -> None:
                        raise OSError("backup write refused")

                yield BrokenBackup()
            else:
                yield stream

    monkeypatch.setattr(recovery_module.os, "fdopen", fail_backup_stream)
    with pytest.raises(OSError, match="backup write refused") as error:
        producer.write_report(report, {"new": True})
    assert report.read_bytes() == PRIOR
    assert any("recovery preparation incomplete" in note for note in error.value.__notes__)
    assert list(tmp_path.glob("*.partial-*")) == []


def test_prior_copy_is_independent_of_later_in_place_target_edits(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = producer.write_report(report, {"new": True})
    recovery = _reopen_prior(tmp_path)
    report.write_bytes(b"edited new report")
    assert recovery.path.read_bytes() == PRIOR
    assert publication.withdraw() is None
    assert report.read_bytes() == PRIOR
    report.write_bytes(b"edited restored report")
    # Restoration uses a fresh inode, never a hard link to the canonical copy.
    assert _ReportRecovery.reopen(recovery.manifest).path.read_bytes() == PRIOR


def _assert_later_recovery_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, unsafe: bool
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    publication = producer.write_report(report, {"new": True})
    earlier = capture._OwnedPublication(tmp_path / "fixture", 123, 456)
    real_withdraw = capture._OwnedPublication.withdraw

    def interrupt_earlier(self: capture._OwnedPublication) -> str | None:
        if self is earlier:
            raise KeyboardInterrupt("earlier rollback interrupted")
        return real_withdraw(self)

    monkeypatch.setattr(capture._OwnedPublication, "withdraw", interrupt_earlier)
    if unsafe:
        # Historical loop aborts at the first thrown withdrawal. Suppress only
        # at this control's boundary so its observable postcondition is checked.
        with contextlib.suppress(KeyboardInterrupt):
            for token in (earlier, publication):
                token.withdraw()
    else:
        capture._withdraw_all(RuntimeError("primary"), earlier, publication)
    assert report.read_bytes() == PRIOR, "later report restoration was never attempted"
    _reopen_prior(tmp_path)


@pytest.mark.parametrize("unsafe", [False, True], ids=["candidate", "unsafe-baseline"])
def test_earlier_interruption_control_rejects_abort_on_first_failure_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: bool
) -> None:
    if unsafe:
        with pytest.raises(AssertionError, match="restoration was never attempted"):
            _assert_later_recovery_after_interruption(tmp_path, monkeypatch, unsafe=True)
    else:
        _assert_later_recovery_after_interruption(tmp_path, monkeypatch, unsafe=False)


def test_prior_read_failure_after_open_refuses_report_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import produce_battle_scenario_recovery as recovery_module

    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    real_fdopen = os.fdopen

    @contextlib.contextmanager
    def fail_source_stream(descriptor: int, mode: str, **kwargs: Any) -> Any:
        with real_fdopen(descriptor, mode, **kwargs) as stream:
            if mode == "rb":

                class BrokenSource:
                    def fileno(self) -> int:
                        return stream.fileno()

                    def read(self, _size: int) -> bytes:
                        raise PermissionError("opened prior report became unreadable")

                yield BrokenSource()
            else:
                yield stream

    monkeypatch.setattr(recovery_module.os, "fdopen", fail_source_stream)
    with pytest.raises(PermissionError, match="became unreadable") as error:
        producer.write_report(report, {"new": True})
    assert report.read_bytes() == PRIOR
    assert any("recovery preparation incomplete" in note for note in error.value.__notes__)


def test_recovery_files_and_directories_sync_before_report_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    real_sync = os.fsync
    real_replace = os.replace
    synchronized: list[str] = []
    replaced: list[Path] = []

    def record_sync(descriptor: int) -> None:
        synchronized.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        real_sync(descriptor)

    def verify_replace(source: Path, destination: Path) -> None:
        assert synchronized == ["file", "file", "directory", "directory", "file"]
        replaced.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(producer.os, "fsync", record_sync)
    monkeypatch.setattr(producer.os, "replace", verify_replace)
    producer.write_report(report, {"new": True})
    assert replaced == [report]
    _reopen_prior(tmp_path)
