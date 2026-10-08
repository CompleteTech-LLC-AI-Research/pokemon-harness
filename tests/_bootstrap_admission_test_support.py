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


def alias_probe_script(script: Path) -> str:
    """Foreign-cwd file-alias import probe for the given bootstrap script."""

    return f"""import importlib.util, pathlib, sys, warnings
warnings.simplefilter("error", ImportWarning)
path = pathlib.Path({str(script.resolve())!r})
spec = importlib.util.spec_from_file_location("bootstrap_pyboy_foreign_file_alias", path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.__package__ == ""
assert module.__spec__ is spec
namespace = sys.modules["scripts"]
assert namespace.__file__ is None
assert namespace.__spec__.origin is None
assert [pathlib.Path(item).resolve() for item in namespace.__path__] == [path.parent]
for name in (
    "_bootstrap_runtime_contract",
    "_bootstrap_runtime_probes",
    "check_import_origins",
    "_import_origin_resolution",
    "_import_origin_paths",
    "_import_origin_finders",
    "_import_origin_attestations",
    "_import_origin_selected_owners",
):
    helper = sys.modules[f"scripts.{{name}}"]
    assert pathlib.Path(helper.__file__).resolve() == path.parent / f"{{name}}.py"
print("foreign-cwd bootstrap file alias imports the lane-owned helpers")
"""


def foreign_namespace_probe_script(script: Path) -> str:
    """Foreign scripts-namespace and cached-helper probe for the given bootstrap script."""

    return f"""import importlib.machinery, importlib.util, pathlib, sys, types, warnings
warnings.simplefilter("error", ImportWarning)
path = pathlib.Path({str(script.resolve())!r})
foreign_path = path.parent.parent / "foreign-scripts"
namespace = types.ModuleType("scripts")
namespace.__file__ = None
namespace.__loader__ = None
namespace.__package__ = "scripts"
namespace.__path__ = [str(foreign_path)]
namespace.__spec__ = importlib.machinery.ModuleSpec("scripts", loader=None, is_package=True)
namespace.__spec__.submodule_search_locations = [str(foreign_path)]
sys.modules["scripts"] = namespace
before_children = {{name: module for name, module in sys.modules.items() if name.startswith("scripts.")}}
spec = importlib.util.spec_from_file_location("bootstrap_pyboy_foreign_file_alias", path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
try:
    spec.loader.exec_module(module)
except ImportError as exc:
    assert "belongs to another source tree" in str(exc)
else:
    raise AssertionError("a foreign scripts namespace was accepted")
assert sys.modules["scripts"] is namespace
assert namespace.__path__ == [str(foreign_path)]
assert module.__spec__ is spec and module.__package__ == ""
assert {{name: module for name, module in sys.modules.items() if name.startswith("scripts.")}} == before_children
print("foreign scripts namespace rejected before helper imports")
sys.modules.pop(spec.name)
sys.modules.pop("scripts")
namespace = types.ModuleType("scripts")
namespace.__file__ = None
namespace.__loader__ = None
namespace.__package__ = "scripts"
namespace.__path__ = [str(path.parent)]
namespace_spec = importlib.machinery.ModuleSpec("scripts", loader=None, is_package=True)
namespace_spec.submodule_search_locations = [str(path.parent)]
namespace.__spec__ = namespace_spec
namespace_before = (
    namespace.__dict__.copy(),
    tuple(namespace.__path__),
    namespace_spec.origin,
    tuple(namespace_spec.submodule_search_locations),
)
sys.modules["scripts"] = namespace
foreign_child = types.ModuleType("scripts._bootstrap_runtime_contract")
foreign_child.__file__ = str(path.parent.parent / "foreign" / "_bootstrap_runtime_contract.py")
sys.modules[foreign_child.__name__] = foreign_child
before_children = {{name: child for name, child in sys.modules.items() if name.startswith("scripts.")}}
assert before_children == {{foreign_child.__name__: foreign_child}}
cached_child_spec = importlib.util.spec_from_file_location("bootstrap_pyboy_cached_child_alias", path)
assert cached_child_spec is not None and cached_child_spec.loader is not None
cached_child_module = importlib.util.module_from_spec(cached_child_spec)
sys.modules[cached_child_spec.name] = cached_child_module
try:
    cached_child_spec.loader.exec_module(cached_child_module)
except ImportError as exc:
    assert "belongs to another source tree" in str(exc)
else:
    raise AssertionError("a foreign cached bootstrap helper was accepted")
assert sys.modules["scripts"] is namespace
assert namespace.__dict__ == namespace_before[0]
assert tuple(namespace.__path__) == namespace_before[1]
assert namespace.__spec__ is namespace_before[0]["__spec__"]
assert namespace.__spec__.origin == namespace_before[2]
assert tuple(namespace.__spec__.submodule_search_locations) == namespace_before[3]
assert cached_child_module.__spec__ is cached_child_spec and cached_child_module.__package__ == ""
assert {{name: child for name, child in sys.modules.items() if name.startswith("scripts.")}} == before_children
print("foreign cached helper rejected before static imports without namespace mutation")
"""
