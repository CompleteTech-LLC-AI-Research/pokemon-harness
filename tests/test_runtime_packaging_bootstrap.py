"""Bootstrap-runtime behaviour for the installed PyBoy runtime (#152).

Split from ``tests/test_runtime_packaging.py`` for #152 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Covers ``scripts/bootstrap_pyboy.py`` only.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from tests._runtime_packaging_support import (
    EXPECTED_PYBOY_REVISION,
    ROOT,
    _load_bootstrap,
)


def test_bootstrap_declares_and_checks_both_runtime_modes() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap_pyboy.py").read_text(encoding="utf-8")

    assert 'choices=("source", "cython")' in bootstrap
    assert '"--check"' in bootstrap
    assert 'env["PYBOY_NO_CYTHON"] = "1"' in bootstrap
    assert 'env.pop("PYBOY_NO_CYTHON", None)' in bootstrap
    assert '"-m", "ensurepip", "--upgrade"' in bootstrap
    assert '"--python", sys.executable' in bootstrap
    assert '"--no-deps"' in bootstrap
    assert "nullcontext(ROOT)" in bootstrap
    assert "_native_build_source(staged_inputs)" in bootstrap
    assert '"--build-evidence"' in bootstrap
    assert "_write_build_evidence(" in bootstrap
    assert "apply_external_edge" in bootstrap
    assert "cython_compiled" in bootstrap


def test_bootstrap_build_evidence_record_is_self_identifying(tmp_path) -> None:
    """The emitted record must carry what only the executed build produces."""

    module = _load_bootstrap()
    target = tmp_path / "evidence" / "native-build.json"
    identity = {
        "python": "3.11.9",
        "version": "2.7.0",
        "revision": EXPECTED_PYBOY_REVISION,
        "cython_compiled": True,
        "modules": {"pyboy.utils": {"kind": "cython", "sha256": "d" * 64}},
    }
    module._write_build_evidence(
        target,
        mode="cython",
        staged_inputs_sha256="b" * 64,
        status="complete",
        identity=identity,
    )
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["evidence_version"] == module.BUILD_EVIDENCE_VERSION
    assert document["procedure"] == "bootstrap_pyboy --mode cython"
    assert document["mode"] == "cython"
    assert document["status"] == "complete"
    assert document["build_inputs_sha256"] == "b" * 64
    assert document["runtime_identity"] == identity
    assert document["installed_fingerprint"] == module._runtime_fingerprint(identity)
    producer = document["producer"]
    assert producer["script"] == "scripts/bootstrap_pyboy.py"
    assert (
        producer["script_sha256"]
        == hashlib.sha256((ROOT / "scripts" / "bootstrap_pyboy.py").read_bytes()).hexdigest()
    )


def test_bootstrap_build_evidence_is_written_atomically(tmp_path) -> None:
    module = _load_bootstrap()
    target = tmp_path / "native-build.json"
    module._write_build_evidence(
        target,
        mode="cython",
        staged_inputs_sha256="b" * 64,
        status="complete",
        identity={"python": "3.11.9"},
    )
    left_behind = [path.name for path in tmp_path.iterdir() if path.name != target.name]
    assert left_behind == []
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "complete"


def test_bootstrap_rejects_build_evidence_with_check(monkeypatch, tmp_path) -> None:
    module = _load_bootstrap()
    monkeypatch.setattr(module, "_validate_source", lambda: None)
    monkeypatch.setattr(module, "_verify_runtime", lambda mode: None)
    with pytest.raises(SystemExit, match="cannot be combined with --check"):
        module.main(
            [
                "--mode",
                "cython",
                "--check",
                "--build-evidence",
                str(tmp_path / "evidence.json"),
            ]
        )


def test_bootstrap_does_not_emit_evidence_when_build_fails(monkeypatch, tmp_path) -> None:
    """A failed build must not leave a record that could be read as success."""

    module = _load_bootstrap()
    monkeypatch.setattr(module, "_validate_source", lambda: None)
    monkeypatch.setattr(module, "_metadata_was_present", lambda: True)
    monkeypatch.setattr(module, "_pip_command", lambda: [sys.executable, "-m", "pip"])
    monkeypatch.setattr(
        module,
        "_run_bounded",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    target = tmp_path / "evidence.json"
    exit_code = module.main(["--mode", "cython", "--build-evidence", str(target)])
    assert exit_code == 1
    assert not target.exists()


def test_bootstrap_pins_build_dependencies_and_disables_implicit_resolution() -> None:
    module = _load_bootstrap()

    assert module.BUILD_REQUIREMENTS == (
        "setuptools==77.0.3",
        "wheel==0.45.1",
        "cython==3.0.12",
        module._numpy_requirement(),
    )
    assert module._numpy_requirement((3, 11)) == "numpy==2.4.6"
    assert module._numpy_requirement((3, 12)) == "numpy==2.5.2"


def test_bootstrap_removes_transient_pyboy_metadata(tmp_path, monkeypatch) -> None:
    module = _load_bootstrap()
    metadata_dir = tmp_path / "pyboy.egg-info"
    monkeypatch.setattr(module, "PYBOY_SOURCE", tmp_path)
    was_present = module._metadata_was_present()
    metadata_dir.mkdir()
    (metadata_dir / "PKG-INFO").write_text("generated", encoding="utf-8")

    module._remove_generated_pyboy_metadata(was_present=was_present)

    assert not metadata_dir.exists()


def test_bootstrap_cleanup_refuses_a_symlinked_metadata_directory(tmp_path, monkeypatch) -> None:
    module = _load_bootstrap()
    source_root = tmp_path / "pyboy-src"
    source_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    metadata_dir = source_root / "pyboy.egg-info"
    try:
        metadata_dir.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable on this platform")
    monkeypatch.setattr(module, "PYBOY_SOURCE", source_root)

    module._remove_generated_pyboy_metadata(was_present=False)

    assert metadata_dir.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_bootstrap_cleanup_does_not_mask_metadata_removal_errors(tmp_path, monkeypatch) -> None:
    module = _load_bootstrap()
    source_root = tmp_path / "pyboy-src"
    source_root.mkdir()
    metadata_dir = source_root / "pyboy.egg-info"
    metadata_dir.mkdir()
    monkeypatch.setattr(module, "PYBOY_SOURCE", source_root)

    def refuse_removal(_path) -> None:
        raise PermissionError("metadata is temporarily locked")

    monkeypatch.setattr(module.shutil, "rmtree", refuse_removal)

    module._remove_generated_pyboy_metadata(was_present=False)

    assert metadata_dir.is_dir()


def test_bootstrap_cleanup_preserves_preexisting_metadata(tmp_path, monkeypatch) -> None:
    module = _load_bootstrap()
    source_root = tmp_path / "pyboy-src"
    source_root.mkdir()
    metadata_dir = source_root / "pyboy.egg-info"
    metadata_dir.mkdir()
    (metadata_dir / "PKG-INFO").write_text("preexisting", encoding="utf-8")
    monkeypatch.setattr(module, "PYBOY_SOURCE", source_root)

    module._remove_generated_pyboy_metadata(was_present=True)

    assert (metadata_dir / "PKG-INFO").read_text(encoding="utf-8") == "preexisting"


@pytest.mark.skipif(os.name != "posix", reason="process-group descendant checks require POSIX")
def test_bootstrap_timeout_terminates_posix_process_descendants(tmp_path) -> None:
    module = _load_bootstrap()
    pid_file = tmp_path / "descendant.pid"
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )

    descendant_pid: int | None = None

    def pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            module._run_bounded(
                [sys.executable, str(launcher), str(pid_file)],
                cwd=ROOT,
                env=os.environ.copy(),
                timeout=0.5,
            )

        deadline = time.monotonic() + 5.0
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.is_file(), "launcher did not publish its descendant PID"
        descendant_pid = int(pid_file.read_text(encoding="ascii"))

        deadline = time.monotonic() + 5.0
        while pid_is_alive(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not pid_is_alive(descendant_pid), "timeout left an installer descendant alive"
    finally:
        if descendant_pid is not None and pid_is_alive(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_bootstrap_interrupt_runs_descendant_safe_cleanup(monkeypatch) -> None:
    module = _load_bootstrap()

    class InterruptingProcess:
        pid = 12345

        def wait(self, timeout) -> int:
            raise KeyboardInterrupt

    process = InterruptingProcess()
    terminated: list[object] = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(module, "_terminate_process_tree", terminated.append)

    with pytest.raises(KeyboardInterrupt):
        module._run_bounded(["installer"], timeout=1.0)

    assert terminated == [process]


def test_bootstrap_windows_timeout_uses_recursive_taskkill(monkeypatch) -> None:
    module = _load_bootstrap()
    process_calls: list[tuple[list[str], dict]] = []

    class Process:
        pid = 12345

        def wait(self, timeout) -> int:
            return 0

        def kill(self) -> None:
            pass

    def fake_run(command, **kwargs):
        process_calls.append((list(command), kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "C:/Windows/System32/taskkill.exe")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._terminate_process_tree(Process())

    assert process_calls == [
        (
            ["C:/Windows/System32/taskkill.exe", "/PID", "12345", "/T", "/F"],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "check": False,
                "timeout": module.PROCESS_TERMINATION_GRACE_SECONDS,
            },
        )
    ]


def test_bootstrap_source_validation_rejects_a_symlinked_vendor_root(tmp_path, monkeypatch) -> None:
    module = _load_bootstrap()
    actual_root = tmp_path / "actual-pyboy"
    actual_root.mkdir()
    (actual_root / "POKERED_HARNESS_PYBOY_REVISION").write_text(
        EXPECTED_PYBOY_REVISION + "\n", encoding="ascii"
    )
    linked_root = tmp_path / "linked-pyboy"
    try:
        linked_root.symlink_to(actual_root, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable on this platform")
    monkeypatch.setattr(module, "PYBOY_SOURCE", linked_root)
    monkeypatch.setattr(
        module,
        "REVISION_FILE",
        linked_root / "POKERED_HARNESS_PYBOY_REVISION",
    )

    with pytest.raises(SystemExit):
        module._validate_source()


def test_bootstrap_rejects_modules_hidden_inside_a_foreign_distribution_root(
    tmp_path, monkeypatch
) -> None:
    module = _load_bootstrap()
    pinned_root = tmp_path / "vendor" / "pyboy-src"
    pinned_root.mkdir(parents=True)
    site_packages = tmp_path / "site-packages"
    foreign_root = site_packages / "untrusted"
    foreign_root.mkdir(parents=True)
    module_paths = {
        name: foreign_root / f"{name.rsplit('.', 1)[-1]}.py" for name in module.RUNTIME_MODULES
    }
    fake_modules = {name: ModuleType(name) for name in module.RUNTIME_MODULES}
    for name, fake_module in fake_modules.items():
        fake_module.__file__ = str(module_paths[name])
    fake_pyboy = fake_modules["pyboy"]
    fake_pyboy.__version__ = "2.7.0"
    fake_pyboy.__pokered_harness_revision__ = EXPECTED_PYBOY_REVISION
    fake_utils = fake_modules["pyboy.utils"]
    fake_utils.cython_compiled = False
    fake_pyboy.utils = fake_utils
    monkeypatch.setattr(module, "PYBOY_SOURCE", pinned_root)
    monkeypatch.setattr(module.importlib, "import_module", fake_modules.__getitem__)
    monkeypatch.setitem(sys.modules, "pyboy", fake_pyboy)

    class Distribution:
        def locate_file(self, relative) -> Path:
            return site_packages / Path(relative)

    monkeypatch.setattr(module.importlib.metadata, "distribution", lambda _name: Distribution())
    monkeypatch.setattr(module, "_package_distributions", lambda _package: {"pokered-harness"})
    monkeypatch.setattr(
        module,
        "_new_serial_instance",
        lambda: SimpleNamespace(
            backend=object(), apply_external_edge=lambda *_args: None, peek_out_bit=lambda: 0
        ),
    )

    with pytest.raises(SystemExit, match="loaded outside"):
        module._verify_runtime("source")


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

    monkeypatch.setattr(module, "_run_bounded", fake_run)

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

    monkeypatch.setattr(module, "_run_bounded", fake_run)
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
    monkeypatch.setattr(module, "_run_bounded", fake_run)

    assert module.main(["--mode", "source"]) == 0
    assert len(calls) == 2
    build_command, build_kwargs = calls[0]
    assert build_command == [
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        *module.BUILD_REQUIREMENTS,
    ]
    assert build_kwargs["cwd"] == module.ROOT

    command, kwargs = calls[1]
    assert command == [
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "--no-build-isolation",
        module.CYTHON_REQUIREMENT,
        str(module.ROOT),
    ]
    assert kwargs["cwd"] == module.ROOT
    assert kwargs["env"]["PYBOY_NO_CYTHON"] == "1"


def test_bootstrap_cython_mode_targets_only_the_checked_in_fork(tmp_path, monkeypatch) -> None:
    module = _load_bootstrap()
    calls: list[tuple[list[str], dict]] = []
    staged_paths: list[Path] = []
    monkeypatch.setattr(module, "ROOT", tmp_path)

    class Result:
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        if len(calls) == 3:
            staged = Path(command[-1])
            staged_paths.append(staged)
            assert staged != module.PYBOY_SOURCE
            assert (staged / "pyboy" / "core" / "mb.pxd").read_bytes() == (
                module.PYBOY_SOURCE / "pyboy" / "core" / "mb.pxd"
            ).read_bytes()
            assert (staged / "pyboy" / "core" / "cpu.py").is_file()
            assert not list(staged.rglob("*.c"))
            assert not list(staged.rglob("*.so"))
        return Result()

    monkeypatch.setattr(module, "_validate_source", lambda: None)
    monkeypatch.setattr(module, "_verify_runtime", lambda _mode: None)
    monkeypatch.setattr(module, "_pip_command", lambda: ["pip", "install"])
    monkeypatch.setattr(module, "_run_bounded", fake_run)

    assert module.main(["--mode", "cython"]) == 0
    assert len(calls) == 3
    build_command, build_kwargs = calls[0]
    assert build_command == [
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        *module.BUILD_REQUIREMENTS,
    ]
    assert build_kwargs["cwd"] == module.ROOT

    project_command, project_kwargs = calls[1]
    assert project_command == [
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "--no-build-isolation",
        "-e",
        str(module.ROOT),
    ]
    assert project_kwargs["cwd"] == module.ROOT
    assert "PYBOY_NO_CYTHON" not in project_kwargs["env"]

    command, kwargs = calls[2]
    assert command[:5] == [
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "--no-build-isolation",
    ]
    assert command[-2:] == [module.CYTHON_REQUIREMENT, str(staged_paths[0])]
    assert not staged_paths[0].exists()
    assert "PYBOY_NO_CYTHON" not in kwargs["env"]
    assert module.CYTHON_MODULES == (
        "pyboy.pyboy",
        "pyboy.utils",
        "pyboy.core.mb",
        "pyboy.core.serial",
    )


def test_native_build_snapshot_excludes_stale_outputs_and_preserves_inputs(
    tmp_path, monkeypatch, capsys
) -> None:
    module = _load_bootstrap()
    source = tmp_path / "vendor" / "pyboy-src"
    source.mkdir(parents=True)
    files = {
        "POKERED_HARNESS_PYBOY_REVISION": b"revision",
        "setup.py": b"source setup",
        "pyproject.toml": b"source metadata",
        "pyboy/core/mb.py": b"new motherboard",
        "pyboy/core/mb.pxd": b"new method table",
        "pyboy/core/cpu.py": b"dependent module",
        "pyboy/core/bootrom_dmg.bin": b"boot resource",
        "pyboy/core/mb.c": b"old generated C",
        "pyboy/core/cpu.c": b"old imported method table",
        "pyboy/core/cpu.so": b"old extension",
        "build/lib/pyboy/core/mb.py": b"old build copy",
        "pyboy.egg-info/SOURCES.txt": b"old manifest",
    }
    for name, contents in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PYBOY_SOURCE", source)
    monkeypatch.setattr(module, "REVISION_FILE", source / "POKERED_HARNESS_PYBOY_REVISION")
    with module._native_build_source() as staged:
        assert {
            path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()
        } == {
            "POKERED_HARNESS_PYBOY_REVISION",
            "setup.py",
            "pyproject.toml",
            "pyboy/core/mb.py",
            "pyboy/core/mb.pxd",
            "pyboy/core/cpu.py",
            "pyboy/core/bootrom_dmg.bin",
        }
        assert (staged / "pyboy/core/mb.pxd").read_bytes() == b"new method table"
    first_fingerprint = capsys.readouterr().out
    assert not staged.exists()
    assert all((source / name).read_bytes() == contents for name, contents in files.items())

    # Stale output cannot influence the fingerprint; an ABI input must.
    (source / "pyboy/core/cpu.c").write_bytes(b"other stale output")
    with module._native_build_source():
        pass
    assert capsys.readouterr().out == first_fingerprint
    (source / "pyboy/core/mb.pxd").write_bytes(b"changed method table")
    with module._native_build_source():
        pass
    assert capsys.readouterr().out != first_fingerprint


def test_native_build_snapshot_cleans_only_its_staging_on_failure(tmp_path, monkeypatch) -> None:
    module = _load_bootstrap()
    source = tmp_path / "vendor"
    source.mkdir()
    (source / "setup.py").write_text("source")
    preserved = tmp_path / "build" / "user-output"
    preserved.mkdir(parents=True)
    (preserved / "keep.txt").write_text("keep")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PYBOY_SOURCE", source)
    with (
        pytest.raises(RuntimeError, match="installer failed"),
        module._native_build_source() as staged,
    ):
        assert (staged / "setup.py").is_file()
        raise RuntimeError("installer failed")
    assert not staged.exists()
    assert (source / "setup.py").read_text() == "source"
    assert (preserved / "keep.txt").read_text() == "keep"


def test_bootstrap_source_mode_rejects_a_competing_pyboy_distribution(monkeypatch) -> None:
    module = _load_bootstrap()
    monkeypatch.setattr(
        module,
        "_package_distributions",
        lambda package: {"pokered-harness", "pyboy"} if package == "pyboy" else set(),
    )

    with pytest.raises(SystemExit, match="competing installed owners"):
        module._verify_runtime("source")


def test_bootstrap_cython_mode_rejects_a_missing_harness_owner(monkeypatch) -> None:
    module = _load_bootstrap()
    monkeypatch.setattr(
        module,
        "_package_distributions",
        lambda package: {"pyboy"} if package == "pyboy" else set(),
    )

    with pytest.raises(SystemExit, match="not provided by the installed pokered-harness"):
        module._verify_runtime("cython")


def test_bootstrap_reports_an_incompatible_serial_constructor(monkeypatch) -> None:
    module = _load_bootstrap()

    class BrokenSerial:
        def __init__(self, *_args) -> None:
            raise TypeError("stock PyBoy ABI")

    monkeypatch.setattr(module, "_new_serial_instance", lambda: BrokenSerial(False))
    monkeypatch.setattr(
        module,
        "_package_distributions",
        lambda package: {"pokered-harness", "pyboy"} if package == "pyboy" else set(),
    )

    with pytest.raises(SystemExit, match="serial contract could not be constructed"):
        module._verify_runtime("source")


def test_bootstrap_rejects_a_runtime_without_frame_ownership(monkeypatch) -> None:
    import pyboy
    from pyboy import utils

    module = _load_bootstrap()
    monkeypatch.setattr(pyboy, "PyBoy", SimpleNamespace(_tick=None))
    mode = "cython" if utils.cython_compiled else "source"
    with pytest.raises(SystemExit, match="PyBoy._tick frame ownership is unavailable"):
        module._verify_runtime(mode)
