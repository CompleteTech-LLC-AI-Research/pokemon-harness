"""Write isolation and shared-input integrity for concurrent runtime jobs (#107.2, #107.4).

Prevent: the parent builds a private immutable snapshot (every tracked file plus byte
copies of the ROM and fixture roots) with all write permission bits removed, then PROBES
that the OS really refuses a write; if it does not, the run fails closed with that
observed evidence.  Every job runs from the snapshot.  Redirect: every writer the gate
controls (pytest cache, bytecode, temp, HOME, XDG, build dir) points into the job's
private tree.  Detect: a stat manifest of the snapshot before and after is a secondary
tripwire only; it is never presented as prevention.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PYTEST_CACHE_ENV = "POKERED_GATE_PYTEST_CACHE_DIR"
PORT_BASE_ENV = "POKERED_PARALLEL_PORT_BASE"
PORT_COUNT_ENV = "POKERED_PARALLEL_PORT_COUNT"
PORT_LOCK_ENV = "POKERED_PARALLEL_PORT_LOCK_DIR"
PORT_POOL_START = 30000
PORT_RANGE_SIZE = 500
SNAPSHOT_EXCLUDES = (".git",)
SNAPSHOT_MAX_BYTES = 4 << 30
WRITE_BITS = 0o222


class IsolationError(RuntimeError):
    """The private immutable snapshot could not be built or does not deny writes."""


def assign_port_ranges(
    job_ids: Sequence[str], start: int = PORT_POOL_START, size: int = PORT_RANGE_SIZE
) -> dict[str, tuple[int, int]]:
    """Disjoint loopback port ranges, one per job, below the kernel ephemeral range."""

    return {job: (start + index * size, size) for index, job in enumerate(sorted(job_ids))}


def port_range_problems(ranges: Mapping[str, tuple[int, int]]) -> list[str]:
    spans = sorted((base, base + count, job) for job, (base, count) in ranges.items())
    problems = [
        f"port ranges of {a[2]} and {b[2]} overlap"
        for a, b in itertools.pairwise(spans)
        if b[0] < a[1]
    ]
    return problems + [
        f"{job}: invalid port range"
        for job, (b, c) in ranges.items()
        if not (0 < b and c > 0 and b + c <= 65536)
    ]


def isolation_environment(
    tree: Mapping[str, Path], port_range: tuple[int, int] | None = None
) -> dict[str, str]:
    """Environment entries that move every controllable writer into the job's tree."""

    cache = tree["cache"]
    ports = (
        {
            PORT_BASE_ENV: str(port_range[0]),
            PORT_COUNT_ENV: str(port_range[1]),
            PORT_LOCK_ENV: str(tree["ports"] if "ports" in tree else tree["tmp"]),
        }
        if port_range
        else {}
    )
    return {
        **ports,
        "TMPDIR": str(tree["tmp"]),
        "XDG_CACHE_HOME": str(cache),
        "HOME": str(tree["root"]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(cache / "pycache"),
        PYTEST_CACHE_ENV: str(cache / "pytest"),
        "POKERED_PARALLEL_JOB_ROOT": str(tree["root"]),
    }


def distinct_tree_problems(trees: Mapping[str, Mapping[str, Path]]) -> list[str]:
    """Every writable directory must be unique and never nested inside another job's tree."""

    roots = {job: Path(tree["root"]).resolve() for job, tree in trees.items()}
    problems: list[str] = []
    for job, root in roots.items():
        for other, other_root in roots.items():
            if job < other and (
                root == other_root
                or root.is_relative_to(other_root)
                or other_root.is_relative_to(root)
            ):
                problems.append(f"{job} and {other} share or nest writable trees")
    return problems


def snapshot_tree(
    root: Path, exclude: Sequence[str] = SNAPSHOT_EXCLUDES
) -> dict[str, tuple[int, int]]:
    """Stat manifest (size, mtime_ns) of every file under ``root`` except ``exclude`` names."""

    manifest: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in exclude)
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            manifest[os.path.relpath(path, root)] = (info.st_size, info.st_mtime_ns)
    return manifest


def diff_snapshots(
    before: Mapping[str, tuple[int, int]], after: Mapping[str, tuple[int, int]]
) -> list[str]:
    changed = sorted(key for key in before.keys() & after.keys() if before[key] != after[key])
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    problems: list[str] = []
    for label, names in (("changed", changed), ("added", added), ("removed", removed)):
        if names:
            problems.append(f"shared input {label} during the run ({len(names)}): {names[:5]}")
    return problems


def _copy_file(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    shutil.copymode(source, target)
    return source.stat().st_size


def _copy_tree(source: Path, target: Path, budget: list[int]) -> int:
    count = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for name in sorted(filenames):
            origin = Path(dirpath) / name
            if not origin.is_file():
                continue
            budget[0] -= _copy_file(origin, target / origin.relative_to(source))
            count += 1
            if budget[0] < 0:
                raise IsolationError("the private snapshot exceeds its byte ceiling")
    return count


def _git_environment() -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
    environment.update(
        {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    )
    return environment


def _init_repository(checkout: Path) -> None:
    """Give the snapshot a real, independent git repository (index and one commit).

    Required unit tests run ``git ls-files`` against the checkout root; a snapshot outside any
    repository would fail them.  The repository is built from the copied tracked files only, uses
    no global configuration, hooks or signing, and is frozen with the rest of the snapshot.
    """

    for argv in (
        ("init", "-q"),
        ("add", "-f", "-A"),
        (
            "-c",
            "user.name=snapshot",
            "-c",
            "user.email=snapshot@invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--no-verify",
            "--allow-empty",
            "-m",
            "immutable snapshot",
        ),
    ):
        done = subprocess.run(
            ["git", "-C", str(checkout), *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=_git_environment(),
        )
        if done.returncode != 0:
            raise IsolationError(
                f"snapshot repository setup failed at git {argv[0]}: {done.stderr.strip()[:200]}"
            )


def freeze_tree(root: Path) -> None:
    """Remove every write permission bit from the snapshot's own copies (files, then dirs)."""

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                os.chmod(path, path.stat().st_mode & ~WRITE_BITS & 0o7777)
        for name in dirnames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                os.chmod(path, path.stat().st_mode & ~WRITE_BITS & 0o7777)
    os.chmod(root, root.stat().st_mode & ~WRITE_BITS & 0o7777)


def thaw_and_remove(root: Path) -> None:
    """Restore owner write bits on this module's own copies only, then delete them."""

    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o700)
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                os.chmod(path, 0o600)
    shutil.rmtree(root, ignore_errors=True)


def write_denied_problems(root: Path) -> list[str]:
    """Probe for real: a write or create inside the snapshot must be refused by the OS."""

    problems: list[str] = []
    probe_file = next(
        (p for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()), None
    )
    if probe_file is None:
        return ["the snapshot contains no file to probe"]
    try:
        descriptor = os.open(probe_file, os.O_WRONLY)
    except PermissionError:
        pass
    except OSError as exc:
        problems.append(f"snapshot write probe was inconclusive: {type(exc).__name__}")
    else:
        os.close(descriptor)
        problems.append(
            "isolation capability not established: a snapshot file opened for writing "
            "(permission bits do not bind this process, for example it runs as root)"
        )
    probe_dir = root / ".write-probe"
    try:
        probe_dir.mkdir()
    except PermissionError:
        pass
    except OSError as exc:
        problems.append(f"snapshot create probe was inconclusive: {type(exc).__name__}")
    else:
        probe_dir.rmdir()
        problems.append("isolation capability not established: a directory could be created")
    return problems


def build_snapshot(
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    destination: Path,
    *,
    max_bytes: int = SNAPSHOT_MAX_BYTES,
) -> dict[str, Any]:
    """Create the private immutable snapshot and prove it denies writes, or raise."""

    if destination.exists():
        raise IsolationError("snapshot destination already exists")
    destination.mkdir(mode=0o700, parents=True)
    checkout = destination / "checkout"
    listing = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "-z", "--cached"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if listing.returncode != 0:
        raise IsolationError("git could not list the tracked files")
    budget = [max_bytes]
    files = 0
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", "surrogateescape")
        source = project_root / relative
        if source.is_symlink():
            link = os.readlink(source)
            if os.path.isabs(link) or ".." in Path(link).parts:
                raise IsolationError(f"tracked link escapes the checkout: {relative}")
            (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link, checkout / relative)
        elif source.is_dir():
            files += _copy_tree(source, checkout / relative, budget)
        elif source.is_file():
            budget[0] -= _copy_file(source, checkout / relative)
            files += 1
        else:
            raise IsolationError(f"tracked path is missing from the working tree: {relative}")
        if budget[0] < 0:
            raise IsolationError("the private snapshot exceeds its byte ceiling")
    if checkout.is_dir():
        _init_repository(checkout)
    assets = destination / "assets"
    files += _copy_tree(rom_root, assets / "rom", budget)
    files += _copy_tree(fixture_root, assets / "fixtures", budget)
    freeze_tree(destination)
    problems = write_denied_problems(destination)
    if problems:
        thaw_and_remove(destination)
        raise IsolationError("; ".join(problems))
    return {
        "root": str(destination),
        "checkout": str(checkout),
        "rom_root": str(assets / "rom"),
        "fixture_root": str(assets / "fixtures"),
        "files": files,
        "bytes": max_bytes - budget[0],
        "write_probe": "denied",
    }
