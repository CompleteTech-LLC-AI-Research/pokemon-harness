"""Focused checks for the installed PyBoy runtime contract."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

from pyboy.core.serial import Serial

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYBOY_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"
EXPECTED_RUNTIME_DEPENDENCIES = {
    "mcp": "==1.29.1",
    "cython": "==3.0.12",
    "numpy": "==2.5.2",
    "pydantic": "==2.13.5",
    "pysdl2": "==0.9.17",
    "pysdl2-dll": "==2.32.10",
}
EXPECTED_DEV_DEPENDENCIES = {
    "pytest": "==9.1.1",
    "pytest-asyncio": "==1.4.0",
    "pytest-cov": "==7.1.0",
    "ruff": "==0.16.5",
}


def _split_exact_requirement(requirement: str) -> tuple[str, str]:
    name, separator, version = requirement.partition("==")
    assert separator == "==", requirement
    assert name and version, requirement
    return name.lower(), f"=={version}"


def test_project_bundles_the_pinned_pyboy_source() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = project["tool"]["setuptools"]
    packages = setuptools["packages"]
    package_dir = setuptools["package-dir"]

    assert "pyboy" in packages
    assert "pyboy.link" not in packages
    assert package_dir["pyboy"] == "vendor/pyboy-src/pyboy"
    assert not any(dep.lower().startswith("pyboy") for dep in project["project"]["dependencies"])

    marker = (
        (ROOT / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION")
        .read_text(encoding="ascii")
        .strip()
    )
    assert marker == EXPECTED_PYBOY_REVISION


def test_project_direct_dependencies_are_exactly_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = dict(_split_exact_requirement(req) for req in project["project"]["dependencies"])
    dev_dependencies = dict(
        _split_exact_requirement(req) for req in project["project"]["optional-dependencies"]["dev"]
    )

    assert dependencies == EXPECTED_RUNTIME_DEPENDENCIES
    assert dev_dependencies == EXPECTED_DEV_DEPENDENCIES


def test_bootstrap_declares_and_checks_both_runtime_modes() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap_pyboy.py").read_text(encoding="utf-8")

    assert 'choices=("source", "cython")' in bootstrap
    assert '"--check"' in bootstrap
    assert 'env["PYBOY_NO_CYTHON"] = "1"' in bootstrap
    assert '"-m", "ensurepip", "--upgrade"' in bootstrap
    assert "apply_external_edge" in bootstrap
    assert "cython_compiled" in bootstrap


def test_bootstrap_rehydrates_missing_pip(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "pokered_bootstrap_test", ROOT / "scripts" / "bootstrap_pyboy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls: list[list[str]] = []
    pip_probes = 0

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(command, **_kwargs):
        nonlocal pip_probes
        command = list(command)
        calls.append(command)
        if command == [sys.executable, "-m", "pip", "--version"]:
            pip_probes += 1
            return Result(1 if pip_probes == 1 else 0)
        if command == [sys.executable, "-m", "ensurepip", "--upgrade"]:
            return Result(0)
        raise AssertionError(f"unexpected command: {command!r}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._pip_command() == [sys.executable, "-m", "pip"]
    assert calls == [
        [sys.executable, "-m", "pip", "--version"],
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        [sys.executable, "-m", "pip", "--version"],
    ]


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
