"""Asset-free guardrails for the controlled Cable Club diagnostics."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FULL_TO_BROCK = SCRIPTS / "full_to_brock.py"
DIAGNOSTIC_SCRIPTS = (
    SCRIPTS / "live_battle_demo.py",
    SCRIPTS / "produce_vanilla_cable_club_after_brock.py",
    SCRIPTS / "transplant_stock_cable_club_fixture.py",
)


def _load_script(path: Path, monkeypatch):
    """Load a script module without invoking its CLI or a ROM session."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    name = f"diagnostic_safety_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", DIAGNOSTIC_SCRIPTS)
def test_controlled_scripts_are_labeled_and_cannot_skip_asset_pins(path):
    source = path.read_text(encoding="utf-8")

    assert source.startswith('"""Diagnostic-only')
    assert "VERSIONS.md" in source
    assert "POKERED_SKIP_SHA1" not in source
    assert "not human-valid" in source or "human-valid gameplay" in source


class _Closable:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.closed = False

    def close(self):
        self.closed = True
        if self.fail:
            raise RuntimeError("close failed")


@pytest.mark.parametrize(
    ("path", "helper_name"),
    [
        (SCRIPTS / "live_battle_demo.py", "_close_sessions"),
        (SCRIPTS / "transplant_stock_cable_club_fixture.py", "_close_sessions"),
    ],
)
def test_pair_cleanup_continues_after_first_close_failure(
    path, helper_name, monkeypatch, capsys
):
    module = _load_script(path, monkeypatch)
    first = _Closable(fail=True)
    second = _Closable()
    active_error = RuntimeError("operation failed")

    with pytest.raises(ExceptionGroup) as raised:
        getattr(module, helper_name)(
            ("first", first), ("second", second), active_error=active_error
        )

    assert first.closed
    assert second.closed
    assert raised.value.__cause__ is active_error
    assert len(raised.value.exceptions) == 1
    assert "diagnostic cleanup failed" in str(raised.value.exceptions[0].__notes__)
    assert "first" in str(raised.value.exceptions[0].__notes__)
    assert "diagnostic cleanup" in capsys.readouterr().err


def test_battle_setup_closes_first_peer_when_second_peer_setup_fails(monkeypatch):
    module = _load_script(SCRIPTS / "live_battle_demo.py", monkeypatch)
    first = _Closable(fail=True)

    def _open(version, *, view, window_pos):
        if version == "blue":
            raise RuntimeError("state load failed")
        return first

    monkeypatch.setattr(module, "_open_validated_session", _open)

    with pytest.raises(ExceptionGroup) as raised:
        module._open_battle_sessions("red", "blue", view=False)

    assert first.closed
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "state load failed"


def test_single_producer_cleanup_failure_is_raised(monkeypatch, capsys):
    module = _load_script(
        SCRIPTS / "produce_vanilla_cable_club_after_brock.py", monkeypatch
    )
    session = _Closable(fail=True)

    with pytest.raises(ExceptionGroup) as raised:
        module._close_session(session)

    assert session.closed
    assert len(raised.value.exceptions) == 1
    assert "vanilla producer session" in str(raised.value.exceptions[0].__notes__)
    assert "diagnostic cleanup" in capsys.readouterr().err


class _FakeOrchestratorSession:
    """Small lifecycle fake; no ROM or PyBoy construction is involved."""

    last = None
    fail_load = False

    @classmethod
    def from_files(cls, *args, **kwargs):
        del args, kwargs
        cls.last = cls()
        return cls.last

    def load_state(self, payload):
        del payload
        if type(self).fail_load:
            raise RuntimeError("start state load failed")

    def close(self):
        self.closed = True

    def __init__(self):
        self.closed = False


def test_full_to_brock_closes_session_after_stop_after_return(monkeypatch, tmp_path):
    module = _load_script(FULL_TO_BROCK, monkeypatch)
    _FakeOrchestratorSession.last = None
    _FakeOrchestratorSession.fail_load = False
    monkeypatch.setattr(module, "Session", _FakeOrchestratorSession)
    monkeypatch.setattr(module, "register_default_hooks", lambda session: None)
    seen = {}

    def _run_session(session, **kwargs):
        seen["session"] = session
        seen["stop_after"] = kwargs["args"].stop_after
        return 0

    monkeypatch.setattr(module, "_run_session", _run_session)
    monkeypatch.setenv("POKERED_ROM_PATH", "rom/red/pokemon-red.gb")
    monkeypatch.setenv("POKERED_SYM_PATH", "rom/red/pokemon-red.sym")
    monkeypatch.setattr(
        sys,
        "argv",
        ["full_to_brock.py", "--outdir", str(tmp_path), "--stop-after", "intro"],
    )

    assert module.main() == 0
    assert seen["session"] is _FakeOrchestratorSession.last
    assert seen["stop_after"] == "intro"
    assert _FakeOrchestratorSession.last.closed


def test_full_to_brock_closes_session_when_start_state_load_fails(monkeypatch, tmp_path):
    module = _load_script(FULL_TO_BROCK, monkeypatch)
    _FakeOrchestratorSession.last = None
    _FakeOrchestratorSession.fail_load = True
    monkeypatch.setattr(module, "Session", _FakeOrchestratorSession)
    monkeypatch.setattr(module, "register_default_hooks", lambda session: None)
    start_state = tmp_path / "start.state"
    start_state.write_bytes(b"not a ROM state")
    monkeypatch.setenv("POKERED_ROM_PATH", "rom/red/pokemon-red.gb")
    monkeypatch.setenv("POKERED_SYM_PATH", "rom/red/pokemon-red.sym")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "full_to_brock.py",
            "--outdir",
            str(tmp_path / "out"),
            "--start-state",
            str(start_state),
        ],
    )

    with pytest.raises(RuntimeError, match="start state load failed"):
        module.main()

    assert _FakeOrchestratorSession.last.closed
    _FakeOrchestratorSession.fail_load = False


def test_full_to_brock_cleanup_failure_is_raised_and_chains_operation(monkeypatch):
    module = _load_script(FULL_TO_BROCK, monkeypatch)
    session = _Closable(fail=True)
    active_error = RuntimeError("phase failed")

    with pytest.raises(ExceptionGroup) as raised:
        module._close_session(session, active_error=active_error)

    assert session.closed
    assert raised.value.__cause__ is active_error
    assert "full_to_brock session" in str(raised.value.exceptions[0].__notes__)


@pytest.mark.parametrize(
    ("path", "args"),
    [
        (
            SCRIPTS / "produce_vanilla_cable_club_after_brock.py",
            (Path("rom/red/pokemon-red.gb"), Path("rom/red/pokemon-red.sym"), "0" * 40),
        ),
        (
            SCRIPTS / "transplant_stock_cable_club_fixture.py",
            (
                Path("rom/red/pokemon-red.gb"),
                Path("rom/red/pokemon-red.sym"),
                "0" * 40,
                "stock",
            ),
        ),
    ],
)
def test_derived_state_producers_reject_mismatched_rom_pin(path, args, monkeypatch):
    module = _load_script(path, monkeypatch)

    with pytest.raises(ValueError, match="does not match"):
        module._validated_pins(*args)
