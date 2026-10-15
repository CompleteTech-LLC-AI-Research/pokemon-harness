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


@pytest.mark.parametrize(
    "kind", ["missing", "launcher", "directory", "nonexecutable", "broken", "copy", "loop"]
)
def test_measured_request_rejects_invalid_same_environment_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Matching sys.prefix alone never establishes interpreter identity (#612)."""
    environment = tmp_path / "venv"
    bindir = environment / "bin"
    bindir.mkdir(parents=True)
    observed = bindir / "python"
    observed.symlink_to(sys.executable)
    requested = bindir / "python-requested"
    if kind == "launcher":
        requested.write_text("#!/bin/sh\nexit 99\n")
        requested.chmod(0o755)
    elif kind == "directory":
        requested.mkdir()
    elif kind == "nonexecutable":
        requested.write_bytes(b"not an interpreter")
        requested.chmod(0o644)
    elif kind == "broken":
        requested.symlink_to(bindir / "absent")
    elif kind == "copy":
        requested.write_bytes(observed.read_bytes())
        requested.chmod(0o755)
    elif kind == "loop":
        requested.symlink_to(requested.name)
    monkeypatch.setattr(producer.sys, "executable", str(observed))
    monkeypatch.setattr(producer.sys, "prefix", str(environment))
    monkeypatch.setattr(producer, "_pyboy_cython_flag", lambda: False)
    monkeypatch.setattr(producer, "_pyboy_module_version", lambda: "observed-version")
    monkeypatch.setattr(producer, "_pyboy_module_revision", lambda: "observed-revision")

    with pytest.raises(producer.ScenarioRefusal, match="requested interpreter"):
        producer.measure_runtime_identity(runtime="source", python=requested, repo_root=ROOT)


@pytest.mark.parametrize("layout", ["bin", "Scripts", ""])
@pytest.mark.parametrize("kind", ["running", "alias", "relative", "directory_alias", "hardlink"])
def test_measured_request_accepts_same_environment_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, layout: str
) -> None:
    environment = tmp_path / "venv"
    bindir = environment / layout
    bindir.mkdir(parents=True)
    observed = bindir / "python"
    # A local executable file keeps hard-link controls on the same filesystem.
    observed.write_bytes(b"observed process identity double")
    observed.chmod(0o755)
    requested = observed
    if kind in {"alias", "relative"}:
        requested = bindir / "python3"
        requested.symlink_to(observed.name)
    elif kind == "directory_alias":
        alias = tmp_path / "venv-alias"
        alias.symlink_to(environment, target_is_directory=True)
        requested = alias / layout / "python"
    elif kind == "hardlink":
        requested = bindir / "python3"
        requested.hardlink_to(observed)
    if kind == "relative":
        monkeypatch.chdir(tmp_path)
        requested = requested.relative_to(tmp_path)
    monkeypatch.setattr(producer.sys, "executable", str(observed))
    monkeypatch.setattr(producer.sys, "prefix", str(environment))
    monkeypatch.setattr(producer, "_pyboy_cython_flag", lambda: False)
    monkeypatch.setattr(producer, "_pyboy_module_version", lambda: "observed-version")
    monkeypatch.setattr(producer, "_pyboy_module_revision", lambda: "observed-revision")

    identity = producer.measure_runtime_identity(runtime="source", python=requested, repo_root=ROOT)
    assert identity["requested_python"] == str(requested)
    assert identity["executable_path"] == str(observed)
    assert identity["executable"] == str(observed.resolve())
    assert identity["environment"] == str(environment)
    assert identity["pyboy_version_observed"] == "observed-version"
    assert identity["pyboy_revision_observed"] == "observed-revision"
    assert identity["measured"] is True
