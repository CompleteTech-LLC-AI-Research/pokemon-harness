"""Authored controls: private immutable snapshot, disjoint ports, tripwire (#107.2, finding 6)."""

import os
import subprocess

import pytest

from scripts.production_gate_parallel_isolation import (
    IsolationError,
    assign_port_ranges,
    build_snapshot,
    diff_snapshots,
    distinct_tree_problems,
    isolation_environment,
    port_range_problems,
    snapshot_tree,
    thaw_and_remove,
)


def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "a.py").write_text("x = 1\n")
    (root / "tool.sh").write_text("#!/bin/sh\n")
    os.chmod(root / "tool.sh", 0o755)
    for argv in (
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "c",
        ],
    ):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    rom, fixtures = tmp_path / "rom", tmp_path / "fixtures"
    rom.mkdir()
    fixtures.mkdir()
    (rom / "red.gb").write_bytes(b"rom")
    (fixtures / "f.state").write_bytes(b"fx")
    return root, rom, fixtures


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root; see the root test")
def test_the_snapshot_is_a_private_copy_and_writes_are_denied_by_the_os(tmp_path):
    root, rom, fixtures = repo(tmp_path)
    info = build_snapshot(root, rom, fixtures, tmp_path / "run" / "snapshot")
    copy = tmp_path / "run" / "snapshot" / "checkout" / "scripts" / "a.py"
    assert info["write_probe"] == "denied" and copy.read_text() == "x = 1\n"
    assert (tmp_path / "run" / "snapshot" / "assets" / "rom" / "red.gb").read_bytes() == b"rom"
    assert os.access(tmp_path / "run" / "snapshot" / "checkout" / "tool.sh", os.X_OK)
    with pytest.raises(PermissionError):
        copy.write_text("tamper")
    with pytest.raises(PermissionError):
        (copy.parent / "new.py").write_text("x")
    (root / "scripts" / "a.py").write_text("x = 2\n")
    assert copy.read_text() == "x = 1\n"  # the source checkout is independent of the snapshot
    thaw_and_remove(tmp_path / "run" / "snapshot")
    assert not (tmp_path / "run" / "snapshot").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="only when permission bits do not bind this process")
def test_without_write_denial_the_snapshot_fails_closed_with_the_observed_evidence(tmp_path):
    root, rom, fixtures = repo(tmp_path)
    with pytest.raises(IsolationError, match="isolation capability not established"):
        build_snapshot(root, rom, fixtures, tmp_path / "run" / "snapshot")


def test_a_second_snapshot_never_reuses_a_destination(tmp_path):
    root, rom, fixtures = repo(tmp_path)
    (tmp_path / "taken").mkdir()
    with pytest.raises(IsolationError, match="already exists"):
        build_snapshot(root, rom, fixtures, tmp_path / "taken")


def test_port_ranges_are_disjoint_and_overlap_is_reported():
    ranges = assign_port_ranges(["b-job", "a-job", "c-job"])
    assert ranges == {"a-job": (30000, 500), "b-job": (30500, 500), "c-job": (31000, 500)}
    assert port_range_problems(ranges) == []
    assert port_range_problems({"a": (30000, 500), "b": (30400, 500)})
    assert port_range_problems({"a": (65500, 500)})


def test_the_environment_moves_every_controllable_writer_into_the_job_tree(tmp_path):
    tree = {name: tmp_path / name for name in ("root", "tmp", "cache", "build", "ports")}
    env = isolation_environment(tree, (30000, 500))
    assert env["TMPDIR"] == str(tree["tmp"]) and env["HOME"] == str(tree["root"])
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["POKERED_GATE_PYTEST_CACHE_DIR"] == str(tree["cache"] / "pytest")
    assert (
        env["POKERED_PARALLEL_PORT_BASE"] == "30000" and env["POKERED_PARALLEL_PORT_COUNT"] == "500"
    )
    assert env["POKERED_PARALLEL_PORT_LOCK_DIR"] == str(tree["ports"])
    assert env["POKERED_PARALLEL_BUILD_DIR"] == str(tree["build"])


def test_the_tripwire_reports_changed_added_and_removed_files(tmp_path):
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("2")
    before = snapshot_tree(tmp_path)
    (tmp_path / "a").write_text("111")
    (tmp_path / "c").write_text("3")
    (tmp_path / "b").unlink()
    problems = diff_snapshots(before, snapshot_tree(tmp_path))
    assert {
        word for item in problems for word in ("changed", "added", "removed") if word in item
    } == {"changed", "added", "removed"}
    assert diff_snapshots(before, before) == []


def test_nested_or_shared_job_trees_are_refused(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "inner").mkdir()
    (tmp_path / "b").mkdir()
    assert (
        distinct_tree_problems({"x": {"root": tmp_path / "a"}, "y": {"root": tmp_path / "b"}}) == []
    )
    assert distinct_tree_problems(
        {"x": {"root": tmp_path / "a"}, "y": {"root": tmp_path / "a" / "inner"}}
    )
