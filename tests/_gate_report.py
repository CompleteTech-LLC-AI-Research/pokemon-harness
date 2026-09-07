"""Small pytest plugin used by :mod:`scripts.production_gate`.

Pytest's terminal summary is intended for humans and is awkward to parse
reliably across pytest versions.  This plugin records one terminal outcome per
test, including setup skips and xfail/xpass metadata, as JSON supplied by the
gate through ``POKERED_GATE_REPORT``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def pytest_configure(config: Any) -> None:
    config._pokered_gate_records = {}
    config._pokered_gate_collection_errors = []


def pytest_runtest_logreport(report: Any) -> None:
    config = report.config if hasattr(report, "config") else None
    # TestReport does not expose config on all supported pytest versions; the
    # active plugin state is retained by the module-level mapping instead.
    del config

    if report.when == "setup" and report.outcome == "passed":
        return
    if report.when == "teardown" and report.outcome == "passed":
        return

    records = _records()
    nodeid = report.nodeid
    current = records.get(nodeid)

    # A setup skip/error is terminal for the test.  A teardown failure must
    # replace a prior passing call so cleanup failures are never hidden.
    if current is not None and report.when == "call" and current["outcome"] in {
        "skipped",
        "failed",
    }:
        return
    records[nodeid] = {
        "nodeid": nodeid,
        "outcome": report.outcome,
        "when": report.when,
        "was_xfail": bool(getattr(report, "wasxfail", False)),
        "reason": _reason(report),
    }


def pytest_collectreport(report: Any) -> None:
    if report.failed:
        _collection_errors().append(
            {
                "nodeid": getattr(report, "nodeid", "<collection>"),
                "reason": str(report.longrepr),
            }
        )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    target = os.environ.get("POKERED_GATE_REPORT")
    if not target:
        return

    records = list(_records().values())
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "errors": len(_collection_errors()),
    }
    for record in records:
        outcome = record["outcome"]
        if record["was_xfail"] and outcome == "skipped":
            counts["xfailed"] += 1
        elif record["was_xfail"] and outcome == "passed":
            counts["xpassed"] += 1
        elif outcome == "passed":
            counts["passed"] += 1
        elif outcome == "skipped":
            counts["skipped"] += 1
        elif outcome == "failed":
            counts["failed"] += 1
        else:
            counts["errors"] += 1

    payload = {
        "exitstatus": int(exitstatus),
        "counts": {**counts, "total": len(records)},
        "tests": records,
        "collection_errors": list(_collection_errors()),
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


# The plugin hooks receive reports, not the Config object.  Pytest invokes
# hooks on this module, so a short module-level store is sufficient and keeps
# the JSON protocol independent of private pytest internals.
_ACTIVE_RECORDS: dict[str, dict[str, Any]] = {}
_ACTIVE_COLLECTION_ERRORS: list[dict[str, str]] = []


def _records() -> dict[str, dict[str, Any]]:
    return _ACTIVE_RECORDS


def _collection_errors() -> list[dict[str, str]]:
    return _ACTIVE_COLLECTION_ERRORS


def _reason(report: Any) -> str:
    longrepr = getattr(report, "longrepr", None)
    if longrepr is None:
        return ""
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[2])
    return str(longrepr)


__all__ = ["pytest_collectreport", "pytest_configure", "pytest_runtest_logreport", "pytest_sessionfinish"]
