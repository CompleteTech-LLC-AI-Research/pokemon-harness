"""ROM-free CLI/supervisor contracts; mocked evidence is not gameplay proof."""

import hashlib
import inspect
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

from scripts import probe_timed_battle_pair as battle

pytestmark = pytest.mark.unit
MOCK_PHASES = ("mock_battle_prepared", "mock_turn_settled", "mock_room_returned")


def cli(**overrides):
    options = {
        "listener": "red_color",
        "connector": "yellow",
        "listener-move": "1",
        "connector-move": "4",
        "frame-limit": "120",
        "overall-timeout": "30",
        "pair-timeout": "20",
        "operation-timeout": "5",
        "rearm-budget": "4096",
        "rearm-instruction-cap": "1024",
        "max-edge-lateness": "4096",
        "output": "/tmp/unused-mocked-battle-report.json",
    }
    options.update(overrides)
    return [f"--{key}={value}" for key, value in options.items() if value is not None]


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
        "output",
    ],
)
def test_required_explicit_bounds_moves_and_output(option):
    with pytest.raises(SystemExit) as caught:
        battle.parse_args(cli(**{option: None}))
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "option", ["overall-timeout", "pair-timeout", "operation-timeout", "cleanup-timeout"]
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_wall_budgets_are_finite_positive(option, value):
    with pytest.raises(SystemExit):
        battle.parse_args(cli(**{option: value}))


@pytest.mark.parametrize(
    "option", ["frame-limit", "rearm-budget", "rearm-instruction-cap", "max-edge-lateness"]
)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "inf"])
def test_integer_budgets_are_positive(option, value):
    with pytest.raises(SystemExit):
        battle.parse_args(cli(**{option: value}))


@pytest.mark.parametrize("value", ["1", "4.9", "5.1", "6"])
def test_operation_budget_is_exactly_five_seconds(value):
    with pytest.raises(SystemExit):
        battle.parse_args(cli(**{"operation-timeout": value}))


@pytest.mark.parametrize("value", ["30", "31"])
def test_cleanup_must_fit_overall_budget(value):
    with pytest.raises(SystemExit):
        battle.parse_args(cli(**{"cleanup-timeout": value}))


@pytest.mark.parametrize("option", ["listener-move", "connector-move"])
@pytest.mark.parametrize("value", ["0", "5", "-1", "1.5", "nan", "inf"])
def test_moves_are_one_based_slots_one_through_four(option, value):
    with pytest.raises(SystemExit):
        battle.parse_args(cli(**{option: value}))


@pytest.mark.parametrize("listener,connector", [(1, 4), (4, 1), (2, 3)])
def test_move_slots_normalize_once(listener, connector):
    args = battle.parse_args(cli(**{"listener-move": listener, "connector-move": connector}))
    assert (args.listener_move, args.connector_move) == (listener, connector)
    assert (args.listener_move_index, args.connector_move_index) == (listener - 1, connector - 1)


def test_omitted_moves_delegate_automatic_selection():
    args = battle.parse_args(cli(**{"listener-move": None, "connector-move": None}))
    assert args.listener_move_index is args.connector_move_index is None


@pytest.mark.parametrize("version", ["red_color", "blue_color", "yellow"])
def test_canonical_ordinary_versions_are_accepted(version):
    args = battle.parse_args(cli(listener=version, connector=version))
    assert args.listener == args.connector == version


@pytest.mark.parametrize("option", ["listener", "connector"])
@pytest.mark.parametrize("version", ["red", "blue", "yellow_color", "colosseum2"])
def test_noncanonical_versions_are_rejected(option, version):
    with pytest.raises(SystemExit):
        battle.parse_args(cli(**{option: version}))


def test_default_is_room_return_and_execution_is_fixed():
    args = battle.parse_args(cli())
    assert args.checkpoint == "room-return"
    assert args.owner_mode == "process"
    assert args.listener_chunk == args.connector_chunk == 1
    assert args.call_retention == "stream"
    assert args.rom_milestones is True
    assert args.operation_timeout == 5
    assert battle.probe.QUANTUM_CYCLES == 256


def test_settled_turn_is_an_explicit_checkpoint():
    assert battle.parse_args(cli(checkpoint="settled-turn")).checkpoint == "settled-turn"


@pytest.mark.parametrize(
    "extra",
    [
        ["--owner-mode=thread"],
        ["--listener-chunk=2"],
        ["--connector-chunk=2"],
        ["--call-retention=inline"],
        ["--quantum=512"],
        ["--owner-driver=untrusted.module:factory"],
        ["--checkpoint=select-mon"],
    ],
)
def test_cli_cannot_weaken_execution_or_select_other_driver(extra):
    with pytest.raises(SystemExit):
        battle.parse_args(cli() + extra)


def test_output_cannot_be_inside_source_checkout():
    with pytest.raises(SystemExit):
        battle.parse_args(cli(output=battle.ROOT / "unused-mocked-report.json"))


@pytest.mark.parametrize("symlink", [False, True])
def test_output_cannot_enter_asset_checkout(tmp_path, symlink):
    checkout = tmp_path / "assets"
    checkout.mkdir()
    destination = checkout
    if symlink:
        destination = tmp_path / "alias"
        destination.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(SystemExit):
        battle.parse_args(cli(output=destination / "report.json", **{"repo-root": checkout}))
    assert not (checkout / "report.json").exists()


@pytest.mark.parametrize("git_file", [False, True])
def test_output_cannot_enter_an_unrelated_git_checkout(tmp_path, git_file):
    checkout = tmp_path / "other"
    checkout.mkdir()
    marker = checkout / ".git"
    if git_file:
        marker.write_text("gitdir: /unused/mock-git-directory\n")
    else:
        marker.mkdir()
    with pytest.raises(SystemExit):
        battle.parse_args(cli(output=checkout / "report.json"))


def fake_pair(tmp_path):
    """Author transport evidence only: these snapshots contain no game state."""
    owners = []
    for side in ("listener", "connector"):
        payload = (
            json.dumps(
                {
                    "status": "completed",
                    "requested_frames": 1,
                    "actual_completed_frames": 1,
                }
            ).encode()
            + b"\n"
        )
        path = tmp_path / f"{side}-calls.jsonl"
        path.write_bytes(payload)
        owners.append(
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
                "driver_snapshot": {
                    "role": "listen" if side == "listener" else "connect",
                    "version": "red_color" if side == "listener" else "yellow",
                    "checkpoint": "room-return",
                    "move_slot": 0 if side == "listener" else 3,
                    "mock_contract_only": True,
                },
                "call_counts": {"total": 1, "noncompleted": 0},
                "call_log": {
                    "path": str(path),
                    "bytes": len(payload),
                    "record_count": 1,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "complete": True,
                    "error": None,
                },
            }
        )
    return {
        "owner_driver": battle.DRIVER,
        "owner_driver_phases": MOCK_PHASES,
        "stop_reason": "both_owner_goals",
        "owner_goals": [True, True],
        "goal_stop": True,
        "threads_alive": [],
        "processes_alive": [],
        "report_readers_alive": [],
        "supervisor_cancel_errors": [],
        "owners": owners,
    }


@pytest.fixture
def supervisor(monkeypatch, tmp_path):
    pair, calls, judgments = fake_pair(tmp_path), [], []
    objective = {"gameplay_completed": True, "errors": [], "full_authentic_acceptance": True}

    def run(args, *, owner_driver, owner_driver_phases):
        calls.append((deepcopy(vars(args)), owner_driver, owner_driver_phases))
        result = deepcopy(pair)
        if (args.listener, args.connector) == ("yellow", "red_color"):
            left, right = [owner["driver_snapshot"] for owner in result["owners"]]
            for key in ("version", "move_slot"):
                left[key], right[key] = right[key], left[key]
        return result

    def adjudicate(snapshots, checkpoint):
        judgments.append((deepcopy(snapshots), checkpoint))
        return deepcopy(objective)

    # The helper is owned separately and may not exist yet. This authored module
    # tests lazy delegation, not any real phase names or gameplay adjudication.
    helper = ModuleType("scripts._timed_battle_probe")
    helper.READINESS_PHASES = MOCK_PHASES
    helper.adjudicate_pair = adjudicate
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    # Replace the execution boundary, never launch owners or patch readiness globals.
    monkeypatch.setattr(battle.probe, "run_process_pair", run)
    return pair, calls, judgments, objective


@pytest.mark.parametrize("checkpoint", ["room-return", "settled-turn"])
def test_driver_specific_phases_and_objective_are_delegated(supervisor, checkpoint):
    from scripts._timed_battle_probe import READINESS_PHASES

    pair, calls, judgments, _ = supervisor
    for owner in pair["owners"]:
        owner["driver_snapshot"]["checkpoint"] = checkpoint
    original_phases = battle.probe.READINESS_PHASES
    report = battle.run_probe(battle.parse_args(cli(checkpoint=checkpoint)))
    assert len(calls) == 1
    options, driver, phases = calls[0]
    assert driver == "scripts._timed_battle_probe:create_owner_driver"
    assert phases == READINESS_PHASES
    assert options["owner_driver_phases"] == READINESS_PHASES
    assert phases != original_phases
    assert battle.probe.READINESS_PHASES is original_phases
    assert options["owner_driver_options"] == [
        {"version": "red_color", "move_slot": 0, "checkpoint": checkpoint},
        {"version": "yellow", "move_slot": 3, "checkpoint": checkpoint},
    ]
    assert options["listener_chunk"] == options["connector_chunk"] == 1
    assert options["call_retention"] == "stream"
    assert judgments == [([owner["driver_snapshot"] for owner in pair["owners"]], checkpoint)]
    assert report["gameplay_completed"] is True
    assert report["full_authentic_acceptance"] is False
    assert report["complete"] is False
    assert report["source_provenance"] == "unknown_original_playthrough"
    assert report["pairs"][0]["objective"]["full_authentic_acceptance"] is False


def test_reversing_roles_keeps_moves_with_versions_and_one_deadline(supervisor):
    _, calls, _, _ = supervisor
    report = battle.run_probe(battle.parse_args(cli() + ["--both-orientations"]))
    assert len(calls) == 2
    forward, reverse = calls[0][0], calls[1][0]
    assert (reverse["listener"], reverse["connector"]) == ("yellow", "red_color")
    assert (reverse["listener_move"], reverse["connector_move"]) == (4, 1)
    assert (reverse["listener_move_index"], reverse["connector_move_index"]) == (3, 0)
    assert reverse["owner_driver_options"] == list(reversed(forward["owner_driver_options"]))
    assert reverse["absolute_deadline"] == forward["absolute_deadline"]
    assert report["gameplay_completed"] is True


@pytest.mark.parametrize("shape", ["missing", "kwargs_only", "positional_only"])
def test_unsupported_supervisor_fails_before_launch(monkeypatch, shape):
    launches = []

    def missing(args, *, owner_driver):
        launches.append(args)

    def kwargs_only(args, **kwargs):
        launches.append(args)

    def positional_only(args, owner_driver_phases, /, *, owner_driver):
        launches.append(args)

    run = {"missing": missing, "kwargs_only": kwargs_only, "positional_only": positional_only}[
        shape
    ]
    parameter = inspect.signature(run).parameters.get("owner_driver_phases")
    assert parameter is None or parameter.kind is inspect.Parameter.POSITIONAL_ONLY
    monkeypatch.setattr(battle.probe, "run_process_pair", run)
    # A helper import would fail, so the specific signature error also proves
    # that unsupported supervisors are rejected before importing the helper.
    monkeypatch.setitem(sys.modules, "scripts._timed_battle_probe", None)
    with pytest.raises(ValueError, match="owner_driver_phases"):
        battle.run_probe(battle.parse_args(cli()))
    assert launches == []


@pytest.mark.parametrize(
    "phases",
    [
        (),
        ["battle_ready"],
        ("duplicate", "duplicate"),
        ("",),
        (" \t",),
        (1,),
        ("x" * 65,),
        tuple(f"phase_{index}" for index in range(33)),
    ],
)
def test_invalid_helper_phase_contract_fails_before_launch(supervisor, phases):
    _, calls, judgments, _ = supervisor
    # This is the authored helper stub, never the supervisor's readiness globals.
    sys.modules["scripts._timed_battle_probe"].READINESS_PHASES = phases
    with pytest.raises(ValueError):
        battle.run_probe(battle.parse_args(cli()))
    assert calls == []
    assert judgments == []


@pytest.mark.parametrize(
    "error", ["HP reciprocity failed", "status reciprocity failed", "PP delta failed"]
)
def test_objective_rejection_cannot_be_overridden_by_clean_supervisor(supervisor, error):
    _, _, _, objective = supervisor
    objective.update(gameplay_completed=False, errors=[error])
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert error in report["errors"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("alive", True),
        ("forced_termination", True),
        ("actual_missing", True),
        ("watcher_alive", True),
        ("exitcode", 1),
        ("errors", ["cleanup failed"]),
        ("cleanup", ["endpoint_detached"]),
        ("termination", "frame_bound"),
        ("local_goal", False),
        ("milestone_observer", "mock_global_alias"),
        ("stderr", {"drainer_alive": True}),
    ],
)
def test_owner_lifecycle_failure_cannot_be_success(supervisor, field, value):
    pair, _, _, _ = supervisor
    pair["owners"][0][field] = value
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert report["errors"]


@pytest.mark.parametrize("gameplay_completed,exit_code", [(True, 0), (False, 2)])
def test_main_persists_gameplay_separately_from_full_acceptance(
    supervisor,
    tmp_path,
    gameplay_completed,
    exit_code,
):
    _, _, _, objective = supervisor
    objective["gameplay_completed"] = gameplay_completed
    output = tmp_path / "report.json"
    assert battle.main(cli(output=output)) == exit_code
    saved = json.loads(output.read_text())
    assert saved["gameplay_completed"] is gameplay_completed
    assert saved["complete"] is False
    assert saved["full_authentic_acceptance"] is False
    assert saved["source_provenance"] == "unknown_original_playthrough"


def test_existing_output_is_preserved_before_supervisor_launch(supervisor, tmp_path):
    _, calls, _, _ = supervisor
    output = tmp_path / "report.json"
    output.write_text("previous evidence\n")
    with pytest.raises(FileExistsError):
        battle.main(cli(output=output))
    assert output.read_text() == "previous evidence\n"
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("stop_reason", "deadline"),
        ("owner_goals", [True, False]),
        ("goal_stop", False),
        ("processes_alive", ["listener"]),
        ("threads_alive", ["watcher"]),
        ("report_readers_alive", ["reader"]),
        ("supervisor_cancel_errors", ["close failed"]),
    ],
)
def test_supervisor_failure_cannot_be_success(supervisor, field, value):
    pair, calls, _, _ = supervisor
    pair[field] = value
    report = battle.run_probe(battle.parse_args(cli() + ["--both-orientations"]))
    assert report["gameplay_completed"] is False
    assert report["errors"]
    if field.endswith("_alive"):
        assert len(calls) == 1


@pytest.mark.parametrize("failure", ["missing", "hash", "incomplete", "counts"])
def test_bad_call_ledger_cannot_be_success(supervisor, failure):
    pair, _, _, _ = supervisor
    owner = pair["owners"][0]
    if failure == "missing":
        del owner["call_log"]
    elif failure == "hash":
        owner["call_log"]["sha256"] = "0" * 64
    elif failure == "incomplete":
        owner["call_log"]["complete"] = False
    else:
        owner["call_counts"]["total"] = 2
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert report["errors"]


@pytest.mark.parametrize(
    "call",
    [
        {"status": "completed", "requested_frames": 2, "actual_completed_frames": 2},
        {"status": "completed", "requested_frames": 1, "actual_completed_frames": 0},
        {"status": "completed", "requested_frames": 1},
        {"status": "completed", "requested_frames": 1, "actual_completed_frames": True},
        {
            "status": "interrupted",
            "requested_frames": 1,
            "actual_completed_frames": 0,
            "error": "Deadline: expired",
        },
    ],
)
def test_self_consistent_ledger_cannot_hide_bad_whole_frame_evidence(supervisor, call):
    pair, _, _, _ = supervisor
    owner = pair["owners"][0]
    payload = json.dumps(call).encode() + b"\n"
    manifest = owner["call_log"]
    Path(manifest["path"]).write_bytes(payload)
    manifest.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    owner["call_counts"]["noncompleted"] = int(call["status"] != "completed")
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert report["errors"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_mode", "thread"),
        ("listener_chunk", 2),
        ("connector_chunk", 2),
        ("call_retention", "inline"),
        ("rom_milestones", False),
        ("operation_timeout", 6),
        ("input_profile", "none"),
    ],
)
def test_programmatic_settings_cannot_weaken_contract_before_launch(supervisor, field, value):
    _, calls, _, _ = supervisor
    args = battle.parse_args(cli())
    setattr(args, field, value)
    with pytest.raises(ValueError):
        battle.run_probe(args)
    assert calls == []


def test_missing_driver_snapshot_cannot_be_success(supervisor):
    pair, _, _, _ = supervisor
    del pair["owners"][1]["driver_snapshot"]
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert report["errors"]


def test_adjudication_exception_cannot_be_success(supervisor, monkeypatch):
    def reject(*args):
        raise ValueError("mock objective unavailable")

    monkeypatch.setattr(battle, "adjudicate_pair", reject)
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert any("mock objective unavailable" in error for error in report["errors"])


def test_adjudication_exception_preserves_prior_cleanup_errors(supervisor, monkeypatch):
    pair, _, _, _ = supervisor
    pair["owners"][0]["cleanup"].remove("session_closed_without_save")
    adjudications = []

    def reject(snapshots, checkpoint):
        adjudications.append(checkpoint)
        raise ValueError("mock later adjudication failure")

    monkeypatch.setattr(battle, "adjudicate_pair", reject)
    report = battle.run_probe(battle.parse_args(cli()))
    assert adjudications == ["room-return"]
    assert report["gameplay_completed"] is False
    assert "listener cleanup evidence is incomplete" in report["errors"]
    assert any("ValueError: mock later adjudication failure" in error for error in report["errors"])


@pytest.mark.parametrize(
    "case",
    [
        "missing_driver",
        "wrong_driver",
        "missing_phases",
        "wrong_phases",
        "string_phases",
        "null_phases",
        "outer_side",
        "outer_order",
        "snapshot_role",
        "snapshot_version",
        "snapshot_checkpoint",
        "missing_snapshot_role",
        "missing_snapshot_version",
        "missing_snapshot_checkpoint",
        "side_only",
        "contradictory_role_side",
        "move_wrong",
        "move_bool",
        "move_float",
        "move_missing",
        "chosen_wrong",
        "chosen_bool",
        "chosen_disagrees",
        "direct_disagrees",
    ],
)
def test_identity_mismatch_is_rejected_before_adjudication(supervisor, case):
    pair, _, judgments, _ = supervisor
    snapshot = pair["owners"][0]["driver_snapshot"]
    if case == "missing_driver":
        del pair["owner_driver"]
    elif case == "wrong_driver":
        pair["owner_driver"] = "scripts._timed_trade_probe:create_owner_driver"
    elif case == "missing_phases":
        del pair["owner_driver_phases"]
    elif case.endswith("phases"):
        pair["owner_driver_phases"] = {
            "wrong_phases": ("trade_ready",),
            "string_phases": str(MOCK_PHASES),
            "null_phases": None,
        }[case]
    elif case == "outer_side":
        pair["owners"][0]["side"] = "listen"
    elif case == "outer_order":
        pair["owners"].reverse()
    elif case == "side_only":
        snapshot["side"] = snapshot.pop("role")
    elif case == "contradictory_role_side":
        snapshot["side"] = "connect"
    elif case.startswith("missing_snapshot_"):
        del snapshot[case.removeprefix("missing_snapshot_")]
    elif case.startswith("snapshot_"):
        key = case.removeprefix("snapshot_")
        snapshot[key] = {"role": "listener", "version": "yellow", "checkpoint": "settled-turn"}[key]
    elif case in ("move_wrong", "move_bool", "move_float"):
        snapshot["move_slot"] = {"move_wrong": 1, "move_bool": False, "move_float": 0.0}[case]
    elif case == "move_missing":
        del snapshot["move_slot"]
    elif case in ("chosen_wrong", "chosen_bool"):
        del snapshot["move_slot"]
        snapshot["chosen"] = {"slot": 1 if case == "chosen_wrong" else False}
    elif case == "chosen_disagrees":
        snapshot["chosen"] = {"slot": 1}
    elif case == "direct_disagrees":
        snapshot["move_slot"] = 1
        snapshot["chosen"] = {"slot": 0}
    report = battle.run_probe(battle.parse_args(cli()))
    assert judgments == []
    assert report["gameplay_completed"] is False
    assert report["errors"]


@pytest.mark.parametrize("representation", ["nested", "both"])
def test_explicit_move_accepts_matching_nested_chosen_slot(supervisor, representation):
    pair, _, judgments, _ = supervisor
    for owner in pair["owners"]:
        snapshot = owner["driver_snapshot"]
        snapshot["chosen"] = {"slot": snapshot["move_slot"]}
        if representation == "nested":
            del snapshot["move_slot"]
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is True
    assert len(judgments) == 1


def test_optional_matching_snapshot_side_is_accepted(supervisor):
    pair, _, judgments, _ = supervisor
    for owner in pair["owners"]:
        snapshot = owner["driver_snapshot"]
        snapshot["side"] = snapshot["role"]
    assert battle.run_probe(battle.parse_args(cli()))["gameplay_completed"] is True
    assert len(judgments) == 1


@pytest.mark.parametrize("representation", ["list", "tuple"])
def test_recorded_phases_require_matching_list_or_tuple(supervisor, representation):
    pair, _, judgments, _ = supervisor
    if representation == "list":
        pair["owner_driver_phases"] = list(MOCK_PHASES)
    else:
        pair["owner_driver_phases"] = MOCK_PHASES
    assert battle.run_probe(battle.parse_args(cli()))["gameplay_completed"] is True
    assert len(judgments) == 1


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_gameplay_completion_must_be_an_explicit_bool(supervisor, value):
    _, _, judgments, objective = supervisor
    objective.update(gameplay_completed=value, complete=True)
    report = battle.run_probe(battle.parse_args(cli()))
    assert len(judgments) == 1
    assert report["gameplay_completed"] is False
    assert report["errors"]


def test_legacy_complete_cannot_replace_missing_gameplay_completed(supervisor):
    _, _, _, objective = supervisor
    del objective["gameplay_completed"]
    objective["complete"] = True
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert report["errors"]


@pytest.mark.parametrize("value", [None, "", (), False])
def test_adjudicator_errors_must_be_a_list(supervisor, value):
    _, _, _, objective = supervisor
    objective["errors"] = value
    report = battle.run_probe(battle.parse_args(cli()))
    assert report["gameplay_completed"] is False
    assert report["errors"]


@pytest.mark.parametrize("checkpoint", ["settled-turn", "room-return"])
def test_entry_with_real_helper_accepts_synthetic_pair_then_rejects_corrupt_hp(
    monkeypatch,
    tmp_path,
    checkpoint,
):
    # Deliberately do not use the supervisor fixture: its module stub cannot
    # establish compatibility with the separately owned helper's real schema.
    from scripts import _timed_battle_probe as helper
    from tests.test_timed_battle_probe import paired_snapshots

    snapshots = paired_snapshots(room=checkpoint == "room-return")
    pair = fake_pair(tmp_path)
    pair["owner_driver_phases"] = helper.READINESS_PHASES
    for owner, snapshot in zip(pair["owners"], snapshots, strict=True):
        owner["driver_snapshot"] = snapshot
    calls = []

    def run(args, *, owner_driver, owner_driver_phases):
        assert owner_driver == battle.DRIVER
        assert owner_driver_phases == helper.READINESS_PHASES
        calls.append(args)
        return deepcopy(pair)

    # The only replacement is the process boundary. Production entry,
    # identity checks, ledger validation and helper adjudication all execute.
    monkeypatch.setattr(battle.probe, "run_process_pair", run)
    options = cli(checkpoint=checkpoint, **{"connector-move": "1"})
    valid = battle.run_probe(battle.parse_args(options))
    assert valid["gameplay_completed"] is True, valid["errors"]
    assert valid["full_authentic_acceptance"] is False
    assert valid["complete"] is False
    assert valid["pairs"][0]["objective"]["gameplay_completed"] is True

    snapshots[0]["turn"]["enemy"]["hp"] -= 1
    invalid = battle.run_probe(battle.parse_args(options))
    assert invalid["gameplay_completed"] is False
    assert invalid["full_authentic_acceptance"] is False
    assert invalid["pairs"][0]["objective"]["gameplay_completed"] is False
    assert invalid["errors"]
    assert len(calls) == 2
