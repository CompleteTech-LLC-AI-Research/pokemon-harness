"""Shared gate loader, report builders and fake runner for the gate tests (#129).

Split from ``tests/test_production_gate.py`` for #129 with no behavior
change. Every helper and constant below is copied verbatim from the
original module. ``gate`` is ``scripts/production_gate.py`` loaded directly
from source so the gate plumbing is exercised without a build step.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import ClassVar

_GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "production_gate.py"


def load_gate():
    """Return the one gate facade loaded from source for this process.

    ``scripts/production_gate.py`` is now a facade over nine support modules,
    and those modules resolve monkeypatched call targets through the entry-point
    module object registered as ``scripts.production_gate``.  Executing the
    entry script a second time would build a second facade whose attribute
    patches the shared support modules never observe, so every test module
    reuses the first instance loaded from this path.
    """
    cached = sys.modules.get("scripts.production_gate")
    if cached is not None and getattr(cached, "__file__", None) == str(_GATE_PATH):
        return cached
    spec = importlib.util.spec_from_file_location("pokered_production_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()


def _matrix_report(nodeid: str, *, outcome: str = "passed") -> dict:
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
                "reason": "fixture missing" if outcome == "skipped" else "",
                "was_xfail": False,
            }
        ],
        "collection_errors": [],
        "collection_skips": [],
        "collected": 1,
        "nodeids": [nodeid],
        "exitstatus": 0,
    }


def _runtime_result(
    mode: str,
    *,
    gate_problems: tuple[str, ...] = (),
    collection_status: str = "PASS",
    tier_status: str = "PASS",
) -> gate.RuntimeGateResult:
    return gate.RuntimeGateResult(
        mode=mode,
        runtime={"pyboy_mode": mode},
        collections=[
            gate.CollectionResult(
                name="python-module",
                command=["python", "-m", "pytest"],
                status=collection_status,
                returncode=0 if collection_status == "PASS" else 1,
            )
        ],
        fixture_manifest={"status": "PASS", "mode": "schema"},
        matrix_audit={"status": "PASS"},
        tiers=[
            gate.TierResult(
                name="unit",
                description="unit",
                expression="unit",
                required=True,
                status=tier_status,
                counts=gate.Counts(total=1, passed=1 if tier_status == "PASS" else 0),
            )
        ],
        gate_problems=list(gate_problems),
    )


class _FakeMatrixPopen:
    mode = "pass"
    commands: ClassVar[list] = []
    instances: ClassVar[list] = []
    creation_kwargs: ClassVar[list[dict[str, object]]] = []

    def __init__(self, command, *, env, **kwargs):
        self.command = command
        self.returncode = None if self.mode == "hang" else 0
        self.pid = 999999999
        self.stdout = io.StringIO("")
        self.commands.append((command, env))
        self.instances.append(self)
        self.creation_kwargs.append(dict(kwargs))
        nodeid = command[3]
        if self.mode != "hang":
            outcome = "passed" if self.mode == "pass" else self.mode
            payload = _matrix_report(nodeid, outcome=outcome)
            Path(env["POKERED_GATE_REPORT"]).write_text(json.dumps(payload), encoding="utf-8")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self):
        self.returncode = -9


_CANONICAL_BOOT_NODEIDS = frozenset(
    {
        "tests/test_rom_boot.py::test_canonical_rom_boot_state_roundtrip[red-color]",
        "tests/test_rom_boot.py::test_canonical_rom_boot_state_roundtrip[blue-color]",
        "tests/test_rom_boot.py::test_canonical_rom_boot_state_roundtrip[yellow]",
    }
)


def _patch_main_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _root: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _root: ({}, ""))
    return [
        "--repo-root",
        str(tmp_path),
        "--rom-root",
        str(tmp_path / "rom"),
        "--fixture-root",
        str(tmp_path / "fixtures"),
        "--python",
        str(tmp_path / "bin" / "python"),
        "--unit-only",
        "--format",
        "json",
    ]
