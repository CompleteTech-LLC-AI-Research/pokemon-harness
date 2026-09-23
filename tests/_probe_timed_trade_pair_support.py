"""Shared builders for the split trade-probe modules (#160).

``tests/test_probe_timed_trade_pair.py`` was 1024 lines. The ``cli``
argument builder, the fake supervisor/pair builders, and the authored call
artifacts are used by more than one split module, so they live here rather
than being duplicated across the split files.
"""

import hashlib
import json

from scripts import probe_timed_trade_pair as trade


def cli(**overrides):
    options = {
        "frame-limit": "120",
        "overall-timeout": "30",
        "pair-timeout": "20",
        "operation-timeout": "5",
        "rearm-budget": "4096",
        "rearm-instruction-cap": "1024",
        "max-edge-lateness": "4096",
        "listener-slot": "1",
        "connector-slot": "6",
        "output": "/tmp/unused-authored-trade-report.json",
    }
    options.update(overrides)
    return [f"--{key}={value}" for key, value in options.items() if value is not None]


def fake_pair():
    return {
        "stop_reason": "both_owner_goals",
        "owner_goals": [True, True],
        "goal_stop": True,
        "threads_alive": [],
        "processes_alive": [],
        "report_readers_alive": [],
        "supervisor_cancel_errors": [],
        "owners": [
            {
                "side": side,
                "calls": [],
                "errors": [],
                "cleanup": [
                    "endpoint_detached",
                    "owner_driver_closed",
                    "session_closed_without_save",
                ],
                "termination": "goal_cancelled",
                "exitcode": 0,
                "alive": False,
                "forced_termination": False,
                "actual_missing": False,
                "watcher_alive": False,
                "owner_complete": True,
                "local_goal": True,
                "milestone_observer": "owner_driver",
                "driver_snapshot": {"side": side, "authored": True},
                "call_log": {"authored": True},
            }
            for side in ("listener", "connector")
        ],
    }


def fake_supervisor(monkeypatch, pair):
    calls = []

    def run(args, *, owner_driver):
        calls.append((args, owner_driver))
        return pair

    monkeypatch.setattr(trade.probe, "run_process_pair", run)
    monkeypatch.setattr(trade.probe, "runtime_identity", lambda: {"authored": True})
    monkeypatch.setattr(trade, "_validate_calls", lambda owner: None)
    return calls


def authored_call_artifact(tmp_path, calls, *, noncompleted=0, expected=False):
    payload = b"".join((json.dumps(call) + "\n").encode() for call in calls)
    path = tmp_path / "calls.jsonl"
    path.write_bytes(payload)
    return {
        "call_log": {
            "path": str(path),
            "bytes": len(payload),
            "record_count": len(calls),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "complete": True,
            "error": None,
        },
        "call_counts": {"total": len(calls), "noncompleted": noncompleted},
        "expected_goal_cancellation": expected,
    }


def completed_call():
    return {"status": "completed", "requested_frames": 1, "actual_completed_frames": 1}


def cancelled_call():
    return {
        "status": "interrupted",
        "requested_frames": 1,
        "actual_completed_frames": 0,
        "error": "Cancelled: operation cancelled",
        "expected_goal_cancellation": True,
    }
