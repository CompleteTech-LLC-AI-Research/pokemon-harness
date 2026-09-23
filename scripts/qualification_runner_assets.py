"""Asset, native-build and prerequisite validation.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_facts import CheckResult, _is_sha1, _is_sha256, _result
from scripts.qualification_runner_host import _resolve_declared_path, _sha256_of_file
from scripts.qualification_runner_model import (
    _EXTENSION_BACKED_MODULES,
    _NATIVE_BUILD_EVIDENCE_PROCEDURE,
    _NATIVE_BUILD_EVIDENCE_VERSION,
    _NATIVE_PROBE,
    _RUNTIME_MODULES,
)


def _sha1_of_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_versions_pins(repo_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return ROM and symbol pins keyed by root-relative and full paths."""

    try:
        from pokered_harness.config import load_versions
    except ImportError:
        return {}, {}
    versions_path = repo_root / "VERSIONS.md"
    if not versions_path.is_file():
        return {}, {}
    try:
        config = load_versions(versions_path)
    except (OSError, ValueError):
        return {}, {}
    rom_pins: dict[str, str] = {}
    symbol_pins: dict[str, str] = {}
    for documented, sha1 in config.rom_sha1_by_path:
        rom_pins[documented] = sha1
        rom_pins[documented.removeprefix("rom/")] = sha1
    for documented, sha1 in config.symbol_sha1_by_path:
        symbol_pins[documented] = sha1
        symbol_pins[documented.removeprefix("rom/")] = sha1
    return rom_pins, symbol_pins


def _canonical_asset_key(value: str | Path) -> str:
    key = str(value).replace("\\", "/").lstrip("./").lower()
    return key.removeprefix("rom/")


@dataclass
class _AssetRequirement:
    key: str
    root: str
    sha1: str | None = None
    sha256: str | None = None
    size: int | None = None


def _fixture_requirements(repo_root: Path) -> dict[str, _AssetRequirement]:
    manifest_path = repo_root / "release-evidence" / "fixture-manifest.json"
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    fixtures = document.get("fixtures")
    if not isinstance(fixtures, list):
        return {}
    requirements: dict[str, _AssetRequirement] = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        relative = fixture.get("path")
        if not isinstance(relative, str) or not relative.strip():
            continue
        key = _canonical_asset_key(relative)
        sha1 = fixture.get("sha1")
        sha256 = fixture.get("sha256")
        size = fixture.get("size_bytes")
        requirements[key] = _AssetRequirement(
            key=key,
            root="fixture_root",
            sha1=sha1.lower() if _is_sha1(sha1) else None,
            sha256=sha256.lower() if _is_sha256(sha256) else None,
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        )
    return requirements


def _required_asset_entries(
    declaration: dict[str, Any], repo_root: Path
) -> dict[tuple[str, str], _AssetRequirement]:
    """Return the complete pinned ROM/SYM/fixture set for the declared scope."""

    assets = declaration.get("assets")
    assets = assets if isinstance(assets, dict) else {}
    scope = assets.get("scope")
    allowed: set[str] | None = None
    if isinstance(scope, list) and scope:
        allowed = {str(item).lower() for item in scope}

    def in_scope(key: str) -> bool:
        if allowed is None:
            return True
        return bool(allowed.intersection(Path(key).parts))

    requirements: dict[tuple[str, str], _AssetRequirement] = {}

    def add(requirement: _AssetRequirement) -> None:
        if in_scope(requirement.key):
            requirements[(requirement.root, requirement.key)] = requirement

    rom_pins, symbol_pins = _load_versions_pins(repo_root)
    for documented, sha1 in rom_pins.items():
        add(_AssetRequirement(key=_canonical_asset_key(documented), root="rom_root", sha1=sha1))
    for documented, sha1 in symbol_pins.items():
        add(_AssetRequirement(key=_canonical_asset_key(documented), root="rom_root", sha1=sha1))
    for requirement in _fixture_requirements(repo_root).values():
        add(requirement)
    return requirements


def _symlinked_path_component(root: Path, relative: str) -> str | None:
    """Return a relative component of *root*/*relative* that is a symlink, or ``None``.

    A required input is only trustworthy if every step of its path inside the
    asset root is a real directory entry.  Checking just the final component
    would accept a read-only leaf reached through a linked parent, because the
    leaf itself is not a link.
    """

    current = root
    parts: list[str] = []
    for part in Path(relative).parts:
        current = current / part
        parts.append(part)
        if current.is_symlink():
            return "/".join(parts)
    return None


def _verify_required_asset(
    name: str, requirement: _AssetRequirement, root: Path, resolved: Path
) -> CheckResult:
    relative = requirement.key
    linked = _symlinked_path_component(root, relative)
    if linked is not None:
        return _result(
            name,
            "fail",
            "required file",
            linked,
            "required input is reached through a symbolic link; refusing a linked input",
        )
    if resolved.is_symlink() or not resolved.is_file():
        return _result(name, "fail", "required file", relative, "required input is missing")
    try:
        size = resolved.stat().st_size
        if size <= 0:
            return _result(name, "fail", "non-empty file", relative, "required input is empty")
        if requirement.size is not None and size != requirement.size:
            return _result(
                name,
                "fail",
                requirement.size,
                size,
                "required input size does not match the pin",
            )
        actual_sha1 = _sha1_of_file(resolved)
        actual_sha256 = _sha256_of_file(resolved) if requirement.sha256 else None
    except OSError as exc:
        return _result(
            name, "fail", "readable file", relative, f"required input is unreadable: {exc}"
        )
    if requirement.sha1 is not None and actual_sha1 != requirement.sha1:
        return _result(
            name,
            "fail",
            requirement.sha1,
            actual_sha1,
            "required input bytes do not match the pinned SHA-1",
        )
    if (
        requirement.sha256 is not None
        and actual_sha256 is not None
        and actual_sha256 != requirement.sha256
    ):
        return _result(
            name,
            "fail",
            requirement.sha256,
            actual_sha256,
            "required input bytes do not match the pinned SHA-256",
        )
    if requirement.sha1 is None and requirement.sha256 is None:
        return _result(name, "fail", "pinned hash", None, "required input has no verifiable pin")
    return _result(
        name, "ok", requirement.sha1 or requirement.sha256, actual_sha1, "required input verified"
    )


def validate_asset_inputs(declaration: dict[str, Any], repo_root: Path) -> list[CheckResult]:
    """Verify the complete pinned ROM/SYM/fixture set for the declared scope.

    Every repository pin for the declared scope must be present on disk and
    hash-valid.  Operator-declared inputs may only name files inside that pinned
    set; an unknown or unrelated path fails instead of being trusted.
    """

    assets = declaration.get("assets") or {}
    results: list[CheckResult] = []
    rom_root_value = assets.get("rom_root")
    rom_root = Path(rom_root_value) if rom_root_value else None
    if rom_root is None or not rom_root.is_dir():
        results.append(
            _result(
                "assets-rom-root",
                "fail",
                "directory",
                rom_root_value,
                "rom_root is not a directory",
            )
        )
    fixture_root_value = assets.get("fixture_root")
    fixture_root = Path(fixture_root_value) if fixture_root_value else None
    if fixture_root is None or not fixture_root.is_dir():
        results.append(
            _result(
                "assets-fixture-root",
                "fail",
                "directory",
                fixture_root_value,
                "fixture_root is not a directory",
            )
        )

    requirements = _entry._required_asset_entries(declaration, repo_root)
    if not requirements:
        results.append(
            _result(
                "assets-required-set",
                "unsupported",
                "complete pinned input set",
                {},
                "the repository pins for the declared scope could not be resolved",
            )
        )
        return results

    inputs = assets.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        results.append(
            _result(
                "assets-inputs",
                "fail",
                "non-empty list of {path, sha1}",
                inputs,
                "declared ROM/SYM inputs are required",
            )
        )
        return results

    required_keys = {(req.root, req.key) for req in requirements.values()}
    rom_keys = {key for root, key in required_keys if root == "rom_root"}
    for index, entry in enumerate(inputs):
        name = f"assets-input-{index}"
        if not isinstance(entry, dict):
            results.append(_result(name, "fail", "object", entry, "asset input must be an object"))
            continue
        relative = entry.get("path")
        declared_sha1 = entry.get("sha1")
        if not isinstance(relative, str) or not relative.strip():
            results.append(
                _result(name, "fail", "relative path", relative, "asset path is required")
            )
            continue
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            results.append(
                _result(
                    name,
                    "fail",
                    "relative path under rom_root",
                    relative,
                    "asset input must stay under rom_root",
                )
            )
            continue
        key = _canonical_asset_key(relative)
        if ("rom_root", key) not in required_keys and ("fixture_root", key) not in required_keys:
            results.append(
                _result(
                    name,
                    "fail",
                    "a path in the pinned input set",
                    relative,
                    "asset input is not part of the declared qualification scope",
                )
            )
            continue
        if key in rom_keys:
            expected = requirements[("rom_root", key)].sha1
            if declared_sha1 is not None and _is_sha1(declared_sha1):
                if expected is not None and declared_sha1.lower() != expected:
                    results.append(
                        _result(
                            name,
                            "fail",
                            expected,
                            declared_sha1,
                            "declared hash disagrees with the repository pin",
                        )
                    )
                    continue
            elif expected is not None:
                results.append(
                    _result(
                        name,
                        "fail",
                        expected,
                        declared_sha1,
                        "declared input must carry the pinned SHA-1",
                    )
                )
                continue
        results.append(_result(name, "ok", key, key, "asset input is in the pinned scope"))

    rom_root_path = rom_root if rom_root is not None and rom_root.is_dir() else None
    fixture_root_path = fixture_root if fixture_root is not None and fixture_root.is_dir() else None
    ordered = sorted(requirements.values(), key=lambda req: (req.root, req.key))
    for index, requirement in enumerate(ordered):
        name = f"assets-required-{requirement.root}-{index}"
        root_path = rom_root_path if requirement.root == "rom_root" else fixture_root_path
        if root_path is None:
            continue
        results.append(
            _verify_required_asset(name, requirement, root_path, root_path / requirement.key)
        )
    return results


def _load_bootstrap_module() -> Any:
    path = Path(__file__).resolve().parent / "bootstrap_pyboy.py"
    try:
        spec = importlib.util.spec_from_file_location("_qualification_bootstrap_pyboy", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - absence of the bootstrap is reported, not raised
        return None


def _native_source_digest(repo_root: Path) -> str | None:
    """Recompute the deterministic native build-input digest from source.

    This mirrors the fresh-snapshot staging in ``scripts/bootstrap_pyboy.py`` so
    an installed extension build can be tied back to the pinned source.
    """

    bootstrap = _load_bootstrap_module()
    if bootstrap is None:
        return None
    source_root = repo_root / "vendor" / "pyboy-src"
    if source_root.is_symlink() or not source_root.is_dir():
        return None
    revision_file = source_root / "POKERED_HARNESS_PYBOY_REVISION"
    suffixes = bootstrap.NATIVE_INPUT_SUFFIXES
    digest = hashlib.sha256()
    try:
        for directory, children, filenames in os.walk(source_root):
            children[:] = sorted(
                name
                for name in children
                if not name.startswith(".")
                and name not in {"build", "dist", "__pycache__", "venv"}
                and not name.endswith(".egg-info")
            )
            for name in children:
                if (Path(directory) / name).is_symlink():
                    return None
            for name in sorted(filenames):
                source = Path(directory) / name
                if source.suffix not in suffixes and source != revision_file:
                    continue
                if source.is_symlink():
                    return None
                relative = source.relative_to(source_root)
                digest.update(relative.as_posix().encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(source.read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()


def _load_native_build_evidence(path: Path) -> tuple[dict[str, Any] | None, str]:
    if path.is_symlink():
        return None, "the retained native build evidence must be a regular file"
    if not path.is_file():
        return None, "the retained native build evidence is missing"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the retained native build evidence is not readable JSON: {exc}"
    if not isinstance(document, dict):
        return None, "the retained native build evidence must be a JSON object"
    return document, ""


def _native_build_evidence(
    declaration: dict[str, Any],
    repo_root: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> list[CheckResult]:
    """Tie the installed native runtime to a pinned, consistent source build."""

    results: list[CheckResult] = []
    interpreters = declaration.get("interpreters")
    interpreters = interpreters if isinstance(interpreters, dict) else {}
    native = interpreters.get("native")
    expected_inputs = interpreters.get("native_build_inputs_sha256")
    expected_fingerprint = interpreters.get("native_fingerprint")
    if not native:
        results.append(
            _result(
                "native-build-inputs", "fail", "native interpreter", None, "missing interpreter"
            )
        )
        return results

    source_digest = _entry._native_source_digest(repo_root)
    if source_digest is None:
        results.append(
            _result(
                "native-build-inputs",
                "unsupported",
                "pinned vendored PyBoy source",
                None,
                "the vendored source snapshot is unavailable for a consistent-build check",
            )
        )
    elif not _is_sha256(expected_inputs):
        results.append(
            _result(
                "native-build-inputs",
                "fail",
                "pinned native build-inputs sha256",
                expected_inputs,
                "the expected build-inputs digest is not pinned in the declaration",
            )
        )
    elif source_digest != expected_inputs.strip().lower():
        results.append(
            _result(
                "native-build-inputs",
                "fail",
                expected_inputs,
                source_digest,
                "the vendored native build inputs do not match the pinned digest",
            )
        )
    else:
        results.append(
            _result(
                "native-build-inputs",
                "ok",
                expected_inputs,
                source_digest,
                "the vendored source matches the pinned native build-inputs digest",
            )
        )

    probe = runner([str(native), "-c", _NATIVE_PROBE % {"modules": _RUNTIME_MODULES}], repo_root)
    if probe.returncode != 0:
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "installed native fingerprint",
                probe.returncode,
                (probe.stdout + probe.stderr).strip()[-300:],
            )
        )
        return results
    try:
        payload = json.loads(probe.stdout)
    except (TypeError, ValueError):
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "fingerprint JSON",
                probe.stdout.strip()[-120:],
                "the native runtime probe did not report parseable JSON",
            )
        )
        return results
    identity = payload.get("identity") if isinstance(payload, dict) else None
    fingerprint = payload.get("fingerprint") if isinstance(payload, dict) else None
    if not isinstance(identity, dict) or not _is_sha256(fingerprint):
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "fingerprint JSON",
                None,
                "the native runtime probe did not report an identity and fingerprint",
            )
        )
        return results

    bootstrap = _load_bootstrap_module()
    expected_revision = getattr(bootstrap, "EXPECTED_REVISION", None)
    problems: list[str] = []
    if identity.get("revision") != expected_revision:
        problems.append("the installed extension revision does not match the pinned fork")
    if identity.get("cython_compiled") is not True:
        problems.append("the installed runtime does not report compiled extensions")
    modules = identity.get("modules")
    modules = modules if isinstance(modules, dict) else {}
    artifacts = identity.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    if not artifacts:
        problems.append(
            "the installed runtime did not report its complete installed output set, "
            "so a replaced compiled module could go undetected"
        )
    for name in _EXTENSION_BACKED_MODULES:
        entry = modules.get(name)
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind != "cython":
            problems.append(f"{name} is not an installed extension")
        artifact = entry.get("artifact") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(artifact, str) or artifacts.get(artifact) != digest:
            problems.append(f"{name} is not covered by the reported complete installed output set")
    if problems:
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "installed extensions with a pinned fingerprint",
                fingerprint,
                "; ".join(problems),
            )
        )
    elif not _is_sha256(expected_fingerprint):
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "pinned native runtime fingerprint",
                expected_fingerprint,
                "the expected native runtime fingerprint is not pinned in the declaration",
            )
        )
    elif fingerprint.lower() != expected_fingerprint.strip().lower():
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                expected_fingerprint,
                fingerprint,
                "the installed native runtime does not match the pinned fingerprint",
            )
        )
    else:
        results.append(
            _result(
                "native-runtime-fingerprint",
                "ok",
                expected_fingerprint,
                fingerprint,
                "the installed native runtime matches the pinned consistent-build fingerprint",
            )
        )
    results.append(
        _native_build_evidence_check(
            interpreters,
            repo_root,
            expected_inputs,
            expected_fingerprint,
            fingerprint,
            identity,
        )
    )
    return results


def _native_build_evidence_check(
    interpreters: dict[str, Any],
    repo_root: Path,
    expected_inputs: Any,
    expected_fingerprint: Any,
    probe_fingerprint: str,
    probe_identity: dict[str, Any],
) -> CheckResult:
    """Require retained evidence from the complete fresh native build procedure.

    Pinning the source bytes and the installed fingerprint independently does not
    prove the installed extensions were compiled from the pinned inputs: a
    pre-existing mixed build satisfies both comparisons.  The operator must
    retain the fresh-build procedure's own record connecting the staged inputs,
    build completion, and installed outputs; absent that record the check is
    ``unsupported`` rather than a pass.

    The record is only meaningful if a hand-written document cannot pass.  The
    validator therefore requires the fields that the executed procedure alone
    emits -- the evidence format version, the producing script's own digest, and
    a full runtime identity whose canonical fingerprint must equal both the
    recorded fingerprint and the fingerprint observed in the live runtime --
    and recomputes every derivable value instead of trusting the document's own
    summary.  This binds the record to the on-host bootstrap and to the exact
    installed extension bytes; it is not a cryptographic attestation against a
    determined operator that has already read those same bytes.
    """

    evidence_path = _resolve_declared_path(interpreters.get("native_build_evidence"), repo_root)
    if evidence_path is None:
        return _result(
            "native-build-evidence",
            "unsupported",
            "retained fresh native build evidence",
            None,
            "no retained evidence connects the pinned inputs to the installed outputs",
        )
    name = "native-build-evidence"
    evidence_sha = interpreters.get("native_build_evidence_sha256")
    if not _is_sha256(evidence_sha):
        return _result(
            name,
            "fail",
            "sha256 pin for the retained evidence",
            evidence_sha,
            "the retained native build evidence must be pinned by SHA-256",
        )
    try:
        actual_sha = _sha256_of_file(evidence_path)
    except OSError:
        return _result(
            name,
            "unsupported",
            evidence_sha,
            None,
            "the retained native build evidence could not be read",
        )
    if actual_sha != evidence_sha.strip().lower():
        return _result(
            name,
            "fail",
            evidence_sha,
            actual_sha,
            "the retained native build evidence bytes do not match the pinned digest",
        )
    document, error = _load_native_build_evidence(evidence_path)
    if document is None:
        return _result(name, "fail", "valid retained evidence JSON", None, error)
    problems: list[str] = []
    if document.get("evidence_version") != _NATIVE_BUILD_EVIDENCE_VERSION:
        problems.append(
            "the retained evidence does not declare the supported evidence format version"
        )
    if document.get("procedure") != _NATIVE_BUILD_EVIDENCE_PROCEDURE:
        problems.append(
            "the retained evidence was not produced by the fresh native build procedure"
        )
    if document.get("mode") != "cython":
        problems.append("the retained evidence does not record a cython-mode build")
    if document.get("status") != "complete":
        problems.append("the retained evidence does not record a completed native build")
    recorded_inputs = document.get("build_inputs_sha256")
    if recorded_inputs != expected_inputs:
        problems.append("the retained evidence staged inputs do not match the pinned build inputs")
    source_digest = _entry._native_source_digest(repo_root)
    if source_digest is not None and recorded_inputs != source_digest:
        problems.append(
            "the retained evidence staged inputs do not match the vendored build inputs"
        )
    problems.extend(_build_evidence_producer_problems(document))

    recorded_identity = document.get("runtime_identity")
    recorded_fingerprint = document.get("installed_fingerprint")
    if not isinstance(recorded_identity, dict):
        problems.append("the retained evidence does not record the installed runtime identity")
    else:
        derived = _native_build_fingerprint(recorded_identity)
        if derived != recorded_fingerprint:
            problems.append(
                "the retained evidence runtime identity does not correspond to its recorded "
                "fingerprint"
            )
        if recorded_identity != probe_identity:
            problems.append(
                "the retained evidence runtime identity does not match the installed runtime"
            )
    if recorded_fingerprint != expected_fingerprint:
        problems.append(
            "the retained evidence installed outputs do not match the pinned fingerprint"
        )
    if recorded_fingerprint != probe_fingerprint:
        problems.append(
            "the retained evidence installed outputs do not match the installed runtime"
        )
    if problems:
        return _result(
            name,
            "fail",
            "a consistent fresh native build",
            recorded_fingerprint,
            "; ".join(problems),
        )
    return _result(
        name,
        "ok",
        expected_inputs,
        recorded_fingerprint,
        "the retained evidence ties the pinned inputs and completed build to the installed outputs",
    )


def _build_evidence_producer_problems(document: dict[str, Any]) -> list[str]:
    """Verify the record identifies the bootstrap script that actually ran.

    A document that omits the producer entry, or pins a digest that does not
    match the checked-in bootstrap, was not emitted by this host's build
    procedure.  The script bytes are recomputed here rather than trusted from
    the declaration.
    """

    producer = document.get("producer")
    if not isinstance(producer, dict):
        return ["the retained evidence does not identify the producing build script"]
    problems: list[str] = []
    if producer.get("script") != "scripts/bootstrap_pyboy.py":
        problems.append("the retained evidence names an unrecognized producing build script")
    script_path = Path(__file__).resolve().parent / "bootstrap_pyboy.py"
    try:
        actual_script_sha = _sha256_of_file(script_path)
    except OSError:
        return problems + ["the checked-in bootstrap script could not be read"]
    if producer.get("script_sha256") != actual_script_sha:
        problems.append("the retained evidence was not produced by the checked-in bootstrap script")
    return problems


def _native_build_fingerprint(identity: dict[str, Any]) -> str:
    """Recompute the canonical native runtime fingerprint from an identity.

    This mirrors the digest calculation in ``_NATIVE_PROBE`` so a recorded
    identity can be checked against the fingerprint claimed for it.
    """

    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _asset_tree_immutability_problem(root: Path) -> str | None:
    """Return why *root* is not a provably immutable asset tree, else ``None``.

    A read-only directory containing a writable subdirectory is not immutable:
    the owner can delete or replace a protected file through that parent, so
    directories are inspected as well as files.  Symbolic links are refused
    rather than followed: ``Path.rglob`` does not descend into a linked
    directory, so a link can hide a writable file from the scan while the linked
    path still resolves for a reader.  Every entry is walked without following
    links, and an unreadable tree fails closed.
    """

    if root.is_symlink():
        return (
            "the asset root is a symbolic link; a linked tree cannot be shown to be "
            "read-only, so it is refused"
        )
    if os.access(root, os.W_OK):
        return "the asset root is writable by this job; mount it read-only"

    def raise_walk_error(error: OSError) -> None:
        # ``os.walk`` swallows listing errors by default, so an unlistable
        # directory would silently vanish from the scan.  A searchable but
        # unlistable directory (mode 0111) can still hand a writer a protected
        # file, so an unobservable tree must fail closed rather than pass.
        raise error

    try:
        for directory, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=raise_walk_error
        ):
            base = Path(directory)
            for name in dirnames + filenames:
                entry = base / name
                if entry.is_symlink():
                    return (
                        f"the asset root contains the symbolic link {name!r}; a linked "
                        "directory can hide a writable file from the immutability scan, "
                        "so it is refused"
                    )
                if os.access(entry, os.W_OK):
                    return (
                        "the asset root contains a writable entry; an owner can replace a "
                        "protected file through that parent"
                    )
    except OSError as exc:
        return f"the asset tree could not be inspected ({exc}); refusing to admit it"
    return None


def _immutable_asset_checks(declaration: dict[str, Any], repo_root: Path) -> list[CheckResult]:
    assets = declaration.get("assets")
    assets = assets if isinstance(assets, dict) else {}
    results: list[CheckResult] = []
    for key in ("rom_root", "fixture_root"):
        value = assets.get(key)
        path = Path(value) if isinstance(value, str) and value else None
        if path is None or not path.is_dir():
            continue
        problem = _entry._asset_tree_immutability_problem(path)
        results.append(
            _result(
                f"assets-immutable-{key}",
                "fail" if problem else "ok",
                "read-only shared asset root",
                path.name,
                problem or "the asset root is read-only for this job",
            )
        )
    return results


def prerequisite_checks(
    declaration: dict[str, Any],
    repo_root: Path,
    runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
) -> list[CheckResult]:
    """Verify interpreters and pinned assets using the repository validators."""

    if runner is None:
        runner = _entry.run_command
    results: list[CheckResult] = []
    interpreters = declaration.get("interpreters") or {}
    for mode in ("source", "native"):
        python = interpreters.get(mode)
        if not python:
            results.append(
                _result(f"interpreter-{mode}", "fail", "path", None, "missing interpreter path")
            )
            continue
        if not Path(python).exists():
            results.append(
                _result(f"interpreter-{mode}", "fail", python, None, "interpreter not found")
            )
            continue
        bootstrap_mode = "cython" if mode == "native" else "source"
        proc = runner(
            [str(python), "scripts/bootstrap_pyboy.py", "--mode", bootstrap_mode, "--check"],
            repo_root,
        )
        results.append(
            _result(
                f"interpreter-{mode}",
                "ok" if proc.returncode == 0 else "fail",
                f"bootstrap --mode {bootstrap_mode} --check",
                proc.returncode,
                (proc.stdout + proc.stderr).strip()[-300:],
            )
        )

    assets = declaration.get("assets") or {}
    results.extend(_immutable_asset_checks(declaration, repo_root))
    results.extend(_native_build_evidence(declaration, repo_root, runner))
    results.extend(validate_asset_inputs(declaration, repo_root))

    manifest = repo_root / "release-evidence" / "fixture-manifest.json"
    schema = runner(
        [
            sys.executable,
            "scripts/validate_fixture_manifest.py",
            "--manifest",
            str(manifest),
            "--schema-only",
        ],
        repo_root,
    )
    results.append(
        _result(
            "fixture-manifest-schema",
            "ok" if schema.returncode == 0 else "fail",
            "schema-only",
            schema.returncode,
            (schema.stdout + schema.stderr).strip()[-300:],
        )
    )
    fixture_root = assets.get("fixture_root")
    if fixture_root:
        validate = runner(
            [
                sys.executable,
                "scripts/validate_fixture_manifest.py",
                "--manifest",
                str(manifest),
                "--fixture-root",
                str(fixture_root),
            ],
            repo_root,
        )
        results.append(
            _result(
                "fixture-manifest-bytes",
                "ok" if validate.returncode == 0 else "fail",
                "byte-validated",
                validate.returncode,
                (validate.stdout + validate.stderr).strip()[-300:],
            )
        )
    return results


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
