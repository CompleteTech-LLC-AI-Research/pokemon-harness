"""Build and runtime contract for the source/native PyBoy runtimes (#152).

Split from ``tests/test_runtime_packaging.py`` for #152 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

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
from pathlib import Path

import pytest
from pyboy.core.serial import Serial

from tests._runtime_packaging_support import (
    EXPECTED_PYBOY_REVISION,
    ROOT,
)


def test_clean_wheel_install_imports_the_vendored_link_package(tmp_path) -> None:
    """Exercise the wheel, not this checkout's importable vendored source."""
    wheel_dir = tmp_path / "wheels"
    builder = tmp_path / "wheel-builder-venv"
    create_builder = subprocess.run(
        [sys.executable, "-m", "venv", str(builder)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert create_builder.returncode == 0, create_builder.stdout + create_builder.stderr
    builder_python = builder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    build_env = os.environ.copy()
    build_env["PYBOY_NO_CYTHON"] = "1"
    build = subprocess.run(
        [
            str(builder_python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            ".",
        ],
        cwd=ROOT,
        env=build_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    wheels = sorted(wheel_dir.glob("pokered_harness-*.whl"))
    assert len(wheels) == 1, wheels

    environment = tmp_path / "clean-wheel-venv"
    create_venv = subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert create_venv.returncode == 0, create_venv.stdout + create_venv.stderr
    installed_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    install_env = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "PYBOY_NO_CYTHON",
    ):
        install_env.pop(name, None)
    for name in tuple(install_env):
        if name.startswith("POKERED_"):
            install_env.pop(name)

    install = subprocess.run(
        [str(installed_python), "-m", "pip", "install", "--no-cache-dir", str(wheels[0])],
        cwd=tmp_path,
        env=install_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    imported = subprocess.run(
        [
            str(installed_python),
            "-I",
            "-c",
            (
                "import json; import pyboy.link; "
                "from pyboy.link import LinkSession, NetworkBackend; "
                "print(json.dumps({'module': pyboy.link.__file__, "
                "'session': LinkSession.__module__, 'network': NetworkBackend.__module__}))"
            ),
        ],
        cwd=tmp_path,
        env=install_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr
    loaded = json.loads(imported.stdout)
    assert Path(loaded["module"]).is_relative_to(environment)
    assert loaded["session"] == "pyboy.link.session"
    assert loaded["network"] == "pyboy.link.network"


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
    assert "cpdef bint tick(self, unsigned long long) except * nogil" in serial_pxd
    assert "cdef public uint64_t last_cycles, clock, clock_target" in serial_pxd
    assert "cdef public uint8_t _shift_register" in serial_pxd
    assert "cdef public uint8_t _bits_remaining" in serial_pxd
    assert "cdef dict __dict__" in pyboy_pxd

    vendor_root = ROOT / "vendor" / "pyboy-src"
    mb_pxd = (vendor_root / "pyboy" / "core" / "mb.pxd").read_text(encoding="utf-8")
    assert "cpdef bint tick(self) except * with gil" in mb_pxd
    assert "cdef uint8_t getitem_io_ports(self, uint16_t) except * nogil" in mb_pxd


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
    """The network serial owner must be able to wrap one actual frame."""
    from pyboy import PyBoy

    pyboy = PyBoy.__new__(PyBoy)
    original_tick = pyboy._tick

    def owned_frame(*args, **kwargs):
        return original_tick(*args, **kwargs)

    pyboy._tick = owned_frame
    assert pyboy._tick is owned_frame


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
        ROOT / "vendor" / "pyboy-src" / "pyboy" / "core" / "mb.pxd",
    )
    forbidden_fragments = ("/mnt/", "/home/", "/Users/", "C:\\Users\\", "C:/Users/")
    for path in files:
        contents = path.read_text(encoding="utf-8")
        assert not any(fragment in contents for fragment in forbidden_fragments), path


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
