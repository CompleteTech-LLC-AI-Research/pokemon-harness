"""ROM-free runtime-identity measurement tests for the scenario producer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from tests._battle_scenario_fixtures_support import (
    ROOT,
)


def test_producer_measure_runtime_identity_refuses_a_foreign_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two venvs whose ``bin/python`` resolve to one binary are still two environments."""
    source_environment = tmp_path / "source-venv"
    native_environment = tmp_path / "native-venv"
    for environment in (source_environment, native_environment):
        (environment / "bin").mkdir(parents=True)
        (environment / "bin" / "python").symlink_to(sys.executable)

    monkeypatch.setattr(producer, "_pyboy_cython_flag", lambda: False)
    monkeypatch.setattr(producer.sys, "executable", str(source_environment / "bin" / "python"))
    monkeypatch.setattr(producer.sys, "prefix", str(source_environment))
    monkeypatch.setattr(producer.sys, "base_prefix", "/usr")

    identity = producer.measure_runtime_identity(
        runtime="source", python=source_environment / "bin" / "python", repo_root=ROOT
    )
    assert identity["environment"] == str(source_environment)
    assert identity["executable_path"] == str(source_environment / "bin" / "python")
    assert identity["isolated_environment"] is True

    # Both requests name the very same resolved binary ...
    assert (
        Path(source_environment / "bin" / "python").resolve()
        == Path(native_environment / "bin" / "python").resolve()
    )
    # ... and the sibling environment is still refused.
    with pytest.raises(producer.ScenarioRefusal, match="not the executing interpreter"):
        producer.measure_runtime_identity(
            runtime="source", python=native_environment / "bin" / "python", repo_root=ROOT
        )


def test_producer_measure_runtime_identity_refuses_a_requested_mode_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(producer, "_pyboy_cython_flag", lambda: False)
    with pytest.raises(producer.ScenarioRefusal, match="executing PyBoy reports 'source'"):
        producer.measure_runtime_identity(runtime="cython", python=None, repo_root=ROOT)


def test_producer_measure_runtime_identity_refuses_a_foreign_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(producer, "_pyboy_cython_flag", lambda: False)
    with pytest.raises(producer.ScenarioRefusal, match="not the executing interpreter"):
        producer.measure_runtime_identity(
            runtime="source", python="/nonexistent/interpreter", repo_root=ROOT
        )


def test_producer_measure_runtime_identity_records_measured_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(producer, "_pyboy_cython_flag", lambda: True)
    identity = producer.measure_runtime_identity(
        runtime="cython", python=sys.executable, repo_root=ROOT
    )
    assert identity["mode"] == "cython"
    assert identity["measured"] is True
    assert identity["executable"] == str(Path(sys.executable).resolve())
    assert identity["python_version"]
