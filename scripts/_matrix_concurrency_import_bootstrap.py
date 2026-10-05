from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

_HELPER_MODULES = (
    "_matrix_concurrency_import_bootstrap",
    "_matrix_concurrency_constants",
    "_matrix_concurrency_model_values",
    "_matrix_concurrency_source_paths",
    "_matrix_concurrency_accounting",
    "_matrix_concurrency_process",
    "_matrix_concurrency_reporting",
    "_matrix_concurrency_cli",
)


def _validate_modules(
    package_name: str,
    scripts_path: str,
    *,
    require_all: bool,
) -> None:
    """Fail closed on namespace or cached-helper origins outside this source root."""

    scripts = Path(scripts_path).resolve()
    package = sys.modules.get(package_name)
    spec = getattr(package, "__spec__", None)
    if (
        not isinstance(package, ModuleType)
        or package.__name__ != package_name
        or package.__package__ != package_name
        or package.__matrix_root__ != str(scripts)
        or getattr(package, "__file__", None) is not None
        or getattr(package, "__loader__", None) is not None
        or list(getattr(package, "__path__", ())) != [str(scripts)]
        or spec is None
        or spec.name != package_name
        or spec.loader is not None
        or spec.origin is not None
        or spec.parent != package_name
        or list(spec.submodule_search_locations or ()) != [str(scripts)]
    ):
        raise ImportError("matrix helper namespace changed ownership during import")

    for basename in _HELPER_MODULES:
        fullname = f"{package_name}.{basename}"
        module = sys.modules.get(fullname)
        if module is None and not require_all:
            continue
        expected = (scripts / f"{basename}.py").resolve()
        module_spec = getattr(module, "__spec__", None)
        if (
            not isinstance(module, ModuleType)
            or Path(getattr(module, "__file__", "")).resolve() != expected
            or module_spec is None
            or module_spec.name != fullname
            or module_spec.parent != package_name
            or Path(module_spec.origin or "").resolve() != expected
            or module.__package__ != package_name
        ):
            raise ImportError(f"matrix helper is not owned by this source root: {fullname}")
