"""Small file/metadata fixtures for selected-module owner admission tests."""

from __future__ import annotations

import base64
import csv
import hashlib
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace


def make_recorded_module(root: Path, name: str, owner: str, content: bytes) -> ModuleType:
    """Create one installed module and the owning wheel's exact RECORD row."""

    site = root / "site-packages"
    module_path = site.joinpath(*name.split("."))
    if name in {"pyboy", "pokered_harness", "pyboy.link"}:
        module_path = module_path / "__init__.py"
    else:
        module_path = module_path.with_suffix(".py")
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_bytes(content)
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    distribution = site / f"{owner}-1.0.dist-info"
    distribution.mkdir(parents=True, exist_ok=True)
    (distribution / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {owner}\n", encoding="utf-8"
    )
    record = distribution / "RECORD"
    with record.open("a", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerow(
            [module_path.relative_to(site).as_posix(), f"sha256={digest}", str(len(content))]
        )
    module = ModuleType(name)
    module.__file__ = str(module_path)
    return module


def write_hashless_claim(site: Path, owner: str, module: ModuleType) -> None:
    path = Path(module.__file__)
    distribution = site / f"{owner}-1.0.dist-info"
    distribution.mkdir(parents=True, exist_ok=True)
    (distribution / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {owner}\n", encoding="utf-8"
    )
    with (distribution / "RECORD").open("a", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerow(
            [Path(os.path.relpath(path, site)).as_posix(), "", str(path.stat().st_size)]
        )


def make_contract_case(bootstrap, root: Path, *, mode: str = "source") -> SimpleNamespace:
    """Build valid module identities and a recording owner-attestation seam."""

    vendor = root / "vendor" / "pyboy-src"
    modules = {}
    for name in bootstrap.RUNTIME_MODULES:
        relative = Path(*name.split("."))
        if name in {"pyboy", "pyboy.link"}:
            relative = relative / "__init__"
        suffix = ".py"
        if mode == "cython" and name in bootstrap.CYTHON_MODULES:
            suffix = next(iter(bootstrap.importlib.machinery.EXTENSION_SUFFIXES))
        elif name not in {"pyboy", "pyboy.link"}:
            relative = relative.with_suffix("")
        module = ModuleType(name)
        module.__file__ = str(vendor / relative) + suffix
        modules[name] = module
    pyboy = modules["pyboy"]
    pyboy.__version__ = bootstrap.EXPECTED_PYBOY_VERSION
    pyboy.__pokered_harness_revision__ = bootstrap.EXPECTED_REVISION
    pyboy.PyBoy = SimpleNamespace(_tick=lambda: None)
    utils = modules["pyboy.utils"]
    utils.cython_compiled = mode == "cython"
    pyboy.utils = utils

    harness_path = root / "src" / "pokered_harness" / "__init__.py"
    harness_path.parent.mkdir(parents=True, exist_ok=True)
    harness_path.write_text("", encoding="utf-8")
    harness = ModuleType("pokered_harness")
    harness.__file__ = str(harness_path)

    owner_calls = []

    def attest_selected_module_owners(selected, expected, **kwargs):
        owner_calls.append((dict(selected), dict(expected), kwargs))
        return {"ok": True, "errors": []}

    api = dict(bootstrap.__dict__)
    api.update(
        {
            "ROOT": root,
            "PYBOY_SOURCE": vendor,
            "_runtime_roots": lambda: (vendor,),
            "_path_is_within": lambda path, allowed: path.is_relative_to(allowed),
            "_package_distributions": lambda _package: (
                {bootstrap.PROJECT_DISTRIBUTION, "pyboy"}
                if mode == "cython"
                else {bootstrap.PROJECT_DISTRIBUTION}
            ),
            "_origin_guard": SimpleNamespace(
                attest_selected_module_owners=attest_selected_module_owners
            ),
        }
    )
    return SimpleNamespace(
        api=api,
        modules=modules,
        pyboy=pyboy,
        utils=utils,
        harness=harness,
        owner_calls=owner_calls,
        root=root,
        vendor=vendor,
    )
