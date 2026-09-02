"""Focused checks for the installed PyBoy runtime contract."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from pathlib import Path, PureWindowsPath

import pytest
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


def test_cython_build_pins_the_compiler_and_preserves_serial_widths() -> None:
    vendor_project = tomllib.loads(
        (ROOT / "vendor" / "pyboy-src" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert (
        "cython==3.0.12; platform_python_implementation == 'CPython'"
        in vendor_project["build-system"]["requires"]
    )

    serial_pxd = (ROOT / "vendor" / "pyboy-src" / "pyboy" / "core" / "serial.pxd").read_text(
        encoding="utf-8"
    )
    pyboy_pxd = (ROOT / "vendor" / "pyboy-src" / "pyboy" / "pyboy.pxd").read_text(encoding="utf-8")
    assert "cpdef bint tick(self, unsigned long long) noexcept nogil" in serial_pxd
    assert "cdef public uint64_t last_cycles, clock, clock_target" in serial_pxd
    assert "cdef public uint8_t _shift_register" in serial_pxd
    assert "cdef public uint8_t _bits_remaining" in serial_pxd
    assert "cdef dict __dict__" in pyboy_pxd


def test_source_and_native_runtime_expose_lockstep_timing_attributes() -> None:
    """The Cython ABI must expose the scheduler's source-runtime inputs."""
    from pyboy.core.cpu import CPU
    from pyboy.core.lcd import LCD
    from pyboy.pyboy import defaults

    cpu = CPU(None)
    lcd = LCD(
        False,
        False,
        defaults["color_palette"],
        defaults["cgb_color_palette"],
    )

    assert cpu.cycles == 0
    assert lcd.speed_shift == 0
    assert lcd._cycles_to_frame == 70224
    assert lcd._cycles_to_interrupt == 0


def test_source_and_native_runtime_allow_instance_tick_ownership() -> None:
    """The network serial owner must be able to wrap ``PyBoy.tick`` per instance."""
    from pyboy import PyBoy

    pyboy = PyBoy.__new__(PyBoy)
    original_tick = pyboy.tick

    def owned_frame(*args, **kwargs):
        return original_tick(*args, **kwargs)

    pyboy.tick = owned_frame
    assert pyboy.tick is owned_frame


def test_cython_serial_translation_unit_compiles_with_the_checked_in_pxd() -> None:
    """Compile the serial C translation unit without writing build output to the repo."""
    compiler_spec = os.environ.get("CC") or sysconfig.get_config_var("CC")
    compiler = shlex.split(compiler_spec or "")
    if not compiler or shutil.which(compiler[0]) is None:
        pytest.skip("a C compiler is required for the Cython ABI smoke check")

    python_include_candidates = {
        Path(candidate)
        for candidate in (
            sysconfig.get_config_var("INCLUDEPY"),
            sysconfig.get_path("include"),
            sysconfig.get_path("platinclude"),
            str(
                Path(sys.prefix)
                / "include"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
            ),
            str(
                Path(sys.base_prefix)
                / "include"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
            ),
        )
        if candidate
    }
    python_include_path = next(
        (
            candidate
            for candidate in python_include_candidates
            if (candidate / "Python.h").is_file()
        ),
        None,
    )
    if python_include_path is None:
        pytest.skip("the active Python does not expose an include directory")
    python_include = str(python_include_path)

    import numpy

    vendor_root = ROOT / "vendor" / "pyboy-src"
    with tempfile.TemporaryDirectory(prefix="pokered-cython-serial-") as temporary:
        c_file = Path(temporary) / "serial.c"
        cython_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cython",
                "--3str",
                "--directive",
                "language_level=3",
                "--output-file",
                str(c_file),
                "pyboy/core/serial.py",
            ],
            cwd=vendor_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert cython_result.returncode == 0, cython_result.stdout + cython_result.stderr

        compiler_name = Path(compiler[0]).name.lower()
        if compiler_name in {"cl", "cl.exe", "clang-cl", "clang-cl.exe"}:
            compile_command = [
                *compiler,
                "/nologo",
                "/c",
                f"/I{numpy.get_include()}",
                f"/I{python_include}",
                f"/Fo{Path(temporary) / 'serial.obj'}",
                str(c_file),
            ]
        else:
            compile_command = [
                *compiler,
                "-pthread",
                "-fPIC",
                "-Werror=incompatible-pointer-types",
                "-fsyntax-only",
                f"-I{numpy.get_include()}",
                f"-I{python_include}",
                str(c_file),
            ]
        compile_result = subprocess.run(
            compile_command,
            cwd=vendor_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr


def test_runtime_build_files_have_no_machine_specific_absolute_paths() -> None:
    files = (
        ROOT / "pyproject.toml",
        ROOT / ".mcp.json",
        ROOT / "scripts" / "bootstrap_pyboy.py",
        ROOT / "vendor" / "pyboy-src" / "pyproject.toml",
        ROOT / "vendor" / "pyboy-src" / "setup.py",
        ROOT / "vendor" / "pyboy-src" / "pyboy" / "core" / "serial.py",
        ROOT / "vendor" / "pyboy-src" / "pyboy" / "core" / "serial.pxd",
    )
    forbidden_fragments = ("/mnt/", "/home/", "/Users/", "C:\\Users\\", "C:/Users/")
    for path in files:
        contents = path.read_text(encoding="utf-8")
        assert not any(fragment in contents for fragment in forbidden_fragments), path


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
        "pyboy.core": ["bootrom_cgb.bin", "bootrom_dmg.bin"],
        "pyboy.plugins": ["font.txt"],
    }


def test_project_direct_dependencies_are_exactly_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = dict(_split_exact_requirement(req) for req in project["project"]["dependencies"])
    dev_dependencies = dict(
        _split_exact_requirement(req) for req in project["project"]["optional-dependencies"]["dev"]
    )

    assert dependencies == EXPECTED_RUNTIME_DEPENDENCIES
    assert dev_dependencies == EXPECTED_DEV_DEPENDENCIES


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
    project = next(package for package in lock["package"] if package["name"] == "pokered-harness")
    locked_requirements = {
        item["name"].lower(): item["specifier"]
        for item in project["metadata"]["requires-dist"]
        if "marker" not in item
    }
    locked_dev_requirements = {
        item["name"].lower(): item["specifier"]
        for item in project["metadata"]["requires-dist"]
        if item.get("marker") == "extra == 'dev'"
    }
    assert locked_requirements == EXPECTED_RUNTIME_DEPENDENCIES
    assert locked_dev_requirements == EXPECTED_DEV_DEPENDENCIES


def test_bootstrap_declares_and_checks_both_runtime_modes() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap_pyboy.py").read_text(encoding="utf-8")

    assert 'choices=("source", "cython")' in bootstrap
    assert '"--check"' in bootstrap
    assert 'env["PYBOY_NO_CYTHON"] = "1"' in bootstrap
    assert 'env.pop("PYBOY_NO_CYTHON", None)' in bootstrap
    assert '"-m", "ensurepip", "--upgrade"' in bootstrap
    assert '"--python", sys.executable' in bootstrap
    assert '"--no-deps"' in bootstrap
    assert "install_target = ROOT" in bootstrap
    assert "install_target = PYBOY_SOURCE" in bootstrap
    assert "apply_external_edge" in bootstrap
    assert "cython_compiled" in bootstrap


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "pokered_bootstrap_runtime_test", ROOT / "scripts" / "bootstrap_pyboy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_rehydrates_missing_pip(monkeypatch) -> None:
    module = _load_bootstrap()
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

    assert module._pip_command() == [sys.executable, "-m", "pip", "install"]
    assert calls == [
        [sys.executable, "-m", "pip", "--version"],
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        [sys.executable, "-m", "pip", "--version"],
    ]


def test_bootstrap_uses_uv_when_pip_and_ensurepip_are_unavailable(monkeypatch) -> None:
    module = _load_bootstrap()
    calls: list[list[str]] = []

    class Result:
        returncode = 1

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/opt/uv" if name == "uv" else None)

    assert module._pip_command() == ["/opt/uv", "pip", "install", "--python", sys.executable]
    assert calls == [
        [sys.executable, "-m", "pip", "--version"],
        [sys.executable, "-m", "ensurepip", "--upgrade"],
    ]


def test_bootstrap_source_mode_installs_the_harness_distribution(monkeypatch) -> None:
    module = _load_bootstrap()
    calls: list[tuple[list[str], dict]] = []

    class Result:
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        return Result()

    monkeypatch.setattr(module, "_validate_source", lambda: None)
    monkeypatch.setattr(module, "_verify_runtime", lambda _mode: None)
    monkeypatch.setattr(module, "_pip_command", lambda: ["pip", "install"])
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main(["--mode", "source"]) == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        module.CYTHON_REQUIREMENT,
        str(module.ROOT),
    ]
    assert kwargs["cwd"] == module.ROOT
    assert kwargs["env"]["PYBOY_NO_CYTHON"] == "1"


def test_bootstrap_cython_mode_targets_only_the_checked_in_fork(monkeypatch) -> None:
    module = _load_bootstrap()
    calls: list[tuple[list[str], dict]] = []

    class Result:
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        return Result()

    monkeypatch.setattr(module, "_validate_source", lambda: None)
    monkeypatch.setattr(module, "_verify_runtime", lambda _mode: None)
    monkeypatch.setattr(module, "_pip_command", lambda: ["pip", "install"])
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main(["--mode", "cython"]) == 0
    command, kwargs = calls[0]
    assert command[:4] == ["pip", "install", "--force-reinstall", "--no-deps"]
    assert command[-2:] == [module.CYTHON_REQUIREMENT, str(module.PYBOY_SOURCE)]
    assert "PYBOY_NO_CYTHON" not in kwargs["env"]
    assert module.CYTHON_MODULES == (
        "pyboy.pyboy",
        "pyboy.utils",
        "pyboy.core.mb",
        "pyboy.core.serial",
    )


def test_bootstrap_source_mode_rejects_a_competing_pyboy_distribution(monkeypatch) -> None:
    module = _load_bootstrap()
    monkeypatch.setattr(
        module,
        "_package_distributions",
        lambda package: {"pokered-harness", "pyboy"} if package == "pyboy" else set(),
    )

    with pytest.raises(SystemExit, match="competing installed owners"):
        module._verify_runtime("source")


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


def test_pyboy_runtime_exposes_the_harness_serial_contract() -> None:
    import pyboy
    from pyboy import utils

    assert pyboy.__version__ == "2.7.0"
    assert pyboy.__pokered_harness_revision__ == EXPECTED_PYBOY_REVISION

    owners = {
        name.lower().replace("_", "-")
        for name in importlib.metadata.packages_distributions().get("pyboy", ())
    }
    expected_owners = {"pokered-harness", "pyboy"} if utils.cython_compiled else {"pokered-harness"}
    assert owners <= expected_owners
    assert "pokered-harness" in owners

    serial = Serial(False)
    for name in ("backend", "apply_external_edge", "peek_out_bit"):
        assert hasattr(serial, name), name


def test_vendored_runtime_contains_no_game_rom_artifacts() -> None:
    source = ROOT / "vendor" / "pyboy-src" / "pyboy"
    forbidden = tuple(source.rglob("*.gb")) + tuple(source.rglob("*.gbc"))
    assert not forbidden


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
    assert "scripts/network_concurrency_probe.py" in workflow
    assert "--unit-only" in workflow
    assert "--repeat-timing 5" in workflow
    assert "pip wheel" in workflow
    assert "python -m venv" in workflow
    assert "pip check" in workflow
    assert "upload-artifact@v4" in workflow
