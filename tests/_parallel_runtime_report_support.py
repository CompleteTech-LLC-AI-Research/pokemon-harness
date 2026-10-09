"""Shared authored builders for the concurrent runtime controls (#107).

Everything here is a fabricated fixture: it exercises validators and orchestration, and
is never gameplay, real-ROM or performance evidence.
"""

from __future__ import annotations

from typing import Any

from scripts.production_gate_parallel_identity import expected_identity
from scripts.production_gate_parallel_plan import ParallelPlan, build_parallel_plan
from scripts.production_gate_parallel_records import build_rows

TOKEN = "t" * 32
REQUIRED = {"unit": ["tests/test_a.py::test_one", "tests/test_a.py::test_two"], "timing": ["t::x"]}
BUILDS = {"source": "e" * 64, "cython": "f" * 64}


def make_plan(**overrides: Any) -> ParallelPlan:
    arguments: dict[str, Any] = {
        "commit": "a" * 40,
        "build_identity": BUILDS,
        "input_digests": {"VERSIONS.md": "b" * 64},
        "asset_digest": "c" * 64,
        "games": ("red",),
        "roles": ("listener", "connector"),
        "modes": ("source", "cython"),
        "tiers": ("unit", "timing"),
        "required_nodeids_by_tier": REQUIRED,
        "repetitions": 5,
        "deadline_seconds": 30.0,
        "total_pairs": 2,
        "policy_digest": "d" * 64,
        "early_smoke": False,
        "fail_fast": False,
    }
    arguments.update(overrides)
    return build_parallel_plan(**arguments)


def counts(total: int) -> dict[str, int]:
    return {
        "passed": total,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "errors": 0,
        "total": total,
    }


def gate_tiers(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Tier records shaped like the real gate report (selected_nodeids / returncodes)."""

    tiers = []
    for name in job["tiers"]:
        if name == "timing":
            tiers.append(
                {"name": name, "status": "PASS", "counts": counts(5), "returncodes": [0] * 5}
            )
            continue
        cases = REQUIRED.get(name) or [f"tests/test_{name}.py::test_case"]
        tiers.append(
            {
                "name": name,
                "status": "PASS",
                "counts": counts(len(cases)),
                "returncodes": [0],
                "selected_nodeids": list(cases),
            }
        )
    return tiers


def good_artifact(
    plan: ParallelPlan,
    job_id: str,
    *,
    pid: int = 4242,
    token: str = TOKEN,
    started: str = "2026-10-09T00:00:00+00:00",
    finished: str = "2026-10-09T00:00:01+00:00",
) -> dict[str, Any]:
    body, job = plan.as_dict(), plan.job(job_id)
    mode = job["mode"]
    identity = expected_identity(body, mode)
    raw = gate_tiers(job)
    return {
        "schema_version": 2,
        "run_id": body["run_id"],
        "plan_sha256": plan.digest,
        "job_id": job_id,
        "mode": mode,
        "commit": identity["commit"],
        "clean_head": True,
        "build_identity": identity["build_identity"],
        "input_digests": identity["input_digests"],
        "asset_digest": identity["asset_digest"],
        "identity_after": {
            "commit": identity["commit"],
            "clean_head": True,
            "build_identity": identity["build_identity"],
            "input_digests": identity["input_digests"],
            "asset_digest": identity["asset_digest"],
        },
        "launch_token": token,
        "evidence": {"gate_report_bytes": 10, "gate_report_sha256": "9" * 64},
        "pid": pid,
        "child_exit_status": 0,
        "started_utc": started,
        "finished_utc": finished,
        "fresh_run": True,
        "diagnostic_reuse": False,
        "runtime": {"pyboy_mode": mode},
        "tiers": [
            {k: tier[k] for k in ("name", "status", "counts", "returncodes")} | {"diagnostics": {}}
            for tier in raw
        ],
        "rows": build_rows(mode, raw, body["games"], body["roles"]),
        "gate_problems": [],
        "cancellation": "",
        "overall": "PASS",
    }
