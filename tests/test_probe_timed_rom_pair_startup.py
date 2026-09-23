"""Startup-failure and shared-event checks for spawned timed-probe owners."""

import signal
import time

from scripts import probe_timed_rom_pair as probe
from tests._probe_timed_rom_pair_support import (
    _NoSharedEventContext,
    _print_startup_evidence,
    _spawn_diagnostic_owner,
    _spawn_wait_for_startup_cancel,
)


def test_process_startup_failure_signals_without_shared_event_locks(spawned_probe_args):
    context = _NoSharedEventContext(fail_second_start=True)
    started = time.monotonic()
    try:
        result = probe.run_process_pair(
            spawned_probe_args,
            context=context,
            child_target=_spawn_wait_for_startup_cancel,
        )
    finally:
        context.close()
    returned = time.monotonic()
    _print_startup_evidence(context, spawned_probe_args, result, started, returned)
    assert time.monotonic() - started < spawned_probe_args.overall_timeout
    assert result["stop_reason"] == "startup_failure"
    assert any(
        "injected second owner start failure" in error
        for error in result["supervisor_cancel_errors"]
    )
    assert result["processes_alive"] == result["report_readers_alive"] == []
    assert result["owners"][0]["test_cancel_seen"] is True
    assert result["owners"][0]["exitcode"] == 0
    assert result["owners"][0]["forced_termination"] is False
    assert context.startup["phase"] == "cancellation_wait"
    assert context.startup["pid"] == result["owners"][0]["pid"]
    assert context.startup["ready_monotonic"] == result["owners"][0]["test_ready_monotonic"]
    assert started <= context.startup["ready_monotonic"] <= context.startup["injected_monotonic"]
    assert context.startup["injected_monotonic"] < context.startup["pair_deadline"]
    assert context.startup["injected_monotonic"] <= result["cancelled_monotonic"]
    assert (
        result["cancelled_monotonic"]
        <= result["owners"][0]["test_cancel_seen_monotonic"]
        <= returned
    )
    assert result["owners"][1]["pid"] is None
    assert result["owners"][1]["actual_missing"] is True


def test_process_early_startup_failure_forces_termination_without_shared_event_locks(
    spawned_probe_args,
):
    context = _NoSharedEventContext(fail_second_start=True, block_before_owner=True)
    started = time.monotonic()
    try:
        result = probe.run_process_pair(
            spawned_probe_args, context=context, child_target=_spawn_wait_for_startup_cancel
        )
    finally:
        context.close()
    returned = time.monotonic()
    _print_startup_evidence(context, spawned_probe_args, result, started, returned)
    assert returned - started < spawned_probe_args.overall_timeout
    assert result["stop_reason"] == "startup_failure"
    assert any(
        "injected second owner start failure" in error
        for error in result["supervisor_cancel_errors"]
    )
    assert context.startup["phase"] == "before_owner_entry"
    assert started <= context.startup["ready_monotonic"] <= context.startup["injected_monotonic"]
    assert context.startup["injected_monotonic"] < context.startup["pair_deadline"]
    assert context.startup["injected_monotonic"] <= result["cancelled_monotonic"] <= returned
    assert result["processes_alive"] == result["report_readers_alive"] == []
    owner = result["owners"][0]
    assert owner["pid"] == context.startup["pid"]
    assert owner["owner_complete"] and not owner["alive"]
    assert owner["forced_termination"] is True
    assert owner["exitcode"] in (-signal.SIGTERM, -signal.SIGKILL)
    assert owner["termination"] == "missing_report"
    assert "test_cancel_seen" not in owner
    assert owner["actual_missing"] is True
    assert owner["cleanup"] == []
    assert owner["cleanup_status"] == "nongraceful_forced_or_live"
    assert result["owners"][1]["pid"] is None
    assert result["owners"][1]["actual_missing"] is True


def test_process_partial_report_kill_reaps_reader_without_shared_event_locks(spawned_probe_args):
    spawned_probe_args.test_scenario = "partial_report"
    started = time.monotonic()
    result = probe.run_process_pair(
        spawned_probe_args,
        context=_NoSharedEventContext(),
        child_target=_spawn_diagnostic_owner,
    )
    assert time.monotonic() - started < spawned_probe_args.overall_timeout
    assert result["stop_reason"] == "deadline"
    assert result["processes_alive"] == result["report_readers_alive"] == []
    for owner in result["owners"]:
        assert owner["forced_termination"]
        assert owner["exitcode"] == -signal.SIGKILL
        assert owner["actual_missing"] is True
        assert owner["errors"]
        assert owner["cleanup"] == []
