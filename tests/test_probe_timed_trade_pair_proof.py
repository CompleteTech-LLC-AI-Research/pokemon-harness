"""Supervisor/adjudicator reciprocal-proof admission and peer readiness (#160).

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
    fake_pair,
    fake_supervisor,
)


@pytest.mark.parametrize("checkpoint", ["reciprocal-exchange", "select-mon"])
def test_entry_reuses_supervisor_and_parent_adjudicator(monkeypatch, checkpoint):
    pair = fake_pair()
    calls = fake_supervisor(monkeypatch, pair)
    judgments = []

    def adjudicate(snapshots, checkpoint):
        judgments.append((snapshots, checkpoint))
        return {
            "complete": False,
            "checkpoint_only": checkpoint == "select-mon",
            "errors": [],
            "status": "authored_partial",
        }

    monkeypatch.setattr(trade, "adjudicate_pair", adjudicate)
    args = trade.parse_args(cli() + ["--checkpoint", checkpoint])
    report = trade.run_probe(args)
    assert len(calls) == 1
    assert calls[0][0] is args
    assert calls[0][1] == trade.DRIVER == "scripts._timed_trade_probe:create_owner_driver"
    assert args.owner_driver_options == [
        {"version": args.listener, "outgoing_slot": 0, "checkpoint": checkpoint},
        {"version": args.connector, "outgoing_slot": 5, "checkpoint": checkpoint},
    ]
    assert judgments == [([owner["driver_snapshot"] for owner in pair["owners"]], checkpoint)]
    assert report["complete"] is False


def test_joint_local_goals_do_not_replace_reciprocal_parent_proof(monkeypatch):
    pair = fake_pair()
    for owner in pair["owners"]:
        owner["driver_snapshot"]["objective_complete"] = True
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": False,
            "checkpoint_only": False,
            "errors": ["reciprocal party records do not match"],
            "status": "incomplete",
        },
    )
    report = trade.run_probe(trade.parse_args(cli()))
    assert report["complete"] is False


def test_reverse_orientation_swaps_slots_with_versions_and_shares_deadline(monkeypatch):
    calls = fake_supervisor(monkeypatch, fake_pair())
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    args = trade.parse_args(cli() + ["--both-orientations"])
    report = trade.run_probe(args)
    assert len(calls) == 2
    forward, reverse = [call[0] for call in calls]
    assert (reverse.listener, reverse.connector) == (forward.connector, forward.listener)
    assert (reverse.listener_slot, reverse.connector_slot) == (6, 1)
    assert (reverse.listener_slot_index, reverse.connector_slot_index) == (5, 0)
    assert reverse.owner_driver_options == list(reversed(forward.owner_driver_options))
    assert forward.absolute_deadline == reverse.absolute_deadline
    assert len(report["pairs"]) == 2


@pytest.mark.parametrize(
    "failure", ["forced_termination", "actual_missing", "alive", "watcher_alive"]
)
def test_reciprocal_proof_cannot_override_unhealthy_owner(monkeypatch, failure):
    pair = fake_pair()
    pair["owners"][0][failure] = True
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


@pytest.mark.parametrize("reason", ["deadline", "startup_failure", "owner_completion_or_failure"])
def test_full_proof_requires_supervisor_joint_goal_stop(monkeypatch, reason):
    pair = fake_pair()
    pair["stop_reason"] = reason
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    report = trade.run_probe(trade.parse_args(cli()))
    assert report["complete"] is False
    assert report["errors"]


@pytest.mark.parametrize(
    "termination", ["frame_bound", "owner_failure", "no_progress", "cancelled_or_deadline"]
)
def test_full_proof_does_not_hide_non_goal_termination(monkeypatch, termination):
    pair = fake_pair()
    pair["owners"][0]["termination"] = termination
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


@pytest.mark.parametrize("field", ["owner_goals", "goal_stop", "local_goal"])
@pytest.mark.parametrize("missing", [False, True])
def test_full_proof_requires_explicit_joint_and_local_goal_evidence(monkeypatch, field, missing):
    pair = fake_pair()
    target = pair["owners"][0] if field == "local_goal" else pair
    if missing:
        del target[field]
    else:
        target[field] = [True, False] if field == "owner_goals" else False
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


@pytest.mark.parametrize("missing", ["milestone_observer", "owner_driver_closed"])
def test_full_proof_requires_delegated_observation_and_driver_cleanup(monkeypatch, missing):
    pair = fake_pair()
    owner = pair["owners"][0]
    if missing == "milestone_observer":
        del owner[missing]
    else:
        owner["cleanup"].remove(missing)
        owner["cleanup"].append("milestone_hooks_removed")
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


def test_select_mon_never_claims_full_completion_even_if_adjudicator_does(monkeypatch):
    fake_supervisor(monkeypatch, fake_pair())
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": True,
            "errors": [],
            "status": "complete",
        },
    )
    args = trade.parse_args(cli() + ["--checkpoint", "select-mon"])
    assert trade.run_probe(args)["complete"] is False


@pytest.mark.parametrize("case", ["reciprocal", "select_mon", "copy_only", "one_way_copy"])
def test_entry_uses_real_helper_to_require_reciprocal_copy_and_posttrade_endpoint(
    monkeypatch, case
):
    def party(species):
        return {
            "count": 1,
            "species": [species, 255],
            "records": [(bytes([species]) + bytes(43)).hex()],
            "ot_names": [(bytes([species]) + bytes(10)).hex()],
            "nicknames": [(bytes([species + 1]) + bytes(10)).hex()],
        }

    first, second = party(1), party(2)
    checkpoint = "select-mon" if case == "select_mon" else "reciprocal-exchange"
    pair = fake_pair()
    for index, (before, copied) in enumerate(((first, second), (second, first))):
        pair["owners"][index]["driver_snapshot"] = {
            "role": ("listen", "connect")[index],
            "checkpoint": checkpoint,
            "outgoing_slot": 0,
            "checkpoint_reached": True,
            "readiness": [1] * 5 + ([0] * 3 if case == "select_mon" else [1] * 3),
            "copy_complete": case != "select_mon",
            "trade_endpoint_observed": case == "reciprocal",
            "before_party": before,
            "copied_party": copied,
            "final_party": copied,
        }
    if case == "one_way_copy":
        pair["owners"][1]["driver_snapshot"].update(
            copied_party=second,
            final_party=second,
            trade_endpoint_observed=True,
        )
        pair["owners"][0]["driver_snapshot"]["trade_endpoint_observed"] = True
    fake_supervisor(monkeypatch, pair)
    report = trade.run_probe(trade.parse_args(cli() + ["--checkpoint", checkpoint]))
    assert report["complete"] is (case == "reciprocal")
    assert report["checkpoint_only"] is (case == "select_mon")


def test_missing_driver_snapshot_fails_closed(monkeypatch):
    pair = fake_pair()
    del pair["owners"][1]["driver_snapshot"]
    fake_supervisor(monkeypatch, pair)
    report = trade.run_probe(trade.parse_args(cli()))
    assert report["complete"] is False
    assert report["errors"]


def test_readiness_is_monotonic_per_owner_and_snapshots_are_detached():
    import multiprocessing

    raw = multiprocessing.get_context("spawn").RawArray("B", 16)
    first, second = ordinary._PeerReadiness(raw, 0), ordinary._PeerReadiness(raw, 1)
    before = second.snapshot()
    for phase in ordinary.READINESS_PHASES:
        assert not second.peer_ready(phase)
        first.publish_ready(phase)
        first.publish_ready(phase)
        assert second.peer_ready(phase)
        assert not first.peer_ready(phase)
    assert list(raw) == [1] * 8 + [0] * 8
    assert not any(before["peer"].values())
    detached = second.snapshot()
    detached["peer"][ordinary.READINESS_PHASES[0]] = False
    assert second.peer_ready(ordinary.READINESS_PHASES[0])
