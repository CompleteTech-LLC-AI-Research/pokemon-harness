"""Deterministic fake-clock controls for the diagnostic timing ledger.

These prove timing accumulation and finalization only. They say nothing about
real emulator speed or whether the 1200 s source-local battle deadline is met.
"""

import asyncio
import io
import json

import pytest

from scripts import normal_red_battle_drive as drive
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
    stream = TimedStream(sink, ledger, "observation_journal_io")
    stream.write("abc\n")
    stream.flush()
    assert sink.getvalue() == "abc\n"
    assert ledger.summary()["operations"]["observation_journal_io"]["count"] == 2
    assert ledger.summary()["operations"]["observation_journal_io"]["total_seconds"] == 0.5


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
