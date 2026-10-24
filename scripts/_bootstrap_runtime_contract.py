"""Runtime identity, module ownership, and admission ordering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

ContractApi = dict[str, Any]


def _editable_source_paths(api: ContractApi) -> dict[str, Path]:
    root = api["ROOT"]
    vendor = api["PYBOY_SOURCE"] / "pyboy"
    paths = {"pokered_harness": root / "src" / "pokered_harness" / "__init__.py"}
    for name in api["RUNTIME_MODULES"]:
        parts = name.split(".")[1:]
        relative = Path(*parts) if parts else Path()
        if name in {"pyboy", "pyboy.link"}:
            relative = relative / "__init__.py"
        else:
            relative = relative.with_suffix(".py")
        paths[name] = vendor / relative
    return paths


def verify_preconstruction_contract(
    mode: str,
    modules: dict[str, object],
    pyboy: object,
    utils: object,
    harness_module: object | None,
    *,
    api: ContractApi,
) -> list[str]:
    """Check identity and active-file owners before constructing Serial."""

    expected_modules = (
        {name: "cython" for name in api["CYTHON_MODULES"]}
        if mode == "cython"
        else {name: "source" for name in api["RUNTIME_MODULES"]}
    )
    problems: list[str] = []
    if getattr(pyboy, "__version__", None) != api["EXPECTED_PYBOY_VERSION"]:
        problems.append(
            f"version={getattr(pyboy, '__version__', None)!r}, "
            f"expected {api['EXPECTED_PYBOY_VERSION']!r}"
        )
    if getattr(pyboy, "__pokered_harness_revision__", None) != api["EXPECTED_REVISION"]:
        problems.append(
            "revision="
            f"{getattr(pyboy, '__pokered_harness_revision__', None)!r}, "
            f"expected {api['EXPECTED_REVISION']!r}"
        )
    if not callable(getattr(getattr(pyboy, "PyBoy", None), "_tick", None)):
        problems.append("PyBoy._tick frame ownership is unavailable; rebuild the bundled runtime")

    serial_type = getattr(modules["pyboy.core.serial"], "Serial", None)
    for name in ("set_owner_pump", "claim_owner_pump", "release_owner_pump"):
        if not callable(getattr(serial_type, name, None)):
            problems.append(f"API {name} is missing")
    motherboard_type = getattr(modules["pyboy.core.mb"], "Motherboard", None)
    if not callable(getattr(motherboard_type, "get_physical_clock", None)):
        problems.append("API get_physical_clock is missing")

    for name, expected_kind in expected_modules.items():
        actual_kind = api["_module_kind"](modules[name])
        if actual_kind != expected_kind:
            problems.append(f"{name} is {actual_kind}, expected {expected_kind}")

    allowed_roots = api["_runtime_roots"]()
    for name, module in modules.items():
        filename = str(getattr(module, "__file__", "") or "")
        module_path = Path(filename).resolve() if filename else None
        if module_path is None or not any(
            api["_path_is_within"](module_path, root) for root in allowed_roots
        ):
            problems.append(
                f"{name} loaded outside the pinned runtime roots: {filename or '<none>'}"
            )

    owners = api["_package_distributions"]("pyboy")
    if api["PROJECT_DISTRIBUTION"] not in owners:
        problems.append("pyboy is not provided by the installed pokered-harness distribution")
    if mode == "source":
        unexpected = owners - {api["PROJECT_DISTRIBUTION"]}
        if unexpected:
            problems.append(
                "pyboy has competing installed owners: " + ", ".join(sorted(unexpected))
            )
    else:
        unexpected = owners - {api["PROJECT_DISTRIBUTION"], "pyboy"}
        if unexpected:
            problems.append("pyboy has foreign installed owners: " + ", ".join(sorted(unexpected)))

    if bool(getattr(utils, "cython_compiled", False)) != (mode == "cython"):
        problems.append(
            f"cython_compiled={getattr(utils, 'cython_compiled', None)!r}, "
            f"expected {mode == 'cython'!r}"
        )
    if problems:
        return problems

    selected: dict[str, object] = dict(modules)
    expected_owners = {
        name: ("pyboy" if mode == "cython" else api["PROJECT_DISTRIBUTION"]) for name in modules
    }
    expected_owners["pokered_harness"] = api["PROJECT_DISTRIBUTION"]
    if harness_module is not None:
        selected["pokered_harness"] = harness_module
    owner_report = api["_origin_guard"].attest_selected_module_owners(
        selected,
        expected_owners,
        project_root=api["ROOT"],
        editable_module_paths=(
            _editable_source_paths(api)
            if mode == "source"
            else {"pokered_harness": _editable_source_paths(api)["pokered_harness"]}
        ),
    )
    if not owner_report.get("ok"):
        problems.extend(owner_report.get("errors", ["selected module owner check failed"]))
    return problems


def verify_serial_instance(serial: object) -> list[str]:
    missing = [name for name in ("backend", "backend_failed") if not hasattr(serial, name)]
    missing.extend(
        name
        for name in (
            "apply_external_edge",
            "peek_out_bit",
            "check_error",
            "claim_owner_pump",
            "release_owner_pump",
        )
        if not callable(getattr(serial, name, None))
    )
    return [f"serial contract missing {', '.join(missing)}"] if missing else []


def verify_behavior(mode: str, pyboy: object, serial_module: object, *, api: ContractApi) -> None:
    """Run each live facade seam only after identity and owner checks pass."""

    api["_verify_serial_features"](mode, serial_module)
    api["_verify_owner_clock_features"](mode, pyboy, serial_module)
    api["_verify_owner_poll_features"](mode, pyboy, serial_module)
