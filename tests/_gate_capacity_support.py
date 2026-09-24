"""Shared fixtures and helpers for the split gate-capacity tests.

Split from ``tests/test_gate_capacity.py`` for issue #122 with no behavior
change: the ROM-free helpers below are copied verbatim, and each split test
module imports only the helpers it uses while keeping every original test ID.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from scripts import gate_capacity

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gate_capacity_production_gate", ROOT / "scripts" / "production_gate.py"
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def make_facts(**overrides):
    """A complete, valid fact mapping with deterministic defaults."""

    data = {
        "platform": "Linux test",
        "logical_cpus": 8,
        "affinity_cpus": [0, 1, 2, 3],
        "affinity_count": 4,
        "affinity_supported": True,
        "cgroup_version": "v2",
        "cpu_quota_cores": 4.0,
        "cpu_weight": 100,
        "cpu_throttled": {"nr_throttled": 0},
        "cpu_quota_status": "limited",
        "cgroup_cpu_some_avg300": 1.0,
        "cgroup_visibility": ["visible hierarchy only"],
        "memory_total_bytes": 16 * 1024**3,
        "memory_available_bytes": 8 * 1024**3,
        "load_average": [0.1, 0.2, 0.3],
        "psi_cpu_some_avg300": 1.0,
        "repo_disk_free_bytes": 100 * 1024**3,
        "temp_disk_free_bytes": 100 * 1024**3,
        "shm_path": "/dev/shm",
        "shm_size_bytes": 1024**3,
        "shm_writable": True,
        "unsupported": [],
    }
    data.update(overrides)
    return data


def make_policy(**overrides):
    values = {
        "policy_version": 2,
        "runner_id": "runner-86",
        "effective_cpus": 4,
        "max_concurrent_pairs": 2,
        "memory_bytes_min": 1024,
        "disk_free_bytes_min": 1024,
        "observation_seconds": 0.0,
        "admission_deadline_seconds": 10.0,
        "max_system_some_avg300": 20.0,
        "max_cgroup_some_avg300": 20.0,
        "max_load_per_cpu": 1.0,
        "measurement_sha256": "a" * 64,
    }
    values.update(overrides)
    return gate_capacity.CapacityPolicy(**values)


def write_tier_config(
    root: Path,
    *,
    markers: dict[str, tuple[str, ...]] | None = None,
    tier_rules: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
) -> None:
    """Write a minimal, self-contained ``tests/_tier_config.py``.

    ``markers`` maps a test key (``"module.py::test_name"`` or just a module
    name) to its markers; ``tier_rules`` maps a tier to its
    ``(required, excluded)`` marker sets.  The generated module exposes the
    same ``classify_test``/``tier_matches`` contract the gate loads.
    """

    markers = markers or {"test_rom_boot.py": ("real_rom",)}
    tier_rules = tier_rules or {"local": (("real_rom",), ())}
    config = root / "tests" / "_tier_config.py"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "from __future__ import annotations\n"
        f"_MARKERS = {markers!r}\n"
        f"_RULES = {tier_rules!r}\n"
        "def classify_test(path, test_name):\n"
        "    import os\n"
        "    filename = os.path.basename(str(path))\n"
        "    key = filename + '::' + str(test_name)\n"
        "    marks = _MARKERS.get(key) or _MARKERS.get(filename)\n"
        "    if marks is None:\n"
        "        raise ValueError('test module is not classified')\n"
        "    return frozenset(marks)\n"
        "def tier_matches(name, markers):\n"
        "    rule = _RULES.get(name)\n"
        "    if rule is None:\n"
        "        return False\n"
        "    required, excluded = rule\n"
        "    marks = set(markers)\n"
        "    return all(item in marks for item in required) and not any(\n"
        "        item in marks for item in excluded\n"
        "    )\n",
        encoding="utf-8",
    )


class SequenceSampler:
    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def __call__(self):
        sample = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1
        return sample


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _matrix_report(nodeid, outcome="passed", reason=""):
    counts = {
        "total": 1,
        "passed": int(outcome == "passed"),
        "failed": int(outcome == "failed"),
        "skipped": int(outcome == "skipped"),
        "xfailed": 0,
        "xpassed": 0,
        "errors": 0,
    }
    return {
        "collection_only": False,
        "counts": counts,
        "tests": [
            {
                "nodeid": nodeid,
                "outcome": outcome,
                "when": "call",
                "reason": reason,
                "was_xfail": False,
            }
        ],
        "collection_errors": [],
        "collection_skips": [],
        "collected": 1,
        "nodeids": [nodeid],
        "exitstatus": int(outcome == "failed"),
    }


class FakeMatrixPopen:
    """Deterministic matrix child; node IDs ending in ``fail`` fail."""

    def __init__(self, command, *, env, **kwargs):
        del kwargs
        self.pid = 999999999
        nodeid = command[3]
        self.outcome = "failed" if nodeid.endswith("fail]") else "passed"
        self.returncode = int(self.outcome == "failed")
        reason = "runtime-identity-marker" if self.outcome == "failed" else ""
        report = _matrix_report(nodeid, self.outcome, reason=reason)
        Path(env["POKERED_GATE_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
        Path(env["POKERED_GATE_PROGRESS_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
        self.stdout = io.StringIO(f"MATRIX-OUTPUT {nodeid}\n")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _runtime_result(mode, tier_status="PASS"):
    return gate.RuntimeGateResult(
        mode=mode,
        runtime={"pyboy_mode": mode},
        collections=[gate.CollectionResult("python-module", [], "PASS", 0)],
        fixture_manifest={"status": "PASS", "mode": "schema"},
        matrix_audit={"status": "PASS"},
        tiers=[
            gate.TierResult(
                name="unit",
                description="unit",
                expression="unit",
                required=True,
                status=tier_status,
                counts=gate.Counts(total=1, passed=int(tier_status == "PASS")),
            )
        ],
    )


@pytest.fixture
def runtime_gate_stubs(tmp_path, monkeypatch):
    calls = []

    def environment(*_args, runtime_mode):
        return {"TEST_MODE": runtime_mode}

    def probe(_python, _root, env):
        return {"pyboy_mode": env["TEST_MODE"]}

    def tier(**kwargs):
        calls.append(kwargs)
        return gate.TierResult(
            name=kwargs["name"],
            description=kwargs["name"],
            expression=kwargs["name"],
            required=True,
            status="PASS",
            counts=gate.Counts(total=1, passed=1),
        )

    monkeypatch.setattr(gate, "build_test_environment", environment)
    monkeypatch.setattr(gate, "probe_runtime", probe)
    monkeypatch.setattr(gate, "runtime_problems", lambda _root, _runtime, **_kwargs: [])
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **_kwargs: [])
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_kwargs: [
            gate.CollectionResult("python-module", [], "PASS", 0, nodeids=()),
            gate.CollectionResult("pytest-console", [], "PASS", 0, nodeids=()),
        ],
    )
    monkeypatch.setattr(
        gate,
        "run_fixture_manifest_validation",
        lambda **_kwargs: {"status": "PASS", "mode": "byte"},
    )
    monkeypatch.setattr(gate, "fixture_manifest_input_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(gate, "fixture_manifest_provenance_problems", lambda *_a: [])
    monkeypatch.setattr(
        gate,
        "run_matrix_collection_audit",
        lambda **_kwargs: {
            "status": "PASS",
            "structural_pass": True,
            "acceptance_matrix_complete": True,
            "audited_nodeids": {},
            "groups": {},
        },
    )
    monkeypatch.setattr(gate, "run_tier", tier)
    return calls


def _standalone_environment():
    """Environment where ``scripts`` is not importable as a package."""

    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "vendor" / "pyboy-src"), str(ROOT / "src")]
    )
    environment["PYBOY_NO_CYTHON"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("POKERED_SKIP_SHA1", None)
    return environment


def _line_of(function, prefix):
    """Return the absolute line number of the first line starting with prefix."""

    source, start = inspect.getsourcelines(inspect.unwrap(function))
    return start + next(index for index, line in enumerate(source) if line.startswith(prefix))


@contextlib.contextmanager
def _real_sigint_at_line(function, prefix):
    """Deliver one real SIGINT when ``function`` reaches ``prefix``."""

    target = inspect.unwrap(function)
    boundary = _line_of(target, prefix)
    signalled: list[bool] = []

    def trace(frame, event, arg):
        if (
            not signalled
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == boundary
        ):
            signalled.append(True)
            os.kill(os.getpid(), signal.SIGINT)
        return trace

    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        yield signalled
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous_handler)


def _matrix_row_prepare(rows):
    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"], "pyboy_revision": "runtime-identity-marker"},
                collections=[
                    gate.CollectionResult(
                        "python-module",
                        [],
                        "PASS",
                        0,
                        nodeids=tuple(nodeid for values in rows.values() for nodeid in values),
                    )
                ],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )

    return prepare
