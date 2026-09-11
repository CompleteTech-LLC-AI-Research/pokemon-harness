"""Small pytest plugin used by :mod:`scripts.production_gate`.

Pytest's terminal summary is intended for humans and is awkward to parse
reliably across pytest versions.  This plugin records one terminal outcome per
test, including setup skips and xfail/xpass metadata, as JSON supplied by the
gate through ``POKERED_GATE_REPORT``.  A bounded periodic checkpoint is used
for the live progress report so a large suite does not repeatedly serialize its
entire history on the test runner's critical path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_VALID_OUTCOMES = frozenset({"passed", "failed", "skipped"})
_PROGRESS_EVERY = 32
_PROGRESS_SECONDS = 2.0


def pytest_configure(config: Any) -> None:
    # pytest.main() can be invoked more than once in one interpreter by
    # callers embedding the gate.  Reset the module-level state explicitly;
    # otherwise a later run could inherit records or collection failures from
    # an earlier run and produce a plausible but incorrect report.
    _ACTIVE_RECORDS.clear()
    _ACTIVE_COLLECTION_ERRORS.clear()
    _ACTIVE_COLLECTION_SKIPS.clear()
    _ACTIVE_COLLECTED_NODEIDS.clear()
    global _LAST_PROGRESS_COUNT, _LAST_PROGRESS_WRITE
    _LAST_PROGRESS_COUNT = 0
    _LAST_PROGRESS_WRITE = time.monotonic()
    config._pokered_gate_records = {}
    config._pokered_gate_collection_errors = []
    config._pokered_gate_collection_skips = []


def pytest_collection_finish(session: Any) -> None:
    """Capture the post-selection item set used by this pytest process."""

    _ACTIVE_COLLECTED_NODEIDS[:] = [item.nodeid for item in session.items]
    _write_report(
        session,
        target_name="POKERED_GATE_PROGRESS_REPORT",
        exitstatus=-1,
    )


def pytest_runtest_logreport(report: Any) -> None:
    if report.when == "setup" and report.outcome == "passed":
        return
    if report.when == "teardown" and report.outcome == "passed":
        return

    records = _records()
    nodeid = report.nodeid
    current = records.get(nodeid)
    outcome = str(report.outcome)
    if outcome not in _VALID_OUTCOMES:
        # Keep an unexpected pytest outcome visible to the gate as an error,
        # rather than allowing an unknown value to disappear from accounting.
        outcome = "error"

    # A setup skip/error is terminal for the test.  A teardown failure must
    # replace a prior passing call so cleanup failures are never hidden.  Once
    # a failure/error is recorded, a later teardown skip/pass must not turn it
    # into a green or skipped result.
    if current is not None:
        if current["outcome"] in {"failed", "error"}:
            if outcome in {"failed", "error"} and report.when != current["when"]:
                current["reason"] = _combine_reasons(current["reason"], _reason(report))
            return
        if report.when == "call" and current["when"] == "setup":
            return
    records[nodeid] = {
        "nodeid": nodeid,
        "outcome": outcome,
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
    elif getattr(report, "skipped", False):
        _collection_skips().append(
            {
                "nodeid": getattr(report, "nodeid", "<collection>"),
                "reason": _reason(report),
            }
        )


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    """Persist terminal test progress so a killed worker remains auditable."""

    global _LAST_PROGRESS_COUNT, _LAST_PROGRESS_WRITE
    del nodeid, location
    target = os.environ.get("POKERED_GATE_PROGRESS_REPORT")
    if not target:
        return
    now = time.monotonic()
    count = len(_records())
    if (
        count - _LAST_PROGRESS_COUNT < _PROGRESS_EVERY
        and now - _LAST_PROGRESS_WRITE < _PROGRESS_SECONDS
    ):
        return
    _write_report(
        None,
        target_name="POKERED_GATE_PROGRESS_REPORT",
        exitstatus=-1,
    )
    _LAST_PROGRESS_COUNT = count
    _LAST_PROGRESS_WRITE = now


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    _write_report(
        session,
        target_name="POKERED_GATE_REPORT",
        exitstatus=exitstatus,
    )


def _write_report(
    session: Any | None,
    *,
    target_name: str,
    exitstatus: int,
) -> None:
    target = os.environ.get(target_name)
    if not target:
        return

    config = getattr(session, "config", None)
    collection_only = bool(getattr(getattr(config, "option", None), "collectonly", False))
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
        "collection_skips": list(_collection_skips()),
        "collected": len(_ACTIVE_COLLECTED_NODEIDS),
        "nodeids": list(_ACTIVE_COLLECTED_NODEIDS),
        "collection_only": collection_only,
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


# The plugin hooks receive reports, not the Config object.  Pytest invokes
# hooks on this module, so a short module-level store is sufficient and keeps
# the JSON protocol independent of private pytest internals.
_ACTIVE_RECORDS: dict[str, dict[str, Any]] = {}
_ACTIVE_COLLECTION_ERRORS: list[dict[str, str]] = []
_ACTIVE_COLLECTION_SKIPS: list[dict[str, str]] = []
_ACTIVE_COLLECTED_NODEIDS: list[str] = []
_LAST_PROGRESS_COUNT = 0
_LAST_PROGRESS_WRITE = 0.0


def _records() -> dict[str, dict[str, Any]]:
    return _ACTIVE_RECORDS


def _collection_errors() -> list[dict[str, str]]:
    return _ACTIVE_COLLECTION_ERRORS


def _collection_skips() -> list[dict[str, str]]:
    return _ACTIVE_COLLECTION_SKIPS


def _reason(report: Any) -> str:
    longrepr = getattr(report, "longrepr", None)
    if longrepr is None:
        return ""
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[2])
    return str(longrepr)


def _combine_reasons(previous: str, current: str) -> str:
    if not previous:
        return current
    if not current or current == previous:
        return previous
    return f"{previous}\n{current}"


__all__ = [
    "pytest_collection_finish",
    "pytest_collectreport",
    "pytest_configure",
    "pytest_runtest_logfinish",
    "pytest_runtest_logreport",
    "pytest_sessionfinish",
]
