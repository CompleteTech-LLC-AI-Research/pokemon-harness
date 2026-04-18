from __future__ import annotations

import pytest

from pokered_harness.config import (
    VersionsConfig,
    VersionsConfigError,
    load_versions,
)


_VALID = """\
# Pinned versions

## Emulator

| Component | Pin | Notes |
|---|---|---|
| PyBoy | `2.7.0` | v2 API. |

## Target ROM

| Field | Value |
|---|---|
| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |
| Size | 1,048,576 bytes |
"""


def test_load_versions_parses_valid_file(tmp_path):
    p = tmp_path / "VERSIONS.md"
    p.write_text(_VALID, encoding="utf-8")
    cfg = load_versions(p)
    assert isinstance(cfg, VersionsConfig)
    assert cfg.rom_sha1 == "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
    assert cfg.pyboy_version == "2.7.0"


def test_load_versions_is_case_insensitive_for_sha(tmp_path):
    p = tmp_path / "VERSIONS.md"
    p.write_text(_VALID.replace("ea9b", "EA9B"), encoding="utf-8")
    cfg = load_versions(p)
    # Canonicalised to lowercase.
    assert cfg.rom_sha1 == "ea9bcae617fdf159b045185467ae58b2e4a48b9a"


def test_missing_file_raises(tmp_path):
    with pytest.raises(VersionsConfigError):
        load_versions(tmp_path / "does_not_exist.md")


def test_missing_sha_row_raises(tmp_path):
    p = tmp_path / "VERSIONS.md"
    p.write_text("| PyBoy | `2.7.0` |\nno sha here\n", encoding="utf-8")
    with pytest.raises(VersionsConfigError, match="SHA-1"):
        load_versions(p)


def test_missing_pyboy_row_raises(tmp_path):
    p = tmp_path / "VERSIONS.md"
    p.write_text(
        "| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |\n",
        encoding="utf-8",
    )
    with pytest.raises(VersionsConfigError, match="PyBoy"):
        load_versions(p)


def test_short_sha_rejected(tmp_path):
    p = tmp_path / "VERSIONS.md"
    p.write_text(
        "| PyBoy | `2.7.0` |\n| SHA-1 | `deadbeef` |\n",
        encoding="utf-8",
    )
    with pytest.raises(VersionsConfigError, match="SHA-1"):
        load_versions(p)


def test_repo_versions_md_parses():
    """The actual VERSIONS.md in the repo must parse. This is the
    regression that prevents silent drift between docs and the loader."""
    cfg = load_versions("VERSIONS.md")
    assert cfg.pyboy_version == "2.7.0"
    assert cfg.rom_sha1 == "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
