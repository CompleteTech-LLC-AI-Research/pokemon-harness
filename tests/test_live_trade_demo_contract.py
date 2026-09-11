"""ROM-free lifecycle and ownership contracts for the live trade demo."""

from __future__ import annotations

import ast
import builtins
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "live_trade_demo.py"
_SPEC = importlib.util.spec_from_file_location("live_trade_demo_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
demo = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = demo
_SPEC.loader.exec_module(demo)


class _FakeSymbols(dict):
    def addr_of(self, _symbol: str) -> int:
        return 0


class _FakeSession:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.symbols = _FakeSymbols()
        self._pyboy = SimpleNamespace(memory={0: 0})
        self.events = events if events is not None else []
        self.close_error = close_error
        self.map_id = 0

    def press(self, *_args, **_kwargs) -> None:
        pass

    def step(self, *_args, **_kwargs):
        raise AssertionError("attached sessions must be stepped through the pair owner")

    def close(self) -> None:
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error

    def read_game_state(self):
        return SimpleNamespace(
            overworld=SimpleNamespace(map_id=self.map_id, x=0, y=0),
        )


class _FakeLink:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        detach_error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, int]] = []
        self.events = events if events is not None else []
        self.detach_error = detach_error

    def step_interleaved(self, frames: int) -> None:
        self.calls.append(("step_interleaved", frames))

    def step(self, frames: int) -> None:
        self.calls.append(("step", frames))

    def detach_all(self) -> None:
        self.events.append("detach_all")
        if self.detach_error is not None:
            raise self.detach_error


class _MainFakeLink(_FakeLink):
    def attach(self, _pyboy) -> None:
        self.events.append("attach")


def test_pair_driver_has_no_direct_attached_session_step_calls() -> None:
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"), filename=str(_SCRIPT))

    direct_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "step"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"a", "b"}
    ]

    assert not direct_calls


def test_link_menu_driver_uses_pair_owner_for_coarse_frames() -> None:
    a = _FakeSession()
    b = _FakeSession()
    link = _FakeLink()

    result = demo._drive_two_sessions_to_link_menu(
        a,
        b,
        link,
        total_frames=60,
        frames_per_attempt=20,
    )

    assert result["frames_used"] == 60
    assert link.calls == [
        ("step_interleaved", 20),
        ("step_interleaved", 20),
        ("step_interleaved", 20),
    ]


def test_trade_driver_uses_pair_owner_for_every_budgeted_frame() -> None:
    a = _FakeSession()
    b = _FakeSession()
    link = _FakeLink()
    counters = {
        key: [0, 0]
        for key in (
            "_AddEnemyMonToPlayerParty",
            "TradeCenter_Trade",
            "CableClub_DoBattleOrTrade",
            "TradeCenter_SelectMon.selectStatsMenuItem",
            "TradeCenter_SelectMon.selectTradeMenuItem",
            "TradeCenter_SelectMon.playerMonMenu_HandleInput",
        )
    }

    result = demo._drive_complete_trade(
        a,
        b,
        link,
        counters=counters,
        trade_budget_frames=0,
        step_frames=20,
    )

    assert result["trade_phase_frames"] == 0
    assert all(method in {"step", "step_interleaved"} for method, _ in link.calls)
    assert sum(frames for _method, frames in link.calls) == 4 * 20 + 1800
    assert all(frames == 20 for _method, frames in link.calls)


def test_pair_startup_rolls_back_first_session_when_second_open_fails(monkeypatch) -> None:
    events: list[str] = []
    first = _FakeSession(events=events)

    def open_session(version: str, **_kwargs):
        if version == "blue":
            raise RuntimeError("second session failed")
        return first

    monkeypatch.setattr(demo, "_open_session", open_session)

    with pytest.raises(RuntimeError, match="second session failed"):
        demo._open_pair_sessions("red", "blue", view=False)

    assert events == ["close"]


def test_pair_teardown_detaches_owner_before_closing_peers() -> None:
    events: list[str] = []
    link = _FakeLink(events=events)
    a = _FakeSession(events=events)
    b = _FakeSession(events=events)

    demo._cleanup_pair(link, a, b)

    assert events == ["detach_all", "close", "close"]


def test_pair_cleanup_attempts_every_operation_and_raises_group() -> None:
    events: list[str] = []
    link = _FakeLink(events=events, detach_error=KeyboardInterrupt("detach failed"))
    a = _FakeSession(events=events, close_error=RuntimeError("A close failed"))
    b = _FakeSession(events=events, close_error=SystemExit("B close failed"))

    with pytest.raises(BaseExceptionGroup) as raised:
        demo._cleanup_pair(link, a, b)

    assert events == ["detach_all", "close", "close"]
    assert [type(error) for error in raised.value.exceptions] == [
        KeyboardInterrupt,
        RuntimeError,
        SystemExit,
    ]
    assert "link.detach_all" in str(raised.value.exceptions[0].__notes__)
    assert "session A.close" in str(raised.value.exceptions[1].__notes__)
    assert "session B.close" in str(raised.value.exceptions[2].__notes__)


def test_pair_cleanup_preserves_primary_error_and_records_cleanup_failures() -> None:
    events: list[str] = []
    link = _FakeLink(events=events, detach_error=RuntimeError("detach failed"))
    a = _FakeSession(events=events, close_error=RuntimeError("A close failed"))
    b = _FakeSession(events=events, close_error=RuntimeError("B close failed"))
    primary = ValueError("trade phase failed")

    with pytest.raises(ValueError, match="trade phase failed") as raised:
        try:
            raise primary
        except BaseException as active_error:
            demo._cleanup_pair(link, a, b, active_error=active_error)
            raise

    assert raised.value is primary
    assert events == ["detach_all", "close", "close"]
    notes = "\n".join(raised.value.__notes__)
    assert "pair cleanup failures:" in notes
    assert "link.detach_all" in notes
    assert "session A.close" in notes
    assert "session B.close" in notes


def test_main_rolls_back_pair_when_post_open_print_fails(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    a = _FakeSession(events=events)
    b = _FakeSession(events=events)
    args = SimpleNamespace(
        no_cgb=False,
        versions="red,blue",
        outdir=str(tmp_path),
        venv=None,
        view=True,
        sample_every=0,
        speed=0.5,
        hold_after_s=0,
        natural=False,
    )
    real_print = builtins.print

    def fail_on_dwell_message(*values, **kwargs):
        if values and str(values[0]).startswith("[info] dwell per driver"):
            raise RuntimeError("post-open print failed")
        return real_print(*values, **kwargs)

    monkeypatch.setattr(demo, "_parse_args", lambda: args)
    monkeypatch.setattr(demo, "_assert_fixtures_available", lambda _version: None)
    monkeypatch.setattr(
        demo,
        "_open_pair_sessions",
        lambda _version_a, _version_b, *, view: (a, b, []),
    )
    monkeypatch.setattr(builtins, "print", fail_on_dwell_message)

    with pytest.raises(RuntimeError, match="post-open print failed"):
        demo.main()

    assert events == ["close", "close"]


def _patch_main_dependencies(monkeypatch, tmp_path, *, add_mon):
    """Make ``main`` exercise its real result/finally path without ROMs."""
    import pokered_harness.link.pyboy_link_session as link_module

    events: list[str] = []
    a = _FakeSession(events=events)
    b = _FakeSession(events=events)
    link = _MainFakeLink(events=events)
    args = SimpleNamespace(
        no_cgb=False,
        versions="red,blue",
        outdir=str(tmp_path),
        venv=None,
        view=False,
        sample_every=0,
        speed=1.0,
        hold_after_s=0,
        natural=False,
    )

    class _FakePyBoyLinkSession:
        @classmethod
        def local(cls, *, view=False):
            assert view is False
            return link

    class _NoopShooter:
        def __init__(self, outdir):
            self.outdir = outdir

        def shoot_pair(self, _a, _b, stem):
            return [
                self.outdir / f"{stem}__red.png",
                self.outdir / f"{stem}__blue.png",
            ]

    monkeypatch.setattr(demo, "_parse_args", lambda: args)
    monkeypatch.setattr(demo, "_assert_fixtures_available", lambda _version: None)
    monkeypatch.setattr(
        demo,
        "_open_pair_sessions",
        lambda _version_a, _version_b, *, view: (a, b, []),
    )
    monkeypatch.setattr(demo, "Shooter", _NoopShooter)
    monkeypatch.setattr(link_module, "PyBoyLinkSession", _FakePyBoyLinkSession)
    monkeypatch.setattr(
        demo,
        "_drive_past_link_menu_to_trade_center",
        lambda *_args, **_kwargs: {
            "final_map_a": demo.TRADE_CENTER_MAP_ID,
            "final_map_b": demo.TRADE_CENTER_MAP_ID,
            "extra_frames": 0,
        },
    )
    monkeypatch.setattr(
        demo,
        "_drive_complete_trade",
        lambda *_args, **_kwargs: {"add_mon": add_mon},
    )

    if add_mon == [1, 1]:
        species = iter((1, 2, 2, 1))
        ot = iter((b"A", b"B", b"B", b"A"))
    else:
        species = iter((1, 1, 1, 1))
        ot = iter((b"A", b"A", b"A", b"A"))
    monkeypatch.setattr(demo, "_lead_species", lambda _session: next(species))
    monkeypatch.setattr(demo, "_lead_ot_fingerprint", lambda _session: next(ot))
    return events


def test_main_returns_zero_after_trade_and_runs_finally_cleanup(monkeypatch, tmp_path) -> None:
    events = _patch_main_dependencies(monkeypatch, tmp_path, add_mon=[1, 1])

    assert demo.main() == 0
    assert events == ["attach", "attach", "detach_all", "close", "close"]


def test_main_returns_one_after_incomplete_trade_and_runs_finally_cleanup(
    monkeypatch, tmp_path
) -> None:
    events = _patch_main_dependencies(monkeypatch, tmp_path, add_mon=[0, 0])

    assert demo.main() == 1
    assert events == ["attach", "attach", "detach_all", "close", "close"]
