"""Fail closed when the selected interpreter imports another tree's source.

The repository is developed across many Git worktrees that share one virtual
environment.  An editable install whose finder still points at a different
worktree makes every test result silently describe that other tree instead of
the checkout under test, in whichever direction it errs.  This check resolves
the two packages a release depends on -- ``pokered_harness`` and the vendored
``pyboy`` -- and refuses to continue unless both trace back to ``project_root``.

An origin counts as this checkout's when it either

* lives beneath ``project_root`` (a source tree or ``PYTHONPATH`` checkout), or
* lives in the site-packages directory of an installed distribution that is
  traceable to this checkout.

The second case is the release lane: CI runs ``pip install -e ".[dev]"`` into a
virtual environment created outside the checkout, so ``pyboy`` legitimately
resolves to ``<venv>/site-packages/pyboy``.  An install is traceable only
through the directory pip recorded in ``direct_url.json``, which for a local
install is always the literal source tree that was built.  There are two
legitimate source directories:

* ``project_root`` itself, for the editable harness install and any plain local
  install of the checkout.
* a staging directory *inside* this checkout's own ``build/`` tree, used by the
  native CI lane, which builds ``pyboy`` from a staged copy of
  ``vendor/pyboy-src`` so no generated C or object files can be reused.  The
  staged path is still physically inside the checkout, so location alone
  separates it from a sibling worktree.

Nothing the imported package says about itself is used as evidence.  The
vendored revision marker is a compile-time constant carried by every worktree
at the same pin, so it cannot distinguish two checkouts and an installed copy
could report any value it liked.  Provenance is decided by location only, which
is the property an installed copy cannot fabricate about itself.

A stale worktree satisfies neither: its install records that other directory,
which is neither this checkout nor a staging directory beneath this checkout.

Exit codes: ``0`` when every package traces to ``project_root``, ``1`` on any
mismatch or resolution failure, ``2`` on usage errors.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

# The distributions whose import origins decide what the suite actually
# measures.  ``pokered_harness`` is the harness under test and ``pyboy`` is the
# vendored emulator it drives; a stale copy of either invalidates a release.
REQUIRED_PACKAGES = ("pokered_harness", "pyboy")


def _is_within(candidate: Path, root: Path, *, strict: bool = False) -> bool:
    """Return whether ``candidate`` is ``root`` or lives beneath it.

    Both sides are resolved first.  A lexical comparison would treat
    ``<checkout>/build/pyboy-native-link`` as inside ``<checkout>/build``
    even when that name is a symlink into a sibling worktree, and would
    compare ``<checkout>/build/../../elsewhere`` as inside without ever
    applying the traversal.  Provenance is a statement about where a file
    really is, so the paths must be real before they are compared.

    ``strict`` additionally refuses a root that was not itself a real
    directory in the checkout.  Resolving is necessary but not sufficient: if
    ``<checkout>/build`` is a symlink into another worktree then resolving it
    yields that other worktree's build tree, and every source beneath it then
    compares as "inside" while living somewhere else entirely.  The staging
    root must therefore still be a real subdirectory of the checkout after
    resolution.
    """

    resolved_root = Path(root).resolve()
    if strict and Path(root).is_symlink():
        return False
    candidate = Path(candidate).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _installed_from(distribution_name: str, project_root: Path) -> Path | None:
    """Return the checkout an installed distribution was built from.

    ``direct_url.json`` is written by pip for both editable and local installs
    and names the directory the install was produced from.  A distribution
    without that record cannot be attributed to a checkout, so it contributes
    no allowed site-packages root.
    """

    try:
        distribution = _distribution(distribution_name)
        text = distribution.read_text("direct_url.json")
    except (OSError, importlib.metadata.PackageNotFoundError):
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    url = payload.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    if parsed.netloc not in ("", "localhost"):
        return None
    return Path(unquote(parsed.path)).resolve()


def _normalise_distribution_name(name: str) -> str:
    """Return the PEP 503 normalized form of a distribution name."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _distribution(distribution_name: str):
    """Return the installed distribution owning ``distribution_name``.

    ``packages_distributions`` reports owners in the form recorded at install
    time -- the native CI lane's wheel is named ``PyBoy`` -- while lookups by
    an ad-hoc spelling must still resolve.  Try the normalized name, then the
    literal one, so a case or separator difference cannot silently skip a
    distribution that would otherwise be admitted.
    """

    for candidate in dict.fromkeys(
        (
            _normalise_distribution_name(distribution_name),
            distribution_name,
        )
    ):
        try:
            return importlib.metadata.distribution(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise importlib.metadata.PackageNotFoundError(distribution_name)


def _staging_root(project_root: Path) -> Path:
    """Return the checkout-local staging directory used by the native build."""

    return project_root / "build"


def _is_this_checkout(project_root: Path, source: Path | None) -> bool:
    """Return whether a recorded install source belongs to this checkout.

    Two locations qualify, and both are decided by the filesystem rather than
    by anything the installed distribution reports about itself:

    * ``project_root`` -- an editable or plain local install of the checkout.
    * a directory inside this checkout's own ``build/`` tree -- the native lane
      stages ``vendor/pyboy-src`` there before building it, and pip records
      that staging path as the install source.  Requiring the recorded source
      to be *physically inside this checkout* is what a stale worktree cannot
      satisfy, because its own ``build/`` tree belongs to that other checkout.

    Anything else, including a missing record, an unresolvable path, or a
    staging directory that merely shares a name, is refused.
    """

    if source is None:
        return False
    root = project_root.resolve()
    source = Path(source).resolve()
    if source == root:
        return True
    return _is_within(source, _staging_root(root), strict=True)


def _allowed_roots(project_root: Path) -> list[Path]:
    """Return every directory whose contents legitimately serve this checkout.

    Mirrors ``bootstrap_pyboy._runtime_roots``: a distribution's own
    site-packages directory is allowed only when the distribution records this
    checkout as its source, and only for that distribution's own package
    directory.  Never the whole site-packages tree, which would let an
    unrelated copy of the package shadow the pinned one.
    """

    roots = [project_root]
    try:
        distributions = importlib.metadata.packages_distributions()
    except Exception:  # noqa: BLE001 - metadata is advisory here
        return roots
    for package in REQUIRED_PACKAGES:
        for owner in distributions.get(package, ()) or ():
            if not _is_this_checkout(project_root, _installed_from(owner, project_root)):
                continue
            try:
                located = _distribution(owner).locate_file(package)
            except (OSError, importlib.metadata.PackageNotFoundError):
                continue
            roots.append(Path(located).resolve())
    return roots


def _resolve_origin(package: str) -> tuple[Path | None, str]:
    """Import ``package`` and return ``(module file, error)`` for its origin."""

    try:
        module = __import__(package)
    except BaseException as exc:  # noqa: BLE001 - a failed import is a finding
        return None, f"import failed: {type(exc).__name__}: {exc}"
    origin = getattr(module, "__file__", None)
    if origin is None:
        # Namespace packages legitimately report ``None``; their search path is
        # the only available statement of where they resolved.
        locations = list(getattr(module, "__path__", ()) or ())
        if not locations:
            return None, "module exposed neither __file__ nor __path__"
        return Path(locations[0]).resolve(), ""
    return Path(origin).resolve(), ""


def _foreign_namespace_locations(
    package: str, allowed_roots: list[Path]
) -> list[Path]:
    """Return namespace portions of ``package`` that resolve outside the checkout.

    A namespace package exposes no ``__file__``, so every entry of its
    ``__path__`` is a place the interpreter will import from.  Crediting only
    ``__path__[0]`` passes a package whose in-checkout portion shadows an
    additional portion outside the checkout -- exactly the shape a shared
    environment produces when ``sys.path`` mixes two worktrees, where a
    subpackage missing locally still imports from the foreign portion.
    """

    module = sys.modules.get(package)
    if module is None or getattr(module, "__file__", None) is not None:
        return []
    outside = []
    for location in getattr(module, "__path__", ()) or ():
        if not any(
            _is_within(location, allowed, strict=False) for allowed in allowed_roots
        ):
            outside.append(Path(location).resolve())
    return outside


def check_origins(project_root: Path, packages: tuple[str, ...] = REQUIRED_PACKAGES) -> dict:
    """Return a JSON-serializable report of every package's resolved origin."""

    root = project_root.resolve()
    allowed_roots = _allowed_roots(root) if packages == REQUIRED_PACKAGES else [root]
    findings: list[dict] = []
    for package in packages:
        origin, error = _resolve_origin(package)
        if error:
            findings.append(
                {
                    "package": package,
                    "origin": None,
                    "status": "FAIL",
                    "detail": error,
                }
            )
            continue
        assert origin is not None  # guaranteed when ``error`` is empty
        foreign = _foreign_namespace_locations(package, allowed_roots)
        if foreign:
            findings.append(
                {
                    "package": package,
                    "origin": str(origin),
                    "status": "FAIL",
                    "detail": (
                        "namespace package also resolves outside this checkout: "
                        + ", ".join(str(item) for item in foreign)
                    ),
                }
            )
            continue
        inside = any(_is_within(origin, allowed) for allowed in allowed_roots)
        findings.append(
            {
                "package": package,
                "origin": str(origin),
                "status": "PASS" if inside else "FAIL",
                "detail": (
                    ""
                    if inside
                    else f"resolves outside this checkout {root} and outside the "
                    "site-packages of an install made from it; the interpreter "
                    "is importing a different checkout"
                ),
            }
        )
    return {
        "project_root": str(root),
        "packages": findings,
        "status": "PASS" if all(item["status"] == "PASS" for item in findings) else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that the selected interpreter imports pokered_harness and the "
            "vendored pyboy from the checkout under test."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="checkout under test; defaults to this script's repository root",
    )
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        help="additional distribution name to verify (repeatable)",
    )
    arguments = parser.parse_args(argv)

    packages = tuple(arguments.packages or ())
    if not packages:
        packages = REQUIRED_PACKAGES
    report = check_origins(arguments.project_root, packages)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
