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
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "pyboy-src"

FORK_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"
UPSTREAM_TAG_REVISION = "4627b90b878e91faff443b3acd6d4e4be09a4387"
PIN = "eceaa3bb15dedd6847a3a37d3400421e3024cb5c"

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
