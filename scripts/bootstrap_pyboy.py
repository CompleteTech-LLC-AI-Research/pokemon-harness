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
import importlib
import importlib.machinery
import importlib.metadata
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYBOY_SOURCE = ROOT / "vendor" / "pyboy-src"
REVISION_FILE = PYBOY_SOURCE / "POKERED_HARNESS_PYBOY_REVISION"
EXPECTED_PYBOY_VERSION = "2.7.0"
EXPECTED_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"
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

    expected_modules = (
        {name: "cython" for name in CYTHON_MODULES}
        if mode == "cython"
        else {name: "source" for name in RUNTIME_MODULES}
    )
    problems: list[str] = []
    if getattr(pyboy, "__version__", None) != EXPECTED_PYBOY_VERSION:
        problems.append(
            f"version={getattr(pyboy, '__version__', None)!r}, expected {EXPECTED_PYBOY_VERSION!r}"
        )
    if getattr(pyboy, "__pokered_harness_revision__", None) != EXPECTED_REVISION:
        problems.append(
            "revision="
            f"{getattr(pyboy, '__pokered_harness_revision__', None)!r}, expected {EXPECTED_REVISION!r}"
        )

    for name, expected_kind in expected_modules.items():
        actual_kind = _module_kind(modules[name])
        if actual_kind != expected_kind:
            problems.append(f"{name} is {actual_kind}, expected {expected_kind}")

    allowed_roots = _runtime_roots()
    for name, module in modules.items():
        filename = str(getattr(module, "__file__", "") or "")
        module_path = Path(filename).resolve() if filename else None
        if module_path is None or not any(
            _path_is_within(module_path, root) for root in allowed_roots
        ):
            problems.append(
                f"{name} loaded outside the pinned runtime roots: {filename or '<none>'}"
            )

    owners = _package_distributions("pyboy")
    if PROJECT_DISTRIBUTION not in owners:
        problems.append("pyboy is not provided by the installed pokered-harness distribution")
    if mode == "source":
        unexpected = owners - {PROJECT_DISTRIBUTION}
        if unexpected:
            problems.append(
                "pyboy has competing installed owners: " + ", ".join(sorted(unexpected))
            )
    else:
        # Cython mode intentionally installs the checked-in PyBoy project as
        # an extension-backed distribution. Its revision marker remains
        # authoritative, so an unmodified stock package cannot pass below.
        unexpected = owners - {PROJECT_DISTRIBUTION, "pyboy"}
        if unexpected:
            problems.append("pyboy has foreign installed owners: " + ", ".join(sorted(unexpected)))

    if bool(getattr(utils, "cython_compiled", False)) != (mode == "cython"):
        problems.append(
            f"cython_compiled={getattr(utils, 'cython_compiled', None)!r}, "
            f"expected {mode == 'cython'!r}"
        )

    try:
        serial = _new_serial_instance()
    except Exception as exc:  # noqa: BLE001 - an incompatible ABI must fail closed
        # A separately installed stock PyBoy may import successfully while
        # exposing an incompatible constructor or ABI. Report it alongside
        # the ownership/module-kind violations instead of leaking a traceback.
        problems.append(f"serial contract could not be constructed: {type(exc).__name__}: {exc}")
    else:
        missing = [
            name
            for name in ("backend", "apply_external_edge", "peek_out_bit")
            if not hasattr(serial, name)
        ]
        if missing:
            problems.append(f"serial contract missing {', '.join(missing)}")

    if problems:
        raise SystemExit(f"PyBoy runtime contract failed for --mode {mode}: " + "; ".join(problems))


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
    args = parser.parse_args(argv)

    _validate_source()
    if args.check:
        _verify_runtime(args.mode)
        return 0

    metadata_was_present = _metadata_was_present()

    env = _isolated_environment()
    if args.mode == "source":
        env["PYBOY_NO_CYTHON"] = "1"
        install_target = ROOT
    else:
        env.pop("PYBOY_NO_CYTHON", None)
        install_target = PYBOY_SOURCE

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
            timeout=INSTALL_TIMEOUT_SECONDS,
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
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
            if project_result.returncode:
                return project_result.returncode

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
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        if result.returncode:
            if args.mode == "cython":
                print(
                    "Cython runtime build failed for the pinned PyBoy source; "
                    "the supported production runtime remains --mode source.",
                    file=sys.stderr,
                )
            return result.returncode
        _verify_runtime(args.mode)
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
