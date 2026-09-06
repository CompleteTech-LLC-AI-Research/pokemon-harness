"""Authored control-only checks for explicit owner readiness phase schemas."""

import json
import multiprocessing
import time

import pytest

from scripts import probe_timed_rom_pair as probe

TRADE_PHASES = (
    "party_qualified",
    "link_menu_trade_ready",
    "link_menu_a_applied",
    "trade_center_reached",
    "select_mon_ready",
    "outgoing_slot_ready",
    "pre_evolution_copy_validated",
    "post_save_cycle_returned",
)
DRIVER = "tests.test_probe_owner_phases:authored_driver"


def arguments():
    args = probe.parse_args(
        [
            "--owner-mode=process",
            "--listener-chunk=1",
            "--connector-chunk=1",
            "--operation-timeout=5",
            "--rearm-budget=4096",
            "--rearm-instruction-cap=1024",
            "--max-edge-lateness=4096",
            "--overall-timeout=12",
            "--pair-timeout=8",
            "--cleanup-timeout=3",
            "--output=/tmp/unused-owner-phase-report.json",
        ]
    )
    args.owner_driver_options = [{"authored_side": 0}, {"authored_side": 1}]
    return args


class NoStartup:
    def __getattr__(self, name):
        pytest.fail(f"malformed phases reached process context: {name}")


@pytest.mark.parametrize(
    "phases",
    [
        (),
        [],
        ["phase"],
        "phase",
        {"phase"},
        {"phase": 1},
        1,
        True,
        ("",),
        ("   ",),
        ("\t\n",),
        ("same", "same"),
        (None,),
        (False,),
        (1,),
        (b"phase",),
        ("x" * 65,),
        tuple(f"phase-{index}" for index in range(33)),
    ],
)
def test_malformed_phases_fail_before_context_spawn_or_assets(monkeypatch, phases):
    def forbidden(*args, **kwargs):
        pytest.fail("malformed phases reached asset or process startup")

    monkeypatch.setattr(probe, "resolve_assets", forbidden)
    monkeypatch.setattr(probe.multiprocessing, "get_context", forbidden)
    with pytest.raises(ValueError):
        probe.run_process_pair(
            arguments(),
            context=NoStartup(),
            child_target=forbidden,
            owner_driver=DRIVER,
            owner_driver_phases=phases,
        )


def test_custom_phases_without_driver_fail_before_startup():
    with pytest.raises(ValueError):
        probe.run_process_pair(arguments(), context=NoStartup(), owner_driver_phases=("ready",))


@pytest.mark.parametrize("driver,phases", [(None, None), (DRIVER, None), (DRIVER, ("custom",))])
def test_early_budget_report_preserves_effective_phase_schema_without_resources(driver, phases):
    args = arguments()
    args.absolute_deadline = time.monotonic() - 1
    report = probe.run_process_pair(
        args,
        context=NoStartup(),
        owner_driver=driver,
        owner_driver_phases=phases,
    )
    assert report["stop_reason"] == "insufficient_remaining_budget"
    if driver is None:
        assert "owner_driver" not in report
    else:
        assert report["owner_driver"] == driver
    assert report["owner_driver_phases"] == list(TRADE_PHASES if phases is None else phases)


def test_custom_readiness_keeps_order_and_rejects_unknown_phase():
    phases = ("z-last", "a-first")
    raw = multiprocessing.get_context("spawn").RawArray("B", 4)
    first = probe._PeerReadiness(raw, 0, phases=phases)
    second = probe._PeerReadiness(raw, 1, phases=phases)
    assert first.phases == phases
    assert isinstance(first.phases, tuple)
    first.publish_ready("a-first")
    assert list(raw) == [0, 1, 0, 0]
    assert second.peer_ready("a-first")
    assert not second.peer_ready("z-last")
    assert tuple(first.snapshot()["own"]) == phases
    with pytest.raises(ValueError):
        first.publish_ready("unknown")
    with pytest.raises(ValueError):
        second.peer_ready("unknown")
    assert list(raw) == [0, 1, 0, 0]


def test_none_preserves_trade_phase_names_and_default_readiness_schema():
    assert probe.READINESS_PHASES == TRADE_PHASES
    raw = multiprocessing.get_context("spawn").RawArray("B", 16)
    first, second = probe._PeerReadiness(raw, 0), probe._PeerReadiness(raw, 1)
    assert list(first.snapshot()["own"]) == list(TRADE_PHASES)
    for phase in TRADE_PHASES:
        first.publish_ready(phase)
        assert second.peer_ready(phase)
    assert list(raw) == [1] * 8 + [0] * 8


def authored_driver(**kwargs):
    raise AssertionError("control-only child must never construct a game driver")


def authored_phase_child(
    options,
    index,
    sock,
    cancel,
    done,
    barrier,
    deadline,
    overall,
    sender,
    stderr_path,
    *driver_args,
):
    """Exercise serialized readiness using real spawn, without an emulator."""
    record = {
        "side": ("listener", "connector")[index],
        "calls": [],
        "cleanup": [],
        "errors": [],
        "final": {"authored": True},
    }
    try:
        assert len(driver_args) == 5
        path, readiness, goal, goal_stop, driver_options = driver_args
        assert path == DRIVER
        assert driver_options == options["owner_driver_options"][index]
        phases = tuple(options["authored_expected_phases"])
        original = readiness.snapshot()
        assert tuple(original["own"]) == tuple(original["peer"]) == phases
        for phase in phases:
            readiness.publish_ready(phase)
            readiness.publish_ready(phase)
        barrier.wait(timeout=max(0.01, deadline - time.monotonic()))
        assert all(readiness.peer_ready(phase) for phase in phases)
        assert not any(original["own"].values())
        copied = readiness.snapshot()
        copied["own"][phases[0]] = False
        assert readiness.snapshot()["own"][phases[0]] is True
        record["phase_snapshot"] = readiness.snapshot()
        goal.set()
        assert cancel.wait(max(0, deadline - time.monotonic()))
        assert goal_stop.is_set()
        record["termination"] = "goal_cancelled"
    except (AssertionError, RuntimeError, ValueError, TimeoutError) as exc:
        record["errors"].append(f"{type(exc).__name__}: {exc}")
        done.set()
    finally:
        sender.send_bytes(json.dumps(record).encode())
        sender.close()
        sock.close()


class RecordingSpawnContext:
    def __init__(self):
        self.context = multiprocessing.get_context("spawn")
        self.arrays = []

    def __getattr__(self, name):
        return getattr(self.context, name)

    def RawArray(self, code, size):
        self.arrays.append((code, size))
        return self.context.RawArray(code, size)

    def Event(self):
        pytest.fail("readiness must not depend on multiprocessing Event locks")


@pytest.mark.parametrize(
    "phases",
    [
        None,
        ("battle_ready",),
        ("z-last", "a-first"),
        ("x" * 64,),
        tuple(f"phase-{i}" for i in range(32)),
    ],
)
def test_spawn_serializes_and_reports_exact_owner_phase_schema(phases):
    args = arguments()
    expected = TRADE_PHASES if phases is None else phases
    args.authored_expected_phases = list(expected)
    context = RecordingSpawnContext()
    report = probe.run_process_pair(
        args,
        context=context,
        child_target=authored_phase_child,
        owner_driver=DRIVER,
        owner_driver_phases=phases,
    )
    assert report["supervisor_cancel_errors"] == []
    assert report["processes_alive"] == report["report_readers_alive"] == []
    assert report["stop_reason"] == "both_owner_goals"
    assert report["owner_goals"] == [True, True]
    assert report["owner_driver"] == DRIVER
    assert tuple(report["owner_driver_phases"]) == expected
    assert context.arrays == [("B", 2 * len(expected))]
    for owner in report["owners"]:
        assert owner["errors"] == []
        assert owner["exitcode"] == 0
        assert not owner["forced_termination"]
        assert tuple(owner["phase_snapshot"]["own"]) == expected
        assert all(owner["phase_snapshot"]["own"].values())
        assert all(owner["phase_snapshot"]["peer"].values())
