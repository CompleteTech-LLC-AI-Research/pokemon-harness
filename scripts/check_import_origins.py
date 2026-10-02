"""Fail closed when the selected interpreter imports another tree's source.

The repository is developed across many Git worktrees that share one virtual
environment.  An editable install whose finder still points at a different
worktree makes every test result silently describe that other tree instead of
the checkout under test, in whichever direction it errs.  This check resolves
the two packages a release depends on -- ``pokered_harness`` and the vendored
``pyboy`` -- and refuses to continue unless both come from ``project_root``.

Exit codes: ``0`` when both packages resolve inside the project root, ``1`` on
any mismatch or resolution failure, ``2`` on usage errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The distributions whose import origins decide what the suite actually
# measures.  ``pokered_harness`` is the harness under test and ``pyboy`` is the
# vendored emulator it drives; a stale copy of either invalidates a release.
REQUIRED_PACKAGES = ("pokered_harness", "pyboy")


def _is_within(candidate: Path, root: Path) -> bool:
    """Return whether ``candidate`` is ``root`` or lives beneath it."""

    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


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
        inside = _is_within(origin, root)
        findings.append(
            {
                "package": package,
                "origin": str(origin),
                "status": "PASS" if inside else "FAIL",
                "detail": (
                    ""
                    if inside
                    else f"resolves outside the project root {root}; "
                    "the interpreter is importing a different checkout"
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
