"""Pinned project metadata, lockfile and MCP entrypoint checks (#152).

Split from ``tests/test_runtime_packaging.py`` for #152 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

import json
import tomllib
from pathlib import Path, PureWindowsPath

from tests._runtime_packaging_support import (
    EXPECTED_DEV_DEPENDENCIES,
    EXPECTED_PYBOY_REVISION,
    ROOT,
    _expected_runtime_requirements,
    _normalise_marker,
    _split_exact_requirement,
)


def test_project_bundles_the_pinned_pyboy_source() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = project["tool"]["setuptools"]
    packages = setuptools["packages"]
    package_dir = setuptools["package-dir"]

    assert "pyboy" in packages
    assert "pyboy.link" in packages
    assert package_dir["pyboy"] == "vendor/pyboy-src/pyboy"
    assert not any(dep.lower().startswith("pyboy") for dep in project["project"]["dependencies"])

    marker = (
        (ROOT / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION")
        .read_text(encoding="ascii")
        .strip()
    )
    assert marker == EXPECTED_PYBOY_REVISION


def test_product_lint_boundary_excludes_pinned_vendored_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = project["tool"]["ruff"]

    assert "vendor/pyboy-src" in ruff["extend-exclude"]


def test_project_exposes_the_installed_mcp_entrypoint_and_explicit_package_data() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = project["tool"]["setuptools"]

    assert "setuptools>=77" in project["build-system"]["requires"]
    assert project["project"]["scripts"] == {
        "pokered-harness": "pokered_harness.mcp_server:main",
    }
    assert setuptools["include-package-data"] is False
    assert setuptools["package-data"] == {
        "pyboy": [
            "_pyboy_init.pxi",
            "_pyboy_runtime.pxi",
            "_pyboy_controls.pxi",
            "_pyboy_api.pxi",
            "_pyboy_memory.pxi",
        ],
        "pyboy.core": [
            "bootrom_cgb.bin",
            "bootrom_dmg.bin",
            "opcodes.pxd",
            "opcode_components/*.pxi",
            "mb_components/*.pxi",
            "mb_components_manifest.py",
            "lcd_components/*.pxi",
            "lcd_components_manifest.py",
        ],
        "pyboy.plugins": ["font.txt"],
    }


def test_project_direct_dependencies_are_exactly_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.11"
    dependencies = sorted(
        _split_exact_requirement(req) for req in project["project"]["dependencies"]
    )
    dev_dependencies = sorted(
        _split_exact_requirement(req) for req in project["project"]["optional-dependencies"]["dev"]
    )

    assert dependencies == _expected_runtime_requirements()
    assert dev_dependencies == sorted(
        (name, specifier, None) for name, specifier in EXPECTED_DEV_DEPENDENCIES.items()
    )


def test_project_exposes_stable_mcp_entrypoints() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"] == {
        "pokered-harness": "pokered_harness.mcp_server:main",
    }
    module = ROOT / "src" / "pokered_harness" / "__main__.py"
    assert module.is_file()
    assert "pokered_harness.mcp_server" in module.read_text(encoding="utf-8")


def test_lockfile_records_the_same_exact_project_requirements() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock["requires-python"] == ">=3.11"
    project = next(package for package in lock["package"] if package["name"] == "pokered-harness")
    locked_requirements = sorted(
        (
            item["name"].lower(),
            item["specifier"],
            _normalise_marker(item.get("marker")),
        )
        for item in project["metadata"]["requires-dist"]
        if not item.get("marker", "").startswith("extra ==")
    )
    locked_dev_requirements = sorted(
        (
            item["name"].lower(),
            item["specifier"],
            _normalise_marker(item.get("marker")),
        )
        for item in project["metadata"]["requires-dist"]
        if item.get("marker") == "extra == 'dev'"
    )
    assert locked_requirements == _expected_runtime_requirements()
    assert locked_dev_requirements == sorted(
        (name, specifier, "extra == 'dev'") for name, specifier in EXPECTED_DEV_DEPENDENCIES.items()
    )


def test_mcp_config_uses_the_installed_runtime_without_absolute_paths() -> None:
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["pokered"]

    assert server["command"] == "python"
    assert server["args"] == ["-m", "pokered_harness.mcp_server"]
    assert "PYTHONPATH" not in server["env"]
    assert all(
        not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute())
        for value in server["env"].values()
    )
    assert server["env"]["POKERED_ROM_PATH"] == "${PWD}/rom/red/pokemon-red-color.gb"
    assert server["env"]["POKERED_SYM_PATH"] == "${PWD}/rom/red/pokemon-red.sym"
    assert server["env"]["POKERED_SYM_SHA1"] == "03783c86a42588bd77f73bd7814cf8d70e590118"
    assert server["env"]["POKERED_VERSIONS_PATH"] == "${PWD}/VERSIONS.md"
