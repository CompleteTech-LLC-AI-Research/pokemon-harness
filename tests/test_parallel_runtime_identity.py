"""Authored controls: identities are measured, never copied (#107.1, #107.3, finding 2)."""

import copy
import subprocess
import sys

from scripts.production_gate_parallel_artifacts import validate_terminal_artifact
from scripts.production_gate_parallel_identity import (
    expected_identity,
    identity_differences,
    measure_assets,
    measure_build_identity,
    measure_commit,
    measure_inputs,
    tree_digest,
)
from tests._parallel_runtime_report_support import TOKEN, good_artifact, make_plan


def git(root, *argv):
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            *argv,
        ],
        check=True,
        capture_output=True,
    )


def test_asset_digest_covers_every_rom_and_fixture_byte(tmp_path):
    rom, fixtures = tmp_path / "rom", tmp_path / "fixtures"
    rom.mkdir()
    fixtures.mkdir()
    (rom / "red.gb").write_bytes(b"rom")
    (fixtures / "battle.state").write_bytes(b"fixture-1")
    first, problems = measure_assets(rom, fixtures)
    assert problems == []
    (fixtures / "battle.state").write_bytes(b"fixture-2")
    assert measure_assets(rom, fixtures)[0] != first
    assert measure_assets(rom, tmp_path / "missing")[1]


def test_inputs_and_trees_report_missing_and_changed_bytes(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("2")
    digests, problems = measure_inputs(
        tmp_path, ["a.txt", "tree:sub", "absent.txt", "tree:nothing"]
    )
    assert set(digests) == {"a.txt", "tree:sub"} and len(problems) == 2
    before = tree_digest(tmp_path)[0]
    (tmp_path / "sub" / "b.txt").write_text("3")
    assert tree_digest(tmp_path)[0] != before


def test_a_clean_and_a_dirty_checkout_are_measured_from_git(tmp_path):
    git(tmp_path, "init")
    (tmp_path / "f").write_text("x")
    git(tmp_path, "add", "f")
    git(tmp_path, "commit", "-m", "c")
    head, clean, problems = measure_commit(tmp_path)
    assert len(head) == 40 and clean and problems == []
    (tmp_path / "f").write_text("y")
    assert measure_commit(tmp_path)[1:] == (
        False,
        ["the checkout is not clean: an exact clean commit is required"],
    )
    assert measure_commit(tmp_path / "nope")[0] == ""


def test_the_build_identity_binds_the_interpreter_and_the_runtime_kind(tmp_path):
    (tmp_path / "vendor" / "pyboy-src" / "pyboy").mkdir(parents=True)
    (tmp_path / "vendor" / "pyboy-src" / "pyboy" / "m.py").write_text("v=1")
    source, problems = measure_build_identity("source", sys.executable, tmp_path)
    assert problems == [] and len(source) == 64
    (tmp_path / "vendor" / "pyboy-src" / "pyboy" / "m.py").write_text("v=2")
    assert measure_build_identity("source", sys.executable, tmp_path)[0] != source
    assert measure_build_identity("source", str(tmp_path / "missing-python"), tmp_path)[1]


def test_each_measured_difference_is_named_and_an_unclean_head_is_refused():
    plan = make_plan()
    expected = expected_identity(plan.as_dict(), "source")
    measured = {**expected, "clean_head": True}
    assert identity_differences(measured, expected) == []
    for key, value in (
        ("commit", "0" * 40),
        ("build_identity", "1" * 64),
        ("asset_digest", "2" * 64),
        ("input_digests", {"x": "3" * 64}),
    ):
        assert any(key in item for item in identity_differences({**measured, key: value}, expected))
    assert identity_differences({**measured, "clean_head": False}, expected)


def test_copying_plan_values_is_not_enough_a_wrong_measurement_is_rejected():
    plan = make_plan()
    for key, value in (
        ("commit", "9" * 40),
        ("asset_digest", "8" * 64),
        ("build_identity", "7" * 64),
    ):
        artifact = copy.deepcopy(good_artifact(plan, "source-job"))
        artifact[key] = value
        artifact["identity_after"][key] = value
        assert validate_terminal_artifact(
            artifact, plan, "source-job", child_exit_status=0, expected_token=TOKEN
        )


def test_identity_that_changes_during_the_run_or_a_stale_token_is_rejected():
    plan = make_plan()
    artifact = good_artifact(plan, "source-job")
    artifact["identity_after"]["asset_digest"] = "6" * 64
    assert any(
        "after the run" in item
        for item in validate_terminal_artifact(artifact, plan, "source-job", child_exit_status=0)
    )
    stale = good_artifact(plan, "source-job", token="s" * 32)
    assert any(
        "launch token" in item
        for item in validate_terminal_artifact(
            stale, plan, "source-job", child_exit_status=0, expected_token=TOKEN
        )
    )


def test_repository_metadata_is_never_part_of_a_content_digest(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    before = tree_digest(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("changes on every git command")
    assert tree_digest(tmp_path) == before
