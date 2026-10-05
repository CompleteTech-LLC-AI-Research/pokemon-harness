#!/usr/bin/env python3
"""Install the pinned PyBoy source runtime into the active environment.

The normal harness distribution already bundles this source tree.  This
bootstrap is for the two explicit runtime modes used by development and
performance testing:

* ``source`` (default): disable Cython and install the harness distribution,
  including its bundled Python sources;
* ``cython``: install the current checkout as an editable harness distribution
  and build the checked-in PyBoy fork with its Cython extensions. This is an
  optional source-checkout diagnostic; source mode is the supported production
  runtime and a Cython compile failure is reported without weakening the
  source-runtime contract.

Both modes use the checked-in source snapshot.  No network VCS checkout,
``PYTHONPATH`` override, or machine-specific path is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.machinery
import importlib.metadata
import io  # noqa: F401 - shared with the static probe-helper facade
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading  # noqa: F401 - shared with the static probe-helper facade
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

if __package__:
    from . import _bootstrap_runtime_contract, _bootstrap_runtime_probes
    from . import check_import_origins as _origin_guard
elif __name__ == "__main__":
    import _bootstrap_runtime_contract
    import _bootstrap_runtime_probes
    import check_import_origins as _origin_guard
else:
    _scripts_root = Path(__file__).resolve().parent
    _scripts_package = sys.modules.get("scripts")
    if _scripts_package is None:
        _scripts_package = ModuleType("scripts")
        _scripts_spec = importlib.machinery.ModuleSpec("scripts", loader=None, is_package=True)
        _scripts_spec.submodule_search_locations = [str(_scripts_root)]
        _scripts_package.__file__ = None
        _scripts_package.__loader__ = None
        _scripts_package.__package__ = "scripts"
        _scripts_package.__path__ = [str(_scripts_root)]
        _scripts_package.__spec__ = _scripts_spec
        _scripts_package = sys.modules.setdefault("scripts", _scripts_package)
    if type(_scripts_package) is not ModuleType:
        raise ImportError("bootstrap scripts namespace is owned by another module")
    _scripts_spec = _scripts_package.__spec__
    if type(_scripts_spec) is not importlib.machinery.ModuleSpec:
        raise ImportError("bootstrap scripts namespace has an invalid import spec")
    try:
        _scripts_paths = tuple(Path(item).resolve() for item in _scripts_package.__path__)
        _spec_paths = tuple(
            Path(item).resolve() for item in _scripts_spec.submodule_search_locations or ()
        )
    except (AttributeError, OSError, TypeError) as exc:
        raise ImportError("bootstrap scripts namespace has invalid search paths") from exc
    if (
        _scripts_package.__file__ is not None
        or _scripts_package.__package__ != "scripts"
        or _scripts_package.__loader__ is not _scripts_spec.loader
        or _scripts_spec.name != "scripts"
        or _scripts_spec.parent != "scripts"
        or _scripts_spec.origin is not None
        or (
            _scripts_spec.loader is not None
            and not isinstance(_scripts_spec.loader, importlib.machinery.NamespaceLoader)
        )
        or not _scripts_paths
        or not _spec_paths
        or _scripts_paths != _spec_paths
        or any(path != _scripts_root for path in _scripts_paths)
        or any(path != _scripts_root for path in _spec_paths)
    ):
        raise ImportError("bootstrap scripts namespace belongs to another source tree")
    for _helper_name in (
        "_bootstrap_runtime_contract",
        "_bootstrap_runtime_probes",
        "check_import_origins",
        "_import_origin_resolution",
        "_import_origin_paths",
        "_import_origin_finders",
        "_import_origin_attestations",
        "_import_origin_selected_owners",
    ):
        _cached_helper = sys.modules.get(f"scripts.{_helper_name}")
        if _cached_helper is not None and (
            type(_cached_helper) is not ModuleType
            or getattr(_cached_helper, "__file__", None) is None
            or Path(_cached_helper.__file__).resolve() != _scripts_root / f"{_helper_name}.py"
        ):
            raise ImportError(f"bootstrap helper {_helper_name} belongs to another source tree")
    from scripts import _bootstrap_runtime_contract, _bootstrap_runtime_probes
    from scripts import check_import_origins as _origin_guard  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PYBOY_SOURCE = ROOT / "vendor" / "pyboy-src"
REVISION_FILE = PYBOY_SOURCE / "POKERED_HARNESS_PYBOY_REVISION"
EXPECTED_PYBOY_VERSION = "2.7.0"
EXPECTED_REVISION = "fd765b1808ac9cb192b42ae971987158ff36ae48"
CYTHON_REQUIREMENT = "cython==3.0.12"
SETUPTOOLS_REQUIREMENT = "setuptools==77.0.3"
WHEEL_REQUIREMENT = "wheel==0.45.1"
NUMPY_311_REQUIREMENT = "numpy==2.4.6"
NUMPY_312_REQUIREMENT = "numpy==2.5.2"


def _numpy_requirement(python_version: tuple[int, int] = sys.version_info[:2]) -> str:
    """Return the pinned NumPy build dependency for *python_version*."""
    return NUMPY_311_REQUIREMENT if python_version < (3, 12) else NUMPY_312_REQUIREMENT


NUMPY_REQUIREMENT = _numpy_requirement()
BUILD_REQUIREMENTS = (
    SETUPTOOLS_REQUIREMENT,
    WHEEL_REQUIREMENT,
    CYTHON_REQUIREMENT,
    NUMPY_REQUIREMENT,
)
PROJECT_DISTRIBUTION = "pokered-harness"
PIP_PROBE_TIMEOUT_SECONDS = 60
INSTALL_TIMEOUT_SECONDS = 1800
CHECK_TIMEOUT_SECONDS = 30
PROCESS_TERMINATION_GRACE_SECONDS = 5
ISOLATION_ENVIRONMENT_KEYS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "PIP_PREFIX",
    "PIP_TARGET",
    "PIP_USER",
)
RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
    "pyboy.link",
)
# The link transport is bundled Python source in both runtime modes.  Cython
# mode validates its import and provenance through RUNTIME_MODULES, but must
# not require it to be an extension module.
CYTHON_MODULES = tuple(name for name in RUNTIME_MODULES if name not in {"pyboy", "pyboy.link"})
NATIVE_INPUT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyx",
        ".pxd",
        ".pxi",
        ".txt",
        ".bin",
        ".md",
        ".in",
        ".toml",
    }
)
BUILD_EVIDENCE_VERSION = 2


def _process_group_options() -> dict[str, object]:
    """Return subprocess options that put a command in its own process tree.

    POSIX children get a new session, making the process id a process-group id
    that can be terminated together with descendants.  Windows has no
    portable ``killpg`` equivalent; a new process group enables a graceful
    Ctrl-Break fallback, while ``taskkill /T`` below provides the normal tree
    termination path.
    """
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[object]) -> None:
    """Best-effort terminate *process* and all descendants within a bound.

    This function is used only after a timeout or an interrupt.  Cleanup must
    not replace the original failure with a second exception, so every
    platform-specific termination step is deliberately best effort.  On
    POSIX, SIGTERM followed by SIGKILL is sent to the private process group.
    On Windows, the built-in ``taskkill`` command can terminate a process tree
    even when the child created further processes; if it is unavailable, the
    private process group and direct-process fallbacks are used.
    """
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=PROCESS_TERMINATION_GRACE_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
                pass
        else:
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                try:
                    process.send_signal(ctrl_break)
                except (OSError, ValueError):
                    pass

        try:
            process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
    else:
        try:
            # ``start_new_session=True`` makes ``process.pid`` the process
            # group id.  Signal the group even if the direct child has already
            # transitioned to a zombie; descendants may still be alive.
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass

    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        pass

    if os.name == "nt":
        try:
            process.kill()
        except (OSError, ValueError):
            pass
    else:
        # Escalate the whole group even when the direct child honored SIGTERM
        # and exited.  Descendants can ignore SIGTERM and otherwise survive
        # while ``process.wait`` has already returned.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except (OSError, ValueError):
                pass
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        # Returning keeps the original TimeoutExpired/KeyboardInterrupt
        # visible to the caller.  There is no safe broader kill target.
        pass


def _run_bounded(
    command: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.CompletedProcess[object]:
    """Run a command with a deadline and descendant-safe interruption.

    ``subprocess.run(..., timeout=...)`` kills only its direct child.  Package
    installers and build backends can create workers, so use ``Popen`` with a
    private process group/session and explicitly tear down that group before
    propagating the timeout or interrupt.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        **_process_group_options(),
    )
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        raise
    except KeyboardInterrupt:
        _terminate_process_tree(process)
        raise
    return subprocess.CompletedProcess(command, returncode)


def _pip_command() -> list[str]:
    """Return a usable install-command prefix for the active interpreter.

    ``uv venv`` and some embedded Python distributions intentionally omit
    pip.  The bootstrap command must still work in those environments, so
    install the standard-library copy first instead of assuming that
    ``python -m pip`` is already available.
    """
    command = [sys.executable, "-m", "pip"]
    env = _isolated_environment()
    probe = _run_bounded(
        [*command, "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=PIP_PROBE_TIMEOUT_SECONDS,
    )
    if probe.returncode == 0:
        return [*command, "install"]

    bootstrap = _run_bounded(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        env=env,
        timeout=PIP_PROBE_TIMEOUT_SECONDS,
    )
    if bootstrap.returncode != 0:
        # ``uv venv`` deliberately omits pip unless seeded, and distro Python
        # builds may omit ensurepip altogether. Use uv's interpreter-targeted
        # installer when it is available rather than silently falling back to
        # a different Python executable.
        uv = shutil.which("uv")
        if uv is not None:
            return [uv, "pip", "install", "--python", sys.executable]
        raise SystemExit(
            "pip is unavailable and ensurepip failed; install pip in the "
            "active environment (or install uv) before running bootstrap_pyboy.py"
        )

    verify = _run_bounded(
        [*command, "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=PIP_PROBE_TIMEOUT_SECONDS,
    )
    if verify.returncode != 0:
        raise SystemExit(
            "ensurepip completed but the active interpreter still cannot run python -m pip"
        )
    return [*command, "install"]


def _isolated_environment() -> dict[str, str]:
    """Return an install environment tied to the active interpreter.

    Pip is invoked as ``sys.executable -m pip`` (or with uv's explicit
    ``--python`` fallback), but path and target-selection variables can still
    redirect imports or installation outside that interpreter.  Keep index
    and proxy configuration intact while removing only those redirection
    controls.
    """
    environment = os.environ.copy()
    for key in ISOLATION_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _validate_source() -> None:
    # The revision marker alone is not sufficient provenance: a symlink can
    # point the bootstrap at an unrelated checkout while preserving the
    # expected marker.  Keep the source boundary physical and fail closed.
    if PYBOY_SOURCE.is_symlink() or not PYBOY_SOURCE.is_dir():
        raise SystemExit(f"vendored PyBoy source is missing: {PYBOY_SOURCE}")
    try:
        revision = REVISION_FILE.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read PyBoy revision marker: {REVISION_FILE}: {exc}") from exc
    if revision != EXPECTED_REVISION:
        raise SystemExit(
            "vendored PyBoy revision mismatch: "
            f"expected {EXPECTED_REVISION}, got {revision or '<empty>'}"
        )


@contextmanager
def _native_build_source(digest_sink: list[str] | None = None) -> Iterator[Path]:
    """Build every extension from one source-only snapshot.

    Cython extension types share method tables across modules. Reusing old
    generated C or objects after a declaration change can produce a mixed
    runtime even when every import succeeds. The allowlist stages Python/Cython
    sources and resources; generated C, headers, and binaries are inspected for
    orphaned outputs but never enter this fresh build directory.

    ``digest_sink``, when supplied, receives the staged-inputs digest. The
    caller uses it to bind retained build evidence to the exact bytes that
    were handed to the compiler instead of a value it supplied itself.
    """
    build_root = ROOT / "build"
    build_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyboy-native-", dir=build_root) as temporary:
        destination = Path(temporary) / "pyboy-src"
        destination.mkdir()
        digest = hashlib.sha256()
        for directory, children, filenames in os.walk(PYBOY_SOURCE):
            children[:] = sorted(
                name
                for name in children
                if not name.startswith(".")
                and name not in {"build", "dist", "__pycache__", "venv"}
                and not name.endswith(".egg-info")
            )
            for name in children:
                if (Path(directory) / name).is_symlink():
                    raise ValueError("native build inputs must not contain symlinked directories")
            for name in sorted(filenames):
                source = Path(directory) / name
                if source.is_symlink():
                    raise ValueError("native build inputs must not contain symlinked files")
                if source.suffix.lower() in {".c", ".cpp", ".h", ".hpp"}:
                    try:
                        with source.open("rb") as stream:
                            generated = (
                                stream.read(128).lstrip().startswith(b"/* Generated by Cython ")
                            )
                    except OSError as exc:
                        raise ValueError(
                            f"cannot inspect native build input {source}: {exc}"
                        ) from exc
                    if generated:
                        stems = [source.stem]
                        if source.suffix.lower() in {".h", ".hpp"} and source.stem.endswith("_api"):
                            stems.append(source.stem[:-4])
                        regeneration_sources = [
                            source.with_name(stem + suffix)
                            for stem in stems
                            for suffix in (".py", ".pyx")
                        ]
                        if not any(
                            candidate.is_file() and not candidate.is_symlink()
                            for candidate in regeneration_sources
                        ):
                            raise ValueError(
                                f"generated Cython output has no regeneration source: {source}"
                            )
                        continue
                if source.suffix not in NATIVE_INPUT_SUFFIXES and source != REVISION_FILE:
                    continue
                relative = source.relative_to(PYBOY_SOURCE)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                # Hash the staged bytes, which are the compiler's inputs.
                digest.update(relative.as_posix().encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(target.read_bytes()).digest())
        staged_digest = digest.hexdigest()
        if digest_sink is not None:
            digest_sink.append(staged_digest)
        print(f"PyBoy native build inputs SHA-256: {staged_digest}", flush=True)
        yield destination


def _module_kind(module: object) -> str:
    """Classify the loaded module as source Python or a Cython extension."""
    filename = str(getattr(module, "__file__", "") or "")
    if filename.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
        return "cython"
    if filename.endswith(".py"):
        return "source"
    return "unknown"


def _normalise_distribution_name(name: str) -> str:
    return name.lower().replace("_", "-")


def _package_distributions(package: str) -> set[str]:
    """Return normalized distributions that claim ``package``.

    Editable installs can expose the same distribution more than once; the
    set removes that harmless duplication while preserving a competing owner
    such as a separately installed stock PyBoy.
    """
    return {
        _normalise_distribution_name(name)
        for name in importlib.metadata.packages_distributions().get(package, ())
    }


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _runtime_roots() -> tuple[Path, ...]:
    """Return package-specific roots allowed for the pinned PyBoy modules.

    A distribution's ``locate_file("")`` is normally the entire
    ``site-packages`` directory.  Treating that as an allowed runtime root
    would let an unrelated module shadow the pinned package while the
    distribution metadata still names ``pokered-harness``.  Resolve only the
    distribution's concrete ``pyboy`` package directory instead.
    """
    roots = [PYBOY_SOURCE.resolve()]
    for distribution_name in (PROJECT_DISTRIBUTION, "pyboy"):
        try:
            location = Path(importlib.metadata.distribution(distribution_name).locate_file("pyboy"))
            roots.append(location.resolve())
        except (OSError, importlib.metadata.PackageNotFoundError):
            continue
    return tuple(dict.fromkeys(roots))


def _new_serial_instance() -> object:
    """Construct the serial object after the module import contract passes."""
    from pyboy.core.serial import Serial

    return Serial(False)


def _verify_serial_features(mode: str, serial_module: object) -> None:
    return _bootstrap_runtime_probes._verify_serial_features(mode, serial_module, api=globals())


def _verify_owner_clock_features(mode: str, pyboy_module: object, serial_module: object) -> None:
    return _bootstrap_runtime_probes._verify_owner_clock_features(
        mode, pyboy_module, serial_module, api=globals()
    )


def _verify_owner_poll_features(mode: str, pyboy_module: object, serial_module: object) -> None:
    return _bootstrap_runtime_probes._verify_owner_poll_features(
        mode, pyboy_module, serial_module, api=globals()
    )


def _runtime_identity() -> dict[str, object]:
    """Return the installed runtime identity used for consistent-build binding.

    This mirrors the identity recomputed by the qualification runner's native
    probe so the retained evidence fingerprints the same fields.  The identity
    covers the *complete* installed output set, not only the named entry
    modules: a compiled module the module list never names (for example
    ``pyboy/core/cpu*.so``) must change the fingerprint when it is replaced.
    """

    modules = {name: importlib.import_module(name) for name in RUNTIME_MODULES}
    import pyboy
    from pyboy import utils

    package_root = Path(pyboy.__file__).resolve().parent

    def _relative(filename: str) -> str:
        path = Path(filename)
        try:
            return path.resolve().relative_to(package_root).as_posix()
        except ValueError:
            return path.name

    report: dict[str, object] = {}
    for name, module in modules.items():
        filename = str(getattr(module, "__file__", "") or "")
        digest = None
        if filename:
            try:
                with open(filename, "rb") as stream:
                    digest = hashlib.sha256(stream.read()).hexdigest()
            except OSError:
                digest = None
        report[name] = {
            "kind": _module_kind(module),
            "sha256": digest,
            "artifact": _relative(filename) if filename else None,
        }
    artifacts: dict[str, str] = {}
    for entry in sorted(package_root.rglob("*")):
        if "__pycache__" in entry.parts or entry.suffix in (".pyc", ".pyo"):
            continue
        if not entry.is_file():
            continue
        try:
            with open(entry, "rb") as stream:
                payload = stream.read()
        except OSError as exc:
            raise RuntimeError(f"unreadable installed artifact {entry.name}: {exc}") from exc
        artifacts[entry.relative_to(package_root).as_posix()] = hashlib.sha256(payload).hexdigest()
    return {
        "python": sys.version.split()[0],
        "version": getattr(pyboy, "__version__", None),
        "revision": getattr(pyboy, "__pokered_harness_revision__", None),
        "cython_compiled": bool(getattr(utils, "cython_compiled", False)),
        "modules": report,
        "artifacts": artifacts,
    }


def _runtime_fingerprint(identity: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_build_evidence(
    path: Path,
    *,
    mode: str,
    staged_inputs_sha256: str | None,
    status: str,
    identity: dict[str, object] | None,
    detail: str = "",
) -> None:
    """Retain the fresh-build procedure's own record for later verification.

    Only the executed build can emit this document: the staged-inputs digest
    comes from the bytes copied for the compiler, the fingerprint comes from
    the runtime installed by this run, and the producer digest identifies the
    bootstrap that ran. The record is written atomically so a partial write
    can never be mistaken for a completed build.
    """

    try:
        script = Path(__file__).resolve().relative_to(ROOT)
        script_name = script.as_posix()
    except ValueError:
        script_name = Path(__file__).name
    document: dict[str, object] = {
        "evidence_version": BUILD_EVIDENCE_VERSION,
        "procedure": f"bootstrap_pyboy --mode {mode}",
        "mode": mode,
        "status": status,
        "build_inputs_sha256": staged_inputs_sha256,
        "interpreter": {
            "python_version": sys.version.split()[0],
            "prefix_name": Path(sys.prefix).name,
        },
        "producer": {
            "script": script_name,
            "script_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        },
        "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if identity is not None:
        document["runtime_identity"] = identity
        document["installed_fingerprint"] = _runtime_fingerprint(identity)
    else:
        document["runtime_identity"] = None
        document["installed_fingerprint"] = None
    if detail:
        document["detail"] = detail

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".partial"
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _metadata_was_present() -> bool:
    """Return whether the native metadata entry existed before this run.

    An unknown inspection result is treated as present.  The bootstrap must
    be able to prove that it owns a cleanup target before recursively deleting
    it; preserving an uninspectable entry is safer than guessing.
    """
    metadata_dir = PYBOY_SOURCE / "pyboy.egg-info"
    try:
        metadata_dir.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _remove_generated_pyboy_metadata(*, was_present: bool) -> None:
    """Safely remove only the native build metadata owned by this bootstrap.

    ``Path.is_dir`` follows symlinks, which would make a replacement
    ``pyboy.egg-info`` entry an unsafe recursive-delete target.  Refuse
    symlinked roots and entries.  A pre-existing entry is never removed, and
    cleanup races or permission problems do not mask the install/build result.
    """
    if was_present:
        return

    source_root = PYBOY_SOURCE
    if source_root.is_symlink():
        print(
            f"refusing to remove generated metadata through symlinked source: {source_root}",
            file=sys.stderr,
        )
        return

    try:
        source_stat = source_root.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"could not inspect PyBoy source for metadata cleanup: {exc}", file=sys.stderr)
        return
    if not stat.S_ISDIR(source_stat.st_mode):
        print(
            f"refusing to remove generated metadata from non-directory source: {source_root}",
            file=sys.stderr,
        )
        return

    metadata_dir = source_root / "pyboy.egg-info"
    try:
        metadata_stat = metadata_dir.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"could not inspect generated PyBoy metadata: {exc}", file=sys.stderr)
        return
    if not stat.S_ISDIR(metadata_stat.st_mode) or stat.S_ISLNK(metadata_stat.st_mode):
        print(
            f"refusing to recursively remove non-directory generated metadata: {metadata_dir}",
            file=sys.stderr,
        )
        return

    try:
        shutil.rmtree(metadata_dir)
    except FileNotFoundError:
        # Another cleanup process won the race; the desired end state holds.
        pass
    except OSError as exc:
        print(f"could not remove generated PyBoy metadata {metadata_dir}: {exc}", file=sys.stderr)


def _verify_runtime(mode: str) -> None:
    """Fail closed unless the installed runtime matches the requested mode."""
    try:
        modules = {name: importlib.import_module(name) for name in RUNTIME_MODULES}
        import pyboy
        from pyboy import utils
    except Exception as exc:
        raise SystemExit(
            "PyBoy runtime cannot be imported after bootstrap; install the "
            f"harness dependencies first or inspect the build output: {type(exc).__name__}: {exc}"
        ) from exc
    harness_module = sys.modules.get("pokered_harness")
    if harness_module is None:
        try:
            harness_module = importlib.import_module("pokered_harness")
        except Exception:  # noqa: BLE001 - missing witness is a contract refusal below
            harness_module = None
    problems = _bootstrap_runtime_contract.verify_preconstruction_contract(
        mode, modules, pyboy, utils, harness_module, api=globals()
    )
    if problems:
        raise SystemExit(f"PyBoy runtime contract failed for --mode {mode}: " + "; ".join(problems))
    try:
        serial = _new_serial_instance()
    except Exception as exc:
        raise SystemExit(
            "PyBoy runtime contract failed for --mode "
            f"{mode}: serial contract could not be constructed: {type(exc).__name__}: {exc}"
        ) from exc
    problems = _bootstrap_runtime_contract.verify_serial_instance(serial)
    if problems:
        raise SystemExit(f"PyBoy runtime contract failed for --mode {mode}: " + "; ".join(problems))
    _bootstrap_runtime_contract.verify_behavior(
        mode, pyboy, modules["pyboy.core.serial"], api=globals()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("source", "cython"),
        default="source",
        help=("production mode, cython is an optional source-checkout diagnostic"),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the active PyBoy runtime without reinstalling it",
    )
    parser.add_argument(
        "--install-timeout",
        type=float,
        default=INSTALL_TIMEOUT_SECONDS,
        help=f"maximum seconds per install/build child (default: {INSTALL_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--check-timeout",
        type=float,
        default=CHECK_TIMEOUT_SECONDS,
        help=f"maximum seconds per check child (default: {CHECK_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument("--_runtime-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--build-evidence",
        metavar="PATH",
        help=(
            "retain the fresh-build procedure's own evidence record at PATH; "
            "only a completed build emits this document"
        ),
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.install_timeout) or args.install_timeout <= 0:
        parser.error("--install-timeout must be finite and positive")
    if not math.isfinite(args.check_timeout) or args.check_timeout <= 0:
        parser.error("--check-timeout must be finite and positive")

    _validate_source()
    if args.check and args.build_evidence:
        raise SystemExit("--build-evidence cannot be combined with --check")
    if args._runtime_probe:
        _verify_runtime(args.mode)
        return 0
    if args.check:
        env = _isolated_environment()
        if args.mode == "source":
            env["PYBOY_NO_CYTHON"] = "1"
        else:
            env.pop("PYBOY_NO_CYTHON", None)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--mode",
            args.mode,
            "--check-timeout",
            f"{args.check_timeout:g}",
            "--_runtime-probe",
        ]
        try:
            return _run_bounded(command, cwd=ROOT, env=env, timeout=args.check_timeout).returncode
        except subprocess.TimeoutExpired as exc:
            print(
                f"bootstrap command timed out after {exc.timeout} seconds: {exc.cmd}",
                file=sys.stderr,
            )
            return 124

    metadata_was_present = _metadata_was_present()

    env = _isolated_environment()
    if args.mode == "source":
        env["PYBOY_NO_CYTHON"] = "1"
    else:
        env.pop("PYBOY_NO_CYTHON", None)

    try:
        pip_install = _pip_command()
        build_result = _run_bounded(
            [
                *pip_install,
                "--force-reinstall",
                "--no-deps",
                *BUILD_REQUIREMENTS,
            ],
            cwd=ROOT,
            env=env,
            timeout=args.install_timeout,
        )
        if build_result.returncode:
            return build_result.returncode

        if args.mode == "cython":
            # Keep the harness distribution installed in native environments too.
            # The vendored fork has its own ``pyboy`` distribution metadata, but
            # that package alone cannot provide the MCP entry point or harness
            # modules. Install the checkout first so the native fork can overlay
            # its extension-backed PyBoy modules without losing project ownership.
            project_command = [
                *pip_install,
                "--force-reinstall",
                "--no-deps",
                "--no-build-isolation",
                "-e",
                str(ROOT),
            ]
            project_result = _run_bounded(
                project_command,
                cwd=ROOT,
                env=env,
                timeout=args.install_timeout,
            )
            if project_result.returncode:
                return project_result.returncode

        staged_inputs: list[str] = []
        try:
            source_context = (
                nullcontext(ROOT) if args.mode == "source" else _native_build_source(staged_inputs)
            )
            with source_context as install_target:
                command = [
                    *pip_install,
                    "--force-reinstall",
                    "--no-deps",
                    "--no-build-isolation",
                    CYTHON_REQUIREMENT,
                    str(install_target),
                ]
                result = _run_bounded(
                    command,
                    cwd=ROOT,
                    env=env,
                    timeout=args.install_timeout,
                )
        except (OSError, ValueError) as exc:
            print(f"PyBoy native source staging failed: {exc}", file=sys.stderr)
            return 1
        if result.returncode:
            if args.mode == "cython":
                print(
                    "Cython runtime build failed for the pinned PyBoy source; "
                    "the supported production runtime remains --mode source.",
                    file=sys.stderr,
                )
            return result.returncode
        if args.mode == "cython":
            dependency_check = _run_bounded(
                [sys.executable, "-m", "pip", "check"],
                cwd=ROOT,
                env=env,
                timeout=args.check_timeout,
            )
            if dependency_check.returncode:
                return dependency_check.returncode
        runtime_probe = _run_bounded(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--mode",
                args.mode,
                "--check-timeout",
                f"{args.check_timeout:g}",
                "--_runtime-probe",
            ],
            cwd=ROOT,
            env=env,
            timeout=args.check_timeout,
        )
        if runtime_probe.returncode:
            return runtime_probe.returncode
        if args.build_evidence:
            _write_build_evidence(
                Path(args.build_evidence),
                mode=args.mode,
                staged_inputs_sha256=staged_inputs[0] if staged_inputs else None,
                status="complete",
                identity=_runtime_identity(),
            )
        return 0
    except subprocess.TimeoutExpired as exc:
        print(
            f"bootstrap command timed out after {exc.timeout} seconds: {exc.cmd}",
            file=sys.stderr,
        )
        return 124
    except KeyboardInterrupt:
        print("bootstrap interrupted; transient metadata cleanup was attempted", file=sys.stderr)
        return 130
    finally:
        _remove_generated_pyboy_metadata(was_present=metadata_was_present)


if __name__ == "__main__":
    raise SystemExit(main())
