"""Unit tests for the inspection-only local pair checkpoint helper."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokered_harness.ownership import owner_for
from tests import _pair_checkpoints as checkpoints


class _Backend:
    edge_count = 11
    peer_unarmed_edges = 2
    peer_master_edges = 3
    peer_rearm_attempts = 4
    peer_rearm_successes = 5


class _Serial:
    def __init__(self, marker: bytes) -> None:
        self.SB = 0xA0
        self.SC = 0x80
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.double_speed = 0
        self.last_cycles = 64
        self._cycles_to_interrupt = 1 << 31
        self.clock = 64
        self.clock_target = 1 << 31
        self._shift_register = 0xA0
        self._bits_remaining = 4
        self.transfer_generation = 9
        self.backend_failed = False
        self.owner_pump_active = False
        self.owner_poll_enabled = False
        self._owner_pump_claim = None
        self._owner_boundary_pending = {}
        self.backend = _Backend()
        self.marker = marker


class _Endpoint:
    def __init__(self, marker: bytes, *, physical: tuple[int, int]) -> None:
        self.mb = SimpleNamespace(
            serial=_Serial(marker),
            cpu=SimpleNamespace(cycles=128),
        )
        self.register_file = SimpleNamespace(PC=0x150)
        self.frame_count = 7
        self.events = [3]
        self.queued_input = [(9, 4)]
        self.physical = physical
        self.save_calls = 0
        self.tick_calls = 0
        self.input_calls = 0

    def save_state(self, stream) -> None:
        self.save_calls += 1
        stream.write(self.mb.serial.marker)

    def tick(self, *_args, **_kwargs) -> None:
        self.tick_calls += 1

    def button(self, *_args, **_kwargs) -> None:
        self.input_calls += 1


class _Link:
    _network_backend = None
    _step_active = False
    _peer_progress_active = False
    _scheduler_fault = None

    def __init__(self) -> None:
        self.a = _Endpoint(b"A", physical=(0, 100))
        self.b = _Endpoint(b"B", physical=(0, 100))
        self._pyboys = [self.a, self.b]
        self._operation_lock = threading.RLock()
        self._epoch_mbs = (self.a.mb, self.b.mb)
        self._epoch_serials = (self.a.mb.serial, self.b.mb.serial)
        self._physical_generations = (0, 0)
        self._physical_now = (100, 100)
        self._physical_expected = (100, 100)
        self._epoch_expected = (64, 64)
        self._coordinator = object()
        self.step_calls = 0

    @property
    def paired(self) -> bool:
        return True

    @property
    def coordinator(self):
        return self._coordinator

    @staticmethod
    def _read_physical_clock(endpoint):
        return endpoint.physical

    def step(self, *_args, **_kwargs):
        self.step_calls += 1
        raise AssertionError("checkpoint helper must not advance the pair")


@pytest.fixture
def link() -> _Link:
    return _Link()


def test_capture_is_inspection_only_and_manifest_is_last(tmp_path: Path, link: _Link):
    writer = checkpoints.PairCheckpointWriter(
        tmp_path,
        max_count=2,
        selected_frame_ordinals=(7,),
    )

    assert writer.capture(link, 6) is None
    assert list(tmp_path.iterdir()) == []

    artifact = writer.capture(link, 7, metadata={"phase": "trade"})
    assert artifact is not None
    assert artifact.is_dir()
    assert (artifact / "side-0.state").read_bytes() == b"A"
    assert (artifact / "side-1.state").read_bytes() == b"B"
    manifest = json.loads((artifact / "manifest.json").read_text())
    assert manifest["inspection_only"] is True
    assert manifest["replay_supported"] is False
    assert manifest["frame_ordinal"] == 7
    assert manifest["caller_metadata"] == {"phase": "trade"}
    assert manifest["provider"]["endpoint_count"] == 2
    assert manifest["endpoints"][0]["serial"]["transfer_generation"] == 9
    assert manifest["endpoints"][0]["backend"]["counters"]["edge_count"] == 11
    assert manifest["endpoints"][0]["pending_input"] == {
        "events": [3],
        "queued_input": [[9, 4]],
    }
    assert link.a.save_calls == link.b.save_calls == 1
    assert link.a.tick_calls == link.b.tick_calls == 0
    assert link.a.input_calls == link.b.input_calls == 0
    assert link.step_calls == 0
    assert writer.captured_count == 1


def test_output_root_and_bound_are_explicit():
    with pytest.raises(ValueError, match="output_root"):
        checkpoints.PairCheckpointWriter("", max_count=1)
    with pytest.raises(ValueError, match="output_root"):
        checkpoints.PairCheckpointWriter(None, max_count=1)
    with pytest.raises(ValueError, match="max_count"):
        checkpoints.PairCheckpointWriter("out", max_count=0)
    with pytest.raises(ValueError, match="selected"):
        checkpoints.PairCheckpointWriter("out", max_count=1, selected_frame_ordinals=(1, 2))


def test_selected_ordinals_and_count_are_bounded(tmp_path: Path, link: _Link):
    writer = checkpoints.PairCheckpointWriter(tmp_path, max_count=1)
    assert writer.capture(link, 3) is not None
    with pytest.raises(checkpoints.PairCheckpointError, match="already selected"):
        writer.capture(link, 3)
    with pytest.raises(checkpoints.PairCheckpointError, match="count limit"):
        writer.capture(link, 4)


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("_step_active", True, "scheduler is active"),
        ("_scheduler_fault", "fault", "scheduler fault"),
        ("_network_backend", object(), "network mode"),
    ],
)
def test_refuses_unsettled_or_nonlocal_pair(
    tmp_path: Path,
    link: _Link,
    attribute: str,
    value: object,
    message: str,
):
    setattr(link, attribute, value)
    writer = checkpoints.PairCheckpointWriter(tmp_path, max_count=1)
    with pytest.raises(checkpoints.PairCheckpointError, match=message):
        writer.capture(link, 0)
    assert link.a.save_calls == link.b.save_calls == 0
    assert list(tmp_path.iterdir()) == []


def test_refuses_epoch_mismatch_without_saving(tmp_path: Path, link: _Link):
    link.a.physical = (1, 100)
    writer = checkpoints.PairCheckpointWriter(tmp_path, max_count=1)
    with pytest.raises(checkpoints.PairCheckpointError, match="load epoch"):
        writer.capture(link, 0)
    assert link.a.save_calls == link.b.save_calls == 0


def test_both_locks_cover_native_snapshot_but_not_filesystem_io(tmp_path: Path, link: _Link):
    entered = threading.Event()
    release = threading.Event()
    result: list[Path] = []
    errors: list[BaseException] = []

    def blocking_save(stream):
        entered.set()
        assert release.wait(5)
        stream.write(b"A")

    link.a.save_state = blocking_save
    writer = checkpoints.PairCheckpointWriter(tmp_path, max_count=1)

    def capture():
        try:
            result.append(writer.capture(link, 0))
        except RuntimeError as error:  # pragma: no cover - assertion below
            errors.append(error)

    thread = threading.Thread(target=capture)
    thread.start()
    assert entered.wait(5)
    assert not link._operation_lock.acquire(blocking=False)
    endpoint_owners = [owner_for(endpoint) for endpoint in (link.a, link.b)]
    for endpoint_owner in endpoint_owners:
        assert not endpoint_owner.lock.acquire(blocking=False)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert result and result[0].is_dir()

    # Pause the external writer itself.  Both endpoint owners and the
    # provider lock must be available while filesystem I/O is in progress.
    write_entered = threading.Event()
    write_release = threading.Event()
    real_write = writer._write_snapshot

    def blocking_write(snapshot):
        write_entered.set()
        assert write_release.wait(5)
        return real_write(snapshot)

    writer = checkpoints.PairCheckpointWriter(tmp_path / "second", max_count=1)
    real_write = writer._write_snapshot
    writer._write_snapshot = blocking_write
    second_result: list[Path] = []
    second_errors: list[RuntimeError] = []

    def second_capture():
        try:
            second_result.append(writer.capture(link, 1))
        except RuntimeError as error:  # pragma: no cover - assertion below
            second_errors.append(error)

    second = threading.Thread(target=second_capture)
    second.start()
    assert write_entered.wait(5)
    for endpoint in (link.a, link.b):
        endpoint_owner = owner_for(endpoint)
        assert endpoint_owner.lock.acquire(blocking=False)
        endpoint_owner.lock.release()
    assert link._operation_lock.acquire(blocking=False)
    link._operation_lock.release()
    write_release.set()
    second.join(5)
    assert not second.is_alive()
    assert second_errors == []
    assert second_result and second_result[0].is_dir()


def test_failed_native_save_writes_no_manifest(tmp_path: Path, link: _Link):
    def fail(stream):
        raise RuntimeError("synthetic save failure")

    link.b.save_state = fail
    writer = checkpoints.PairCheckpointWriter(tmp_path, max_count=1)
    with pytest.raises(RuntimeError, match="synthetic save failure"):
        writer.capture(link, 0)
    assert list(tmp_path.iterdir()) == []
    assert writer.captured_count == 0


def test_manifest_replace_is_last_filesystem_operation(tmp_path: Path, link: _Link, monkeypatch):
    calls: list[tuple[str, str]] = []
    real_replace = checkpoints.os.replace

    def record_replace(source, target):
        calls.append((Path(source).name, Path(target).name))
        real_replace(source, target)

    monkeypatch.setattr(checkpoints.os, "replace", record_replace)
    writer = checkpoints.PairCheckpointWriter(tmp_path, max_count=1)
    artifact = writer.capture(link, 0)
    assert artifact is not None
    assert calls[-1][1] == "manifest.json"
    assert [target for _source, target in calls[:-1]] == ["side-0.state", "side-1.state"]


def test_filesystem_failure_leaves_no_manifest_and_allows_one_bounded_retry(
    tmp_path: Path,
    link: _Link,
    monkeypatch,
):
    real_replace = checkpoints.os.replace
    failed_dirs: list[Path] = []

    def fail_manifest(source, target):
        target_path = Path(target)
        if target_path.name == "manifest.json":
            failed_dirs.append(target_path.parent)
            raise OSError("synthetic disk-full failure")
        real_replace(source, target)

    monkeypatch.setattr(checkpoints.os, "replace", fail_manifest)
    writer = checkpoints.PairCheckpointWriter(tmp_path, max_count=1)
    with pytest.raises(OSError, match="disk-full"):
        writer.capture(link, 0)
    assert writer.captured_count == 0
    assert len(failed_dirs) == 1
    assert not (failed_dirs[0] / "manifest.json").exists()

    monkeypatch.setattr(checkpoints.os, "replace", real_replace)
    retried = writer.capture(link, 0)
    assert retried is not None
    assert (retried / "manifest.json").exists()
    with pytest.raises(checkpoints.PairCheckpointError, match="count limit"):
        writer.capture(link, 1)
