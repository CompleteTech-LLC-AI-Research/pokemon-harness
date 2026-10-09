"""Authored controls: private immutable runtime inputs, or kernel-proven protection (#107 F2).

A tiny fake virtual environment stands in for the real one; the resolution probe is replaced by
its result so nothing is imported or executed.  Every write-denial assertion uses the real OS.
"""

import os
import sys
import sysconfig
from pathlib import Path

import pytest

from scripts import production_gate_parallel_runtime_inputs as inputs
from scripts.production_gate_parallel_isolation import IsolationError, thaw_and_remove


def make_environment(tmp_path):
    original = tmp_path / "checkout"
    snapshot = tmp_path / "snapshot-checkout"
    original.mkdir()
    snapshot.mkdir()
    venv = tmp_path / "venv"
    site = venv / "lib" / "python3" / "site-packages"
    site.mkdir(parents=True)
    (venv / "bin").mkdir()
    (venv / "bin" / "python").symlink_to(os.path.realpath(sys.executable))
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (site / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site / "__editable__.project.pth").write_text(f"{original}/src\n", encoding="utf-8")
    return original, snapshot, venv


def fake_probe(venv, **overrides):
    info = {
        "real_executable": os.path.realpath(sys.executable),
        "prefix": str(venv),
        "base_prefix": sys.base_prefix,
        "path": [str(venv / "lib" / "python3" / "site-packages")],
        "stdlib": sysconfig.get_paths()["stdlib"],
        "platstdlib": sysconfig.get_paths()["platstdlib"],
        "sites": [str(venv / "lib" / "python3" / "site-packages")],
        "libraries": [],
    }
    info.update(overrides)

    def probe(interpreter, pythonpath):
        prefix = Path(interpreter).parents[
            1
        ]  # the original and the private copy each report their own
        return {**info, "path": [str(prefix / "lib" / "python3" / "site-packages")]}

    return probe


@pytest.fixture
def protected_base(monkeypatch):
    real = inputs.writable_entries
    base = os.path.realpath(sys.base_prefix)

    def fake(root, limit=inputs.WRITABLE_REPORT_LIMIT):
        return [] if os.path.realpath(str(root)).startswith(base) else real(root, limit)

    monkeypatch.setattr(inputs, "writable_entries", fake)


def test_a_virtual_environment_is_copied_redirected_and_frozen_against_real_writes(
    tmp_path, monkeypatch, protected_base
):
    original, snapshot, venv = make_environment(tmp_path)
    monkeypatch.setattr(inputs, "probe_runtime", fake_probe(venv))
    result = inputs.build_private_runtimes(
        {"source": str(venv / "bin" / "python"), "cython": str(venv / "bin" / "python")},
        project_root=original,
        checkout=snapshot,
        destination=tmp_path / "runtime",
    )
    try:
        for mode in ("source", "cython"):
            assert result[mode]["problems"] == []
            private = Path(result[mode]["manifest"]["private_prefix"])
            assert result[mode]["interpreter"].startswith(str(private))
            assert private != venv and private.is_relative_to(tmp_path / "runtime")
        assert len({r["manifest"]["private_prefix"] for r in result.values()}) == 1, "copied once"
        manifest = result["source"]["manifest"]
        assert manifest["immutability"][manifest["private_prefix"]] == "private_copy_mode_bits"
        assert (
            "chmod" in " ".join(manifest["acceptance_limits"]) and "static" in manifest["evidence"]
        )
        copy = Path(result["source"]["manifest"]["private_prefix"])
        pth = copy / "lib" / "python3" / "site-packages" / "__editable__.project.pth"
        assert str(snapshot) in pth.read_text() and str(original) not in pth.read_text()
        assert (
            str(original)
            in (venv / "lib/python3/site-packages/__editable__.project.pth").read_text()
        )
        with pytest.raises(PermissionError):
            (copy / "lib/python3/site-packages/dependency.py").write_text("VALUE = 2\n")
        with pytest.raises(PermissionError):
            (copy / "lib/python3/site-packages/created.py").write_text("x")
        assert os.access(venv / "lib/python3/site-packages/dependency.py", os.W_OK), (
            "shared tree untouched"
        )
    finally:
        thaw_and_remove(tmp_path / "runtime")


def test_a_writable_base_prefix_is_copied_frozen_and_the_venv_is_rebased_onto_it(
    tmp_path, monkeypatch
):
    original, snapshot, venv = make_environment(tmp_path)
    base = tmp_path / "managed-python"
    (base / "bin").mkdir(parents=True)
    (base / "lib" / "python3").mkdir(parents=True)
    (base / "bin" / "python3").write_text("#!interpreter\n", encoding="utf-8")
    (base / "lib" / "python3" / "os.py").write_text("", encoding="utf-8")
    (venv / "bin" / "python").unlink()
    (venv / "bin" / "python").symlink_to(base / "bin" / "python3")
    (venv / "pyvenv.cfg").write_text(f"home = {base}/bin\n", encoding="utf-8")

    def probe(interpreter, pythonpath):
        private = str(tmp_path / "runtime") in interpreter
        root = Path(interpreter).parents[1]
        home = next((tmp_path / "runtime").glob("base-*"), base) if private else base
        return {
            "real_executable": str(home / "bin" / "python3"),
            "prefix": str(root),
            "base_prefix": str(base),
            "path": [str(root / "lib" / "python3" / "site-packages")],
            "stdlib": str(home / "lib" / "python3"),
            "platstdlib": str(home / "lib" / "python3"),
            "sites": [str(root / "lib" / "python3" / "site-packages")],
            "libraries": [],
        }

    monkeypatch.setattr(inputs, "probe_runtime", probe)
    try:
        result = inputs.build_private_runtimes(
            {"source": str(venv / "bin" / "python")},
            project_root=original,
            checkout=snapshot,
            destination=tmp_path / "runtime",
        )
        info = result["source"]
        assert info["problems"] == [], info["problems"]
        private_base = Path(info["manifest"]["private_base"])
        private_env = Path(info["manifest"]["private_prefix"])
        assert (private_base / "lib" / "python3" / "os.py").is_file()
        assert str(private_base) in (private_env / "pyvenv.cfg").read_text()
        assert str(base) not in (private_env / "pyvenv.cfg").read_text()
        assert os.readlink(private_env / "bin" / "python").startswith(str(private_base))
        with pytest.raises(PermissionError):
            (private_base / "lib" / "python3" / "os.py").write_text("tampered")
        assert os.access(base / "lib" / "python3" / "os.py", os.W_OK), (
            "the shared base is untouched"
        )
    finally:
        thaw_and_remove(tmp_path / "runtime")


def test_a_mapped_library_the_executing_uid_could_replace_is_refused(
    tmp_path, monkeypatch, protected_base
):
    original, snapshot, venv = make_environment(tmp_path)
    library = tmp_path / "libshared.so"
    library.write_bytes(b"\0")
    monkeypatch.setattr(inputs, "probe_runtime", fake_probe(venv, libraries=[str(library)]))
    try:
        result = inputs.build_private_runtimes(
            {"source": str(venv / "bin" / "python")},
            project_root=original,
            checkout=snapshot,
            destination=tmp_path / "runtime",
        )
    finally:
        thaw_and_remove(tmp_path / "runtime")
    assert any("linked library" in p for p in result["source"]["problems"])


def test_writable_entries_reports_only_what_the_kernel_lets_this_uid_modify(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "file").write_text("x", encoding="utf-8")
    assert inputs.writable_entries(tree) != []
    (tree / "file").chmod(0o444)
    tree.chmod(0o555)
    try:
        assert inputs.writable_entries(tree) == []
    finally:
        tree.chmod(0o755)


def test_an_unresolvable_runtime_is_an_isolation_error_not_a_pass(tmp_path):
    with pytest.raises(IsolationError):
        inputs.probe_runtime(str(tmp_path / "missing-python"), "")


def test_a_default_sys_path_entry_the_uid_can_modify_outside_the_copy_is_refused(
    tmp_path, monkeypatch, protected_base
):
    original, snapshot, venv = make_environment(tmp_path)
    stray = tmp_path / "stray-source"
    stray.mkdir()
    base = fake_probe(venv)
    monkeypatch.setattr(
        inputs,
        "probe_runtime",
        lambda interpreter, pythonpath: {**base(interpreter, pythonpath), "path": [str(stray)]},
    )
    try:
        result = inputs.build_private_runtimes(
            {"source": str(venv / "bin" / "python")},
            project_root=original,
            checkout=snapshot,
            destination=tmp_path / "runtime",
        )
    finally:
        thaw_and_remove(tmp_path / "runtime")
    assert any("default sys.path names" in p for p in result["source"]["problems"])


def _tree(root, snapshot):
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "dep.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "lib" / "x.pth").write_text(f"{snapshot}/src\n", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "python").symlink_to("/usr/bin/python3")
    return root


def test_the_runtime_digest_is_path_independent_and_sensitive_to_every_change(tmp_path):
    first = _tree(tmp_path / "a", "/run1/snapshot/checkout")
    second = _tree(tmp_path / "b", "/run2/snapshot/checkout")
    digest = inputs.runtime_digest(first, Path("/run1/snapshot/checkout"))
    assert inputs.runtime_digest(second, Path("/run2/snapshot/checkout")) == digest
    (second / "lib" / "dep.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert inputs.runtime_digest(second, Path("/run2/snapshot/checkout")) != digest
    (second / "lib" / "dep.py").write_text("VALUE = 1\n", encoding="utf-8")
    (second / "lib" / "added.py").write_text("", encoding="utf-8")
    assert inputs.runtime_digest(second, Path("/run2/snapshot/checkout")) != digest
    (second / "lib" / "added.py").unlink()
    (second / "bin" / "python").unlink()
    (second / "bin" / "python").symlink_to("/usr/bin/python3.11")
    assert inputs.runtime_digest(second, Path("/run2/snapshot/checkout")) != digest


def test_the_pin_check_refuses_a_runtime_that_differs_from_the_frozen_digest(tmp_path):
    root, checkout = tmp_path / "r", tmp_path / "checkout"
    root.mkdir()
    (root / "m.py").write_text("x", encoding="utf-8")
    body = {"runtime_inputs": {"source": inputs.runtime_digest(root, checkout)}}
    assert inputs.runtime_pin_problem(body, "source", root, checkout) == ""
    (root / "m.py").write_text("y", encoding="utf-8")
    assert "differs from the digest the plan pinned" in inputs.runtime_pin_problem(
        body, "source", root, checkout
    )
    assert inputs.runtime_pin_problem(body, "cython", root, checkout) == "", "no pin for that mode"
    assert inputs.runtime_pin_problem({}, "source", root, checkout) == ""
