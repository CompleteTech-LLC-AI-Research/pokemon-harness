from __future__ import annotations

import pytest

from pokered_harness.config import (
    SUPPORTED_ROM_VERSIONS,
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
| PyBoy | `2.7.0` + fork `c565df66c3731fad2856169a90f6bbec99925915` | v2 API. |

## Target ROM

| Field | Value |
|---|---|
| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |
| Size | 1,048,576 bytes |
| Path | `rom/red/pokemon-red.gb` |
| Symbols | `rom/red/pokemon-red.sym` |
| Symbol SHA-1 | `03783c86a42588bd77f73bd7814cf8d70e590118` |
"""


def test_load_versions_parses_valid_file(tmp_path):
    p = tmp_path / "VERSIONS.md"
    p.write_text(_VALID, encoding="utf-8")
    cfg = load_versions(p)
    assert isinstance(cfg, VersionsConfig)
    assert cfg.rom_sha1 == "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
    assert cfg.pyboy_version == "2.7.0"
    assert cfg.pyboy_revision == "c565df66c3731fad2856169a90f6bbec99925915"
    assert cfg.symbol_sha1_for_path("rom/red/pokemon-red.sym") == (
        "03783c86a42588bd77f73bd7814cf8d70e590118"
    )


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
    assert cfg.pyboy_revision == "fd765b1808ac9cb192b42ae971987158ff36ae48"
    assert cfg.rom_sha1 == "ea9bcae617fdf159b045185467ae58b2e4a48b9a"


def test_repo_versions_selects_hash_by_rom_path():
    cfg = load_versions("VERSIONS.md")
    assert cfg.sha1_for_path("rom/blue/pokemon-blue.gb") == (
        "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2"
    )
    assert (
        cfg.sha1_for_path("/isolated/worktree/rom/yellow/pokemon-yellow.gbc")
        == "cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1"
    )
    assert cfg.sha1_for_path("rom/unknown/custom.gb") is None


@pytest.mark.parametrize("lookup_name", ["symbol_sha1_for_path", "sha1_for_symbol_path"])
def test_repo_versions_selects_symbol_hash_by_symbol_path(lookup_name):
    cfg = load_versions("VERSIONS.md")
    lookup = getattr(cfg, lookup_name)
    assert lookup("rom/red/pokemon-red.sym") == ("03783c86a42588bd77f73bd7814cf8d70e590118")
    assert (
        lookup(r"C:\isolated\worktree\rom\yellow\pokemon-yellow.sym")
        == "7c4205723943e7722230dcf014e5e8a2012474aa"
    )
    assert lookup("rom/unknown/custom.sym") is None


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
    monkeypatch.setenv("POKERED_PEER_ROM_SHA1", "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2")
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


def test_blank_environment_values_are_treated_as_unset(monkeypatch):
    monkeypatch.setenv("POKERED_ROM_PATH", "  ")
    monkeypatch.setenv("POKERED_SYM_PATH", "")
    monkeypatch.setenv("POKERED_ROM_SHA1", " ")
    env = load_primary_env()
    assert env.rom_path is None
    assert env.sym_path is None
    assert env.rom_sha1 is None


def test_session_env_rejects_partial_or_unknown_configuration(monkeypatch):
    monkeypatch.setenv("POKERED_ROM_PATH", "rom.gb")
    monkeypatch.delenv("POKERED_SYM_PATH", raising=False)
    with pytest.raises(VersionsConfigError, match="both ROM and symbol"):
        load_primary_env().validate(role="primary")

    assert SUPPORTED_ROM_VERSIONS == frozenset({"red", "blue", "yellow"})


def test_session_env_rejects_hash_without_a_peer_asset(monkeypatch):
    _clear_env(monkeypatch, _PEER_VARS)
    monkeypatch.setenv("POKERED_PEER_ROM_SHA1", "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2")
    with pytest.raises(VersionsConfigError, match="requires a ROM path"):
        load_peer_env().validate(role="peer")
