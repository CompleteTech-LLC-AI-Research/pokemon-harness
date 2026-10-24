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

That statement describes ``check_origins`` and its CLI.  The separate
``attest_selected_module_owners`` API can additionally compare the actual
selected module files with editable source paths or local wheel ``RECORD``
digests; those records are installation evidence, not signatures.

A stale worktree satisfies neither: its install records that other directory,
which is neither this checkout nor a staging directory beneath this checkout.

Exit codes: ``0`` when every package traces to ``project_root``, ``1`` on any
mismatch or resolution failure, ``2`` on usage errors.
"""

from __future__ import annotations

import argparse
import importlib.metadata  # noqa: F401 - preserves the established monkeypatch seam
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

if __package__:
    from . import _import_origin_resolution
elif __name__ in {"__main__", "check_import_origins"}:
    import _import_origin_resolution
else:
    from scripts import _import_origin_resolution

if __package__:
    from . import _import_origin_paths
elif __name__ in {"__main__", "check_import_origins"}:
    import _import_origin_paths
else:
    from scripts import _import_origin_paths

if __package__:
    from . import _import_origin_finders
elif __name__ in {"__main__", "check_import_origins"}:
    import _import_origin_finders
else:
    from scripts import _import_origin_finders

if __package__:
    from . import _import_origin_attestations
elif __name__ in {"__main__", "check_import_origins"}:
    import _import_origin_attestations
else:
    from scripts import _import_origin_attestations

if __package__:
    from . import _import_origin_selected_owners
elif __name__ in {"__main__", "check_import_origins"}:
    import _import_origin_selected_owners
else:
    from scripts import _import_origin_selected_owners

_VARIABLE_LENGTH_ALGORITHMS = _import_origin_attestations._VARIABLE_LENGTH_ALGORITHMS
_MAX_RECORDED_DIGEST_LENGTH = _import_origin_attestations._MAX_RECORDED_DIGEST_LENGTH
_BASE64_ALPHABET = _import_origin_attestations._BASE64_ALPHABET


def _decoded_digest_length(digest: str) -> int | None:
    return _import_origin_attestations._decoded_digest_length(digest)


_decoded_digest_length.__doc__ = _import_origin_attestations._decoded_digest_length.__doc__


# The distributions whose import origins decide what the suite actually
# measures.  ``pokered_harness`` is the harness under test and ``pyboy`` is the
# vendored emulator it drives; a stale copy of either invalidates a release.
REQUIRED_PACKAGES = ("pokered_harness", "pyboy")


def _trusted_stdlib_finders() -> set[object]:
    return _import_origin_finders._trusted_stdlib_finders(api=globals())


_trusted_stdlib_finders.__doc__ = _import_origin_finders._trusted_stdlib_finders.__doc__


def _is_trusted_stdlib_finder(finder: object) -> bool:
    return _import_origin_finders._is_trusted_stdlib_finder(finder, api=globals())


_is_trusted_stdlib_finder.__doc__ = _import_origin_finders._is_trusted_stdlib_finder.__doc__


def _is_unmodified(finder: object) -> bool:
    return _import_origin_finders._is_unmodified(finder, api=globals())


_is_unmodified.__doc__ = _import_origin_finders._is_unmodified.__doc__


def _site_packages_roots() -> list[Path]:
    return _import_origin_paths._site_packages_roots(api=globals())


_site_packages_roots.__doc__ = _import_origin_paths._site_packages_roots.__doc__


def _finder_module(finder: object) -> object | None:
    return _import_origin_finders._finder_module(finder, api=globals())


_finder_module.__doc__ = _import_origin_finders._finder_module.__doc__


def _finder_source(finder: object) -> Path | None:
    return _import_origin_finders._finder_source(finder, api=globals())


_finder_source.__doc__ = _import_origin_finders._finder_source.__doc__


def _safe_resolve(candidate: Path) -> Path | None:
    return _import_origin_paths._safe_resolve(candidate, api=globals())


_safe_resolve.__doc__ = _import_origin_paths._safe_resolve.__doc__


def _code_objects(code: object):
    return _import_origin_finders._code_objects(code, api=globals())


_code_objects.__doc__ = _import_origin_finders._code_objects.__doc__


def _code_signature(code: object) -> tuple:
    return _import_origin_finders._code_signature(code, api=globals())


_code_signature.__doc__ = _import_origin_finders._code_signature.__doc__


def _unreadable_signature() -> object:
    return _import_origin_finders._unreadable_signature(api=globals())


_unreadable_signature.__doc__ = _import_origin_finders._unreadable_signature.__doc__


def _code_matches_source(function: object, source_file: Path) -> bool:
    return _import_origin_finders._code_matches_source(function, source_file, api=globals())


_code_matches_source.__doc__ = _import_origin_finders._code_matches_source.__doc__


def _finder_code_file(finder: object) -> Path | None:
    return _import_origin_finders._finder_code_file(finder, api=globals())


_finder_code_file.__doc__ = _import_origin_finders._finder_code_file.__doc__


def _finder_was_imported_from(finder: object, source_file: Path) -> bool:
    return _import_origin_finders._finder_was_imported_from(finder, source_file, api=globals())


_finder_was_imported_from.__doc__ = _import_origin_finders._finder_was_imported_from.__doc__


def _record_attestations(root: Path) -> dict[str, dict[str, set[tuple[str, str]]]]:
    return _import_origin_attestations._record_attestations(root, api=globals())


_record_attestations.__doc__ = _import_origin_attestations._record_attestations.__doc__


def _record_digests(root: Path) -> dict[str, set[tuple[str, str]]]:
    return _import_origin_attestations._record_digests(root, api=globals())


_record_digests.__doc__ = _import_origin_attestations._record_digests.__doc__


def _record_claim_rows(root: Path) -> dict[str, list[dict[str, str | None]]]:
    return _import_origin_attestations._record_claim_rows(root, api=globals())


_record_claim_rows.__doc__ = _import_origin_attestations._record_claim_rows.__doc__


def _file_digest(
    candidate: Path, algorithm: str = "sha256", expected_length: int | None = None
) -> str | None:
    return _import_origin_attestations._file_digest(candidate, algorithm, expected_length)


_file_digest.__doc__ = _import_origin_attestations._file_digest.__doc__


def _is_recorded_by_an_install(source_file: Path, root: Path) -> bool:
    return _import_origin_attestations._is_recorded_by_an_install(source_file, root, api=globals())


_is_recorded_by_an_install.__doc__ = _import_origin_attestations._is_recorded_by_an_install.__doc__


def _is_imported_by_a_pth(source_file: Path, root: Path) -> bool:
    return _import_origin_attestations._is_imported_by_a_pth(source_file, root, api=globals())


_is_imported_by_a_pth.__doc__ = _import_origin_attestations._is_imported_by_a_pth.__doc__


def _is_installation_finder(finder: object) -> bool:
    return _import_origin_finders._is_installation_finder(finder, api=globals())


_is_installation_finder.__doc__ = _import_origin_finders._is_installation_finder.__doc__


def _untrusted_meta_path_finders() -> list[tuple[object, Path | None]]:
    return _import_origin_finders._untrusted_meta_path_finders(api=globals())


_untrusted_meta_path_finders.__doc__ = _import_origin_finders._untrusted_meta_path_finders.__doc__


def _describe_finder(entry: tuple[object, Path | None]) -> str:
    return _import_origin_finders._describe_finder(entry, api=globals())


_describe_finder.__doc__ = _import_origin_finders._describe_finder.__doc__


def _is_within(candidate: Path, root: Path, *, strict: bool = False) -> bool:
    return _import_origin_paths._is_within(candidate, root, strict=strict, api=globals())


_is_within.__doc__ = _import_origin_paths._is_within.__doc__


def _installed_from(distribution_name: str, project_root: Path) -> Path | None:
    return _import_origin_paths._installed_from(distribution_name, project_root, api=globals())


_installed_from.__doc__ = _import_origin_paths._installed_from.__doc__


def _normalise_distribution_name(name: str) -> str:
    return _import_origin_paths._normalise_distribution_name(name, api=globals())


_normalise_distribution_name.__doc__ = _import_origin_paths._normalise_distribution_name.__doc__


def _distribution(distribution_name: str):
    return _import_origin_paths._distribution(distribution_name, api=globals())


_distribution.__doc__ = _import_origin_paths._distribution.__doc__


def _staging_root(project_root: Path) -> Path:
    return _import_origin_paths._staging_root(project_root, api=globals())


_staging_root.__doc__ = _import_origin_paths._staging_root.__doc__


def _is_this_checkout(project_root: Path, source: Path | None) -> bool:
    return _import_origin_paths._is_this_checkout(project_root, source, api=globals())


_is_this_checkout.__doc__ = _import_origin_paths._is_this_checkout.__doc__


def _allowed_roots(project_root: Path, packages: tuple[str, ...] = REQUIRED_PACKAGES) -> list[Path]:
    return _import_origin_paths._allowed_roots(project_root, packages, api=globals())


_allowed_roots.__doc__ = _import_origin_paths._allowed_roots.__doc__


# Module objects published by ``_resolve_origin``, keyed by package name.  The
# origin is read from ``module.__file__`` exactly once, and the portion check
# reuses the same module rather than re-reading that mutable value.
_RESOLVED_MODULES: dict = {}


def _resolve_origin(package: str) -> tuple[Path | None, str]:
    return _import_origin_resolution._resolve_origin(package, api=globals())


_resolve_origin.__doc__ = _import_origin_resolution._resolve_origin.__doc__


def _resolve_path(candidate: object, package: str) -> tuple[Path | None, str]:
    return _import_origin_resolution._resolve_path(candidate, package, api=globals())


_resolve_path.__doc__ = _import_origin_resolution._resolve_path.__doc__


def _type_name(value: object) -> str:
    """Return ``type(value).__name__`` without trusting the metaclass.

    A hostile object controls its own metaclass, so ``type(value)`` is an
    object whose ``__name__`` lookup can run attacker code or raise.  This is
    the same exposure ``_describe`` closes for ``str``, applied to the type of
    the value rather than the value, so a finding can still be rendered when
    the thing being reported about is itself a trap.
    """

    try:
        name = type(value).__name__
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return "<unknown type>"
    try:
        return str(name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return "<unknown type name>"


def _describe(value: object) -> str:
    """Return a printable rendering of ``value`` that cannot itself raise.

    Everything this module reports came from the interpreter or from install
    metadata, so any of it may be an object with a hostile ``__str__``.  A
    detail string that raises while being built turns the finding back into a
    traceback, which is the exact failure this module exists to prevent.
    """

    try:
        return str(value)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return f"<unprintable {_type_name(value)}>"


def _foreign_path_locations(
    package: str, allowed_roots: list[Path], origin: Path, module: object = None
) -> tuple[list[Path], list[str]]:
    return _import_origin_resolution._foreign_path_locations(
        package, allowed_roots, origin, module, api=globals()
    )


_foreign_path_locations.__doc__ = _import_origin_resolution._foreign_path_locations.__doc__


def attest_selected_module_owners(
    modules: Mapping[str, ModuleType],
    expected_owners: Mapping[str, str],
    *,
    project_root: Path,
    editable_module_paths: Mapping[str, Path] | None = None,
) -> dict[str, object]:
    """Attest selected live module files against editable roots or RECORD bytes.

    This optional API supplements the location-only ``check_origins`` report.
    ``direct_url.json`` and ``RECORD`` are local installation evidence, not
    signatures or protection against changes made after this bounded check.
    """

    return _import_origin_selected_owners.attest_selected_module_owners(
        modules,
        expected_owners,
        project_root=project_root,
        editable_module_paths=editable_module_paths,
        api=globals(),
    )


def check_origins(project_root: Path, packages: tuple[str, ...] = REQUIRED_PACKAGES) -> dict:
    """Return a JSON-serializable report of every package's resolved origin.

    Every value inspected below comes from outside this process: ``sys``,
    ``site``, ``sys.modules``, the meta-path, and the on-disk layout an install
    produced.  Thirteen review rounds across #558 and #559 each found one more
    such read that a hostile object could raise a direct ``BaseException``
    subclass from, so the reads are individually guarded.

    That is not sufficient on its own and this boundary is the backstop for
    the same reason: a guard whose only answer to a hostile input is a
    traceback is fail-open, because it emits no machine-readable finding for a
    caller to gate on.  A read nobody has thought of yet must still produce a
    refusal.  So any ``BaseException`` that escapes the analysis below is
    converted here into the same FAIL document every other refusal produces.
    ``KeyboardInterrupt`` and ``SystemExit`` deliberately pass through: an
    operator interrupt must still stop the process.
    """

    try:
        return _check_origins(project_root, packages)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - the guard must never traceback
        return {
            "project_root": _describe(project_root),
            "packages": [
                {
                    "package": "<guard>",
                    "origin": None,
                    "status": "FAIL",
                    "detail": (
                        "the import-origin guard could not complete: "
                        f"{_type_name(exc)}: {_describe(exc)}"
                    ),
                }
            ],
            "status": "FAIL",
        }


def _check_origins(project_root: Path, packages: tuple[str, ...]) -> dict:
    """Return the report, or raise whatever a hostile input raises.

    The analysis proper.  ``check_origins`` wraps this so that a hostile input
    escaping any read becomes a refusal rather than a traceback; nothing here
    catches on its behalf.
    """

    # Start from a clean slate.  An entry left over from an earlier call would
    # pair this call's origin -- which the release lane may have resolved from
    # install metadata rather than from a live import -- with that earlier
    # tree's module, and report a foreign portion for an honest install.
    _RESOLVED_MODULES.clear()
    # ``project_root`` is supplied by the caller, and a path-like whose
    # ``resolve`` raises would otherwise abort the guard before any report
    # exists -- the same traceback-instead-of-a-verdict failure the rest of
    # this module is written to prevent.  A root that cannot be resolved
    # cannot be shown to be the checkout under test, so it is refused.
    try:
        root = project_root.resolve()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - untrusted data, see docstring
        return {
            # ``_describe`` rather than a bare ``str``: the root that failed to
            # resolve is itself the hostile path-like, so rendering it here
            # would raise out of the very handler meant to report the escape.
            "project_root": _describe(project_root),
            "packages": [
                {
                    "package": "<project-root>",
                    "origin": None,
                    "status": "FAIL",
                    "detail": (
                        f"project root could not be resolved: {_type_name(exc)}: {_describe(exc)}"
                    ),
                }
            ],
            "status": "FAIL",
            "detail": "project root could not be resolved",
        }
    allowed_roots = _allowed_roots(root, packages)
    findings: list[dict] = []
    # A meta-path finder this checkout did not install can place a submodule
    # anywhere, and it runs before PathFinder, so every origin and __path__
    # check below would still read PASS while foreign code loaded.  Refuse the
    # interpreter itself rather than certifying a provenance it cannot
    # establish.
    intruders = _untrusted_meta_path_finders()
    if intruders:
        return {
            "project_root": str(root),
            "packages": [
                {
                    "package": "<interpreter>",
                    "origin": None,
                    "status": "FAIL",
                    "detail": (
                        "untrusted sys.meta_path finder(s) can import a checked "
                        "package's submodules from outside this checkout: "
                        + ", ".join(_describe_finder(item) for item in intruders)
                    ),
                }
            ],
            "status": "FAIL",
        }
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
        # Judge the top-level origin first.  When the package itself resolves
        # outside every allowed root that is the whole finding, and reporting a
        # path-portion detail as well would bury it.
        if not any(_is_within(origin, allowed) for allowed in allowed_roots):
            findings.append(
                {
                    "package": package,
                    "origin": str(origin),
                    "status": "FAIL",
                    "detail": (
                        f"resolves outside this checkout {root} and outside the "
                        "site-packages of an install made from it; the "
                        "interpreter is importing a different checkout"
                    ),
                }
            )
            continue
        # Use the module ``_resolve_origin`` actually imported, so ``__file__``
        # is read once and a hostile value cannot change its answer between the
        # origin resolution and the portion check.  When the origin came from
        # install metadata instead -- the release lane resolves it that way --
        # nothing was published and the ambient module is some *other* tree's
        # package, which must not be judged against these roots, so the
        # function's own fallback handles it.
        live = _RESOLVED_MODULES.get(package)
        foreign, unusable = _foreign_path_locations(package, allowed_roots, origin, live)
        if foreign or unusable:
            detail = ""
            if foreign:
                detail = "package also resolves outside this checkout: " + ", ".join(
                    _describe(item) for item in foreign
                )
            if unusable:
                joined = "path portion is not usable: " + ", ".join(unusable)
                detail = f"{detail}; {joined}" if detail else joined
            findings.append(
                {
                    "package": package,
                    "origin": str(origin),
                    "status": "FAIL",
                    "detail": detail,
                }
            )
            continue
        findings.append(
            {
                "package": package,
                "origin": str(origin),
                "status": "PASS",
                "detail": "",
            }
        )
    # `all()` over an empty sequence is True, so an empty request would report
    # PASS for verifying nothing.  A gate that checked no packages must not be
    # able to certify that all is well; fail closed at the report boundary so
    # the verdict holds no matter which caller supplied the list.
    if not findings:
        return {
            "project_root": str(root),
            "packages": [],
            "status": "FAIL",
            "detail": "no packages were selected to verify",
        }
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
        help=(
            "distribution name to verify *instead of* the defaults "
            "(repeatable; replaces -- not adds to -- pokered_harness and pyboy)"
        ),
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
