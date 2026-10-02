"""Deterministic fake-clock controls for the diagnostic timing ledger.

These prove timing accumulation and finalization only. They say nothing about
real emulator speed or whether the 1200 s source-local battle deadline is met.
"""

import asyncio
import io
import json
import sys
import types

import pytest

from scripts import normal_red_battle_drive as drive
from scripts import normal_red_link_journal as journal_module
from scripts import qualify_normal_red_link as qualify
from scripts.normal_red_link_journal import (
    MAX_PHASE_MARKS,
    TIMING_SCHEMA,
    JournalPair,
    TimedStream,
    TimingLedger,
)
from tests.test_normal_red_link_admission import battle_snapshot, exercise_battle


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def rows_of(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.mark.asyncio
async def test_operation_durations_accumulate_per_operation():
    clock = FakeClock()

    class Pair:
        async def step(self, frames):
            clock.advance(2.0 if frames == 4 else 0.5)

        async def state(self, owner):
            clock.advance(0.25)
            return {}

    stream = io.StringIO()
    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), stream, ledger)
    await pair.step(4)
    await pair.step(4)
    await pair.step(1)
    await pair.state(0)
    summary = ledger.summary()
    assert summary["schema"] == TIMING_SCHEMA
    assert summary["operations"]["step"] == {
        "count": 3,
        "total_seconds": 4.5,
        "max_seconds": 2.0,
    }
    assert summary["operations"]["state"]["total_seconds"] == 0.25
    completions = [row for row in rows_of(stream) if row["event"] == "completion"]
    assert [row["seconds"] for row in completions] == [2.0, 2.0, 0.5, 0.25]
    assert summary["interrupted"] is None


@pytest.mark.asyncio
async def test_journal_io_is_attributed_separately_from_operation_time():
    clock = FakeClock()

    class SlowStream(io.StringIO):
        def flush(self):
            clock.advance(0.125)

    class Pair:
        async def step(self, frames):
            clock.advance(1.0)

    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), SlowStream(), ledger)
    await pair.step(4)
    operations = ledger.summary()["operations"]
    assert operations["step"]["total_seconds"] == 1.0
    # One intent write and one completion write, each flushed.
    assert operations["operation_journal_io"]["count"] == 2
    assert operations["operation_journal_io"]["total_seconds"] == 0.25


@pytest.mark.asyncio
async def test_failed_operation_is_reported_and_still_has_no_fake_completion():
    clock = FakeClock()

    class Pair:
        async def step(self, frames):
            clock.advance(3.0)
            raise RuntimeError("real call failed")

    stream = io.StringIO()
    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), stream, ledger)
    with pytest.raises(RuntimeError, match="real call failed"):
        await pair.step(4)
    assert [row["event"] for row in rows_of(stream)] == ["intent"]
    interrupted = ledger.summary()["interrupted"]
    assert interrupted["operation"] == "step" and interrupted["seconds"] == 3.0
    assert interrupted["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_deadline_cancellation_finalizes_the_in_flight_operation():
    clock = FakeClock()

    class Pair:
        async def step(self, frames):
            clock.advance(7.0)
            await asyncio.Event().wait()

    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), io.StringIO(), ledger)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pair.step(4), 0.01)
    summary = ledger.summary()
    assert summary["interrupted"]["operation"] == "step"
    assert summary["interrupted"]["seconds"] == 7.0
    assert summary["interrupted"]["error"] == "CancelledError"
    assert summary["operations"]["step"]["count"] == 1
    json.dumps(summary)


@pytest.mark.asyncio
async def test_backwards_clock_never_yields_negative_durations():
    clock = FakeClock()

    class Pair:
        async def step(self, frames):
            clock.advance(-5.0)

    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), io.StringIO(), ledger)
    await pair.step(4)
    summary = ledger.summary()
    assert summary["operations"]["step"]["total_seconds"] == 0.0
    assert summary["elapsed_seconds"] == 0.0


def test_phase_marks_are_bounded_and_counted():
    clock = FakeClock()
    ledger = TimingLedger(clock)
    for index in range(MAX_PHASE_MARKS + 5):
        clock.advance(1.0)
        ledger.mark(f"p{index}")
    summary = ledger.summary()
    assert len(summary["phases"]) == MAX_PHASE_MARKS
    assert summary["phases_dropped"] == 5
    assert summary["phases"][0] == {"phase": "p0", "seconds_since_start": 1.0}


def test_timed_stream_forwards_bytes_unchanged_and_times_io():
    clock = FakeClock()

    class Sink(io.StringIO):
        def write(self, text):
            clock.advance(0.5)
            return super().write(text)

    sink = Sink()
    ledger = TimingLedger(clock)
    stream = TimedStream(sink, ledger, "observation_stream_write_flush")
    stream.write("abc\n")
    stream.flush()
    assert sink.getvalue() == "abc\n"
    assert ledger.summary()["operations"]["observation_stream_write_flush"]["count"] == 2
    assert ledger.summary()["operations"]["observation_stream_write_flush"]["total_seconds"] == 0.5


@pytest.mark.asyncio
async def test_untimed_journal_is_byte_compatible_with_existing_schema():
    class Pair:
        async def step(self, frames):
            return {"ok": True}

    stream = io.StringIO()
    pair = JournalPair(Pair(), stream)
    await pair.step(4)
    pair.mark("ignored")
    rows = rows_of(stream)
    assert [row["event"] for row in rows] == ["intent", "completion"]
    assert all("seconds" not in row for row in rows)


@pytest.mark.asyncio
async def test_timing_does_not_change_battle_actions_or_outcome(monkeypatch):
    states = [
        battle_snapshot(),
        battle_snapshot(hp=0, replacement=True),
        battle_snapshot(hp=15),
        battle_snapshot(terminal=True),
    ]

    async def no_op(client):
        pass

    for name in ("_drive_to_link_menu", "_select_colosseum", "_enter_battle"):
        monkeypatch.setattr(drive, name, no_op)

    async def run(timing):
        actions = []

        class Pair:
            def __init__(self):
                self.index = 0

            async def link_up(self, *, arm_barrier=None):
                pass

            async def state(self, owner):
                return states[min(self.index, len(states) - 1)]

            async def press(self, owner, button, *, duration):
                actions.append((self.index, owner, button, duration))

            async def step(self, count):
                self.index += 1

        pair = Pair()
        if timing is not None:
            pair = JournalPair(pair, io.StringIO(), timing)
        result = await drive.complete_battle(pair, io.StringIO(), budget_frames=32)
        return actions, result

    plain_actions, plain = await run(None)
    ledger = TimingLedger(FakeClock())
    timed_actions, timed = await run(ledger)
    assert timed_actions == plain_actions
    for key in ("frames", "terminal", "last_live", "replacement_completed"):
        assert timed[key] == plain[key]
    phases = [row["phase"] for row in ledger.summary()["phases"]]
    assert phases[:5] == [
        "link_up_done",
        "link_menu_done",
        "colosseum_selected",
        "battle_entered",
        "battle_frames_0",
    ]
    assert phases[-1].startswith("battle_loop_end_frames_")


@pytest.mark.asyncio
async def test_battle_drive_still_works_with_pair_lacking_mark(monkeypatch):
    # The pre-existing fake pair has no ``mark``; marking must stay optional.
    result = await exercise_battle(
        monkeypatch,
        [
            battle_snapshot(),
            battle_snapshot(hp=0, replacement=True),
            battle_snapshot(hp=15),
            battle_snapshot(terminal=True),
        ],
    )
    assert all(result["terminal"])


@pytest.mark.asyncio
async def test_redaction_cost_is_journal_io_not_operation_time(monkeypatch):
    clock = FakeClock()

    def slow_redacted(value):
        clock.advance(2.0)
        return value

    monkeypatch.setattr(journal_module, "redacted", slow_redacted)

    class Pair:
        async def state(self, owner):
            clock.advance(1.0)
            return {"ok": True}

    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), io.StringIO(), ledger)
    await pair.state(0)
    operations = ledger.summary()["operations"]
    assert operations["state"]["total_seconds"] == 1.0
    # args, kwargs and result are each redacted: three slow calls.
    assert operations["operation_journal_io"]["total_seconds"] == 6.0


@pytest.mark.asyncio
async def test_release_buckets_are_exclusive_and_sum_to_elapsed():
    clock = FakeClock()

    class Client:
        async def request(self, method, arguments):
            clock.advance(1.0)
            return {"tools": [{"name": "release"}, {"name": "link_peer_release"}]}

        async def tool(self, name, arguments):
            clock.advance(1.0)
            return {"ok": True}

    class Pair:
        transport = "local_pair"
        client = Client()

    class SlowStream(io.StringIO):
        def flush(self):
            clock.advance(0.125)

    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), SlowStream(), ledger)
    await pair.release_buttons()
    operations = ledger.summary()["operations"]
    # Two owners, one tools/list and four releases each; sixteen journal rows.
    assert "release_buttons" not in operations
    assert operations["release_rpc"]["count"] == 10
    assert operations["release_rpc"]["total_seconds"] == 10.0
    assert operations["operation_journal_io"]["count"] == 16
    assert operations["operation_journal_io"]["total_seconds"] == 2.0
    total = sum(row["total_seconds"] for row in operations.values())
    assert total == ledger.summary()["elapsed_seconds"] == 12.0


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_write", [1, 2])
@pytest.mark.parametrize("fail_on", ["write", "flush"])
async def test_journal_failure_is_attributed_to_its_stage_and_still_raises(failing_write, fail_on):
    clock = FakeClock()
    stages = {1: "intent", 2: "completion"}

    class FailingStream(io.StringIO):
        writes = 0

        def write(self, text):
            if fail_on == "write":
                self._maybe_fail()
            return super().write(text)

        def flush(self):
            if fail_on == "flush":
                self._maybe_fail()

        def _maybe_fail(self):
            if fail_on == "write":
                type(self).writes += 1
                count = type(self).writes
            else:
                count = type(self).writes = type(self).writes + 1
            if count == failing_write:
                clock.advance(3.0)
                raise OSError("disk")

    class Pair:
        async def step(self, frames):
            clock.advance(1.0)
            return {}

    ledger = TimingLedger(clock)
    pair = JournalPair(Pair(), FailingStream(), ledger)
    with pytest.raises(OSError, match="disk"):
        await pair.step(4)
    summary = ledger.summary()
    interrupted = summary["interrupted"]
    assert interrupted["operation"] == "operation_journal_io"
    assert interrupted["stage"] == stages[failing_write]
    assert interrupted["seconds"] == 3.0 and interrupted["error"] == "OSError"
    assert summary["operations"]["operation_journal_io"]["total_seconds"] >= 3.0
    if failing_write == 2:
        # The underlying call really completed; only its completion row failed.
        assert summary["operations"]["step"]["count"] == 1


def test_timed_stream_failure_records_stage_and_duration():
    clock = FakeClock()

    class Sink(io.StringIO):
        def flush(self):
            clock.advance(4.0)
            raise OSError("flush failed")

    ledger = TimingLedger(clock)
    stream = TimedStream(Sink(), ledger, "observation_stream_write_flush")
    with pytest.raises(OSError, match="flush failed"):
        stream.flush()
    summary = ledger.summary()
    assert summary["interrupted"]["stage"] == "flush"
    assert summary["interrupted"]["seconds"] == 4.0
    assert summary["operations"]["observation_stream_write_flush"]["total_seconds"] == 4.0


def test_markers_past_the_cap_are_not_journaled():
    ledger = TimingLedger(FakeClock())
    stream = io.StringIO()
    pair = JournalPair(object(), stream, ledger)
    for index in range(MAX_PHASE_MARKS + 5):
        pair.mark(f"p{index}")
    phase_rows = [row for row in rows_of(stream) if row["event"] == "phase"]
    assert phase_rows == []
    assert len(ledger.summary()["phases"]) == MAX_PHASE_MARKS
    assert ledger.summary()["phases_dropped"] == 5


@pytest.mark.asyncio
async def test_untimed_rows_are_literally_the_pre_patch_bytes():
    class Pair:
        async def step(self, frames):
            return {"ok": True}

    stream = io.StringIO()
    await JournalPair(Pair(), stream).step(4)
    assert stream.getvalue() == (
        '{"sequence": 1, "event": "intent", "operation": "step", "args": [4], "kwargs": {}}\n'
        '{"sequence": 1, "event": "completion", "operation": "step", "result": {"ok": true}}\n'
    )


def run_main(monkeypatch, tmp_path, row, *, diagnostic=True):
    asset = {
        "pins": {"expected_pyboy_version": "2.7.0", "expected_pyboy_revision": "rev"},
        "provenance": {"registry": {"sha256": "f" * 64}},
    }
    monkeypatch.setattr(qualify, "resolve_normal_red_assets", lambda *args: asset)
    monkeypatch.setattr(qualify, "runtime_identity", dict)
    fake = types.SimpleNamespace(__version__="2.7.0", __pokered_harness_revision__="rev")
    monkeypatch.setitem(sys.modules, "pyboy", fake)
    monkeypatch.setattr(qualify, "battle_row", row)
    output = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify",
            "--rom",
            "r",
            "--symbols",
            "s",
            "--fixture-root",
            "f",
            "--output",
            str(output),
            "--transport",
            "local",
            "--kind",
            "battle",
        ],
    )
    if diagnostic:
        sys.argv.append("--diagnostic-timing")
    return output


@pytest.mark.parametrize("error", [asyncio.TimeoutError, RuntimeError])
def test_main_persists_timing_in_the_receipt_on_timeout_and_error(monkeypatch, tmp_path, error):
    clock = FakeClock()
    monkeypatch.setattr(qualify, "TimingLedger", lambda: TimingLedger(clock))

    async def row(output, asset, transport, journal, timing):
        class Pair:
            async def step(self, frames):
                clock.advance(5.0)
                raise error()

        await JournalPair(Pair(), journal, timing).step(4)

    output = run_main(monkeypatch, tmp_path, row)
    with pytest.raises(error):
        qualify.main()
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "FAILED"
    assert receipt["timing"]["schema"] == TIMING_SCHEMA
    assert receipt["timing"]["interrupted"]["operation"] == "step"
    assert receipt["timing"]["interrupted"]["error"] == error.__name__
    assert receipt["timing"]["operations"]["step"]["total_seconds"] == 5.0


def test_main_receipt_keeps_existing_fields_when_timing_is_added(monkeypatch, tmp_path):
    async def row(output, asset, transport, journal, timing):
        return {}

    output = run_main(monkeypatch, tmp_path, row)
    assert qualify.main() == 0
    receipt = json.loads((output / "receipt.json").read_text())
    for key in (
        "status",
        "scenario",
        "transport",
        "kind",
        "fixture_sha256",
        "source_sha256",
        "loaded_source_footprint",
        "runtime_identity",
        "seconds",
    ):
        assert key in receipt
    assert receipt["status"] == "PASS" and receipt["timing"]["interrupted"] is None


def test_default_cli_leaves_timing_and_journal_schema_unchanged(monkeypatch, tmp_path):
    async def row(output, asset, transport, journal):
        class Pair:
            async def step(self, count):
                return {"ok": True}

        pair = JournalPair(Pair(), journal)
        pair.mark("ignored")
        await pair.step(4)
        return {}

    output = run_main(monkeypatch, tmp_path, row, diagnostic=False)
    monkeypatch.setattr(
        qualify, "TimingLedger", lambda: pytest.fail("default must not enable timing")
    )
    assert qualify.main() == 0
    assert "timing" not in json.loads((output / "receipt.json").read_text())
    rows = [
        json.loads(line) for line in (output / "pair-operations.jsonl").read_text().splitlines()
    ]
    assert [row["event"] for row in rows] == ["intent", "completion"]
    assert all("seconds" not in row for row in rows)


@pytest.mark.asyncio
async def test_timed_phases_preserve_strict_operation_pairing():
    class Pair:
        async def step(self, count):
            return {"ok": True}

    stream = io.StringIO()
    ledger = TimingLedger(FakeClock())
    pair = JournalPair(Pair(), stream, ledger)
    pair.mark("start")
    await pair.step(1)
    pair.mark("middle")
    await pair.step(2)
    pair.mark("end")
    rows = rows_of(stream)
    assert len(rows) == 4
    for index in range(0, len(rows), 2):
        intent, completion = rows[index : index + 2]
        assert intent["event"] == "intent" and completion["event"] == "completion"
        assert intent["sequence"] == completion["sequence"] == index // 2 + 1
    assert [row["phase"] for row in ledger.summary()["phases"]] == ["start", "middle", "end"]


@pytest.mark.asyncio
async def test_concurrent_calls_are_detected_without_serializing_or_summing():
    ready = asyncio.Event()
    finish = asyncio.Event()
    entered = 0

    class Pair:
        async def step(self, count):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await finish.wait()

    ledger = TimingLedger(FakeClock())
    pair = JournalPair(Pair(), io.StringIO(), ledger)
    first = asyncio.create_task(pair.step(1))
    second = asyncio.create_task(pair.step(2))
    await asyncio.wait_for(ready.wait(), 1)
    assert entered == 2
    finish.set()
    await asyncio.gather(first, second)
    assert ledger.summary()["overlap_detected"] is True
    assert ledger.summary()["buckets_additive"] is False
    assert ledger._active_intervals == 0


@pytest.mark.parametrize("diagnostic", [False, True])
def test_receipt_failure_preserves_original_row_exception(monkeypatch, tmp_path, diagnostic):
    failure = RuntimeError("original row")

    async def row(output, asset, transport, journal, timing=None):
        raise failure

    run_main(monkeypatch, tmp_path, row, diagnostic=diagnostic)
    original_write = qualify.Path.write_text

    def write(path, *args, **kwargs):
        if path.name == "receipt.json":
            raise OSError("receipt disk")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(qualify.Path, "write_text", write)
    with pytest.raises(RuntimeError) as caught:
        qualify.main()
    assert caught.value is failure
    assert "receipt evidence failed: OSError" in failure.__notes__


def test_receipt_failure_after_success_is_not_silently_accepted(monkeypatch, tmp_path):
    async def row(output, asset, transport, journal, timing=None):
        return {}

    run_main(monkeypatch, tmp_path, row)
    failure = OSError("receipt disk")
    monkeypatch.setattr(
        qualify.Path, "write_text", lambda *args, **kwargs: (_ for _ in ()).throw(failure)
    )
    with pytest.raises(OSError) as caught:
        qualify.main()
    assert caught.value is failure


@pytest.mark.parametrize("diagnostic", [False, True])
def test_default_and_timed_cli_keep_original_1200_timeout(monkeypatch, tmp_path, diagnostic):
    async def row(output, asset, transport, journal, timing=None):
        return {}

    run_main(monkeypatch, tmp_path, row, diagnostic=diagnostic)
    original_wait = qualify.asyncio.wait_for
    seen = []

    async def wait(invocation, timeout):
        seen.append(timeout)
        return await original_wait(invocation, timeout)

    monkeypatch.setattr(qualify.asyncio, "wait_for", wait)
    assert qualify.main() == 0
    assert seen == [1200]
