"""Cooperative runtime IO controls: all 14 cases NOTRUN; owned temporary files only."""

import os
import stat

import pytest

from scripts import production_gate_parallel_io as io
from scripts import production_gate_parallel_isolation as isolation
from scripts import production_gate_parallel_runtime_inputs as inputs


class Cancelled(OSError):
    """OSError-shaped callback failure must not be mistaken for filesystem refusal."""


def test_copy_refusal_keeps_partial_target_and_does_not_touch_source(tmp_path):
    source, target = tmp_path / "source", tmp_path / "partial"
    data = b"a" * (3 * io.CHUNK_BYTES)
    source.write_bytes(data)
    pulses = []

    def checkpoint():
        pulses.append(True)
        if target.exists() and target.stat().st_size > 0:
            raise Cancelled("copy cancelled after progress")

    with pytest.raises(Cancelled, match="after progress"):
        io.copy_file_bytes(source, target, checkpoint=checkpoint)
    assert 0 < target.stat().st_size < len(data)
    assert source.read_bytes() == data and len(pulses) > 1


def test_runtime_copy_preserves_links_metadata_ignore_and_services_nested_entries(tmp_path):
    source, target = tmp_path / "source", tmp_path / "copy"
    nested = source / "one" / "two"
    nested.mkdir(parents=True)
    file = nested / "module"
    file.write_bytes(b"payload")
    file.chmod(0o751)
    os.utime(file, ns=(1234567890000000000, 1234567890000000000))
    (source / "link").symlink_to("one/two/module")
    (source / "broken").symlink_to("missing")
    (nested / "__pycache__").mkdir()
    (nested / "__pycache__" / "ignored").write_bytes(b"no")
    observations = []
    inputs._copy_frozen(source, target, checkpoint=lambda: observations.append(target.exists()))
    copied = target / "one/two/module"
    assert copied.read_bytes() == b"payload"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o751
    assert copied.stat().st_mtime_ns == file.stat().st_mtime_ns
    assert os.readlink(target / "link") == "one/two/module"
    assert (target / "broken").is_symlink() and not (target / "broken").exists()
    assert not (target / "one/two/__pycache__").exists()
    assert observations[0] is False and any(observations) and len(observations) > 10


@pytest.mark.parametrize("text", [False, True])
def test_rewrite_cross_chunk_matches_and_services_read_replace_write(tmp_path, monkeypatch, text):
    monkeypatch.setattr(io, "CHUNK_BYTES", 7)
    old, new = ("/old/tree", "/snapshot") if text else (b"/old/tree", b"/snapshot")
    raw = "aaaaaa/old/tree/old/treeZ\r\n" * 5
    file = tmp_path / "editable"
    file.write_bytes(raw.encode())
    calls = []
    checkpoint = lambda: calls.append(True)
    data = io.read_file_data(file, text=text, checkpoint=checkpoint)
    expected = raw.replace("\r\n", "\n") if text else raw.encode()
    assert data == expected
    result = io.replace_file_data(data, old, new, checkpoint=checkpoint)
    assert result == expected.replace(old, new)
    io.write_file_data(file, result, checkpoint=checkpoint)
    assert file.read_bytes() == (result.encode() if text else result)
    assert len(calls) > 30


def test_editable_and_cfg_rewrite_keep_link_policy_and_public_defaults(tmp_path):
    original, snapshot = tmp_path / "old", tmp_path / "new"
    copy = tmp_path / "copy"
    (copy / "deep" / "nested").mkdir(parents=True)
    pth = copy / "deep/nested/project.pth"
    pth.write_text(str(original) + "/src\n")
    (copy / "bin").mkdir()
    link = copy / "bin/python"
    link.symlink_to(str(original) + "/bin/python")
    (copy / "pyvenv.cfg").write_bytes(("home=" + str(original) + "\r\n").encode())
    assert inputs.redirect_editable_paths(copy, original, snapshot) == 1
    assert pth.read_text() == str(snapshot) + "/src\n"
    assert inputs.rebase_links(copy, original, snapshot) == 2
    assert os.readlink(link) == str(snapshot) + "/bin/python"
    assert (copy / "pyvenv.cfg").read_bytes() == ("home=" + str(snapshot) + "\n").encode()


def test_writable_traversal_propagates_callback_oserror_in_nested_directory(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    (root / "a/b").mkdir(parents=True)
    leaf = root / "a/b/leaf"
    leaf.write_bytes(b"x")
    visited = []
    real_entries = io.directory_entries

    def entries(path, **kwargs):
        for child in real_entries(path, **kwargs):
            visited.append(child)
            yield child

    monkeypatch.setattr(inputs, "directory_entries", entries)
    monkeypatch.setattr(inputs.os, "access", lambda path, mode: False)

    def checkpoint():
        if leaf in visited:
            raise Cancelled("nested traversal")

    with pytest.raises(Cancelled, match="nested traversal"):
        inputs.writable_entries(root, checkpoint=checkpoint)
    assert root / "a" in visited and root / "a/b" in visited and leaf in visited


@pytest.mark.parametrize("operation", ["read", "replace", "write", "freeze", "probe"])
def test_refusals_do_not_return_partial_success_or_swallow_oserror(tmp_path, operation):
    root = tmp_path / "tree"
    root.mkdir()
    file = root / "data"
    file.write_bytes(b"x" * (3 * io.CHUNK_BYTES))
    calls = []

    def checkpoint():
        calls.append(True)
        if len(calls) == 3:
            raise Cancelled(operation)

    operations = {
        "read": lambda: io.read_file_data(file, checkpoint=checkpoint),
        "replace": lambda: io.replace_file_data(
            file.read_bytes(), b"x", b"y", checkpoint=checkpoint
        ),
        "write": lambda: io.write_file_data(
            file, b"y" * (3 * io.CHUNK_BYTES), checkpoint=checkpoint
        ),
        "freeze": lambda: isolation.freeze_tree(root, checkpoint=checkpoint),
        "probe": lambda: isolation.write_denied_problems(root, checkpoint=checkpoint),
    }
    with pytest.raises(Cancelled, match=operation):
        operations[operation]()
    assert root.exists() and file.exists()  # refusal never deletes its partial scratch tree


def test_freeze_preserves_execute_bits_and_does_not_chmod_link_target(tmp_path):
    root = tmp_path / "tree"
    (root / "a/b").mkdir(parents=True)
    file = root / "a/b/executable"
    outside = tmp_path / "outside"
    outside.write_bytes(b"external")
    outside.chmod(0o764)
    file.write_bytes(b"internal")
    file.chmod(0o751)
    (root / "link").symlink_to(outside)
    calls = []
    try:
        isolation.freeze_tree(root, checkpoint=lambda: calls.append(True))
        assert stat.S_IMODE(file.stat().st_mode) == 0o551
        assert not any(p.stat().st_mode & 0o222 for p in [root, root / "a", root / "a/b", file])
        assert stat.S_IMODE(outside.stat().st_mode) == 0o764
        assert (root / "link").is_symlink() and len(calls) > 10
    finally:
        # Only these actual test-owned directories, not any synthetic FD or shared tree.
        for path in [root, root / "a", root / "a/b"]:
            path.chmod(0o700)


def test_private_builder_cancellation_uses_original_budget_and_retains_partial_copy(
    tmp_path, monkeypatch
):
    from scripts.production_gate_parallel_measurement_budget import (
        IdentityDeadline,
        MeasurementBudget,
    )

    source, destination = tmp_path / "env", tmp_path / "runtime"
    source.mkdir()
    data = b"x" * (3 * io.CHUNK_BYTES)
    (source / "payload").write_bytes(data)
    partial = destination / "env-0/payload"
    now = [10.0]
    seen = []

    def service():
        if partial.exists() and partial.stat().st_size > 0:
            now[0] = 100.0

    budget = MeasurementBudget(100, 120, clock=lambda: now[0], service=service)

    def probe(interpreter, pythonpath, *, budget, cwd):
        seen.append(budget)
        base = tmp_path / "protected-unused"
        return {
            "prefix": str(source),
            "base_prefix": str(base),
            "real_executable": str(base / "python"),
            "stdlib": str(base / "lib"),
            "platstdlib": str(base / "lib"),
            "sites": [],
            "path": [],
            "libraries": [],
        }

    monkeypatch.setattr(inputs, "probe_runtime", probe)
    with pytest.raises(IdentityDeadline, match="frozen deadline"):
        inputs.build_private_runtimes(
            {"source": str(source / "python")},
            project_root=tmp_path / "project",
            checkout=tmp_path / "snapshot",
            destination=destination,
            budget=budget,
        )
    assert seen == [budget] and budget.deadline == 100 and budget.cleanup_bound() == 120
    assert 0 < partial.stat().st_size < len(data)
    assert (source / "payload").read_bytes() == data


def test_snapshot_copy_cancellation_retains_partial_tree_before_repository_setup(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from scripts.production_gate_parallel_measurement_budget import (
        IdentityDeadline,
        MeasurementBudget,
    )

    project, destination = tmp_path / "project", tmp_path / "snapshot"
    project.mkdir()
    data = b"x" * (3 * io.CHUNK_BYTES)
    (project / "payload").write_bytes(data)
    partial = destination / "checkout/payload"
    now = [10.0]
    calls = []

    def service():
        if partial.exists() and partial.stat().st_size > 0:
            now[0] = 100.0

    def runner(command, **kwargs):
        calls.append((command, kwargs["budget"]))
        return SimpleNamespace(stdout=b"payload\0", returncode=0)

    budget = MeasurementBudget(100, 120, clock=lambda: now[0], service=service, probe_runner=runner)
    monkeypatch.setattr(
        isolation,
        "_init_repository",
        lambda *args, **kwargs: pytest.fail("setup after cancellation"),
    )
    with pytest.raises(IdentityDeadline, match="frozen deadline"):
        isolation.build_snapshot(
            project, tmp_path / "rom", tmp_path / "fixtures", destination, measurement=budget
        )
    assert len(calls) == 1 and calls[0][1].deadline <= budget.deadline
    assert 0 < partial.stat().st_size < len(data) and destination.exists()
    assert budget.deadline == 100 and budget.cleanup_bound() == 120
