"""Independent identity measurement for concurrent runtime jobs (#107.1, #107.3).

The parent measures once to freeze the plan; every worker measures again before and
after its gate run; the parent measures a third time after the job.  Nothing here
copies an identity from a plan: each value is computed from the checkout, the
interpreter build and the real ROM/fixture bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from scripts.production_gate_parallel_measurement_budget import (
    IdentityDeadline,  # noqa: F401 - retained public facade for existing identity consumers
    MeasurementBudget,
    noop_checkpoint,
    pre_dispatch_budget,
)

CHUNK_BYTES = 1 << 20
PROBE_TIMEOUT_SECONDS = 120
BUILD_PROBE = (
    "import hashlib, importlib.util, json, os, sys\n"
    "files = {}\n"
    "origin = None\n"
    "spec = importlib.util.find_spec('pyboy')\n"
    "origin = spec.origin if spec else None\n"
    "SUFFIXES = ('.so', '.pyd', '.dylib', '.py', '.pyx', '.pxd', '.pxi')\n"
    "for base in (spec.submodule_search_locations or []) if spec else []:\n"
    "    for dirpath, dirnames, names in os.walk(base):\n"
    "        dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')\n"
    "        for name in sorted(names):\n"
    "            if name.endswith(SUFFIXES):\n"
    "                path = os.path.join(dirpath, name)\n"
    "                with open(path, 'rb') as handle:\n"
    "                    files[os.path.relpath(path, base)] = hashlib.sha256(handle.read()).hexdigest()\n"
    "print(json.dumps({'version': sys.version, 'origin': origin, 'prefix': sys.prefix, "
    "'files': files}, sort_keys=True))\n"
)
IDENTITY_KEYS = ("commit", "clean_head", "build_identity", "input_digests", "asset_digest")


def sha256_file(path: Path, *, checkpoint: Callable[[], None] = noop_checkpoint) -> str:
    checkpoint()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            checkpoint()
            chunk = handle.read(CHUNK_BYTES)
            checkpoint()
            if not chunk:
                break
            digest.update(chunk)
    checkpoint()
    return digest.hexdigest()


def tree_digest(
    root: Path,
    suffixes: Sequence[str] | None = None,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
) -> tuple[str, int]:
    """Digest every regular file (following links) under ``root`` by path, size and bytes.

    ``.git`` directories are repository metadata, not content, and are never digested.
    """

    entries: list[list[Any]] = []
    checkpoint()
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            checkpoint()
            dirnames[:] = sorted(name for name in dirnames if name != ".git")
            for name in sorted(filenames):
                checkpoint()
                if suffixes and not name.endswith(tuple(suffixes)):
                    continue
                path = Path(dirpath) / name
                if not path.is_file():
                    continue
                entries.append(
                    [
                        path.relative_to(root).as_posix(),
                        path.stat().st_size,
                        sha256_file(path, checkpoint=checkpoint),
                    ]
                )
    text = json.dumps(entries, separators=(",", ":"))
    checkpoint()
    return hashlib.sha256(text.encode("ascii")).hexdigest(), len(entries)


def measure_commit(
    project_root: Path,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
    budget: MeasurementBudget | None = None,
) -> tuple[str, bool, list[str]]:
    """Return (head, clean, problems) from the checkout itself."""

    budget = (budget or pre_dispatch_budget()).with_checkpoint(checkpoint)
    checkpoint = budget.checkpoint

    def git(*argv: str) -> subprocess.CompletedProcess[str]:
        checkpoint()
        result = budget.run_probe(
            ["git", "-C", str(project_root), *argv],
            environment=dict(os.environ),
            cwd=project_root.resolve(),
            max_seconds=60,
        )
        checkpoint()
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode("utf-8"),
            result.stderr.decode("utf-8"),
        )

    try:
        head, status = (
            git("rev-parse", "HEAD"),
            git("status", "--porcelain", "--untracked-files=all"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", False, [f"the checkout commit could not be read: {type(exc).__name__}"]
    if head.returncode != 0 or status.returncode != 0:
        return "", False, ["the checkout commit could not be read"]
    clean = not status.stdout.strip()
    problems = [] if clean else ["the checkout is not clean: an exact clean commit is required"]
    return head.stdout.strip(), clean, problems


def _relative_origin(origin: object, project_root: Path) -> str | None:
    if not isinstance(origin, str):
        return None
    try:
        return Path(origin).resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return origin


def measure_build_identity(
    mode: str,
    interpreter: str,
    project_root: Path,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
    budget: MeasurementBudget | None = None,
) -> tuple[str, list[str]]:
    """Digest the interpreter binary plus the native extensions (cython) or vendored sources."""

    budget = (budget or pre_dispatch_budget()).with_checkpoint(checkpoint)
    checkpoint = budget.checkpoint
    try:
        interpreter_digest = sha256_file(Path(os.path.realpath(interpreter)), checkpoint=checkpoint)
    except OSError as exc:
        return "", [f"{mode}: interpreter is unreadable: {type(exc).__name__}"]
    environment = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    # Resolve pyboy exactly as the gate's pytest children do (see build_test_environment):
    # the harness source first, the vendored PyBoy first only in source mode.
    entries = [str(project_root / "src"), str(project_root)]
    if mode == "source":
        entries.insert(0, str((project_root / "vendor" / "pyboy-src").resolve(strict=False)))
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    try:
        checkpoint()
        probe = budget.run_probe(
            [interpreter, "-c", BUILD_PROBE],
            environment=environment,
            cwd=project_root.resolve(),
            max_seconds=PROBE_TIMEOUT_SECONDS,
        )
        checkpoint()
        measured = json.loads(probe.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return "", [f"{mode}: build probe failed: {type(exc).__name__}"]
    problems: list[str] = []
    record: dict[str, Any] = {
        "mode": mode,
        "interpreter_sha256": interpreter_digest,
        "version": measured.get("version"),
        "resolved_pyboy": measured.get("files")
        if isinstance(measured.get("files"), dict)
        else None,
        "pyboy_origin": _relative_origin(measured.get("origin"), project_root),
    }
    if probe.returncode != 0 or not isinstance(measured.get("files"), dict):
        problems.append(f"{mode}: build probe exited {probe.returncode}")
    elif mode == "cython":
        record["extensions"] = measured["files"]
        if not any(name.endswith((".so", ".pyd", ".dylib")) for name in measured["files"]):
            problems.append("cython: the interpreter exposes no native pyboy extension")
    else:
        digest, count = tree_digest(
            project_root / "vendor" / "pyboy-src" / "pyboy",
            (".py", ".pyx", ".pxd"),
            checkpoint=checkpoint,
        )
        record["vendored_sources"] = digest
        if count == 0:
            problems.append("source: the vendored pyboy sources are missing")
    text = json.dumps(record, sort_keys=True, separators=(",", ":"))
    checkpoint()
    return hashlib.sha256(text.encode("ascii")).hexdigest(), problems


def measure_inputs(
    project_root: Path,
    names: Sequence[str],
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
) -> tuple[dict[str, str], list[str]]:
    digests: dict[str, str] = {}
    problems: list[str] = []
    for name in names:
        checkpoint()
        path = project_root / name
        if name.startswith("tree:"):
            digest, count = tree_digest(project_root / name[5:], checkpoint=checkpoint)
            if count:
                digests[name] = digest
            else:
                problems.append(f"declared input tree is empty or missing: {name}")
        elif path.is_file():
            digests[name] = sha256_file(path, checkpoint=checkpoint)
        else:
            problems.append(f"declared input is missing: {name}")
    return digests, problems


def measure_assets(
    rom_root: Path,
    fixture_root: Path,
    *,
    checkpoint: Callable[[], None] = noop_checkpoint,
) -> tuple[str, list[str]]:
    """Digest every byte under the explicitly resolved ROM and fixture roots."""

    parts: dict[str, Any] = {}
    problems: list[str] = []
    for label, root in (("rom", rom_root), ("fixture", fixture_root)):
        checkpoint()
        digest, count = tree_digest(root, checkpoint=checkpoint)
        parts[label] = [digest, count]
        if count == 0:
            problems.append(f"no {label} files under the resolved {label} root")
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    checkpoint()
    return hashlib.sha256(text.encode("ascii")).hexdigest(), problems


def measure_identity(
    *,
    mode: str,
    interpreter: str,
    project_root: Path,
    input_names: Sequence[str],
    rom_root: Path,
    fixture_root: Path,
    git_root: Path | None = None,
    checkpoint: Callable[[], None] = noop_checkpoint,
    budget: MeasurementBudget | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Measure every plan-bound identity; returns the values and every measurement problem."""

    budget = (budget or pre_dispatch_budget()).with_checkpoint(checkpoint)
    checkpoint = budget.checkpoint
    checkpoint()
    head, clean, problems = measure_commit(
        git_root or project_root, checkpoint=checkpoint, budget=budget
    )
    build, build_problems = measure_build_identity(
        mode, interpreter, project_root, checkpoint=checkpoint, budget=budget
    )
    inputs, input_problems = measure_inputs(project_root, input_names, checkpoint=checkpoint)
    assets, asset_problems = measure_assets(rom_root, fixture_root, checkpoint=checkpoint)
    problems = [*problems, *build_problems, *input_problems, *asset_problems]
    values = {
        "commit": head,
        "clean_head": clean,
        "build_identity": build,
        "input_digests": inputs,
        "asset_digest": assets,
    }
    checkpoint()
    return values, problems


def expected_identity(plan_body: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """The frozen identity one runtime job must reproduce (build identity is per runtime)."""

    builds = plan_body["build_identity"]
    return {
        "commit": plan_body["commit"],
        "build_identity": builds.get(mode) if isinstance(builds, dict) else None,
        "input_digests": plan_body["input_digests"],
        "asset_digest": plan_body["asset_digest"],
    }


def identity_differences(measured: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    """Name every identity whose measured value differs from the frozen expectation."""

    problems = [
        f"{key} measured by the job differs from the frozen plan"
        for key in ("commit", "build_identity", "input_digests", "asset_digest")
        if measured.get(key) != expected.get(key)
    ]
    if measured.get("clean_head") is not True:
        problems.append("the job did not measure a clean checkout head")
    return problems


class Measurer(Protocol):
    def __call__(
        self,
        job_id: str,
        *,
        checkpoint: Callable[[], None] = noop_checkpoint,
        budget: MeasurementBudget | None = None,
    ) -> tuple[dict[str, Any], list[str]]: ...
