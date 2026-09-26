"""Shared helpers and constants for the qualification-runner tests (#112).

Split from ``tests/test_qualification_runner.py`` for issue #112 with no behavior
change: every helper and constant below is copied verbatim from the original
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from scripts import qualification_runner as runner

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_facts(**overrides):
    facts = runner.RunnerFacts(
        platform="Linux test",
        kernel="test",
        logical_cpus=16,
        affinity_cpus=list(range(8)),
        affinity_count=8,
        affinity_supported=True,
        cgroup_version="v2",
        cgroup_relative_path="/user.slice/session.scope",
        cgroup_dir="/sys/fs/cgroup/user.slice/session.scope",
        cgroup_member_pids=[os.getpid()],
        cgroup_descendants={"nr_descendants": 0, "nr_dying_descendants": 0},
        cgroup_sibling_competitors=[],
        cpu_quota_cores=8.0,
        cpu_weight=100,
        cpu_throttled={"nr_throttled": 0},
        memory_total_bytes=64 * 1024**3,
        memory_available_bytes=32 * 1024**3,
        load_average=[1.0, 1.0, 1.0],
        psi_cpu_some_avg300=1.0,
        repo_disk_free_bytes=10**12,
        temp_disk_free_bytes=10**12,
        shm_path="/dev/shm",
        shm_size_bytes=10**9,
        shm_available_bytes=10**9,
        shm_writable=True,
        process_pid=os.getpid(),
        process_ancestor_pids=[],
        process_tree_pids=[os.getpid()],
        process_start_time=runner._process_start_time(os.getpid()),
        foreign_process_affinity={},
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def make_declaration(**overrides):
    declaration = _make_declaration_base()
    declaration.update(overrides)
    return declaration


def _make_declaration_base():
    declaration = {
        "declaration_version": runner.SCHEMA_VERSION,
        "runner_id": "test-runner",
        "reservation_mechanism": "cgroup-quota",
        "reservation": {
            "allocation_id": "test-runner",
            "job_dir": "target/qualification-runs/test-runner",
            "descriptor_path": "target/qualification-runs/test-runner/allocation.json",
            "descriptor_sha256": "a" * 64,
            "host_lock_path": "target/qualification-runs/host.lock",
            "cgroup_path": "/user.slice/session.scope",
        },
        "logical_cpus": 4,
        "affinity_cpus": [0, 1, 2, 3],
        "cpu_quota_cores": 4.0,
        "cpu_weight": 100,
        "memory_bytes": 8 * 1024**3,
        "disk_free_bytes_min": 10**9,
        "shm_bytes_min": 10**8,
        "interpreters": {
            "source": sys.executable,
            "native": sys.executable,
            "native_build_inputs_sha256": "b" * 64,
            "native_fingerprint": NATIVE_FINGERPRINT,
        },
        "assets": {
            "rom_root": "/tmp/rom",
            "fixture_root": "/tmp/fixtures",
            "inputs": [
                {
                    "path": "rom/red/pokemon-red.gb",
                    "sha1": "ea9bcae617fdf159b045185467ae58b2e4a48b9a",
                }
            ],
        },
    }
    return declaration


def native_identity() -> dict:
    """The installed-runtime identity a genuine native build would report."""

    artifacts = {
        f"{name.replace('.', '/')}.cpython-311-x86_64-linux-gnu.so": "d" * 64
        for name in runner._EXTENSION_BACKED_MODULES
    }
    # The installed output set is wider than the named entry modules: a mixed
    # build can replace a compiled module this list never names.
    artifacts["core/cpu.cpython-311-x86_64-linux-gnu.so"] = "a" * 64
    return {
        "python": "3.11.9",
        "version": "2.7.0",
        "revision": "b94bf5dfb042c502ff4bc1bcd417599b02a9419b",
        "cython_compiled": True,
        "modules": {
            name: {
                "kind": "cython",
                "sha256": "d" * 64,
                "artifact": f"{name.replace('.', '/')}.cpython-311-x86_64-linux-gnu.so",
            }
            for name in runner._EXTENSION_BACKED_MODULES
        },
        "artifacts": artifacts,
    }


NATIVE_IDENTITY = native_identity()
NATIVE_FINGERPRINT = runner._native_build_fingerprint(NATIVE_IDENTITY)
_HELD_FDS: list[int] = []


def _lock_observation_supported() -> bool:
    import tempfile

    directory = tempfile.mkdtemp(prefix="qualification-lock-probe-")
    path = Path(directory) / "probe.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        holders = runner._flock_holder_pids(path)
        return holders is not None and os.getpid() in holders
    except OSError:
        return False
    finally:
        os.close(fd)
        os.unlink(path)
        os.rmdir(directory)


_LOCK_OBSERVATION_SUPPORTED = _lock_observation_supported()


def held_reservation(tmp_path: Path, mechanism: str = "cgroup-quota", **overrides):
    """Create a real, held allocation descriptor and matching host facts."""

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    suffix = uuid.uuid4().hex[:8]
    job_dir = tmp_path / f"job-{mechanism}-{suffix}"
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o700)
    host_lock = tmp_path / f"host-{mechanism}-{suffix}.lock"
    lock_fd = os.open(host_lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _HELD_FDS.append(lock_fd)
    identity = runner._lock_identity(host_lock)
    assert identity is not None
    runner._HELD_LEASE_FDS.setdefault((identity["device"], identity["inode"]), []).append(lock_fd)

    if mechanism == "cgroup-quota":
        affinity = [0, 1, 2, 3]
        quota = 4.0
    elif mechanism == "cpuset-affinity":
        affinity = [0, 1, 2, 3]
        quota = None
    else:
        affinity = list(range(16))
        quota = None

    exclusive_token = f"operator-token-{suffix}"
    marker = tmp_path / f"exclusive-{suffix}.marker"
    reservation = {
        "allocation_id": "test-runner",
        "job_dir": str(job_dir),
        "descriptor_path": str(job_dir / "allocation.json"),
        "host_lock_path": str(host_lock),
        "cgroup_path": "/user.slice/session.scope",
        "exclusive": mechanism == "dedicated-host",
    }
    descriptor = {
        "descriptor_version": runner._DESCRIPTOR_VERSION,
        "allocation_id": "test-runner",
        "runner_id": "test-runner",
        "mechanism": mechanism,
        "state": "held",
        "lease_id": "lease-123",
        "holder_pid": os.getpid(),
        "holder_start_time": runner._process_start_time(os.getpid()),
        "lock_path": str(host_lock),
        "lock_identity": identity,
        "cgroup_path": "/user.slice/session.scope",
        "cpuset": affinity,
        "cpu_quota_cores": quota,
        "cpu_weight": 100,
        "logical_cpus": 16,
        "exclusive": mechanism == "dedicated-host",
        "created_at": "2026-01-01T00:00:00Z",
    }
    if mechanism == "dedicated-host":
        marker.write_text(exclusive_token, encoding="utf-8")
        os.chmod(marker, 0o400)
        reservation["exclusive_marker_path"] = str(marker)
        reservation["exclusive_token"] = exclusive_token
        descriptor["exclusive_marker_path"] = str(marker)
        descriptor["exclusive_token"] = exclusive_token
    descriptor_path = job_dir / "allocation.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    os.chmod(descriptor_path, 0o400)
    reservation["descriptor_sha256"] = runner._sha256_of_file(descriptor_path)

    declaration = make_declaration(
        reservation_mechanism=mechanism,
        affinity_cpus=affinity,
        cpu_quota_cores=quota,
        reservation=reservation,
    )
    if mechanism == "cgroup-quota":
        facts = make_facts(
            logical_cpus=16,
            affinity_cpus=list(range(8)),
            affinity_count=8,
            cpu_quota_cores=quota,
            cgroup_relative_path="/user.slice/session.scope",
            cgroup_member_pids=[os.getpid()],
            process_tree_pids=[os.getpid()],
        )
    elif mechanism == "cpuset-affinity":
        facts = make_facts(
            logical_cpus=16,
            affinity_cpus=affinity,
            affinity_count=len(affinity),
            cpu_quota_cores=None,
        )
    else:
        facts = make_facts(
            logical_cpus=16,
            affinity_cpus=list(range(16)),
            affinity_count=16,
            cpu_quota_cores=None,
            cpu_weight=100,
        )
    declaration.update(overrides)
    return declaration, facts


def statuses(results):
    return {item.name: item.status for item in results}


def write_input(root: Path, relative: str, data: bytes = b"rom-bytes") -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative, "sha1": hashlib.sha1(data).hexdigest()}


def required_entries(entries: list[tuple[str, str, str, bytes]]) -> dict:
    requirements = {}
    for root, key, sha1, _data in entries:
        requirements[(root, key)] = runner._AssetRequirement(key=key, root=root, sha1=sha1)
    return requirements


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _probe_payload(fingerprint: str, identity: dict | None = None) -> dict:
    return {
        "identity": dict(identity if identity is not None else NATIVE_IDENTITY),
        "fingerprint": fingerprint,
    }


def _prerequisite_runner(probe_payload: dict):
    calls = []

    def fake_runner(command, cwd):
        calls.append((command, cwd))
        if "-c" in command:
            return Completed(0, json.dumps(probe_payload))
        return Completed(0, "ok")

    return fake_runner, calls


def with_native_evidence(
    declaration: dict,
    tmp_path: Path,
    build_inputs: str = "b" * 64,
    fingerprint: str = NATIVE_FINGERPRINT,
    procedure: str = runner._NATIVE_BUILD_EVIDENCE_PROCEDURE,
    status: str = "complete",
    evidence_version: int = runner._NATIVE_BUILD_EVIDENCE_VERSION,
    mode: str = "cython",
    identity: dict | None = None,
    producer: dict | None = None,
) -> dict:
    """Write retained evidence shaped like the real bootstrap's own record."""

    if identity is None:
        identity = dict(NATIVE_IDENTITY)
    if producer is None:
        producer = {
            "script": "scripts/bootstrap_pyboy.py",
            "script_sha256": hashlib.sha256(
                (REPO_ROOT / "scripts" / "bootstrap_pyboy.py").read_bytes()
            ).hexdigest(),
        }
    document = {
        "evidence_version": evidence_version,
        "procedure": procedure,
        "mode": mode,
        "status": status,
        "build_inputs_sha256": build_inputs,
        "installed_fingerprint": fingerprint,
        "runtime_identity": identity,
        "producer": producer,
        "completed_at": "2026-01-01T00:00:00Z",
    }
    path = tmp_path / "native-build-evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    declaration.setdefault("interpreters", {})["native_build_evidence"] = str(path)
    declaration["interpreters"]["native_build_evidence_sha256"] = runner._sha256_of_file(path)
    return declaration


def _prepare_assets(tmp_path: Path) -> tuple[Path, Path, dict]:
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir(parents=True, exist_ok=True)
    data = b"rom-bytes"
    entry = write_input(rom_root, "red/pokemon-red.gb", data)
    requirements = required_entries(
        [("rom_root", "red/pokemon-red.gb", hashlib.sha1(data).hexdigest(), data)]
    )
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )
    return requirements, declaration


_FAKE_PYBOY_SOURCES = {
    "__init__.py": '__version__ = "2.7.0"\n__pokered_harness_revision__ = "' + "c" * 40 + '"\n',
    "pyboy.py": "from pyboy import utils  # noqa: F401\n",
    "utils.py": "cython_compiled = True\n",
    "link.py": "LINK = True\n",
    "core/__init__.py": "",
    "core/mb.py": "MB = True\n",
    "core/serial.py": "SERIAL = True\n",
    "core/cpu.py": "CPU = True\n",
}


def _write_fake_pyboy(root: Path) -> Path:
    """Create a stand-in installed ``pyboy`` package for the native probe."""

    package = root / "pyboy"
    for relative, content in _FAKE_PYBOY_SOURCES.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return package


def _run_native_probe(fake_root: Path, cwd: Path) -> dict:
    """Execute the real probe script against the stand-in installed runtime."""

    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    probe = runner._NATIVE_PROBE % {"modules": runner._RUNTIME_MODULES}
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


class _StubProcess:
    """The minimum of ``Popen`` that durable job ownership needs to record."""

    def __init__(self, pid: int):
        self.pid = pid


def _stale_job_dir(tmp_path: Path, record: dict | None) -> tuple[dict, Path, Path]:
    """Build a stale lease whose holder is gone, optionally with a job record."""

    job_dir = tmp_path / "stale-job"
    job_dir.mkdir()
    os.chmod(job_dir, 0o700)
    lock_path = job_dir / "allocation.lock"
    lock_path.write_text("", encoding="utf-8")
    descriptor_path = job_dir / "allocation.json"
    descriptor_path.write_text(
        json.dumps(
            {
                "holder_pid": 2**31 - 1,
                "holder_start_time": "0",
                "lock_path": str(lock_path),
                "lock_identity": runner._lock_identity(lock_path),
            }
        ),
        encoding="utf-8",
    )
    if record is not None:
        (job_dir / runner._JOB_RUN_RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    declaration = make_declaration(
        reservation={
            "allocation_id": "test-runner",
            "job_dir": str(job_dir),
            "descriptor_path": str(descriptor_path),
            "descriptor_sha256": runner._sha256_of_file(descriptor_path),
        }
    )
    return declaration, descriptor_path, job_dir


def _job_record(**overrides) -> dict:
    record = {
        "record_version": 1,
        "job_pid": 2**31 - 1,
        "job_start_time": "0",
        "job_process_group": 2**31 - 1,
        "job_session": 2**31 - 1,
        "containment_confirmed": True,
        "recorded_at": "2026-01-01T00:00:00Z",
    }
    record.update(overrides)
    return record


def rewrite_descriptor(declaration: dict, changes: dict) -> None:
    path = Path(declaration["reservation"]["descriptor_path"])
    os.chmod(path, 0o600)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, 0o400)
    declaration["reservation"]["descriptor_sha256"] = runner._sha256_of_file(path)


def _write_grandchild_command(tmp_path: Path, exit_code: int, pidfile: Path) -> list[str]:
    """Build a command that forks a sleeping grandchild, waits for it, then exits.

    The grandchild redirects its own stdio and survives its parent, which is the
    exact shape of the round-5 finding: ``run_command`` returns the parent's
    status while a descendant is still consuming the allocation.
    """

    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    if pidfile.exists():
        pidfile.unlink()
    code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "with open(os.devnull, 'wb') as null:\n"
        f"    subprocess.Popen([sys.executable, {str(grandchild)!r}], "
        "stdin=null, stdout=null, stderr=null)\n"
        f"deadline = time.monotonic() + 20\n"
        f"while not Path({str(pidfile)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, "-c", code]


def _wait_for_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and runner._pid_alive(pid):
        time.sleep(0.05)
    return not runner._pid_alive(pid)


def _write_detached_grandchild_command(
    tmp_path: Path, exit_code: int, pidfile: Path, *, parent_sleeps: bool = False
) -> list[str]:
    """Build a command whose grandchild detaches into its own session.

    ``start_new_session=True`` moves the grandchild out of the command's process
    group, which is the round-6 finding: the process-group sweep cannot see it,
    so it survives even though the command's group is empty.

    ``parent_sleeps`` keeps the parent alive after the detached grandchild
    starts, so the command itself hits its deadline instead of exiting first.
    """

    grandchild = tmp_path / "detached_grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    if pidfile.exists():
        pidfile.unlink()
    tail = "time.sleep(120)\n" if parent_sleeps else ""
    code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "with open(os.devnull, 'wb') as null:\n"
        f"    subprocess.Popen([sys.executable, {str(grandchild)!r}], "
        "stdin=null, stdout=null, stderr=null, start_new_session=True)\n"
        "deadline = time.monotonic() + 20\n"
        f"while not Path({str(pidfile)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        f"{tail}"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, "-c", code]


def _recreate_lock_at_same_path(declaration: dict) -> Path:
    """Replace the recorded lock pathname with a fresh, uncontended inode.

    This reproduces the round-4 reproduction: the original inode is still held,
    but the pathname now names a different inode that no allocation contends
    for. Anything that keys mutual exclusion on the path alone is defeated.
    """

    lock_path = Path(declaration["reservation"]["host_lock_path"])
    original_inode = lock_path.stat().st_ino
    # Build the replacement under a distinct name so the filesystem cannot
    # hand back the same inode number for the recreated pathname.
    replacement = lock_path.with_suffix(lock_path.suffix + ".new")
    fd = os.open(replacement, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.replace(replacement, lock_path)
    assert lock_path.stat().st_ino != original_inode
    return lock_path
