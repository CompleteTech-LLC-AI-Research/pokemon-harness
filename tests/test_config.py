from __future__ import annotations

import pytest

from pokered_harness.config import (
    VersionsConfig,
    VersionsConfigError,
    load_peer_env,
    load_primary_env,
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


# -- per-session env vars --------------------------------------------------


_PEER_VARS = (
    "POKERED_PEER_ROM_PATH",
    "POKERED_PEER_SYM_PATH",
    "POKERED_PEER_ROM_SHA1",
    "POKERED_PEER_ROM_VERSION",
)
_PRIMARY_VARS = (
    "POKERED_ROM_PATH",
    "POKERED_SYM_PATH",
    "POKERED_ROM_SHA1",
    "POKERED_ROM_VERSION",
)


def _clear_env(monkeypatch, names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


def test_load_peer_env_unset_returns_none_fields(monkeypatch):
    _clear_env(monkeypatch, _PEER_VARS)
    env = load_peer_env()
    assert env.rom_path is None
    assert env.sym_path is None
    assert env.rom_sha1 is None
    # Version still resolves to a heuristic default even when rom_path is None.
    assert env.version == "red"


def test_load_peer_env_reads_all_three(monkeypatch):
    monkeypatch.setenv("POKERED_PEER_ROM_PATH", "rom/blue/pokemon-blue.gb")
    monkeypatch.setenv("POKERED_PEER_SYM_PATH", "rom/blue/pokemon-blue.sym")
    monkeypatch.setenv(
        "POKERED_PEER_ROM_SHA1", "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2"
    )
    monkeypatch.delenv("POKERED_PEER_ROM_VERSION", raising=False)
    env = load_peer_env()
    assert env.rom_path == "rom/blue/pokemon-blue.gb"
    assert env.sym_path == "rom/blue/pokemon-blue.sym"
    assert env.rom_sha1 == "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2"
    # Version auto-derived from filename heuristic.
    assert env.version == "blue"


def test_load_peer_env_version_explicit_override(monkeypatch):
    monkeypatch.setenv("POKERED_PEER_ROM_PATH", "rom/ambiguous/cartridge.gb")
    monkeypatch.setenv("POKERED_PEER_SYM_PATH", "rom/ambiguous/cartridge.sym")
    monkeypatch.delenv("POKERED_PEER_ROM_SHA1", raising=False)
    monkeypatch.setenv("POKERED_PEER_ROM_VERSION", "yellow")
    env = load_peer_env()
    assert env.version == "yellow"


def test_load_primary_env_version_yellow_heuristic(monkeypatch):
    monkeypatch.setenv("POKERED_ROM_PATH", "rom/yellow/pokemon-yellow.gbc")
    monkeypatch.setenv("POKERED_SYM_PATH", "rom/yellow/pokemon-yellow.sym")
    monkeypatch.delenv("POKERED_ROM_SHA1", raising=False)
    monkeypatch.delenv("POKERED_ROM_VERSION", raising=False)
    env = load_primary_env()
    assert env.rom_path == "rom/yellow/pokemon-yellow.gbc"
    assert env.version == "yellow"
