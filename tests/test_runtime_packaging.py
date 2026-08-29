"""Focused checks for the installed PyBoy runtime contract."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from pyboy.core.serial import Serial

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYBOY_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"


def test_project_bundles_the_pinned_pyboy_source() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = project["tool"]["setuptools"]
    packages = setuptools["packages"]
    package_dir = setuptools["package-dir"]

    assert "pyboy" in packages
    assert package_dir["pyboy"] == "vendor/pyboy-src/pyboy"
    assert not any(dep.lower().startswith("pyboy") for dep in project["project"]["dependencies"])

    marker = (ROOT / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION").read_text(
        encoding="ascii"
    ).strip()
    assert marker == EXPECTED_PYBOY_REVISION


def test_mcp_config_uses_the_installed_runtime_without_absolute_paths() -> None:
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["pokered"]

    assert server["command"] == "python"
    assert "PYTHONPATH" not in server["env"]
    assert all(not Path(value).is_absolute() for value in server["env"].values())
    assert server["env"]["POKERED_ROM_PATH"] == "${PWD}/rom/red/pokemon-red-color.gb"
    assert server["env"]["POKERED_SYM_PATH"] == "${PWD}/rom/red/pokemon-red.sym"


def test_pyboy_runtime_exposes_the_harness_serial_contract() -> None:
    import pyboy

    assert pyboy.__version__ == "2.7.0"
    assert pyboy.__pokered_harness_revision__ == EXPECTED_PYBOY_REVISION

    serial = Serial(False)
    for name in ("backend", "apply_external_edge", "peek_out_bit"):
        assert hasattr(serial, name), name


def test_vendored_runtime_contains_no_game_rom_artifacts() -> None:
    source = ROOT / "vendor" / "pyboy-src" / "pyboy"
    forbidden = tuple(source.rglob("*.gb")) + tuple(source.rglob("*.gbc"))
    assert not forbidden
