"""Artifact, fixture-provenance and repository-hygiene guards (#152).

Split from ``tests/test_runtime_packaging.py`` for #152 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

import subprocess
from pathlib import Path

import pytest

from scripts import validate_fixture_manifest as fixture_manifest
from tests._runtime_packaging_support import (
    ROOT,
)


def test_vendored_runtime_contains_no_game_rom_artifacts() -> None:
    source = ROOT / "vendor" / "pyboy-src" / "pyboy"
    forbidden = tuple(source.rglob("*.gb")) + tuple(source.rglob("*.gbc"))
    assert not forbidden


def test_fixture_validation_rejects_symlink_escape(tmp_path) -> None:
    outside = tmp_path / "outside.state"
    outside.write_bytes(b"state")
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    linked = fixture_root / "linked.state"
    try:
        linked.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="escapes fixture root"):
        fixture_manifest._validate_assets(
            [
                {
                    "path": "linked.state",
                    "size_bytes": len(b"state"),
                    "sha1": "0" * 40,
                    "sha256": "0" * 64,
                }
            ],
            fixture_root,
        )


def test_git_tracked_tree_excludes_rom_and_generated_runtime_artifacts() -> None:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    tracked = [Path(raw) for raw in result.stdout.decode().split("\0") if raw]
    forbidden_suffixes = {
        ".gb",
        ".gbc",
        ".map",
        ".ram",
        ".sav",
        ".state",
        ".sym",
        ".sqlite",
        ".sqlite3",
        ".so",
        ".pyd",
    }
    forbidden = [path.as_posix() for path in tracked if path.suffix.lower() in forbidden_suffixes]
    assert forbidden == []


def test_gitignore_protects_rom_derived_inputs_and_outputs() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (
        "*.gb",
        "*.gbc",
        "*.sym",
        "*.state",
        "*.ram",
        "*.sav",
        "*.sqlite",
        "*.sqlite3",
    ):
        assert pattern in ignored


def test_ci_runs_gate_clean_install_and_retains_sanitized_evidence() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-hygiene.yml").read_text(encoding="utf-8")
    assert "scripts/production_gate.py" in workflow
    assert "scripts/tcp_link_matrix.py" in workflow
    assert "scripts/validate_fixture_manifest.py" in workflow
    assert "scripts/network_concurrency_probe.py" in workflow
    assert "--unit-only" in workflow
    assert "--runtime-mode source" in workflow
    assert "--repeat-timing 5" in workflow
    assert "pip wheel" in workflow
    assert "python -m venv" in workflow
    assert "pip check" in workflow
    assert "upload-artifact@v4" in workflow
