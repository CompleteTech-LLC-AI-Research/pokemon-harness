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
from types import SimpleNamespace

import pytest
from pyboy.core.serial import Serial

from tests._bootstrap_admission_test_support import make_contract_case
from tests._bootstrap_probe_test_support import (
    ProbeSerial,
    ProbeSerialError,
    run_owner_clock_probe,
    run_owner_poll_probe,
)
from tests._bootstrap_stage_test_support import native_build_fixture, source_fingerprint
from tests._runtime_packaging_support import (
    EXPECTED_PYBOY_REVISION,
    ROOT,
    _load_bootstrap,
)


def _valid_contract_case(tmp_path, mode="source"):
    bootstrap = _load_bootstrap()
    case = make_contract_case(bootstrap, tmp_path, mode=mode)
    case.modules["pyboy.core.serial"].Serial = ProbeSerial
    case.modules["pyboy.core.mb"].Motherboard = type(
        "Motherboard", (), {"get_physical_clock": lambda self: (0, 0)}
    )
    return bootstrap, case


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


def test_contract_checks_nonserial_module_mode(tmp_path):
    bootstrap, case = _valid_contract_case(tmp_path)
    case.modules["pyboy.core.mb"].__file__ = str(case.vendor / "pyboy" / "core" / "mb.so")
    problems = bootstrap._bootstrap_runtime_contract.verify_preconstruction_contract(
        "source", case.modules, case.pyboy, case.utils, case.harness, api=case.api
    )
    assert "pyboy.core.mb is cython, expected source" in problems


def test_contract_does_not_modify_import_paths(tmp_path):
    bootstrap, case = _valid_contract_case(tmp_path)
    before = sys.path.copy()
    problems = bootstrap._bootstrap_runtime_contract.verify_preconstruction_contract(
        "source", case.modules, case.pyboy, case.utils, case.harness, api=case.api
    )
    assert problems == []
    assert sys.path == before
    assert case.owner_calls[0][0]["pokered_harness"] is case.harness

    native_bootstrap, native = _valid_contract_case(tmp_path / "native", mode="cython")
    native_problems = native_bootstrap._bootstrap_runtime_contract.verify_preconstruction_contract(
        "cython", native.modules, native.pyboy, native.utils, native.harness, api=native.api
    )
    assert native_problems == []
    selected, owners, options = native.owner_calls[0]
    assert set(selected) == {*native_bootstrap.RUNTIME_MODULES, "pokered_harness"}
    assert all(owners[name] == "pyboy" for name in native_bootstrap.RUNTIME_MODULES)
    assert owners["pokered_harness"] == "pokered-harness"
    assert set(options["editable_module_paths"]) == {"pokered_harness"}


def test_contract_rejects_missing_serial_backend():
    serial = SimpleNamespace(
        backend_failed=False,
        apply_external_edge=lambda *_args: None,
        peek_out_bit=lambda: 0,
        check_error=lambda: None,
        set_owner_pump=lambda *_args: None,
        claim_owner_pump=lambda *_args: object(),
        release_owner_pump=lambda *_args: None,
    )
    problems = _load_bootstrap()._bootstrap_runtime_contract.verify_serial_instance(serial)
    assert any("backend" in problem for problem in problems)


@pytest.mark.parametrize(
    ("constant", "value"),
    [
        ("CYCLES_PER_EDGE_DMG", 128),
        ("CYCLES_PER_BYTE_DMG", 1024),
        ("CYCLES_8192HZ", 128),
    ],
)
def test_contract_rejects_old_clock_despite_matching_marker(constant, value):
    bootstrap = _load_bootstrap()
    serial_module = SimpleNamespace(
        CYCLES_PER_EDGE_DMG=512,
        CYCLES_PER_BYTE_DMG=4096,
        CYCLES_8192HZ=512,
        Serial=ProbeSerial,
        SerialBackendError=ProbeSerialError,
    )
    setattr(serial_module, constant, value)
    with pytest.raises(
        SystemExit,
        match=f"serial {constant} must be 512"
        if constant != "CYCLES_PER_BYTE_DMG"
        else f"serial {constant} must be 4096",
    ):
        bootstrap._verify_serial_features("source", serial_module)


@pytest.mark.parametrize("feature", ["check_error", "backend_failed", "SerialBackendError"])
def test_contract_rejects_missing_fault_feature(feature):
    bootstrap = _load_bootstrap()

    class MissingStateSerial(ProbeSerial):
        def __init__(self, cgb):
            super().__init__(cgb)
            del self.backend_failed

    serial_type = ProbeSerial
    error_type = ProbeSerialError
    if feature == "check_error":
        serial_type = type("MissingCheckSerial", (ProbeSerial,), {"check_error": None})
    elif feature == "backend_failed":
        serial_type = MissingStateSerial
    else:
        error_type = None
    serial_module = SimpleNamespace(
        CYCLES_PER_EDGE_DMG=512,
        CYCLES_PER_BYTE_DMG=4096,
        CYCLES_8192HZ=512,
        Serial=serial_type,
        SerialBackendError=error_type,
    )
    with pytest.raises(SystemExit, match=feature):
        bootstrap._verify_serial_features("source", serial_module)


def test_contract_rejects_advertised_clock_without_behavior():
    bootstrap = _load_bootstrap()
    broken_serial = type("BrokenClockSerial", (ProbeSerial,), {"tick": lambda self, _cycles: False})
    serial_module = SimpleNamespace(
        CYCLES_PER_EDGE_DMG=512,
        CYCLES_PER_BYTE_DMG=4096,
        CYCLES_8192HZ=512,
        Serial=broken_serial,
        SerialBackendError=ProbeSerialError,
    )
    with pytest.raises(SystemExit, match="first bit does not occur at 512 T-cycles"):
        bootstrap._verify_serial_features("source", serial_module)


def test_bootstrap_fault_probe_fails_closed():
    bootstrap = _load_bootstrap()
    no_raise = type(
        "NoRaiseSerial",
        (ProbeSerial,),
        {"__init__": lambda self, cgb: ProbeSerial.__init__(self, cgb, defect="no_raise")},
    )
    serial_module = SimpleNamespace(
        CYCLES_PER_EDGE_DMG=512,
        CYCLES_PER_BYTE_DMG=4096,
        CYCLES_8192HZ=512,
        Serial=no_raise,
        SerialBackendError=ProbeSerialError,
    )
    with pytest.raises(SystemExit, match="check_error did not raise the latched fault"):
        bootstrap._verify_serial_features("source", serial_module)


@pytest.mark.parametrize(
    "probe",
    ["_verify_serial_features", "_verify_owner_clock_features", "_verify_owner_poll_features"],
)
def test_runtime_probes_run_only_after_origin_ownership_verification(tmp_path, monkeypatch, probe):
    bootstrap, case = _valid_contract_case(tmp_path)
    for name, module in case.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, "pokered_harness", case.harness)
    original_import = bootstrap.importlib.import_module
    monkeypatch.setattr(
        bootstrap.importlib,
        "import_module",
        lambda name: case.modules[name] if name in case.modules else original_import(name),
    )
    monkeypatch.setattr(bootstrap, "ROOT", case.root)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", case.vendor)
    monkeypatch.setattr(bootstrap, "_runtime_roots", lambda: (case.vendor,))
    monkeypatch.setattr(bootstrap, "_path_is_within", lambda path, root: path.is_relative_to(root))
    monkeypatch.setattr(
        bootstrap, "_package_distributions", lambda _name: {bootstrap.PROJECT_DISTRIBUTION}
    )
    monkeypatch.setattr(
        bootstrap,
        "_origin_guard",
        SimpleNamespace(
            attest_selected_module_owners=lambda *_args, **_kwargs: {
                "ok": False,
                "errors": ["selected file owner mismatch"],
            }
        ),
    )
    monkeypatch.setattr(
        bootstrap, "_new_serial_instance", lambda: pytest.fail("Serial built before owner check")
    )
    monkeypatch.setattr(
        bootstrap, probe, lambda *_args: pytest.fail(f"{probe} ran before owner check")
    )
    with pytest.raises(SystemExit, match="selected file owner mismatch"):
        bootstrap._verify_runtime("source")


@pytest.mark.parametrize("feature", ["set_owner_pump", "get_physical_clock"])
@pytest.mark.parametrize("mode", ["source", "cython"])
def test_matching_legacy_marker_rejects_missing_owner_clock_api(tmp_path, feature, mode):
    bootstrap, case = _valid_contract_case(tmp_path, mode=mode)
    if feature == "set_owner_pump":
        original = ProbeSerial.set_owner_pump
        ProbeSerial.set_owner_pump = None
    else:
        case.modules["pyboy.core.mb"].Motherboard.get_physical_clock = None
    try:
        problems = bootstrap._bootstrap_runtime_contract.verify_preconstruction_contract(
            mode, case.modules, case.pyboy, case.utils, case.harness, api=case.api
        )
        assert f"API {feature} is missing" in problems
    finally:
        if feature == "set_owner_pump":
            ProbeSerial.set_owner_pump = original


@pytest.mark.parametrize("feature", ["claim_owner_pump", "release_owner_pump"])
@pytest.mark.parametrize("mode", ["source", "cython"])
def test_matching_marker_rejects_missing_exclusive_owner_api(tmp_path, feature, mode):
    bootstrap, case = _valid_contract_case(tmp_path, mode=mode)
    original = getattr(ProbeSerial, feature)
    setattr(ProbeSerial, feature, None)
    try:
        problems = bootstrap._bootstrap_runtime_contract.verify_preconstruction_contract(
            mode, case.modules, case.pyboy, case.utils, case.harness, api=case.api
        )
        assert f"API {feature} is missing" in problems
    finally:
        setattr(ProbeSerial, feature, original)


def test_authored_owner_clock_probe_passes_and_cleans_its_temporary_cartridge():
    emulator = run_owner_clock_probe(_load_bootstrap())
    assert emulator.physical == 72
    assert emulator.serial.backend_failed


def test_bootstrap_owner_clock_probe_fails_closed():
    with pytest.raises(SystemExit, match="unsupported runtime"):
        run_owner_clock_probe(_load_bootstrap(), defect="no_mmio_pump")


def test_owner_clock_probe_against_actual_selected_source_or_native_runtime():
    import pyboy
    from pyboy.core import serial

    bootstrap = _load_bootstrap()
    mode = "source" if serial.__file__.endswith(".py") else "cython"
    bootstrap._verify_owner_clock_features(mode, pyboy, serial)


def test_owner_claim_and_cpu_halt_poll_probe_passes_and_cleans_up():
    emulator = run_owner_poll_probe(_load_bootstrap())
    assert emulator.register_file.PC == 0x151
    assert emulator.physical == 40


@pytest.mark.parametrize(
    "defect", ["no_poll", "no_halt_poll", "late_poll", "malformed_poll", "wrong_poll_kind"]
)
def test_bootstrap_owner_poll_probe_fails_closed(defect):
    diagnostics = []
    with pytest.raises(SystemExit, match="unsupported runtime"):
        run_owner_poll_probe(_load_bootstrap(), defect=defect, diagnostics=diagnostics)
    if defect == "no_halt_poll":
        assert len(diagnostics) == 1
        assert diagnostics[0].serial.sequence == 2
        assert diagnostics[0].physical == 24


def test_claim_poll_probe_against_actual_selected_source_or_native_runtime():
    import pyboy
    from pyboy.core import serial

    bootstrap = _load_bootstrap()
    mode = "source" if serial.__file__.endswith(".py") else "cython"
    bootstrap._verify_owner_poll_features(mode, pyboy, serial)


def test_native_staging_preserves_contract_with_coarse_destination_timestamps(
    tmp_path, monkeypatch
):
    bootstrap = _load_bootstrap()
    source = native_build_fixture(tmp_path)
    before = source_fingerprint(source)
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    monkeypatch.setattr(bootstrap, "REVISION_FILE", source / "POKERED_HARNESS_PYBOY_REVISION")
    original_copystat = bootstrap.shutil.copystat
    rounded_files = set()

    def coarse_copystat(src, dst, *, follow_symlinks=True):
        original_copystat(src, dst, follow_symlinks=follow_symlinks)
        target = Path(dst)
        if target.is_file():
            stat = target.stat()
            rounded = stat.st_mtime_ns // 1_000_000_000 * 1_000_000_000
            os.utime(target, ns=(stat.st_atime_ns, rounded))
            rounded_files.add(target)

    monkeypatch.setattr(bootstrap.shutil, "copystat", coarse_copystat)
    with bootstrap._native_build_source() as staged:
        copied = source_fingerprint(staged)
        expected = {
            "POKERED_HARNESS_PYBOY_REVISION",
            "setup.py",
            "pyproject.toml",
            "README.md",
            "pyboy/__init__.py",
            "pyboy/core/mb.py",
            "pyboy/core/mb.pxd",
            "pyboy/core/cpu.py",
            "pyboy/core/opcodes.py",
            "pyboy/core/opcodes.pxd",
            "pyboy/core/shared.pxi",
            "pyboy/core/bootrom_dmg.bin",
            "pyboy/plugins/font.txt",
        }
        assert set(copied) == expected
        assert rounded_files == {staged / name for name in copied}
        assert all(value[1] % 1_000_000_000 == 0 for value in copied.values())
        assert {name: value[0] for name, value in copied.items()} == {
            name: before[name][0] for name in copied
        }
        assert not (staged / "pyboy/core/opcodes.c").exists()
        assert not (staged / "pyboy/core/mb_api.h").exists()
        assert not (staged / "pyboy/default_rom.gb").exists()
    assert not staged.exists()
    assert source_fingerprint(source) == before


@pytest.mark.parametrize("suffix", [".c", ".cpp", ".h", ".hpp"])
def test_native_staging_rejects_orphan_generated_output(tmp_path, monkeypatch, suffix):
    bootstrap = _load_bootstrap()
    source = native_build_fixture(tmp_path)
    (source / f"pyboy/core/orphan{suffix}").write_bytes(b"/* Generated by Cython 3.0.12 */\n")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    with (
        pytest.raises(ValueError, match="no regeneration source"),
        bootstrap._native_build_source(),
    ):
        pytest.fail("orphan generated output was accepted")


@pytest.mark.parametrize("relative", ["README.md", "pyboy", "pyboy/core/mb.pxd"])
def test_native_staging_rejects_symlink_inputs(tmp_path, monkeypatch, relative):
    bootstrap = _load_bootstrap()
    source = native_build_fixture(tmp_path)
    target = source / relative
    original = target.with_name(target.name + ".original")
    target.rename(original)
    try:
        target.symlink_to(original, target_is_directory=original.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    with pytest.raises(ValueError, match="symlink"), bootstrap._native_build_source():
        pytest.fail("symlink build input was accepted")
    if relative == "README.md":
        target.unlink()
        original.rename(target)
        ignored_target = tmp_path / "unlisted-extension.so"
        ignored_target.write_bytes(b"synthetic ignored suffix target")
        ignored_link = source / "pyboy" / "unlisted-extension.so"
        ignored_link.symlink_to(ignored_target)
        with pytest.raises(ValueError, match="symlink"), bootstrap._native_build_source():
            pytest.fail("unlisted-suffix symlink was accepted")


@pytest.mark.parametrize(
    "exit_codes",
    [[17], [124], [0, 18], [0, 124], [0, 0, 19], [0, 0, 0]],
)
def test_native_staged_install_cleanup_and_postcheck_order(tmp_path, monkeypatch, exit_codes):
    bootstrap = _load_bootstrap()
    source = native_build_fixture(tmp_path)
    before = source_fingerprint(source)
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    monkeypatch.setattr(bootstrap, "_metadata_was_present", lambda: True)
    monkeypatch.setattr(bootstrap, "_remove_generated_pyboy_metadata", lambda **_kwargs: None)
    monkeypatch.setattr(bootstrap, "_pip_command", lambda: [sys.executable, "-m", "pip", "install"])
    calls = []

    def run(command, **kwargs):
        command = list(command)
        calls.append(command)
        if len(calls) == 1:
            assert command == [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-deps",
                *bootstrap.BUILD_REQUIREMENTS,
            ]
            assert kwargs["timeout"] == bootstrap.INSTALL_TIMEOUT_SECONDS
            return SimpleNamespace(returncode=0)
        if len(calls) == 2:
            assert command == [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-deps",
                "--no-build-isolation",
                "-e",
                str(tmp_path),
            ]
            assert kwargs["timeout"] == bootstrap.INSTALL_TIMEOUT_SECONDS
            return SimpleNamespace(returncode=0)
        if len(calls) == 3:
            assert Path(command[-1]).is_dir() and Path(command[-1]) != source
            assert command[:-1] == [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-deps",
                "--no-build-isolation",
                bootstrap.CYTHON_REQUIREMENT,
            ]
            assert not (Path(command[-1]) / "pyboy/core/opcodes.c").exists()
            assert kwargs["timeout"] == bootstrap.INSTALL_TIMEOUT_SECONDS
        else:
            assert not Path(calls[2][-1]).exists()
            assert kwargs["timeout"] == bootstrap.CHECK_TIMEOUT_SECONDS
        return SimpleNamespace(returncode=exit_codes[len(calls) - 3])

    monkeypatch.setattr(bootstrap, "_run_bounded", run)
    assert bootstrap.main(["--mode", "cython"]) == exit_codes[-1]
    assert len(calls) == 2 + len(exit_codes)
    if len(calls) > 3:
        assert calls[3] == [sys.executable, "-m", "pip", "check"]
    if len(calls) > 4:
        assert calls[4][-1] == "--_runtime-probe"
    assert not Path(calls[2][-1]).exists()
    assert source_fingerprint(source) == before


def test_native_staging_failure_prevents_install_and_reports_error(tmp_path, monkeypatch, capsys):
    bootstrap = _load_bootstrap()
    source = native_build_fixture(tmp_path)
    (source / "pyboy/core/orphan.c").write_bytes(b"/* Generated by Cython 3.0.12 */\n")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    monkeypatch.setattr(bootstrap, "_metadata_was_present", lambda: True)
    monkeypatch.setattr(bootstrap, "_remove_generated_pyboy_metadata", lambda **_kwargs: None)
    monkeypatch.setattr(bootstrap, "_pip_command", lambda: [sys.executable, "-m", "pip", "install"])

    symlink_source = source / "pyboy/core/a_generated.py"
    symlink_source.write_text("regeneration source\n", encoding="utf-8")
    target = tmp_path / "generated-target.c"
    target.write_bytes(b"/* Generated by Cython 3.0.12 */\n")
    generated_symlink = source / "pyboy/core/a_generated.c"
    try:
        generated_symlink.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    original_open = Path.open
    opened_link = []

    def refuse_generated_link(path, *args, **kwargs):
        if path == generated_symlink:
            opened_link.append(path)
            raise AssertionError("generated symlink target was read")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_generated_link)
    with pytest.raises(ValueError, match="symlink"), bootstrap._native_build_source():
        pytest.fail("generated Cython symlink was accepted")
    assert opened_link == []
    monkeypatch.setattr(Path, "open", original_open)
    generated_symlink.unlink()
    calls = []

    def run(*_args, **_kwargs):
        calls.append(1)
        assert len(calls) <= 2, "staged installation started before orphan refusal"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap, "_run_bounded", run)
    assert bootstrap.main(["--mode", "cython"]) == 1
    assert len(calls) == 2
    assert "no regeneration source" in capsys.readouterr().err


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param("duplicate-owned", id="duplicate-owned"),
        pytest.param("relative-owned-alias", id="relative-owned-alias"),
        pytest.param(
            "empty-package-and-package-spec-mismatch", id="empty-package-and-package-spec-mismatch"
        ),
        pytest.param("empty-spec", id="empty-spec"),
        pytest.param("foreign-before-root", id="foreign-before-root"),
        pytest.param("foreign-after-root", id="foreign-after-root"),
    ],
)
def test_bootstrap_namespace_search_path_contract(tmp_path: Path, scenario: str) -> None:
    script = ROOT / "scripts" / "bootstrap_pyboy.py"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    probe = r"""
import importlib
import importlib.machinery
import importlib.util
import os
import pathlib
import sys
import types

script = pathlib.Path(sys.argv[1]).resolve()
work = pathlib.Path(sys.argv[2]).resolve()
scenario = sys.argv[3]
scripts_root = script.parent
project_root = scripts_root.parent

def inside_project_root(entry):
    resolved = pathlib.Path(entry or os.getcwd()).resolve()
    return resolved == project_root or project_root in resolved.parents

base_path = [entry for entry in sys.path if not inside_project_root(entry)]

def synthetic_namespace(package_paths, spec_paths):
    namespace = types.ModuleType("scripts")
    namespace.__file__ = None
    namespace.__loader__ = None
    namespace.__package__ = "scripts"
    namespace.__path__ = list(package_paths)
    spec = importlib.machinery.ModuleSpec("scripts", loader=None, is_package=True)
    spec.submodule_search_locations = list(spec_paths)
    namespace.__spec__ = spec
    sys.modules["scripts"] = namespace
    return namespace

def try_bootstrap(namespace, accepted, alias_name):
    before_dict = namespace.__dict__.copy()
    before_path = tuple(namespace.__path__)
    before_spec = namespace.__spec__
    before_spec_paths = tuple(before_spec.submodule_search_locations or ())
    before_children = {
        name: child for name, child in sys.modules.items() if name.startswith("scripts.")
    }
    alias_spec = importlib.util.spec_from_file_location(alias_name, script)
    assert alias_spec is not None and alias_spec.loader is not None
    module = importlib.util.module_from_spec(alias_spec)
    sys.modules[alias_name] = module
    if accepted:
        alias_spec.loader.exec_module(module)
        for helper in (
            "_bootstrap_runtime_contract",
            "_bootstrap_runtime_probes",
            "check_import_origins",
            "_import_origin_resolution",
            "_import_origin_paths",
            "_import_origin_finders",
            "_import_origin_attestations",
            "_import_origin_selected_owners",
        ):
            child = sys.modules[f"scripts.{helper}"]
            assert pathlib.Path(child.__file__).resolve() == scripts_root / f"{helper}.py"
        return
    try:
        alias_spec.loader.exec_module(module)
    except ImportError as exc:
        assert str(exc) == "bootstrap scripts namespace belongs to another source tree"
    else:
        raise AssertionError("malformed or foreign scripts namespace was accepted")
    assert sys.modules["scripts"] is namespace
    assert namespace.__dict__ == before_dict
    assert tuple(namespace.__path__) == before_path
    assert namespace.__spec__ is before_spec
    assert tuple(namespace.__spec__.submodule_search_locations or ()) == before_spec_paths
    after_children = {
        name: child for name, child in sys.modules.items() if name.startswith("scripts.")
    }
    assert after_children == before_children

if scenario == "duplicate-owned":
    sys.path[:] = [str(project_root), str(project_root), *base_path]
    namespace = importlib.import_module("scripts")
    expected = (scripts_root, scripts_root)
    assert isinstance(namespace.__loader__, importlib.machinery.NamespaceLoader)
    assert tuple(pathlib.Path(item).resolve() for item in namespace.__path__) == expected
    assert tuple(
        pathlib.Path(item).resolve()
        for item in namespace.__spec__.submodule_search_locations
    ) == expected
    try_bootstrap(namespace, True, "bootstrap_duplicate_owned_alias")
elif scenario == "relative-owned-alias":
    os.chdir(project_root)
    sys.path[:] = [".", *[entry for entry in base_path if not inside_project_root(entry)]]
    namespace = importlib.import_module("scripts")
    expected = (scripts_root,)
    assert isinstance(namespace.__loader__, importlib.machinery.NamespaceLoader)
    assert tuple(pathlib.Path(item).resolve() for item in namespace.__path__) == expected
    assert tuple(
        pathlib.Path(item).resolve()
        for item in namespace.__spec__.submodule_search_locations
    ) == expected
    try_bootstrap(namespace, True, "bootstrap_relative_owned_alias")
elif scenario == "empty-package-and-package-spec-mismatch":
    root = str(scripts_root)
    for suffix, package_paths, spec_paths in (
        ("package-empty", [], [root]),
        ("both-empty", [], []),
        ("path-mismatch", [root, root], [root]),
    ):
        namespace = synthetic_namespace(package_paths, spec_paths)
        try_bootstrap(namespace, False, f"bootstrap_{suffix}_alias")
elif scenario == "empty-spec":
    namespace = synthetic_namespace([str(scripts_root)], [])
    try_bootstrap(namespace, False, "bootstrap_empty_spec_alias")
else:
    foreign_root = work / "foreign-root"
    (foreign_root / "scripts").mkdir(parents=True, exist_ok=True)
    entries = [str(foreign_root), str(project_root)]
    if scenario == "foreign-after-root":
        entries.reverse()
    sys.path[:] = [*entries, *base_path]
    namespace = importlib.import_module("scripts")
    expected = (
        (foreign_root / "scripts", scripts_root)
        if scenario == "foreign-before-root"
        else (scripts_root, foreign_root / "scripts")
    )
    assert isinstance(namespace.__loader__, importlib.machinery.NamespaceLoader)
    assert tuple(pathlib.Path(item).resolve() for item in namespace.__path__) == expected
    assert tuple(
        pathlib.Path(item).resolve()
        for item in namespace.__spec__.submodule_search_locations
    ) == expected
    try_bootstrap(namespace, False, f"bootstrap_{scenario}_alias")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(script),
            str(tmp_path),
            scenario,
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
