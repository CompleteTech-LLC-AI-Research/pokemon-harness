"""Trade-probe report publication and call-artifact admission (#160).

Split from ``tests/test_probe_timed_trade_pair.py`` for #160 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Authored/fake trade-entry checks only; never commercial-ROM
qualification.
"""

import json
import os
from pathlib import Path

import pytest

from scripts import probe_timed_rom_pair as ordinary
from scripts import probe_timed_trade_pair as trade
from tests._probe_timed_trade_pair_support import (
    authored_call_artifact,
    cancelled_call,
    cli,
    completed_call,
)


@pytest.mark.parametrize(
    "complete,checkpoint_only,expected",
    [
        (True, False, 0),
        (False, True, 0),
        (False, False, 2),
    ],
)
def test_main_persists_distinct_complete_and_partial_results(
    monkeypatch,
    tmp_path,
    complete,
    checkpoint_only,
    expected,
):
    report = {
        "pairs": [],
        "complete": complete,
        "checkpoint_only": checkpoint_only,
        "status": "complete" if complete else "checkpoint" if checkpoint_only else "incomplete",
        "errors": [],
    }
    monkeypatch.setattr(trade, "run_probe", lambda args: report)
    output = tmp_path / "report.json"
    checkpoint = "select-mon" if checkpoint_only else "reciprocal-exchange"
    result = trade.main(cli(output=output) + ["--checkpoint", checkpoint])
    assert result == expected
    saved = json.loads(output.read_text())
    assert saved["complete"] is complete
    assert saved["checkpoint_only"] is checkpoint_only


def test_main_preserves_existing_evidence(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    output.write_text("previous evidence\n")
    monkeypatch.setattr(
        trade,
        "run_probe",
        lambda args: {
            "pairs": [],
            "complete": False,
            "checkpoint_only": False,
            "status": "incomplete",
            "errors": [],
        },
    )
    with pytest.raises(FileExistsError):
        trade.main(cli(output=output))
    assert output.read_text() == "previous evidence\n"


@pytest.mark.parametrize("symlink", [False, True])
def test_output_cannot_enter_asset_checkout_via_direct_or_symlink_path(tmp_path, symlink):
    checkout = tmp_path / "assets"
    checkout.mkdir()
    destination = checkout
    if symlink:
        destination = tmp_path / "alias"
        destination.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(SystemExit):
        trade.parse_args(cli(output=destination / "report.json") + ["--repo-root", str(checkout)])
    assert not (checkout / "report.json").exists()


def test_output_cannot_enter_source_checkout():
    with pytest.raises(SystemExit):
        trade.parse_args(cli(output=trade.ROOT / "unused-authored-report.json"))


def test_marked_terminal_goal_cancellation_retains_raw_interrupted_evidence(tmp_path):
    calls = [completed_call(), cancelled_call()]
    owner = authored_call_artifact(tmp_path, calls, noncompleted=1, expected=True)
    trade._validate_calls(owner)
    assert owner["call_counts"]["noncompleted"] == 1
    saved = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert saved == calls


def test_whole_completed_call_artifact_is_accepted(tmp_path):
    trade._validate_calls(authored_call_artifact(tmp_path, [completed_call()]))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_owner_marker",
        "missing_call_marker",
        "wrong_exception",
        "deadline_exception",
        "missing_actual_frames",
        "earlier_interrupt",
        "lying_count",
        "multiple_interrupts",
        "partial_completed",
        "no_progress_completed",
    ],
)
def test_only_exact_terminal_postgoal_cancellation_is_tolerated(tmp_path, mutation):
    calls = [completed_call(), cancelled_call()]
    expected, noncompleted = True, 1
    if mutation == "missing_owner_marker":
        expected = False
    elif mutation == "missing_call_marker":
        del calls[-1]["expected_goal_cancellation"]
    elif mutation == "wrong_exception":
        calls[-1]["error"] = "ChannelClosed: peer closed"
    elif mutation == "deadline_exception":
        calls[-1]["error"] = "Deadline: operation expired"
    elif mutation == "missing_actual_frames":
        del calls[-1]["actual_completed_frames"]
    elif mutation == "earlier_interrupt":
        calls.reverse()
    elif mutation == "lying_count":
        noncompleted = 0
    elif mutation == "multiple_interrupts":
        calls.append(cancelled_call())
        noncompleted = 2
    elif mutation == "partial_completed":
        calls[-1]["status"] = "completed_partial"
    elif mutation == "no_progress_completed":
        calls[-1]["status"] = "completed_no_progress"
    owner = authored_call_artifact(tmp_path, calls, noncompleted=noncompleted, expected=expected)
    with pytest.raises(ValueError):
        trade._validate_calls(owner)


def test_tampered_call_artifact_is_rejected(tmp_path):
    owner = authored_call_artifact(tmp_path, [completed_call()])
    (tmp_path / "calls.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(ValueError):
        trade._validate_calls(owner)


@pytest.mark.parametrize("replacement", ["regular", "fifo"])
def test_call_artifact_replacement_after_manifest_check_is_rejected_without_blocking(
    monkeypatch,
    tmp_path,
    replacement,
):
    owner = authored_call_artifact(tmp_path, [dict(completed_call(), evidence="A")])
    path = tmp_path / "calls.jsonl"
    original_validate = ordinary.validate_call_artifact
    original_open = Path.open

    def replace_after_validation(record):
        original_validate(record)
        if replacement == "regular":
            path.write_bytes(path.read_bytes().replace(b'"A"', b'"B"'))
        else:
            path.unlink()
            os.mkfifo(path)

    def reject_blocking_fifo_open(self, *args, **kwargs):
        if replacement == "fifo" and self == path:
            raise AssertionError("FIFO must be opened nonblocking and rejected before reading")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(ordinary, "validate_call_artifact", replace_after_validation)
    monkeypatch.setattr(Path, "open", reject_blocking_fifo_open)
    with pytest.raises(ValueError):
        trade._validate_calls(owner)
