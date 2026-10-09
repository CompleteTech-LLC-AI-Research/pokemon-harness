"""Authenticated final-receipt and report controls; no cgroup or process operations."""

import hashlib
import hmac
import json
from types import SimpleNamespace

from scripts.production_gate_parallel_processes import read_guardian_receipt


def final_cgroup_receipt(tmp_path, **changes):
    path, secret = tmp_path / "guardian.json", b"k" * 32
    group = tmp_path / "allocated-group"
    body = {
        "phase": "final",
        "guardian_pid": 5000,
        "guardian_start": 111,
        "contained": True,
        "proof": True,
        "worker_pid": 6000,
        "remaining": [],
        "adopted": [],
        "cgroup": str(group),
        "cgroup_error": "",
    }
    body.update(changes)
    if body.get("cgroup_error") == "omit":
        body.pop("cgroup_error")
    body["mac"] = hmac.new(
        secret, json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()
    path.write_text(json.dumps(body))
    tree = SimpleNamespace(
        cgroup=SimpleNamespace(path=group),
        cgroup_joined=False,
        holder_mode=True,
        status=SimpleNamespace(guardian=(5000, 111)),
        _entries={},
        _parent_instance_valid=lambda pid: False,
    )
    guardian, problems = read_guardian_receipt(path, secret, 5000, 111)
    return tree, guardian, problems
