"""Fail closed when the selected interpreter imports another tree's source.

The repository is developed across many Git worktrees that share one virtual
environment.  An editable install whose finder still points at a different
worktree makes every test result silently describe that other tree instead of
the checkout under test, in whichever direction it errs.  This check resolves
the two packages a release depends on -- ``pokered_harness`` and the vendored
``pyboy`` -- and refuses to continue unless both trace back to ``project_root``.

An origin counts as this checkout's when it either

* lives beneath ``project_root`` (a source tree or ``PYTHONPATH`` checkout), or
* lives beneath the site-packages directory of an installed distribution whose
  own recorded install source is ``project_root``.

The second case is the release lane: CI runs ``pip install -e ".[dev]"`` into a
virtual environment created outside the checkout, so ``pyboy`` legitimately
resolves to ``<venv>/site-packages/pyboy``.  What makes that safe is that the
install recorded *this* checkout as its source, which is exactly the property
``bootstrap_pyboy.py``'s ``_runtime_roots`` already relies on.  A stale
worktree's install records *that* worktree instead, so it still fails.

Exit codes: ``0`` when every package traces to ``project_root``, ``1`` on any
mismatch or resolution failure, ``2`` on usage errors.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

# The distributions whose import origins decide what the suite actually
# measures.  ``pokered_harness`` is the harness under test and ``pyboy`` is the
# vendored emulator it drives; a stale copy of either invalidates a release.
REQUIRED_PACKAGES = ("pokered_harness", "pyboy")

# The distribution that vendors ``pyboy`` into the same install as the harness.
PROJECT_DISTRIBUTION = "pokered-harness"


def _is_within(candidate: Path, root: Path) -> bool:
    """Return whether ``candidate`` is ``root`` or lives beneath it."""

    try:
        candidate.relative_to(root)
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
        distribution = importlib.metadata.distribution(distribution_name)
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
    # Only an install that recorded *this* checkout as its source may speak for
    # it.  A distribution installed from a sibling worktree records that other
    # directory, so its site-packages copy describes the wrong tree and must
    # not be admitted -- that is precisely the #534 condition.
    if _installed_from(PROJECT_DISTRIBUTION, project_root) != project_root:
        return roots
    for package in REQUIRED_PACKAGES:
        for owner in distributions.get(package, ()) or ():
            if owner != PROJECT_DISTRIBUTION:
                continue
            try:
                located = importlib.metadata.distribution(owner).locate_file(package)
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
