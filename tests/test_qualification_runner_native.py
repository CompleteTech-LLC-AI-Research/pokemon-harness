"""Native build evidence and probe tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import qualification_runner as runner
from tests._qualification_runner_support import (
    _LOCK_OBSERVATION_SUPPORTED,
    REPO_ROOT,
    _prerequisite_runner,
    _probe_payload,
    _run_native_probe,
    _stale_job_dir,
    _write_fake_pyboy,
    held_reservation,
    make_declaration,
    make_facts,
    native_identity,
    statuses,
    with_native_evidence,
)


def test_native_probe_covers_the_complete_installed_output_set(tmp_path: Path):
    """A compiled module the module list never names must change the identity.

    The round-9 finding mutated ``pyboy.core.cpu`` inside an otherwise identical
    installed runtime.  The reported identity and fingerprint did not change,
    because only the six named entry modules were hashed, so a mixed build could
    be admitted as a consistent one.
    """

    fake_root = tmp_path / "site"
    _write_fake_pyboy(fake_root)
    identity = _run_native_probe(fake_root, tmp_path)["identity"]
    covered = identity["artifacts"]
    assert "core/cpu.py" in covered
    for name in runner._RUNTIME_MODULES:
        entry = identity["modules"][name]
        assert covered[entry["artifact"]] == entry["sha256"]

    original = _run_native_probe(fake_root, tmp_path)
    (fake_root / "pyboy" / "core" / "cpu.py").write_text("CPU = False\n", encoding="utf-8")
    mutated = _run_native_probe(fake_root, tmp_path)
    assert mutated["identity"]["artifacts"]["core/cpu.py"] != covered["core/cpu.py"]
    assert mutated["fingerprint"] != original["fingerprint"]


def test_native_probe_and_bootstrap_publish_the_same_artifact_set(tmp_path: Path):
    """Both identity producers must describe the installed output set identically."""

    fake_root = tmp_path / "site"
    _write_fake_pyboy(fake_root)
    probe_identity = _run_native_probe(fake_root, tmp_path)["identity"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import bootstrap_pyboy\n"
        "print(json.dumps(bootstrap_pyboy._runtime_identity()))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT / "scripts")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    bootstrap_identity = json.loads(completed.stdout)
    assert bootstrap_identity["artifacts"] == probe_identity["artifacts"]
    assert runner._native_build_fingerprint(bootstrap_identity) == runner._native_build_fingerprint(
        probe_identity
    )


def test_native_build_evidence_rejects_uncovered_extension_module(tmp_path: Path, monkeypatch):
    """An identity whose artifact map misses a named extension is rejected."""

    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(declaration, tmp_path)
    identity = native_identity()
    del identity["artifacts"]["pyboy/core/serial.cpython-311-x86_64-linux-gnu.so"]
    fingerprint = runner._native_build_fingerprint(identity)
    with_native_evidence(declaration, tmp_path, fingerprint=fingerprint, identity=identity)
    probe_payload = _probe_payload(fingerprint, identity)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    status = statuses(results)["native-runtime-fingerprint"]
    assert status == "fail"


def test_cli_reserve_run_holds_and_releases_lease(monkeypatch, capsys, tmp_path: Path):
    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration = make_declaration()
    declaration["reservation"]["host_lock_path"] = str(tmp_path / "host.lock")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    job_dir = tmp_path / "lease-job"
    sentinel = tmp_path / "ran.txt"
    script = f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')"
    monkeypatch.setattr(runner, "collect_facts", lambda root: make_facts(cpu_quota_cores=4.0))
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda decl, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )

    exit_code = runner.main(
        [
            "--reserve",
            "--json",
            "--declaration",
            str(declaration_path),
            "--job-dir",
            str(job_dir),
            "--run",
            sys.executable,
            "-c",
            script,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert sentinel.read_text(encoding="utf-8") == "ran"
    assert not (job_dir / "allocation.json").exists()
    pinned = json.loads(declaration_path.read_text(encoding="utf-8"))
    assert pinned["reservation"]["descriptor_sha256"]


def test_recover_removes_stale_allocation(tmp_path: Path):
    job_dir = tmp_path / "stale-job"
    job_dir.mkdir()
    os.chmod(job_dir, 0o700)
    lock_path = job_dir / "allocation.lock"
    lock_path.write_text("", encoding="utf-8")
    descriptor_path = job_dir / "allocation.json"
    descriptor_path.write_text(
        json.dumps(
            {
                "holder_pid": 2**31 - 1,
                "holder_start_time": "0",
                "lock_path": str(lock_path),
                "lock_identity": runner._lock_identity(lock_path),
            }
        ),
        encoding="utf-8",
    )
    declaration = make_declaration(
        reservation={
            "allocation_id": "test-runner",
            "job_dir": str(job_dir),
            "descriptor_path": str(descriptor_path),
            "descriptor_sha256": runner._sha256_of_file(descriptor_path),
        }
    )
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "ok"
    assert not descriptor_path.exists()


def test_recover_refuses_a_descriptor_rewritten_by_another_lease(tmp_path: Path):
    """A saved declaration must not act on a later lease's descriptor bytes."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, None)
    replacement = {
        "descriptor_version": runner._DESCRIPTOR_VERSION,
        "holder_pid": os.getpid(),
        "holder_start_time": runner._process_start_time(os.getpid()),
    }
    descriptor_path.write_text(json.dumps(replacement), encoding="utf-8")
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked", message
    assert "digest" in message
    assert descriptor_path.exists(), "the replacement lease's descriptor was removed"


def test_release_refuses_a_descriptor_rewritten_by_another_lease(tmp_path: Path):
    """A stale declaration must not signal or remove a replacement lease."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, None)
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["holder_pid"] = os.getpid()
    descriptor["holder_start_time"] = runner._process_start_time(os.getpid())
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked", message
    assert "digest" in message
    assert descriptor_path.exists()


def test_recover_keeps_active_lease(tmp_path: Path):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "fail"
    assert descriptor_path.exists()


# Adversarial controls for the UV-bootstrap evidence validators. Fixtures are
# authored values; they are not real native build or install proof.
def _uv_ci():
    from tests._qualification_runner_support import load_native_ci

    return load_native_ci()


def _uv_build_case(change):
    from tests._qualification_runner_support import uv_expected, uv_native_build

    ci = _uv_ci()
    expected = uv_expected(ci)
    document = uv_native_build(expected)
    assert ci.validate_native_build(document, expected, document["runtime_identity"]) == []
    return ci, expected, change(document, expected)


def _identity_change(key, value):
    def apply(document, _expected):
        document["runtime_identity"][key] = value
        return document

    return apply


@pytest.mark.parametrize(
    ("change", "expected_text"),
    [
        (lambda d, e: None, "missing"),
        (lambda d, e: {}, "missing"),
        (lambda d, e: {"status": "FAILED"}, "version"),
        (lambda d, e: {**d, "status": "FAILED"}, "not complete"),
        (lambda d, e: {**d, "evidence_version": 1}, "version"),
        (lambda d, e: {**d, "build_inputs_sha256": "0" * 64}, "inputs digest"),
        (lambda d, e: {**d, "producer": {**d["producer"], "script_sha256": "0" * 64}}, "producer"),
        (
            lambda d, e: {**d, "interpreter": {"python_version": "3.9.0", "prefix_name": "x"}},
            "interpreter",
        ),
        (lambda d, e: {**d, "mode": "source"}, "mode"),
        (lambda d, e: {**d, "installed_fingerprint": "0" * 64}, "fingerprint"),
        (lambda d, e: {**d, "runtime_identity": None}, "identity is missing"),
        (_identity_change("revision", "f" * 40), "revision"),
        (_identity_change("cython_compiled", False), "compiled"),
        (_identity_change("modules", {}), "module set"),
        (_identity_change("artifacts", {}), "artifact"),
    ],
    ids=[
        "none",
        "empty",
        "stub-failed",
        "failed-status",
        "old-version",
        "wrong-input",
        "wrong-producer",
        "wrong-runtime",
        "wrong-mode",
        "bad-fingerprint",
        "no-identity",
        "wrong-pin",
        "not-compiled",
        "no-modules",
        "no-artifacts",
    ],
)
def test_native_build_evidence_fails_closed(change, expected_text):
    ci, expected, document = _uv_build_case(change)
    problems = ci.validate_native_build(document, expected, None)
    assert any(expected_text in problem for problem in problems), problems


def test_native_build_evidence_must_match_installed_runtime():
    ci, expected, document = _uv_build_case(lambda d, e: d)
    other = {**document["runtime_identity"], "version": "9"}
    assert any(
        "installed runtime" in p for p in ci.validate_native_build(document, expected, other)
    )


def _pinball_case(change):
    from tests._qualification_runner_support import uv_expected, uv_native_build, uv_pinball

    ci = _uv_ci()
    expected = uv_expected(ci)
    proof, hashes = uv_pinball(ci, expected)
    identity = uv_native_build(expected)["runtime_identity"]
    assert ci.validate_pinball_proof(proof, expected, hashes, identity) == []
    proof, hashes = change(proof, hashes)
    return ci.validate_pinball_proof(proof, expected, hashes, identity)


@pytest.mark.parametrize(
    ("change", "expected_text"),
    [
        (lambda p, h: (None, h), "missing"),
        (lambda p, h: ({}, h), "missing"),
        (lambda p, h: ({"status": "PASS"}, h), "origins are incomplete"),
        (lambda p, h: ({**p, "status": "FAIL"}, h), "status"),
        (lambda p, h: ({**p, "problems": ["x"]}, h), "problems"),
        (lambda p, h: ({**p, "harness_head": "e" * 40}, h), "harness_head"),
        (lambda p, h: ({**p, "loaded_revision": "e" * 40}, h), "loaded_revision"),
        (lambda p, h: ({**p, "scope": "other"}, h), "scope"),
        (lambda p, h: ({**p, "source_line_counts": {}}, h), "line counts"),
        (
            lambda p, h: ({**p, "source_line_counts": dict.fromkeys(p["module_origins"], True)}, h),
            "bounded",
        ),
        (
            lambda p, h: (
                {
                    **p,
                    "module_origins": {k: v[:-3] + ".py" for k, v in p["module_origins"].items()},
                },
                h,
            ),
            "native extension",
        ),
        (
            lambda p, h: (
                {
                    **p,
                    "module_origins": {
                        k: "/elsewhere/pyboy/plugins/" + v.rsplit("/", 1)[1]
                        for k, v in p["module_origins"].items()
                    },
                },
                h,
            ),
            "outside",
        ),
        (lambda p, h: (p, dict.fromkeys(h, "0" * 64)), "build identity artifact"),
        (lambda p, h: (p, {}), "build identity artifact"),
    ],
    ids=[
        "none",
        "empty",
        "pass-stub",
        "failed",
        "problems",
        "wrong-head",
        "wrong-pin",
        "wrong-scope",
        "no-lines",
        "bool-lines",
        "source-origin",
        "foreign-target",
        "wrong-bytes",
        "unhashed",
    ],
)
def test_pinball_proof_requires_complete_bound_native_evidence(change, expected_text):
    problems = _pinball_case(change)
    assert any(expected_text in problem for problem in problems), problems


def _state(label, **changes):
    state = {
        "label": label,
        "target_python": "/w/py",
        "in_venv": True,
        "pip_importable": False,
        "pip_version": {"returncode": 1},
        "ensurepip_version": {"returncode": 1},
    }
    return {**state, **changes}


@pytest.mark.parametrize(
    ("changes", "expected_text"),
    [
        ({"label": "before"}, "foreign phase"),
        ({"in_venv": None}, "isolated"),
        ({"pip_version": {"returncode": True}}, "integer return code"),
        ({"ensurepip_version": {"returncode": "1"}}, "integer return code"),
        ({"pip_version": {}}, "integer return code"),
        ({"pip_importable": None}, "unrecorded"),
        ({"target_python": 3}, "different target"),
    ],
    ids=[
        "foreign-label",
        "missing-venv",
        "bool-code",
        "text-code",
        "no-code",
        "no-import",
        "bad-target",
    ],
)
def test_uv_states_reject_malformed_foreign_or_bool_records(changes, expected_text):
    ci = _uv_ci()
    labels = ci.UV_STATE_LABELS
    states = {name: _state(name) for name in labels}
    assert ci.validate_uv_states(states, target="/w/py") == []
    states["after"] = {**_state("after"), **changes}
    problems = ci.validate_uv_states(states, target="/w/py")
    assert any(expected_text in problem for problem in problems), problems
    wrong = {name: _state(name, target_python="/other/py") for name in labels}
    assert len(ci.validate_uv_states(wrong, target="/w/py")) == 3
    assert any("missing" in p for p in ci.validate_uv_states({"before": {}}, target="/w/py"))
    only = {name: {"in_venv": True} for name in labels}
    assert len(ci.validate_uv_states(only, target="/w/py")) >= 9


def _audit(change):
    from tests._qualification_runner_support import uv_audit_records, uv_expected

    ci = _uv_ci()
    expected = uv_expected(ci)
    records = uv_audit_records("/bin/uv", "/w/py", expected)
    assert ci.validate_uv_audit(records, uv="/bin/uv", target="/w/py", expected=expected) == []
    return ci.validate_uv_audit(change(records), uv="/bin/uv", target="/w/py", expected=expected)


def _argv(index, mutate):
    def apply(records):
        records = [dict(item) for item in records]
        argv = mutate(list(records[index]["argv"]))
        records[index] = {**records[index], "argv": argv, "executable": argv[0]}
        return records

    return apply


@pytest.mark.parametrize(
    ("change", "expected_text"),
    [
        (_argv(2, lambda a: [*a, "unrelated-package"]), "role sequence"),
        (_argv(2, lambda a: [*a[:-1], "setuptools==0.0.1"]), "role sequence"),
        (_argv(3, lambda a: [*a[:-1], "/elsewhere"]), "role sequence"),
        (_argv(3, lambda a: [x for x in a if x != "--no-deps"]), "role sequence"),
        (_argv(4, lambda a: [*a[:-1], "/elsewhere/pyboy-src"]), "role sequence"),
        (
            _argv(4, lambda a: ["cython==9" if x == "cython==3.0.12" else x for x in a]),
            "role sequence",
        ),
        (_argv(5, lambda a: a[:-1]), "unexpected uv command"),
        (lambda r: [*r[:2], r[5], *r[2:5], *r[6:]], "role sequence"),
        (lambda r: [*r[:2], *r[2:5], *r[6:]], "role sequence"),
        (lambda r: [*r[:2], r[2], r[2], *r[3:]], "role sequence"),
        (
            lambda r: [
                {**x, "executable": "/usr/bin/other"} if i == 2 else x for i, x in enumerate(r)
            ],
            "executable",
        ),
        (lambda r: [{k: v for k, v in x.items() if k != "pid"} for x in r], "malformed"),
        (lambda r: [{**x, "pid": True} for x in r], "malformed"),
        (lambda r: [*r[:6], {**r[6], "pid": 300}, *r[7:]], "role sequence"),
    ],
    ids=[
        "extra-arg",
        "wrong-build-pin",
        "wrong-editable-target",
        "missing-no-deps",
        "wrong-native-source",
        "wrong-cython-pin",
        "check-without-python-target",
        "check-before-installs",
        "no-check",
        "duplicate-install",
        "executable-mismatch",
        "no-pid",
        "bool-pid",
        "probe-by-other-process",
    ],
)
def test_uv_audit_requires_exact_roles_targets_and_order(change, expected_text):
    problems = _audit(change)
    assert any(expected_text in problem for problem in problems), problems


def test_uv_audit_ignores_unrelated_build_subprocesses_but_not_stray_bootstrap_calls():
    unrelated = {"pid": 900, "executable": "/usr/bin/gcc", "argv": ["/usr/bin/gcc", "-c", "x.c"]}
    assert _audit(lambda r: [*r[:3], unrelated, *r[3:]]) == []
    stray = {"pid": 100, "executable": "/usr/bin/gcc", "argv": ["/usr/bin/gcc"]}
    assert any("unexpected subprocess" in p for p in _audit(lambda r: [*r[:3], stray, *r[3:]]))


def _commands(change):
    from tests._qualification_runner_support import uv_commands

    ci = _uv_ci()
    records = uv_commands("/bin/uv", "/w/py")
    assert ci.validate_uv_commands(records, uv="/bin/uv", target="/w/py") == []
    return ci.validate_uv_commands(change(records), uv="/bin/uv", target="/w/py")


def _command(index, **changes):
    return lambda r: [{**x, **changes} if i == index else x for i, x in enumerate(r)]


@pytest.mark.parametrize(
    ("change", "expected_text"),
    [
        (_command(0, returncode=1), "version failed"),
        (_command(0, stdout="uv 0.9.0\n"), "uv version is not"),
        (_command(1, returncode=2), "list failed"),
        (_command(2, returncode=1), "check failed"),
        (_command(2, returncode=None, timed_out=True), "terminal return code"),
        (_command(2, returncode=True), "terminal return code"),
        (_command(1, stdout="not json"), "unbound"),
        (
            _command(
                1,
                stdout="not json",
                stdout_sha256=__import__("hashlib").sha256(b"not json").hexdigest(),
            ),
            "JSON package listing",
        ),
        (_command(1, argv=["/bin/uv", "pip", "list"]), "required argv"),
        (_command(2, stderr_sha256="0" * 64), "unbound"),
        (_command(1, truncated=True), "truncated"),
        (lambda r: r[:2], "evidence is missing"),
        (lambda r: [], "evidence is missing"),
        (lambda r: None, "evidence is missing"),
    ],
    ids=[
        "version-rc",
        "wrong-version",
        "list-rc",
        "check-rc",
        "timeout",
        "bool-rc",
        "unbound-output",
        "not-json",
        "wrong-argv",
        "unbound-stderr",
        "truncated",
        "no-check",
        "empty",
        "none",
    ],
)
def test_uv_command_evidence_retains_raw_output_and_terminal_codes(change, expected_text):
    problems = _commands(change)
    assert any(expected_text in problem for problem in problems), problems


def test_uv_command_listing_rejects_pip_and_missing_harness():
    import hashlib

    def listing(names):
        text = json.dumps([{"name": name} for name in names])
        return _command(1, stdout=text, stdout_sha256=hashlib.sha256(text.encode()).hexdigest())

    assert any("pip is installed" in p for p in _commands(listing(["pip", "pokered-harness"])))
    assert any("harness distribution" in p for p in _commands(listing(["cython"])))


def _kind_case(optional, change=None):
    from tests._qualification_runner_support import uv_expected, uv_native_build

    ci = _uv_ci()
    expected = uv_expected(ci)
    document = uv_native_build(expected, optional=optional)
    if change is not None:
        change(document["runtime_identity"]["modules"])
    live = document["runtime_identity"]
    return ci.validate_native_build(document, expected, live)


@pytest.mark.parametrize("optional", ["source", "cython"])
def test_native_build_accepts_optional_modules_as_source_or_compiled(optional):
    assert _kind_case(optional) == []


def _set(name, **changes):
    def apply(modules):
        modules[name] = {**modules[name], **changes}

    return apply


@pytest.mark.parametrize(
    ("optional", "change", "expected_text"),
    [
        ("source", _set("pyboy.utils", kind="source", artifact="u.py"), "pyboy.utils"),
        ("cython", _set("pyboy.core.mb", kind="source", artifact="m.py"), "pyboy.core.mb"),
        ("cython", _set("pyboy.pyboy", kind="unknown"), "pyboy.pyboy"),
        ("source", _set("pyboy", kind="unknown"), "identity module pyboy is not"),
        ("source", _set("pyboy.link", kind="other"), "pyboy.link"),
        ("source", _set("pyboy", kind=None), "identity module pyboy is not"),
        ("source", _set("pyboy.link", kind="cython"), "consistent kind"),
        ("cython", _set("pyboy", kind="source"), "consistent kind"),
        ("cython", _set("pyboy.utils", kind="cython", artifact="utils.py"), "pyboy.utils"),
        ("source", _set("pyboy.link", sha256="0" * 64), "pyboy.link"),
        ("cython", _set("pyboy.link", artifact=None), "pyboy.link"),
    ],
    ids=[
        "required-source",
        "required-source-compiled-optional",
        "required-unknown",
        "optional-unknown",
        "optional-other",
        "optional-missing-kind",
        "compiled-kind-source-file",
        "source-kind-compiled-file",
        "required-compiled-kind-source-file",
        "optional-hash-drift",
        "optional-no-artifact",
    ],
)
def test_native_build_rejects_unknown_or_inconsistent_module_kinds(optional, change, expected_text):
    problems = _kind_case(optional, change)
    assert any(expected_text in problem for problem in problems), problems
