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
    from tests._bootstrap_stage_test_support import load_native_ci

    return load_native_ci()


def _uv_build_case(change):
    from tests._bootstrap_stage_test_support import uv_expected, uv_native_build

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
    from tests._bootstrap_stage_test_support import uv_expected, uv_native_build, uv_pinball

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
    from tests._bootstrap_stage_test_support import uv_audit_records, uv_expected

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
    unrelated = {
        "pid": 900,
        "ppid": 899,
        "executable": "/usr/bin/gcc",
        "argv": ["/usr/bin/gcc", "-c", "x.c"],
    }
    assert _audit(lambda r: [*r[:3], unrelated, *r[3:]]) == []
    stray = {"pid": 100, "ppid": 50, "executable": "/usr/bin/gcc", "argv": ["/usr/bin/gcc"]}
    assert any("unexpected subprocess" in p for p in _audit(lambda r: [*r[:3], stray, *r[3:]]))


def _commands(change):
    from tests._bootstrap_stage_test_support import uv_commands

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
    from tests._bootstrap_stage_test_support import uv_expected, uv_native_build

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


def _record(pid, ppid, *argv):
    return {"pid": pid, "ppid": ppid, "executable": argv[0], "argv": list(argv)}


def _moved(source, to):
    def apply(records):
        rows = list(records)
        rows.insert(to, rows.pop(source))
        return rows

    return apply


def _extra(index, *record):
    return lambda r: [*r[:index], _record(*record), *r[index:]]


# Fixture order: 0-5 installers and check, 6 build probe (pid 100), 7-9 its child's SDL
# discovery (pid 101), 10-12 the build process's own discovery, 13 the --check probe
# (pid 200), 14-16 that probe child's discovery (pid 201).
def test_uv_audit_accepts_sdl_discovery_in_the_real_process_order():
    assert _audit(lambda r: r) == []
    assert _audit(lambda r: [x for x in r if x["pid"] not in (101, 201)]) == []
    objdump = ["/usr/bin/objdump", "-p", "-j", ".dynamic", "/usr/lib/libSDL2-2.0.so.0.3200.10"]
    linked = ["ld", "-t", "-L", "/opt/lib", "-o", "/dev/null", "-lSDL2_image-2.0d"]
    more = [_record(100, 50, *objdump), _record(201, 200, *linked), _record(100, 50, *linked)]
    # V6 correction (reviewed flaw): the former fixture appended the build owner's
    # `linked` call after the --check child's records, i.e. late build discovery.
    assert _audit(lambda r: [*r[:13], more[0], more[2], *r[13:], more[1]]) == []
    unrelated = _record(900, 899, "/usr/bin/gcc", "-c", "x.c")
    assert _audit(lambda r: [*r[:3], unrelated, *r[3:]]) == []


@pytest.mark.parametrize(
    ("change", "expected_text"),
    [
        (_moved(10, 6), "unexpected subprocess records"),
        (_extra(7, 101, 100, "/usr/bin/curl", "https://example.invalid"), "runtime probe"),
        (_extra(10, 100, 50, "/usr/bin/gcc", "-c", "x.c"), "unexpected subprocess records"),
        (_extra(10, 100, 50, "/sbin/ldconfig", "-p", "-v"), "unexpected subprocess records"),
        (_extra(10, 100, 50, "/usr/bin/gcc", "-Wl,-t", "-o", "/t/x", "-lc"), "unexpected"),
        (_extra(10, 100, 50, "/usr/bin/gcc", "-Wl,-t", "-o", "t/x", "-lSDL2"), "unexpected"),
        (_extra(10, 100, 50, "ld", "-t", "-o", "/tmp/x", "-lSDL2"), "unexpected"),
        (_extra(10, 100, 50, "/tmp/ldconfig", "-p"), "unexpected"),
        (_extra(10, 100, 50, "/usr/bin/objdump", "-p", "/lib/libSDL2.so"), "unexpected"),
        (_extra(7, 999, 998, "/sbin/ldconfig", "-p"), "install window"),
        (_extra(14, 201, 200, "/usr/bin/gcc", "-c", "x.c"), "runtime probe"),
        (_extra(15, 300, 200, "/sbin/ldconfig", "-p"), "runtime probe"),
        (_extra(14, 200, 50, "/sbin/ldconfig", "-p"), "--check process"),
        (_extra(14, 200, 50, "/usr/bin/gcc", "-c", "x.c"), "--check process"),
        (lambda r: [*r, dict(r[13])], "exactly one runtime probe"),
        (lambda r: [*r, {**r[13], "pid": 300}], "exactly one runtime probe"),
        (lambda r: [*r[:13], *r[14:]], "exactly one runtime probe"),
        (_moved(13, 6), "exactly one runtime probe"),
        (
            lambda r: [
                *r[:13],
                {**r[13], "argv": ["/usr/bin/python3", *r[13]["argv"][1:]]},
                *r[14:],
            ],
            "exactly one runtime probe",
        ),
        (lambda r: [{k: v for k, v in x.items() if k != "ppid"} for x in r], "malformed"),
        (lambda r: [{**x, "ppid": True} for x in r], "malformed"),
        (lambda r: [{**x, "ppid": "50"} for x in r], "malformed"),
    ],
    ids=[
        "discovery-before-build-probe",
        "unknown-exe-from-probe-child",
        "unknown-owner-call-after-probe",
        "discovery-with-extra-flag",
        "discovery-unrelated-library",
        "discovery-relative-output",
        "discovery-ld-wrong-output",
        "discovery-untrusted-ldconfig-path",
        "discovery-objdump-without-dynamic-section",
        "discovery-from-unrelated-process",
        "unknown-call-from-check-probe-child",
        "discovery-from-wrong-parent",
        "discovery-from-check-owner",
        "unknown-call-from-check-owner",
        "duplicate-check-probe",
        "duplicate-probe-other-pid",
        "missing-check-probe",
        "check-probe-before-build-probe",
        "foreign-interpreter-check-probe",
        "no-ppid",
        "bool-ppid",
        "string-ppid",
    ],
)
def test_uv_audit_rejects_unjustified_or_misplaced_subprocess_records(change, expected_text):
    problems = _audit(change)
    assert any(expected_text in problem for problem in problems), problems


def _objdump(library):
    return ["/usr/bin/objdump", "-p", "-j", ".dynamic", f"/usr/lib/{library}"]


@pytest.mark.parametrize(
    "library",
    [
        "libSDL2.so",
        "libSDL2-2.0.so.0",
        "libSDL2-2.0.so.0.3200.10",
        "libSDL2d.so",
        "libSDL2-2.0d.so",
        "libSDL2_image-2.0.so.0",
        "libSDL2_ttf.so",
    ],
)
def test_uv_audit_accepts_real_sdl_library_names_for_objdump(library):
    assert _audit(_extra(13, 100, 50, *_objdump(library))) == []
    assert _audit(_extra(15, 201, 200, *_objdump(library))) == []


def _shell_parent(pids, ppid):
    return lambda r: [{**x, "ppid": ppid} if x["pid"] in pids else x for x in r]


@pytest.mark.parametrize(
    ("change", "expected_text"),
    [
        (lambda r: [dict(r[13]), *r], "exactly one runtime probe"),
        (lambda r: [{**r[13], "pid": 300}, *r], "exactly one runtime probe"),
        (lambda r: [*r[:3], {**r[13], "pid": 300}, *r[3:]], "exactly one runtime probe"),
        (lambda r: [*r[:6], {**r[13], "pid": 300}, *r[6:]], "exactly one runtime probe"),
        (lambda r: [*r[:6], dict(r[13]), *r[6:]], "exactly one runtime probe"),
        (lambda r: [{**r[13], "pid": 300}, *r[:14], *r[15:]], "exactly one runtime probe"),
        (_shell_parent({200, 201}, 100), "shared shell parent"),
        (_shell_parent({200, 201}, 101), "shared shell parent"),
        (_shell_parent({200, 201}, 51), "shared shell parent"),
        (_shell_parent({100}, 51), "shared shell parent"),
        (lambda r: [{**x, "ppid": 51} if i == 1 else x for i, x in enumerate(r)], "shell parent"),
        (lambda r: [*r, _record(100, 52, "/sbin/ldconfig", "-p")], "shell parent"),
        (lambda r: [*r[:13], *r[14:], r[13]], "runtime probe"),
        (lambda r: [*r[:10], *r[11:14], r[10], *r[14:]], "unexpected subprocess records"),
        (lambda r: [*r[:10], *r[11:], r[10]], "unexpected subprocess records"),
        (lambda r: [*r[:7], *r[10:], *r[7:10]], "runtime probe"),
        (lambda r: [*r[:7], *r[8:], r[7]], "runtime probe"),
        (_extra(13, 100, 50, *_objdump("libSDL2_unrelated.so")), "unexpected"),
        (_extra(13, 100, 50, *_objdump("libSDL2evil.so")), "unexpected"),
        (_extra(13, 100, 50, *_objdump("libSDL2-2.0.so.0.evil")), "unexpected"),
        (_extra(13, 100, 50, *_objdump("libSDL2_mixer-2.0.so.0")), "unexpected"),
        (_extra(13, 100, 50, *_objdump("libSDL3.so")), "unexpected"),
        (_extra(13, 100, 50, *_objdump("libSDL2.so.x")), "unexpected"),
        (_extra(15, 201, 200, *_objdump("libSDL2_unrelated.so")), "runtime probe"),
        (_extra(13, 100, 50, *_objdump("../libSDL2.so")[:4], "libSDL2.so"), "unexpected"),
    ],
    ids=[
        "early-duplicate-check-probe",
        "early-foreign-probe-before-installs",
        "early-foreign-probe-in-install-window",
        "early-foreign-probe-before-build-probe",
        "early-same-checker-probe",
        "early-foreign-probe-replacing-check",
        "checker-child-of-build-owner",
        "checker-nested-under-probe-child",
        "checker-unrelated-parent",
        "owner-unrelated-parent",
        "owner-inconsistent-parent",
        "owner-record-with-second-parent",
        "check-probe-after-its-discovery",
        "late-build-discovery-between-check-probe-and-child",
        "late-build-owner-discovery-after-check",
        "late-build-probe-child-discovery-after-check",
        "late-build-probe-child-single-discovery",
        "objdump-sdl2-unrelated-suffix",
        "objdump-sdl2evil",
        "objdump-non-numeric-version",
        "objdump-other-sdl-module",
        "objdump-sdl3",
        "objdump-text-version",
        "check-child-objdump-unrelated",
        "objdump-relative-path",
    ],
)
def test_uv_audit_rejects_early_probes_inconsistent_parents_late_build_and_unrelated_libs(
    change, expected_text
):
    problems = _audit(change)
    assert any(expected_text in problem for problem in problems), problems


_SUFFIXES = {
    "linux-tagged": ".cpython-312-x86_64-linux-gnu.so",
    "linux-abi3": ".abi3.so",
    "windows-tagged": ".cp312-win_amd64.pyd",
    "windows-abi3": ".abi3.pyd",
    "linux-untagged": ".so",
    "windows-untagged": ".pyd",
}


def _suffix_case(suffix, optional, required=None):
    from tests._bootstrap_stage_test_support import uv_expected, uv_native_build

    ci = _uv_ci()
    expected = uv_expected(ci)
    document = uv_native_build(expected, optional=optional, suffix=suffix, required_suffix=required)
    return ci.validate_native_build(document, expected, document["runtime_identity"])


@pytest.mark.parametrize("optional", ["source", "cython"])
@pytest.mark.parametrize("suffix", _SUFFIXES.values(), ids=_SUFFIXES)
def test_native_build_accepts_every_platform_extension_filename_form(suffix, optional):
    assert _suffix_case(suffix, optional) == []
    mixed = _suffix_case(".so", optional, required=suffix)
    assert mixed == []


@pytest.mark.parametrize(
    "suffix",
    [".dll", ".so.txt", ".pyc", ".cpython-312-x86_64-linux-gnu", ".txt.so.py", "so", ".a.b.so"],
    ids=["dll", "so-txt", "pyc", "no-extension", "py-after-so", "bare-so", "dotted-stem"],
)
def test_native_build_rejects_artifact_names_that_are_not_extension_modules(suffix):
    problems = _suffix_case(suffix, "cython")
    assert any("pyboy.utils" in problem for problem in problems), problems


def test_provenance_tests_collect_without_the_unix_only_reservation_probe():
    # The isolated interpreter makes `import fcntl` fail as on Windows. The pure UV fixtures
    # and the provenance module must still import, while the reservation support, which
    # needs fcntl, must not: that proves the trap is live. No Windows run is claimed.
    code = (
        "import sys\nsys.modules['fcntl'] = None\n"
        "import tests._bootstrap_stage_test_support, tests.test_vendored_provenance_wording\n"
        "print('imported')\n"
        "try:\n    import tests._qualification_runner_support\n"
        "except ImportError:\n    print('trap-live')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["imported", "trap-live"], done.stdout


def test_uv_instrument_records_pid_and_parent_on_posix_layouts(tmp_path):
    # POSIX-specific scope: needs a `bin/python` venv layout and the .pth audit hook; the
    # Windows `Scripts` layout and PowerShell hosts are outside this unit control.
    import venv

    output = tmp_path / "out"
    (output / "evidence").mkdir(parents=True)
    env_dir = output / "work" / "uvenv"
    venv.create(env_dir, with_pip=False)
    python = str(env_dir / "bin" / "python")
    script = str(REPO_ROOT / "scripts" / "native_unit_ci.py")
    setup = subprocess.run(
        [python, script, "uv-instrument", str(output)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert setup.returncode == 0, setup.stderr
    active = {**os.environ, "NATIVE_UV_AUDIT_ACTIVE": "1"}
    code = "import subprocess, os; subprocess.run(['true', 'marker']); print(os.getpid())"
    done = subprocess.run(
        [python, "-c", code], capture_output=True, text=True, timeout=60, env=active, check=False
    )
    records = [
        json.loads(line)
        for line in (output / "evidence" / "uv-audit.jsonl").read_text().splitlines()
    ]
    mine = [r for r in records if r["argv"][-1] == "marker"]
    assert len(mine) == 1 and mine[0]["pid"] == int(done.stdout)
    assert mine[0]["ppid"] == os.getpid() and mine[0]["executable"] == "true"
