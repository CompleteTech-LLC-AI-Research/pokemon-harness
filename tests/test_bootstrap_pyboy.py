"""Installed ownership and bounded bootstrap regressions; no ROM/build required."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "bootstrap_under_test", Path(__file__).resolve().parents[1] / "scripts/bootstrap_pyboy.py"
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class Distribution:
    def __init__(self, name, root, *, editable=False, records=()):
        self.metadata = {"Name": name}
        self.root = root
        self.editable = editable
        self.files = list(records)

    def read_text(self, name):
        assert name == "direct_url.json"
        return json.dumps({"url": self.root.as_uri(), "dir_info": {"editable": self.editable}})

    def locate_file(self, entry):
        return self.root / entry


def record(root, relative, content=b"original module\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    entry = importlib.metadata.PackagePath(relative)
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    entry.hash = importlib.metadata.FileHash("sha256=" + digest)
    return entry


def install_metadata(monkeypatch, harness, native=None):
    def distribution(name):
        found = {"pokered-harness": harness, "pyboy": native}[name]
        if found is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return found

    monkeypatch.setattr(bootstrap.importlib.metadata, "distribution", distribution)


def test_source_editable_accepts_only_declared_import_paths(tmp_path, monkeypatch):
    harness = Distribution("pokered-harness", tmp_path, editable=True)
    install_metadata(monkeypatch, harness)
    origins = {
        "pokered_harness": str(tmp_path / "src/pokered_harness/__init__.py"),
        "pyboy": str(tmp_path / "vendor/pyboy-src/pyboy/__init__.py"),
        "pyboy.core.serial": str(tmp_path / "vendor/pyboy-src/pyboy/core/serial.py"),
    }
    assert bootstrap._verify_ownership("source", origins) == "pokered-harness"
    origins["pyboy.core.serial"] = str(tmp_path / "shadow/pyboy/core/serial.py")
    with pytest.raises(SystemExit, match="not owned"):
        bootstrap._verify_ownership("source", origins)


def test_source_rejects_even_unused_stock_distribution(tmp_path, monkeypatch):
    install_metadata(
        monkeypatch,
        Distribution("pokered-harness", tmp_path, editable=True),
        Distribution("pyboy", tmp_path),
    )
    with pytest.raises(SystemExit, match="standalone pyboy"):
        bootstrap._verify_ownership("source", {})


def test_wheel_record_rejects_overwritten_or_unowned_module(tmp_path, monkeypatch):
    entry = record(tmp_path, "pyboy/__init__.py")
    harness = Distribution("pokered-harness", tmp_path, records=[entry])
    install_metadata(monkeypatch, harness)
    origin = str(tmp_path / entry)
    assert bootstrap._verify_ownership("source", {"pyboy": origin}) == "pokered-harness"
    (tmp_path / entry).write_bytes(b"overwritten by another distribution")
    with pytest.raises(SystemExit, match="not owned"):
        bootstrap._verify_ownership("source", {"pyboy": origin})


def test_native_requires_fork_records_and_retains_harness_owner(tmp_path, monkeypatch):
    entry = record(tmp_path, "pyboy/core/serial.so")
    harness = Distribution("pokered-harness", tmp_path, editable=True)
    native = Distribution("pyboy", tmp_path, records=[entry])
    install_metadata(monkeypatch, harness, native)
    origins = {
        "pyboy.core.serial": str(tmp_path / entry),
        "pokered_harness": str(tmp_path / "src/pokered_harness/__init__.py"),
    }
    assert bootstrap._verify_ownership("cython", origins) == "pyboy"
    origins["pyboy.core.serial"] = str(tmp_path / "vendor/pyboy-src/pyboy/core/serial.py")
    with pytest.raises(SystemExit, match="not owned"):
        bootstrap._verify_ownership("cython", origins)
    install_metadata(monkeypatch, harness)
    with pytest.raises(SystemExit, match="built fork is not installed"):
        bootstrap._verify_ownership("cython", {})


@pytest.mark.parametrize("mode", ["source", "cython"])
def test_install_checks_fresh_runtime_only_after_success(mode, monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    source = native_build_fixture(tmp_path)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    calls = []
    outcomes = iter([0, 0, 9] if mode == "cython" else [0, 9])

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if mode == "cython" and len(calls) == 1:
            assert Path(command[-1]).is_dir()
            assert Path(command[-1]) != source
            assert "--no-cache-dir" in command
        return next(outcomes)

    monkeypatch.setattr(bootstrap, "_run_bounded", run)
    assert bootstrap.main(["--mode", mode, "--install-timeout", "7", "--check-timeout", "3"]) == 9
    if mode == "source":
        assert calls[0][0][-1] == str(bootstrap.ROOT)
    else:
        assert not Path(calls[0][0][-1]).exists()
        assert calls[1][0] == [sys.executable, "-m", "pip", "check"]
        assert calls[1][1]["timeout"] == 3
    assert calls[0][1]["timeout"] == 7
    assert calls[-1][0][-1] == "--probe-contract"
    assert calls[-1][1]["timeout"] == 3
    assert (calls[0][1]["env"].get("PYBOY_NO_CYTHON") == "1") == (mode == "source")


def test_failed_install_does_not_report_contract_success(monkeypatch):
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return 17

    monkeypatch.setattr(bootstrap, "_run_bounded", run)
    assert bootstrap.main([]) == 17
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_deadline_rejected_before_work(monkeypatch, value):
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: pytest.fail("started work"))
    with pytest.raises(SystemExit) as error:
        bootstrap.main(["--install-timeout", value])
    assert error.value.code == 2


def test_bounded_process_returns_exit_status(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    assert (
        bootstrap._run_bounded(
            [sys.executable, "-c", "raise SystemExit(7)"], env=os.environ.copy(), timeout=10
        )
        == 7
    )


def test_bounded_process_timeout_reaps_child(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    processes = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(bootstrap.subprocess, "Popen", popen)
    assert (
        bootstrap._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=os.environ.copy(),
            timeout=0.2,
        )
        == 124
    )
    assert processes[0].poll() is not None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-state observation")
@pytest.mark.parametrize("startup_delay", [0, 3])
def test_timeout_stops_build_descendants(tmp_path, monkeypatch, startup_delay):
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    pid_file = tmp_path / "descendant.pid"
    command = (
        "import pathlib, subprocess, sys, time; "
        f"time.sleep({startup_delay}); "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pid_path = pathlib.Path({str(pid_file)!r}); "
        "ready_path = pid_path.with_suffix('.ready'); "
        "ready_path.write_text(str(child.pid)); ready_path.replace(pid_path); "
        "time.sleep(60)"
    )
    original_popen = subprocess.Popen

    def start_ready_descendant(*args, **kwargs):
        # The behavior under test is killing an existing build descendant,
        # not whether interpreter startup wins a two-second scheduling race.
        # Keep the production timeout unchanged and start its wait only once
        # this test's child has explicitly published the descendant PID.
        process = original_popen(*args, **kwargs)
        deadline = time.monotonic() + 30
        try:
            while True:
                try:
                    if int(pid_file.read_text()) > 0:
                        break
                except (FileNotFoundError, ValueError):
                    pass
                assert process.poll() is None, "build parent exited before descendant readiness"
                assert time.monotonic() < deadline, "build descendant did not become ready"
                time.sleep(0.01)
            return process
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            raise

    monkeypatch.setattr(bootstrap.subprocess, "Popen", start_ready_descendant)
    assert (
        bootstrap._run_bounded([sys.executable, "-c", command], env=os.environ.copy(), timeout=2)
        == 124
    )
    descendant = int(pid_file.read_text())
    status = Path(f"/proc/{descendant}/stat")
    # An orphan may remain as a zombie until PID 1 reaps it; it must not execute.
    deadline = time.monotonic() + 2
    while True:
        try:
            state = status.read_text().split(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            break
        if state in {"Z", "X"}:
            break
        assert time.monotonic() < deadline, f"build descendant is still executing: {state}"
        time.sleep(0.01)


def fake_runtime(monkeypatch, tmp_path, *, mixed_module=None):
    class SerialBackendError(RuntimeError):
        pass

    class Serial:
        def __init__(self, _cgb):
            self.backend = None
            self.backend_failed = False
            self.failure = None
            self._bits_remaining = 8
            self._shift_register = 0
            self.SB = self.SC = 0

        def operation(self, *args):
            pass

        apply_external_edge = peek_out_bit = set_owner_pump = operation
        claim_owner_pump = release_owner_pump = operation

        def set_SB(self, value):
            self.SB = self._shift_register = value

        def set_SC(self, value):
            self.SC = value

        def tick(self, cycles):
            if self.backend_failed or cycles < 512:
                return False
            try:
                self.backend.on_edge(self.SB >> 7, 1)
            except RuntimeError as exc:
                self.backend_failed = True
                self.failure = exc
            return False

        def check_error(self):
            if self.backend_failed:
                raise SerialBackendError("failed") from self.failure

    modules = {
        name: SimpleNamespace(__file__=str(tmp_path / (name.replace(".", "/") + ".py")))
        for name in ("pokered_harness", "pyboy", "pyboy.link", *bootstrap.COMPILED_MODULES)
    }
    modules["pyboy"].__version__ = "2.7.0"
    modules["pyboy"].__pokered_harness_revision__ = bootstrap.EXPECTED_REVISION
    modules["pyboy.core.serial"].Serial = Serial
    modules["pyboy.core.serial"].SerialCore = Serial
    modules["pyboy.core.serial"].SerialBackendError = SerialBackendError
    modules["pyboy.core.serial"].CYCLES_PER_EDGE_DMG = 512
    modules["pyboy.core.serial"].CYCLES_PER_BYTE_DMG = 4096
    modules["pyboy.core.serial"].CYCLES_8192HZ = 512
    modules["pyboy.link"].LinkSession = object
    modules["pyboy.core.mb"].Motherboard = SimpleNamespace(get_physical_clock=lambda: (0, 0))
    if mixed_module:
        modules[mixed_module].__file__ = str(
            tmp_path / (mixed_module + importlib.machinery.EXTENSION_SUFFIXES[0])
        )
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    monkeypatch.setattr(bootstrap, "_verify_ownership", lambda *args: "pokered-harness")
    monkeypatch.setattr(bootstrap, "_verify_owner_clock_features", lambda *args: None)
    monkeypatch.setattr(bootstrap, "_verify_owner_poll_features", lambda *args: None)
    return modules


def test_contract_checks_nonserial_module_mode(monkeypatch, tmp_path):
    fake_runtime(monkeypatch, tmp_path, mixed_module="pyboy.core.mb")
    with pytest.raises(SystemExit, match="pyboy.core.mb is cython"):
        bootstrap._check_contract("source")


def test_contract_does_not_modify_import_paths(monkeypatch, tmp_path):
    fake_runtime(monkeypatch, tmp_path)
    paths = sys.path.copy()
    bootstrap._check_contract("source")
    assert sys.path == paths


def test_contract_rejects_missing_serial_backend(monkeypatch, tmp_path):
    modules = fake_runtime(monkeypatch, tmp_path)
    modules["pyboy.core.serial"].Serial.__init__ = lambda self, cgb: None
    with pytest.raises(SystemExit, match="backend is inaccessible"):
        bootstrap._check_contract("source")


@pytest.mark.parametrize("constant,value", [
    ("CYCLES_PER_EDGE_DMG", 128),
    ("CYCLES_PER_BYTE_DMG", 1024),
    ("CYCLES_8192HZ", 128),
])
def test_contract_rejects_old_clock_despite_matching_marker(monkeypatch, tmp_path, constant, value):
    modules = fake_runtime(monkeypatch, tmp_path)
    setattr(modules["pyboy.core.serial"], constant, value)
    with pytest.raises(SystemExit, match=f"unsupported runtime: serial {constant}"):
        bootstrap._check_contract("source")


@pytest.mark.parametrize("feature", ["check_error", "backend_failed", "SerialBackendError"])
def test_contract_rejects_missing_fault_feature(monkeypatch, tmp_path, feature):
    modules = fake_runtime(monkeypatch, tmp_path)
    serial_module = modules["pyboy.core.serial"]
    if feature == "check_error":
        serial_module.Serial.check_error = None
    elif feature == "SerialBackendError":
        del serial_module.SerialBackendError
    else:
        original_init = serial_module.Serial.__init__

        def init(self, cgb):
            original_init(self, cgb)
            del self.backend_failed

        serial_module.Serial.__init__ = init
    with pytest.raises(SystemExit, match=f"unsupported runtime: .*{feature}"):
        bootstrap._check_contract("source")


def test_contract_rejects_advertised_clock_without_behavior(monkeypatch, tmp_path):
    modules = fake_runtime(monkeypatch, tmp_path)
    modules["pyboy.core.serial"].Serial.tick = lambda self, cycles: False
    with pytest.raises(SystemExit, match="first bit does not occur"):
        bootstrap._check_contract("source")


@pytest.mark.parametrize("defect", ["no_raise", "wrong_cause", "retry", "advance", "irq"])
def test_contract_rejects_advertised_fault_api_without_behavior(monkeypatch, tmp_path, defect):
    modules = fake_runtime(monkeypatch, tmp_path)
    serial_module = modules["pyboy.core.serial"]
    serial = serial_module.Serial
    if defect == "no_raise":
        serial.check_error = lambda self: None
    elif defect == "wrong_cause":
        def check(self):
            if self.backend_failed:
                raise serial_module.SerialBackendError("wrong cause")
        serial.check_error = check
    else:
        original_tick = serial.tick

        def tick(self, cycles):
            if defect == "retry":
                self.backend_failed = False
            result = original_tick(self, cycles)
            if self.backend_failed:
                if defect == "advance":
                    self._bits_remaining -= 1
                elif defect == "irq":
                    return True
            return result

        serial.tick = tick
    with pytest.raises(SystemExit, match="unsupported runtime"):
        bootstrap._check_contract("source")


@pytest.mark.parametrize("probe", [
    "_verify_serial_features", "_verify_owner_clock_features", "_verify_owner_poll_features",
])
def test_runtime_probes_run_only_after_origin_ownership_verification(monkeypatch, tmp_path, probe):
    fake_runtime(monkeypatch, tmp_path)

    def reject(*args):
        raise SystemExit("not owned")

    monkeypatch.setattr(bootstrap, "_verify_ownership", reject)
    monkeypatch.setattr(bootstrap, probe, lambda *args: pytest.fail("early probe"))
    with pytest.raises(SystemExit, match="not owned"):
        bootstrap._check_contract("source")


@pytest.mark.parametrize("feature", ["set_owner_pump", "get_physical_clock"])
@pytest.mark.parametrize("mode", ["source", "cython"])
def test_matching_legacy_marker_rejects_missing_owner_clock_api(monkeypatch, tmp_path, feature, mode):
    modules = fake_runtime(monkeypatch, tmp_path)
    if mode == "cython":
        for name in bootstrap.COMPILED_MODULES:
            modules[name].__file__ = str(tmp_path / (name + importlib.machinery.EXTENSION_SUFFIXES[0]))
    target = (modules["pyboy.core.serial"].Serial if feature == "set_owner_pump"
              else modules["pyboy.core.mb"].Motherboard)
    setattr(target, feature, None)
    with pytest.raises(SystemExit, match=f"API {feature} is missing"):
        bootstrap._check_contract(mode)


class ProbeSerialError(RuntimeError):
    pass


class ProbeEmulator:
    """Small behavioral double; the real selected runtime is also probed below."""
    def __init__(self, path, *, defect="", window, sound_emulated):
        self.path = Path(path)
        data = self.path.read_bytes()
        assert len(data) == 32768 and data[0x143] == 0x80
        assert data[0x100:0x103] == b"\xc3\x50\x01"
        assert window == "null" and sound_emulated is False
        self.defect = defect
        self.program = [0]
        self.physical = self.generation = 0
        self.double = self.incomplete = False
        self.stopped = []
        self.register_file = SimpleNamespace(PC=0, A=0)
        self.memory = self
        self.serial = SimpleNamespace(
            SB=0, SC=0, backend_failed=False, owner_pump_active=False,
            set_SB=lambda value: setattr(self.serial, "SB", value),
            set_SC=lambda value: setattr(self.serial, "SC", value),
            set_owner_pump=lambda callback: setattr(self, "pump", callback),
            check_error=self.check_error,
        )
        self.pump = self.failure = None
        self.mb = SimpleNamespace(
            serial=self.serial, lcd=SimpleNamespace(frame_done=False),
            get_physical_clock=self.clock, tick=self.tick,
        )

    def __setitem__(self, key, value):
        if isinstance(key, tuple):
            assert key[0] == 0 and key[1].start == 0x150
            self.program = value
        else:
            assert key in (0xFF50, 0xFF4D) and value == 1
            if key == 0xFF4D and self.defect == "key1_as_speed":
                self.double = False

    def set_emulation_speed(self, speed):
        assert speed == 0

    def save_state(self, stream):
        stream.write(b"authored test-double state")

    def load_state(self, stream):
        from pyboy.utils import PyBoyAssertException

        data = stream.read()
        if self.defect != "no_load_generation" and (data or self.defect != "lost_failed_epoch"):
            self.generation += 1
        if not data:
            self.incomplete = True
            if self.defect == "empty_load_succeeds":
                return
            raise PyBoyAssertException("truncated state")
        self.incomplete = False
        self.double = self.defect == "wrong_loaded_speed"
        if self.defect == "load_rewinds":
            self.physical = 0

    def clock(self):
        if self.incomplete and self.defect != "missing_load_fault":
            raise RuntimeError("physical clock state load is incomplete")
        if self.defect == "invalid_clock_shape":
            return (self.generation, True)
        return (self.generation, self.physical)

    def tick(self):
        if self.serial.owner_pump_active:
            if self.defect == "recursive_execution":
                return
            raise RuntimeError("recursive CPU execution during serial owner pump")
        opcode = self.program[0]
        old_rate = 1 if self.double else 2
        if opcode == 0x10:
            self.double = not self.double
        rate = old_rate if self.defect == "wrong_stop_rate" else (1 if self.double else 2)
        if opcode not in (0xF0, 0xE0):
            self.physical += 4 * rate
            return
        self.physical += 4 * rate
        if self.defect == "early_mmio_commit":
            self.register_file.A = self.serial.SB
        if self.pump is not None and self.defect != "no_mmio_pump":
            self.serial.owner_pump_active = True
            try:
                self.pump((1, 1 if opcode == 0xF0 else 2, 0, 0, 0xFF01, -1))
            except RuntimeError as exc:
                self.failure = exc
                self.serial.backend_failed = True
                if self.defect == "fault_commits_write":
                    self.serial.SB = self.register_file.A
                if self.defect != "fault_not_raised":
                    cause = RuntimeError("wrong cause") if self.defect == "fault_wrong_cause" else exc
                    raise ProbeSerialError("latched callback failure") from cause
                return
            finally:
                self.serial.owner_pump_active = False
        if opcode == 0xF0:
            self.register_file.A = self.serial.SB
        else:
            self.serial.SB = self.register_file.A
        self.register_file.PC = 0x152
        self.physical += 8 * rate

    def check_error(self):
        if self.failure is not None and self.defect != "fault_clears":
            raise ProbeSerialError("latched callback failure") from self.failure

    def stop(self, *, save):
        self.stopped.append(save)


def run_fake_owner_clock_probe(defect=""):
    created = []

    def factory(path, **options):
        emulator = ProbeEmulator(path, defect=defect, **options)
        created.append(emulator)
        return emulator

    try:
        bootstrap._verify_owner_clock_features(
            "source", SimpleNamespace(PyBoy=factory), SimpleNamespace(SerialBackendError=ProbeSerialError)
        )
    finally:
        assert len(created) == 1
        assert created[0].stopped == [False]
        assert not created[0].path.exists() and not created[0].path.parent.exists()


def test_authored_owner_clock_probe_passes_and_cleans_its_temporary_cartridge():
    run_fake_owner_clock_probe()


@pytest.mark.parametrize("defect", [
    "invalid_clock_shape", "wrong_stop_rate", "key1_as_speed", "no_load_generation", "load_rewinds",
    "wrong_loaded_speed", "empty_load_succeeds", "missing_load_fault", "lost_failed_epoch",
    "no_mmio_pump", "early_mmio_commit", "recursive_execution", "fault_not_raised",
    "fault_wrong_cause", "fault_commits_write", "fault_clears",
])
def test_advertised_owner_clock_apis_without_behavior_fail_closed_and_clean_up(defect):
    with pytest.raises(SystemExit, match="unsupported runtime"):
        run_fake_owner_clock_probe(defect)


def test_owner_clock_probe_against_actual_selected_source_or_native_runtime():
    import pyboy
    from pyboy.core import serial

    mode = "source" if serial.__file__.endswith(".py") else "cython"
    bootstrap._verify_owner_clock_features(mode, pyboy, serial)


def native_build_fixture(tmp_path):
    source = tmp_path / "vendor-source"
    source.mkdir()
    files = {name: b"packaging input\n" for name in bootstrap.NATIVE_BUILD_ROOT_FILES}
    files.update({
        "pyboy/__init__.py": b"",
        "pyboy/core/mb.py": b"class Motherboard: pass\n",
        "pyboy/core/mb.pxd": b"cdef class Motherboard:\n    cdef bint new_abi_field\n",
        "pyboy/core/opcodes.py": b"import cython\n",
        "pyboy/core/opcodes.pxd": b"from pyboy.core.mb cimport Motherboard\n",
        "pyboy/core/shared.pxi": b"DEF SHARED = 1\n",
        "pyboy/core/opcodes.c": b"/* Generated by Cython 3.0.12 */\n/* stale layout: no new_abi_field */\n",
        "pyboy/core/mb.cpp": b"/* Generated by Cython 3.0.12 */\n",
        "pyboy/core/mb.h": b"/* Generated by Cython 3.0.12 */\n",
        "pyboy/core/mb_api.h": b"/* Generated by Cython 3.0.12 */\n",
        "pyboy/core/original.c": b"/* Original C source, not generated. */\n",
        "pyboy/core/original.h": b"/* Original header. */\n",
        "pyboy/core/original.hpp": b"// Original C++ header.\n",
        "pyboy/core/original.cpp": b"// Original C++ source.\n",
        "pyboy/core/bootrom_cgb.bin": b"\x00\x80\xff",
        "pyboy/plugins/font.txt": b"builtin font\n",
        "pyboy/default_rom.gb": b"authored builtin ROM\n",
        "pyboy/core/opcodes.cpython-312-x86_64-linux-gnu.so": b"stale binary",
        "pyboy/core/mb.pyd": b"stale binary",
        "pyboy/core/mb.dll": b"stale binary",
        "pyboy/core/mb.o": b"stale object",
        "pyboy/core/mb.obj": b"stale object",
        "pyboy/core/mb.pyc": b"stale bytecode",
        "pyboy/core/mb.pyo": b"stale bytecode",
        "pyboy/core/__pycache__/cache": b"cached",
        "pyboy/build/cache": b"cached",
        "pyboy/dist/cache": b"cached",
        "pyboy/cython_debug/cache": b"cached",
        "pyboy/old.egg-info/cache": b"cached",
        "build/lib/pyboy/core/opcodes.c": b"stale root build",
        "operator-inputs/private.gb": b"not a packaging input",
    })
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    generated = source / "pyboy/core/opcodes.c"
    dependency = source / "pyboy/core/mb.pxd"
    newer = dependency.stat().st_mtime_ns + 10_000_000_000
    os.utime(generated, ns=(newer, newer))
    assert generated.stat().st_mtime_ns > dependency.stat().st_mtime_ns
    return source


def native_source_fingerprint(source):
    return {
        path.relative_to(source).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in source.rglob("*") if path.is_file()
    }


def test_native_staging_removes_newer_stale_abi_outputs_but_preserves_all_inputs(tmp_path, monkeypatch):
    source = native_build_fixture(tmp_path)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    before = native_source_fingerprint(source)
    expected = set(bootstrap.NATIVE_BUILD_ROOT_FILES) | {
        "pyboy/__init__.py", "pyboy/core/mb.py", "pyboy/core/mb.pxd",
        "pyboy/core/opcodes.py", "pyboy/core/opcodes.pxd", "pyboy/core/shared.pxi",
        "pyboy/core/original.c", "pyboy/core/original.cpp", "pyboy/core/original.h",
        "pyboy/core/original.hpp", "pyboy/core/bootrom_cgb.bin",
        "pyboy/plugins/font.txt", "pyboy/default_rom.gb",
    }
    with bootstrap._native_build_source() as staged:
        staged_fingerprint = native_source_fingerprint(staged)
        assert set(staged_fingerprint) == expected
        # Destination metadata precision is filesystem-dependent (for example,
        # DrvFS utime can round to seconds). Build inputs must be byte-exact;
        # source hashes AND mtimes must remain unchanged, as asserted below.
        assert {name: value[0] for name, value in staged_fingerprint.items()} == {
            name: before[name][0] for name in expected
        }
        assert "new_abi_field" in (staged / "pyboy/core/mb.pxd").read_text()
        assert "cimport Motherboard" in (staged / "pyboy/core/opcodes.pxd").read_text()
        # Simulate setup.py's source touching and generated output creation.
        (staged / "pyboy/core/opcodes.c").write_text("new consistent ABI")
        (staged / "pyboy/core/mb.py").write_text("build touched source")
    assert not staged.exists()
    assert native_source_fingerprint(source) == before


def test_native_staging_preserves_contract_with_coarse_destination_timestamps(tmp_path, monkeypatch):
    source = native_build_fixture(tmp_path)
    before = native_source_fingerprint(source)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    copystat = bootstrap.shutil.copystat
    rounded_files = set()

    def coarse_copystat(src, dst, *, follow_symlinks=True):
        copystat(src, dst, follow_symlinks=follow_symlinks)
        target = Path(dst)
        if target.is_file():
            metadata = target.stat()
            rounded = metadata.st_mtime_ns // 1_000_000_000 * 1_000_000_000
            os.utime(target, ns=(metadata.st_atime_ns, rounded))
            rounded_files.add(target)

    monkeypatch.setattr(bootstrap.shutil, "copystat", coarse_copystat)
    expected = set(bootstrap.NATIVE_BUILD_ROOT_FILES) | {
        "pyboy/__init__.py", "pyboy/core/mb.py", "pyboy/core/mb.pxd",
        "pyboy/core/opcodes.py", "pyboy/core/opcodes.pxd", "pyboy/core/shared.pxi",
        "pyboy/core/original.c", "pyboy/core/original.cpp", "pyboy/core/original.h",
        "pyboy/core/original.hpp", "pyboy/core/bootrom_cgb.bin",
        "pyboy/plugins/font.txt", "pyboy/default_rom.gb",
    }
    with bootstrap._native_build_source() as staged:
        copied = native_source_fingerprint(staged)
        assert set(copied) == expected
        assert rounded_files == {staged / name for name in expected}
        assert all(value[1] % 1_000_000_000 == 0 for value in copied.values())
        assert {name: value[0] for name, value in copied.items()} == {
            name: before[name][0] for name in expected
        }
        # The deliberately newer generated C/header outputs are absent even
        # when copied .py/.pxd times tie; rebuild safety never depends on them.
        for generated in ("opcodes.c", "mb.cpp", "mb.h", "mb_api.h"):
            assert not (staged / "pyboy/core" / generated).exists()
    assert not staged.exists()
    assert native_source_fingerprint(source) == before


@pytest.mark.parametrize("suffix", [".c", ".cpp", ".h", ".hpp"])
def test_native_staging_rejects_orphan_generated_output(tmp_path, monkeypatch, suffix):
    source = native_build_fixture(tmp_path)
    (source / f"pyboy/orphan{suffix}").write_bytes(b"/* Generated by Cython 3.0.12 */\n")
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    with pytest.raises(ValueError, match="no regeneration source"), bootstrap._native_build_source():
        pytest.fail("orphan generated output was accepted")


@pytest.mark.parametrize("relative", ["README.md", "pyboy", "pyboy/core/mb.pxd"])
def test_native_staging_rejects_symlink_inputs(tmp_path, monkeypatch, relative):
    source = native_build_fixture(tmp_path)
    target = source / relative
    original = target.with_name(target.name + ".original")
    target.rename(original)
    try:
        target.symlink_to(original, target_is_directory=original.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    with pytest.raises(ValueError, match="symlink"), bootstrap._native_build_source():
        pytest.fail("symlink build input was accepted")


@pytest.mark.parametrize("exit_codes", [[17], [124], [0, 18], [0, 124], [0, 0, 19], [0, 0, 0]])
def test_native_staged_install_cleanup_and_postcheck_order(tmp_path, monkeypatch, exit_codes):
    source = native_build_fixture(tmp_path)
    before = native_source_fingerprint(source)
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            staged = Path(command[-1])
            assert staged.is_dir() and staged != source
            assert not (staged / "pyboy/core/opcodes.c").exists()
            assert kwargs["timeout"] == 7
        else:
            assert not Path(calls[0][-1]).exists()
            assert kwargs["timeout"] == 3
        return exit_codes[len(calls) - 1]

    monkeypatch.setattr(bootstrap, "_run_bounded", run)
    assert bootstrap.main(["--mode", "cython", "--install-timeout", "7", "--check-timeout", "3"]) == exit_codes[-1]
    assert len(calls) == len(exit_codes)
    assert not Path(calls[0][-1]).exists()
    if len(calls) > 1:
        assert calls[1] == [sys.executable, "-m", "pip", "check"]
    if len(calls) > 2:
        assert calls[2][-1] == "--probe-contract"
    assert native_source_fingerprint(source) == before


def test_native_staging_failure_prevents_install_and_reports_error(tmp_path, monkeypatch, capsys):
    source = native_build_fixture(tmp_path)
    (source / "pyboy/orphan.c").write_bytes(b"/* Generated by Cython 3.0.12 */\n")
    monkeypatch.setattr(bootstrap, "PYBOY_SOURCE", source)
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    monkeypatch.setattr(bootstrap, "_run_bounded", lambda *a, **k: pytest.fail("install started"))
    temporary_directories = []
    temporary_directory = bootstrap.tempfile.TemporaryDirectory

    def track_directory(**kwargs):
        directory = temporary_directory(**kwargs)
        temporary_directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(bootstrap.tempfile, "TemporaryDirectory", track_directory)
    assert bootstrap.main(["--mode", "cython"]) == 1
    assert "no regeneration source" in capsys.readouterr().err
    assert len(temporary_directories) == 1 and not temporary_directories[0].exists()


@pytest.mark.parametrize("mode", ["source", "cython"])
def test_check_only_does_not_stage_or_install(mode, monkeypatch):
    monkeypatch.setattr(bootstrap, "_validate_source", lambda: None)
    monkeypatch.setattr(bootstrap, "_native_build_source", lambda: pytest.fail("staging started"))
    calls = []
    monkeypatch.setattr(bootstrap, "_run_bounded", lambda command, **kwargs: calls.append(command) or 0)
    assert bootstrap.main(["--mode", mode, "--check"]) == 0
    assert len(calls) == 1 and calls[0][-1] == "--probe-contract"


@pytest.mark.parametrize("feature", ["claim_owner_pump", "release_owner_pump"])
@pytest.mark.parametrize("mode", ["source", "cython"])
def test_matching_marker_rejects_missing_exclusive_owner_api(monkeypatch, tmp_path, feature, mode):
    modules = fake_runtime(monkeypatch, tmp_path)
    if mode == "cython":
        for name in bootstrap.COMPILED_MODULES:
            modules[name].__file__ = str(tmp_path / (name + importlib.machinery.EXTENSION_SUFFIXES[0]))
    setattr(modules["pyboy.core.serial"].Serial, feature, None)
    with pytest.raises(SystemExit, match=f"API {feature} is missing"):
        bootstrap._check_contract(mode)


class ClaimProbeSerial:
    """Binding/fault double shared by standalone and authored-CPU probes."""
    def __init__(self, _cgb, defect=""):
        self.defect = defect
        self.SB = self.SC = 0
        self.callback = self.token = self.previous_token = None
        self.owner_thread = self.failure = None
        self.backend_failed = self.owner_pump_active = self.owner_poll_enabled = False
        self.sequence = 0
        self.reused_token = object()

    def set_SB(self, value):
        self.SB = value

    def set_SC(self, value):
        self.SC = value

    def check_error(self):
        if self.backend_failed:
            raise ProbeSerialError("claimed pump failed") from self.failure

    def set_owner_pump(self, callback, poll=False):
        active_allowed = self.owner_pump_active and self.defect == "active_setter_allowed"
        if self.owner_pump_active and not active_allowed:
            raise RuntimeError("recursive callback")
        self.check_error()
        if self.token is not None and self.defect != "setter_overwrites_claim" and not active_allowed:
            raise RuntimeError("exclusively claimed")
        self.callback = callback
        self.owner_thread = threading.get_ident() if callback is not None else None
        self.owner_poll_enabled = callback is not None and poll

    def claim_owner_pump(self, callback, poll=False):
        active_allowed = self.owner_pump_active and self.defect == "active_claim_allowed"
        if self.owner_pump_active and not active_allowed:
            raise RuntimeError("recursive callback")
        if self.defect != "failed_reclaim_allowed":
            self.check_error()
        occupied_allowed = (
            (self.token is None and self.defect == "claim_overwrites_existing")
            or (self.token is not None and self.defect == "duplicate_claim_overwrites")
            or active_allowed
        )
        if (self.callback is not None or self.token is not None) and not occupied_allowed:
            if self.defect == "reject_clears_existing" and self.token is None:
                self.callback = None
            raise RuntimeError("already installed or claimed")
        self.token = self.reused_token if self.defect == "token_reused" else object()
        self.callback = callback
        self.owner_thread = threading.get_ident()
        self.owner_poll_enabled = (poll or self.defect == "default_poll_enabled") and self.defect != "poll_ignored"
        return self.token

    def release_owner_pump(self, token):
        if self.owner_pump_active and self.defect != "active_release_allowed":
            raise RuntimeError("recursive callback")
        if self.backend_failed and self.defect == "release_after_fault_fails":
            self.check_error()
        wrong_allowed = self.defect == "wrong_token_releases" or (
            token is self.previous_token and self.previous_token is not None
            and self.defect == "stale_token_releases"
        )
        if (token is None or token is not self.token) and not wrong_allowed:
            raise RuntimeError("invalid token")
        if threading.get_ident() != self.owner_thread and self.defect != "off_thread_releases":
            raise RuntimeError("off owner thread")
        self.previous_token = self.token
        self.token = self.owner_thread = None
        if self.defect != "release_keeps_callback":
            self.callback = None
        self.owner_poll_enabled = self.defect == "release_keeps_poll"
        if self.backend_failed and self.defect == "release_clears_fault":
            self.backend_failed = False
        if self.backend_failed and self.defect == "release_loses_cause":
            self.failure = RuntimeError("different cause")

    def emit(self, kind, cycles=0, address=-1, value=-1):
        if self.backend_failed or self.callback is None:
            return
        self.sequence += 1
        event = (self.sequence, kind, cycles, 512, address, value, 0, self.SB, self.SC, 0, 8, False)
        if kind == 4 and self.defect == "malformed_poll":
            event = event[:6]
        if kind == 4 and self.defect == "wrong_poll_kind":
            event = (event[0], 1, *event[2:])
        self.owner_pump_active = True
        try:
            self.callback(event)
        except (RuntimeError, SystemExit) as exc:
            self.failure = exc
            self.backend_failed = True
        finally:
            self.owner_pump_active = False

    def tick(self, cycles):
        self.emit(3, cycles)
        return False


class ClaimProbeEmulator:
    def __init__(self, path, *, defect, window, sound_emulated):
        self.path = Path(path)
        data = self.path.read_bytes()
        assert len(data) == 32768 and data[0x143] == 0x80
        assert data[0x100:0x103] == b"\xc3\x50\x01"
        assert window == "null" and sound_emulated is False
        self.defect = defect
        self.serial = ClaimProbeSerial(False, defect)
        self.register_file = SimpleNamespace(PC=0, A=0)
        self.memory = self
        self.physical = 0
        self.halted = False
        self.stopped = []
        self.program = []
        self.mb = SimpleNamespace(serial=self.serial, lcd=SimpleNamespace(frame_done=False),
                                  get_physical_clock=lambda: (0, self.physical), tick=self.tick)

    def __setitem__(self, key, value):
        if isinstance(key, tuple):
            assert key[0] == 0 and key[1] == slice(0x150, 0x152)
            self.program = value
        else:
            assert (key, value) in ((0xFF50, 1), (0xFFFF, 0), (0xFF0F, 0))

    def set_emulation_speed(self, speed):
        assert speed == 0

    def advance(self):
        if not self.halted:
            opcode = self.program[self.register_file.PC - 0x150]
            if opcode == 0x76:
                self.halted = True
            else:
                assert opcode == 0
                self.register_file.PC += 1
        self.physical += 8

    def tick(self):
        self.serial.check_error()
        should_poll = self.serial.owner_poll_enabled and self.defect != "no_poll" and not (
            self.halted and self.defect == "no_halt_poll"
        )
        if self.defect == "late_poll":
            self.advance()
        if should_poll:
            if self.defect == "poll_off_owner_thread":
                worker = threading.Thread(target=lambda: self.serial.emit(4), daemon=True)
                worker.start()
                worker.join(2)
                assert not worker.is_alive()
            else:
                self.serial.emit(4)
            if self.defect == "duplicate_poll":
                self.serial.emit(4)
            self.serial.check_error()
        if self.defect != "late_poll":
            self.advance()

    def stop(self, *, save):
        self.stopped.append(save)


def run_fake_owner_poll_probe(defect=""):
    created = []

    def factory(path, **options):
        emulator = ClaimProbeEmulator(path, defect=defect, **options)
        created.append(emulator)
        return emulator

    serial_module = SimpleNamespace(
        Serial=lambda cgb: ClaimProbeSerial(cgb, defect), SerialBackendError=ProbeSerialError,
    )
    try:
        try:
            bootstrap._verify_owner_poll_features("source", SimpleNamespace(PyBoy=factory), serial_module)
        except Exception as exc:
            # Match the contract boundary: native callbacks latch BaseException
            # and re-raise a typed fault, which the admission wrapper rejects.
            raise SystemExit(f"unsupported runtime: {exc}") from exc
    finally:
        for emulator in created:
            assert emulator.stopped == [False]
            assert not emulator.path.exists() and not emulator.path.parent.exists()


def test_owner_claim_and_cpu_halt_poll_probe_passes_and_cleans_up():
    run_fake_owner_poll_probe()


@pytest.mark.parametrize("defect", [
    "claim_overwrites_existing", "reject_clears_existing", "setter_overwrites_claim",
    "duplicate_claim_overwrites", "wrong_token_releases", "off_thread_releases",
    "active_release_allowed", "active_setter_allowed", "active_claim_allowed",
    "release_keeps_callback", "release_keeps_poll", "token_reused", "stale_token_releases",
    "default_poll_enabled", "release_after_fault_fails", "release_clears_fault",
    "release_loses_cause", "failed_reclaim_allowed", "poll_ignored", "no_poll",
    "no_halt_poll", "late_poll", "duplicate_poll", "malformed_poll", "wrong_poll_kind",
    "poll_off_owner_thread",
])
def test_advertised_claim_poll_apis_without_behavior_are_rejected(defect):
    with pytest.raises(SystemExit, match="unsupported runtime"):
        run_fake_owner_poll_probe(defect)


def test_claim_poll_probe_against_actual_selected_source_or_native_runtime():
    import pyboy
    from pyboy.core import serial

    mode = "source" if serial.__file__.endswith(".py") else "cython"
    bootstrap._verify_owner_poll_features(mode, pyboy, serial)
