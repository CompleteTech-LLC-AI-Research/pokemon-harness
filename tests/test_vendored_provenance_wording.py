"""Guard the vendored-tree provenance wording and its one recorded residual.

The vendored PyBoy tree descends from a *harness fork* revision, not from an
upstream ``Baekalfen/PyBoy`` commit. #237 corrected that wording across every
carrier outside the content identity. Two things must stay true, and both are
cheap to break with a later prose edit:

1. No carrier may call ``c565df66...`` the "upstream base" again.
2. ``pyboy/__init__.py``'s revision comment is *inside* the content identity, so
   its wording is frozen together with the pin. Fixing it without re-pinning
   would falsify the identity; re-pinning without intending to is a release
   event. This test fails on either, and points at the documented procedure.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "pyboy-src"

FORK_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"
UPSTREAM_TAG_REVISION = "4627b90b878e91faff443b3acd6d4e4be09a4387"
PIN = "7ecd4b73db822340467a28796b39504aad8d66c5"

REVISION_MARKER = VENDOR_ROOT / "POKERED_HARNESS_PYBOY_REVISION"
DIVERGENCE_RECORD = VENDOR_ROOT / "POKERED_HARNESS_PYBOY_DIVERGENCE.md"
INIT_MODULE = VENDOR_ROOT / "pyboy" / "__init__.py"

# Carriers corrected by #237, plus the divergence record that owns the residual.
# Every file here is outside the content identity, which is why the wording
# could be fixed without a re-pin.
CORRECTED_CARRIERS = (
    "README.md",
    "VERSIONS.md",
    "agents.md",
    "docs/LINUX_RESUME_PROMPT.md",
    "docs/RELEASE_CHECKLIST.md",
    "docs/VENDORED_PYBOY_SPLIT_DECISION.md",
    "vendor/pyboy-src/POKERED_HARNESS_PYBOY_DIVERGENCE.md",
)

# The one *recorded residual*: the pin-covered comment in `pyboy/__init__.py`
# cannot be reworded without moving the content identity, so it is quoted on
# purpose in these two documents. Each entry must still contain the quoted
# needle, so editing the residual forces a matching edit here instead of
# silently widening the exemption.
RECORDED_RESIDUALS = {
    "vendor/pyboy-src/POKERED_HARNESS_PYBOY_DIVERGENCE.md": "it replaced the upstream base revision",
    "docs/VENDORED_PYBOY_SPLIT_DECISION.md": "it replaced the upstream base revision",
}

# How close the fork revision and the phrase must be for the co-occurrence to
# count as an attribution. Markdown wraps prose across lines, so matching is
# done per paragraph with newlines collapsed; the window keeps unrelated
# statements in a long paragraph from being flagged.
_ATTRIBUTION_WINDOW = 240

_HEX40 = re.compile(rb"[0-9a-f]{40}")
_UPSTREAM_BASE = re.compile(r"upstream[ _-]base", re.IGNORECASE)
# Both the full 40-hex hash and the abbreviated ``c565df66…`` form are used in
# prose, so the guard keys on the shared prefix.
_FORK_REF = re.compile(r"c565df66")


def _text(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _tracked_vendored_files() -> list[str]:
    """Return the git-tracked files under the vendored tree, in path order.

    The identity is defined over the *tracked* manifest, so a local build (or a
    Cython run that drops ``.c``/``.so`` next to the sources) must not move it.
    """

    listed = subprocess.run(
        ["git", "ls-files", "--", "vendor/pyboy-src"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert listed, "no tracked files under vendor/pyboy-src; not a checkout?"
    return sorted(listed)


def _paragraphs(text: str) -> list[str]:
    """Collapse markdown line wrapping so prose can be searched as written."""

    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text)]


def _attribution_paragraphs(text: str) -> list[str]:
    """Paragraphs that tie the fork revision to an "upstream base" claim."""

    flagged: list[str] = []
    for block in _paragraphs(text):
        fork_at = [match.start() for match in _FORK_REF.finditer(block)]
        base_at = [match.start() for match in _UPSTREAM_BASE.finditer(block)]
        if not fork_at or not base_at:
            continue
        if min(abs(fork - base) for fork in fork_at for base in base_at) <= _ATTRIBUTION_WINDOW:
            flagged.append(block)
    return flagged


def test_revision_marker_matches_the_live_pin() -> None:
    assert REVISION_MARKER.read_text(encoding="utf-8").strip() == PIN
    assert f'__pokered_harness_revision__ = "{PIN}"' in INIT_MODULE.read_text(encoding="utf-8")


def test_corrected_carriers_reject_the_upstream_base_attribution() -> None:
    """No paragraph may tie ``c565df66…`` to an "upstream base" claim.

    The check is deliberately scoped to paragraphs that name the fork revision, so
    unrelated true statements (for example the *harness repository's* own
    starting upstream base, which names a different revision) are not flagged.
    """

    offenders: list[str] = []
    residual_seen: set[str] = set()
    for relative_path in CORRECTED_CARRIERS:
        for block in _attribution_paragraphs(_text(relative_path)):
            needle = RECORDED_RESIDUALS.get(relative_path)
            if needle and needle in block:
                residual_seen.add(relative_path)
                continue
            offenders.append(f"{relative_path}: {block[:200]}")
    assert not offenders, (
        "these passages describe the vendored fork revision as an 'upstream base'; it is a "
        "commit of CompleteDotTech/pyboy-link-cable-fork, not of Baekalfen/PyBoy: "
        f"{offenders}"
    )
    assert residual_seen == set(RECORDED_RESIDUALS), (
        "the recorded residual moved or was reworded; update RECORDED_RESIDUALS in "
        f"step with the carrier: missing={sorted(set(RECORDED_RESIDUALS) - residual_seen)}"
    )


def test_divergence_record_names_the_fork_and_the_upstream_tag() -> None:
    record = DIVERGENCE_RECORD.read_text(encoding="utf-8")
    assert FORK_REVISION in record
    assert UPSTREAM_TAG_REVISION in record
    assert "CompleteDotTech/pyboy-link-cable-fork" in record
    assert "not** an upstream `Baekalfen/PyBoy` commit" in record
    # The record owns the residual, so a later edit cannot silently drop it.
    assert "upstream base" in record


def test_pin_is_a_content_identity_over_the_vendored_manifest() -> None:
    """Recompute the pin exactly as the divergence record defines it.

    Two files are excluded because they embed the pin, and in
    ``pyboy/__init__.py`` every 40-hex run is masked before hashing.
    """

    # The identity describes the tree that will be committed. If a vendored file is
    # edited but not staged, this recomputation would describe a different tree than
    # the one carrying the pin, so fail loudly instead of quietly validating it.
    unstaged = subprocess.run(
        ["git", "diff", "--name-only", "--", "vendor/pyboy-src"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert not unstaged, (
        "vendored files are modified but not staged; the content identity would "
        f"describe a different tree than the committed one: {unstaged}"
    )
    # Staged build output under the importable package would create a second import
    # path for a module and silently enter the manifest.
    shadowed = [
        path
        for path in _tracked_vendored_files()
        if path.startswith("vendor/pyboy-src/pyboy/pyboy/")
    ]
    assert not shadowed, f"tracked staged build output shadows the package: {shadowed}"
    excluded = {
        "vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION",
        "vendor/pyboy-src/POKERED_HARNESS_PYBOY_DIVERGENCE.md",
    }
    files = _tracked_vendored_files()
    hasher = hashlib.sha1()
    hashed = 0
    for relative_path in files:
        if relative_path in excluded:
            continue
        body = (PROJECT_ROOT / relative_path).read_bytes()
        if relative_path == "vendor/pyboy-src/pyboy/__init__.py":
            body = _HEX40.sub(b"@POKERED_REV@", body)
        hasher.update(
            relative_path.encode() + b"\0" + hashlib.sha1(body).hexdigest().encode() + b"\n"
        )
        hashed += 1
    assert hashed >= 100
    assert hasher.hexdigest() == PIN, (
        "the vendored content identity moved. If the reword of "
        "pyboy/__init__.py's comment (or any vendored edit) was intentional, follow "
        "docs/VENDORED_PYBOY_SPLIT_DECISION.md condition 2: refresh "
        "POKERED_HARNESS_PYBOY_REVISION, __pokered_harness_revision__, "
        "scripts/bootstrap_pyboy.py::EXPECTED_REVISION, "
        "tests/_runtime_packaging_support.py::EXPECTED_PYBOY_REVISION and the CI carriers "
        "together, and re-qualify the affected runtime records."
    )


def test_init_comment_wording_is_still_the_recorded_residual() -> None:
    """The in-identity comment is frozen until the next pin-moving change.

    Rewording it here would move the content identity without a re-pin, which is
    exactly the falsified-identity failure the decision doc's condition 2
    prohibits. When it *is* fixed, that must happen together with the pin, so
    this test is expected to change in the same commit.
    """

    assert "it replaced the upstream base revision" in INIT_MODULE.read_text(encoding="utf-8")


# Native-lane tooling controls. These use authored doubles, not ROMs or a native
# PyBoy installation. A passing control does not qualify the unit lane itself.
@pytest.fixture
def native_ci():
    spec = importlib.util.spec_from_file_location(
        "native_unit_ci_controls", PROJECT_ROOT / "scripts" / "native_unit_ci.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def native_ci_repo(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Native CI test",
            "-c",
            "user.email=native-ci@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    return root


@pytest.fixture
def native_ci_modules(native_ci, tmp_path, monkeypatch):
    vendor = tmp_path / "vendor" / "pyboy-src"
    vendor.mkdir(parents=True)
    (vendor / "POKERED_HARNESS_PYBOY_REVISION").write_text(PIN + "\n", encoding="ascii")
    extension = native_ci.importlib.machinery.EXTENSION_SUFFIXES[0]
    marker = object()
    wrapper = type("GameWrapperPokemonPinball", (), {})

    class NativeManager:
        __slots__ = ("game_wrapper_pokemon_pinball",)

    facade = SimpleNamespace(ADDR_BALLS_LEFT=marker, GameWrapperPokemonPinball=wrapper)
    data = SimpleNamespace(__all__=["ADDR_BALLS_LEFT"], ADDR_BALLS_LEFT=marker)
    modules = {
        "pyboy": SimpleNamespace(__pokered_harness_revision__=PIN),
        "pyboy.utils": SimpleNamespace(cython_compiled=True),
        "pyboy.plugins.manager": SimpleNamespace(
            PluginManager=NativeManager,
            __file__=str(tmp_path / "installed" / ("manager" + extension)),
        ),
    }
    for name, module in zip(native_ci.PINBALL_MODULES, (facade, data), strict=True):
        source = vendor / (name.replace(".", "/") + ".py")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# authored test input\n", encoding="utf-8")
        module.__file__ = str(tmp_path / "installed" / (name + extension))
        modules[name] = module
    monkeypatch.setattr(native_ci.importlib, "import_module", modules.__getitem__)
    return tmp_path, modules, facade, data


def test_native_ci_accepts_matching_native_pinball_identity(native_ci, native_ci_modules):
    root, _, _, _ = native_ci_modules
    proof = native_ci.inspect_pinball(root)
    assert proof["status"] == "PASS"
    assert proof["expected_revision"] == proof["loaded_revision"] == PIN
    assert set(proof["source_line_counts"].values()) == {1}
    assert not proof["problems"]


@pytest.mark.parametrize("index", [0, 1])
def test_native_ci_rejects_source_fallback(native_ci, native_ci_modules, index):
    root, modules, _, _ = native_ci_modules
    name = native_ci.PINBALL_MODULES[index]
    modules[name].__file__ = name + ".py"
    proof = native_ci.inspect_pinball(root)
    assert proof["status"] == "FAIL"
    assert f"{name} is not an installed native extension" in proof["problems"]


@pytest.mark.parametrize("change", ["pin", "utils", "manager", "data_identity", "missing"])
def test_native_ci_rejects_identity_drift(native_ci, native_ci_modules, change):
    root, modules, facade, _ = native_ci_modules
    if change == "pin":
        modules["pyboy"].__pokered_harness_revision__ = FORK_REVISION
    elif change == "utils":
        modules["pyboy.utils"].cython_compiled = False
    elif change == "manager":
        modules["pyboy.plugins.manager"].PluginManager = object()
    elif change == "data_identity":
        facade.ADDR_BALLS_LEFT = object()
    else:
        del facade.ADDR_BALLS_LEFT
    proof = native_ci.inspect_pinball(root)
    assert proof["status"] == "FAIL"
    assert proof["problems"]


@pytest.mark.parametrize("exports", [None, [], [4], ["Enum"], ["ADDR_BALLS_LEFT"] * 2])
def test_native_ci_rejects_invalid_export_contract(native_ci, native_ci_modules, exports):
    root, _, _, data = native_ci_modules
    data.__all__ = exports
    assert native_ci.inspect_pinball(root)["status"] == "FAIL"


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("lines,expected", [(1000, "PASS"), (1001, "FAIL")])
def test_native_ci_enforces_line_bound(native_ci, native_ci_modules, index, lines, expected):
    root, _, _, _ = native_ci_modules
    source = (
        root / "vendor" / "pyboy-src" / (native_ci.PINBALL_MODULES[index].replace(".", "/") + ".py")
    )
    source.write_text("# line\n" * lines, encoding="utf-8")
    assert native_ci.inspect_pinball(root)["status"] == expected


def test_native_ci_prepare_records_head_without_claiming_test_pass(
    native_ci, native_ci_repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(native_ci, "host_facts", lambda: ({"allocation_attested": False}, []))
    output = tmp_path / "output"
    native_ci.prepare(native_ci_repo, output)
    record = json.loads((output / "evidence" / "preflight.json").read_text())
    assert record["status"] == "READY"
    assert record["harness_head"] == native_ci._git(native_ci_repo, "rev-parse", "HEAD")
    assert record["host"]["allocation_attested"] is False
    assert not (output / "evidence" / "pinball-native.json").exists()
    assert (output / "work").is_dir()


def test_native_ci_prepare_retains_blocked_result(native_ci, native_ci_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(native_ci, "host_facts", lambda: ({}, ["non-root user required"]))
    output = tmp_path / "blocked"
    with pytest.raises(ValueError, match="non-root"):
        native_ci.prepare(native_ci_repo, output)
    record = json.loads((output / "evidence" / "preflight.json").read_text())
    assert record["status"] == "BLOCKED"
    assert record["problems"] == ["non-root user required"]


@pytest.mark.parametrize("location", ["inside", "root", "existing", "symlink_inside"])
def test_native_ci_prepare_preserves_existing_and_checkout_paths(
    native_ci, native_ci_repo, tmp_path, monkeypatch, location
):
    monkeypatch.setattr(native_ci, "host_facts", lambda: ({}, []))
    if location == "inside":
        output = native_ci_repo / "output"
    elif location == "root":
        output = native_ci_repo
    elif location == "symlink_inside":
        link = tmp_path / "alias"
        link.symlink_to(native_ci_repo, target_is_directory=True)
        output = link / "output"
    else:
        output = tmp_path / "existing"
        output.mkdir()
        (output / "retained.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises((ValueError, FileExistsError)):
        native_ci.prepare(native_ci_repo, output)
    assert (native_ci_repo / "tracked.txt").read_text() == "original\n"
    if location == "existing":
        assert (output / "retained.txt").read_text() == "preserve"


@pytest.mark.parametrize("tracked", [True, False])
def test_native_ci_prepare_rejects_dirty_worktree(native_ci, native_ci_repo, tmp_path, tracked):
    path = native_ci_repo / ("tracked.txt" if tracked else "untracked.txt")
    path.write_text("preserve dirty work\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean, committed"):
        native_ci.prepare(native_ci_repo, tmp_path / "output")
    assert path.read_text() == "preserve dirty work\n"


@pytest.mark.parametrize("change", ["head", "dirty", "blocked"])
def test_native_ci_verify_rejects_changed_or_unready_input(
    native_ci, native_ci_repo, tmp_path, monkeypatch, change
):
    monkeypatch.setattr(native_ci, "host_facts", lambda: ({}, []))
    output = tmp_path / "output"
    native_ci.prepare(native_ci_repo, output)
    preflight = output / "evidence" / "preflight.json"
    record = json.loads(preflight.read_text())
    if change == "head":
        record["harness_head"] = "0" * 40
    elif change == "blocked":
        record["status"] = "BLOCKED"
    else:
        (native_ci_repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    preflight.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        native_ci.verify(native_ci_repo, output)
    assert not (output / "evidence" / "pinball-native.json").exists()


def test_native_ci_verify_retains_failure_proof(native_ci, native_ci_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(native_ci, "host_facts", lambda: ({}, []))
    monkeypatch.setattr(
        native_ci,
        "inspect_pinball",
        lambda root: {"status": "FAIL", "problems": ["source fallback"]},
    )
    output = tmp_path / "output"
    native_ci.prepare(native_ci_repo, output)
    with pytest.raises(ValueError, match="source fallback"):
        native_ci.verify(native_ci_repo, output)
    proof = json.loads((output / "evidence" / "pinball-native.json").read_text())
    assert proof["status"] == "FAIL"
    assert proof["harness_head"] == native_ci._git(native_ci_repo, "rev-parse", "HEAD")


def test_native_ci_host_blocks_root_unknown_shm_and_unsafe_environment(native_ci, monkeypatch):
    monkeypatch.setattr(native_ci.os, "geteuid", lambda: 0)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--secret-private-filter")

    def no_shared_memory(*args):
        raise OSError("read-only shared memory")

    monkeypatch.setattr(native_ci.multiprocessing, "get_context", no_shared_memory)
    facts, problems = native_ci.host_facts()
    assert facts["allocation_attested"] is False
    assert facts["shared_memory_semaphore"] == "unusable"
    assert any("non-root" in problem for problem in problems)
    assert any("shared memory" in problem for problem in problems)
    assert any("PYTEST_ADDOPTS" in problem for problem in problems)
    assert "--secret-private-filter" not in json.dumps((facts, problems))


def test_native_ci_workflow_preserves_public_only_native_gate():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "native-unit.yml").read_text()
    script = (PROJECT_ROOT / "scripts" / "run_native_unit_ci.sh").read_text()
    assert "github.event.repository.private == false" in workflow
    assert "github.event.repository.visibility == 'public'" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "persist-credentials: false" in workflow
    assert "if: ${{ always() }}" in workflow
    assert "${{ env.NATIVE_UNIT_OUTPUT }}/evidence/" in workflow
    assert "bash scripts/run_native_unit_ci.sh" in workflow
    assert "set -euo pipefail" in script
    assert "--mode cython --check" in script
    assert "--build-evidence" in script
    assert "--runtime-mode cython --tier unit" in script
    commands = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    for bypass in ("--timeout-seconds", "--unit-only", "--allow-skip", "|| true", " -k "):
        assert bypass not in commands
    assert "rm -" not in script
    assert "trap finish EXIT" in script


@pytest.mark.parametrize("failure,expected", [("none", 0), ("build", 7), ("gate", 9), ("head", 2)])
def test_native_ci_shell_preserves_failures_and_final_head(
    native_ci_repo, tmp_path, failure, expected
):
    """Execute the real shell orchestration with a fake interpreter, not PyBoy."""
    scripts = native_ci_repo / "scripts"
    scripts.mkdir()
    shutil.copy2(PROJECT_ROOT / "scripts" / "run_native_unit_ci.sh", scripts)
    fake = tmp_path / "fake-python"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "%s\\n" "$*" >> "$FAKE_CALLS"\n'
        'case "$*" in\n'
        '  *"native_unit_ci.py prepare"*) mkdir -p "$3/evidence" "$3/work" ;;\n'
        '  *"-m venv"*) mkdir -p "$3/bin"; cp "$0" "$3/bin/python" ;;\n'
        '  *"bootstrap_pyboy.py --mode cython --build-evidence"*)\n'
        '    [[ "$FAKE_FAILURE" != build ]] || exit 7 ;;\n'
        '  *"production_gate.py"*)\n'
        '    [[ "$FAKE_FAILURE" != gate ]] || exit 9\n'
        '    if [[ "$FAKE_FAILURE" == head ]]; then\n'
        "      git -c user.name=Test -c user.email=test@example.invalid "
        "commit --allow-empty -qm changed\n"
        "    fi ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    subprocess.run(["git", "-C", str(native_ci_repo), "add", "scripts"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(native_ci_repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test runner",
        ],
        check=True,
    )
    output = tmp_path / "output"
    calls = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "PYTHON": str(fake),
        "NATIVE_UNIT_OUTPUT": str(output),
        "FAKE_CALLS": str(calls),
        "FAKE_FAILURE": failure,
    }
    completed = subprocess.run(
        ["bash", str(scripts / "run_native_unit_ci.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == expected, completed.stdout + completed.stderr
    assert (output / "evidence" / "exit-code.txt").read_text().strip() == str(expected)
    invocations = calls.read_text()
    assert ("production_gate.py" in invocations) is (failure != "build")
    if failure == "build":
        assert "native_unit_ci.py verify" not in invocations


@pytest.mark.parametrize("pin", ["", "invalid", "g" * 40])
def test_native_ci_rejects_malformed_pin(native_ci, native_ci_modules, pin):
    root, modules, _, _ = native_ci_modules
    marker = root / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION"
    marker.write_text(pin + "\n", encoding="ascii")
    modules["pyboy"].__pokered_harness_revision__ = pin
    proof = native_ci.inspect_pinball(root)
    assert proof["status"] == "FAIL"
    assert "checkout PyBoy revision pin is malformed" in proof["problems"]
