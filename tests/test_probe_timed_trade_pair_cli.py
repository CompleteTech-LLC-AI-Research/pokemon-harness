"""Trade-probe CLI bounds, defaults, and fixed-contract refusals (#160).

Split from ``tests/test_probe_timed_trade_pair.py`` for #160 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Authored/fake trade-entry checks only; never commercial-ROM
qualification.
"""

import pytest

from scripts import probe_timed_rom_pair as ordinary
from scripts import probe_timed_trade_pair as trade
from tests._probe_timed_trade_pair_support import (
    cli,
)


@pytest.mark.parametrize(
    "option",
    [
        "frame-limit",
        "overall-timeout",
        "pair-timeout",
        "operation-timeout",
        "rearm-budget",
        "rearm-instruction-cap",
        "max-edge-lateness",
        "listener-slot",
        "connector-slot",
    ],
)
def test_requires_explicit_bounds_policy_and_outgoing_slots(option):
    with pytest.raises(SystemExit) as caught:
        trade.parse_args(cli(**{option: None}))
    assert caught.value.code == 2


@pytest.mark.parametrize("option", ["overall-timeout", "pair-timeout", "operation-timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_wall_bounds_are_finite_and_positive(option, value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{option: value}))


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "inf"])
def test_frame_bound_is_a_positive_integer(value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{"frame-limit": value}))


@pytest.mark.parametrize("option", ["listener-slot", "connector-slot"])
@pytest.mark.parametrize("value", ["0", "7", "-1", "1.5", "nan"])
def test_outgoing_slot_rejects_out_of_range_or_noninteger(option, value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{option: value}))


@pytest.mark.parametrize("listener,connector", [(1, 6), (6, 1), (3, 4)])
def test_outgoing_slots_convert_once_and_preserve_cli_values(listener, connector):
    args = trade.parse_args(cli(**{"listener-slot": listener, "connector-slot": connector}))
    assert (args.listener_slot, args.connector_slot) == (listener, connector)
    assert (args.listener_slot_index, args.connector_slot_index) == (listener - 1, connector - 1)


def test_trade_defaults_fix_process_whole_frames_and_evidence():
    args = trade.parse_args(cli())
    assert args.checkpoint == "reciprocal-exchange"
    assert args.owner_mode == "process"
    assert args.listener_chunk == args.connector_chunk == 1
    assert args.call_retention == "stream"
    assert args.rom_milestones is True
    assert args.operation_timeout == 5
    assert ordinary.QUANTUM_CYCLES == 256


def test_select_mon_is_explicit_checkpoint():
    assert trade.parse_args(cli() + ["--checkpoint", "select-mon"]).checkpoint == "select-mon"


@pytest.mark.parametrize(
    "goal,checkpoint", [("trade", "reciprocal-exchange"), ("select-mon", "select-mon")]
)
def test_goal_alias_normalizes_to_checkpoint(goal, checkpoint):
    assert trade.parse_args(cli() + ["--goal", goal]).checkpoint == checkpoint


def test_goal_alias_and_checkpoint_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        trade.parse_args(cli() + ["--goal", "trade", "--checkpoint", "select-mon"])


@pytest.mark.parametrize(
    "extra",
    [
        ["--owner-mode", "thread"],
        ["--listener-chunk", "2"],
        ["--connector-chunk", "2"],
        ["--call-retention", "inline"],
        ["--quantum", "512"],
    ],
)
def test_cli_cannot_weaken_fixed_execution_contract(extra):
    with pytest.raises(SystemExit):
        trade.parse_args(cli() + extra)


def test_driver_is_internal_not_an_arbitrary_cli_import():
    with pytest.raises(SystemExit):
        trade.parse_args(cli() + ["--owner-driver", "untrusted.module:factory"])


@pytest.mark.parametrize("option", ["rearm-budget", "rearm-instruction-cap", "max-edge-lateness"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_native_policy_stays_explicit_positive_integer(option, value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{option: value}))


def test_ordinary_probe_defaults_are_preserved():
    args = ordinary.parse_args(
        [
            "--operation-timeout=5",
            "--rearm-budget=4096",
            "--rearm-instruction-cap=1024",
            "--max-edge-lateness=4096",
            "--output=/tmp/unused-authored-ordinary-report.json",
        ]
    )
    assert args.owner_mode == "thread"
    assert args.input_profile == "none"
    assert (args.listener_chunk, args.connector_chunk) == (1, 2)
    assert args.frame_limit == 6
    assert args.call_retention == "inline"
    assert args.rom_milestones is False


@pytest.mark.parametrize("value", ["1", "4.9", "6"])
def test_trade_operation_timeout_requires_the_fixed_five_seconds(value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{"operation-timeout": value}))
