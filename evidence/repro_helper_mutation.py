"""Genuine PathFinder.find_spec code, malicious PathFinder._get_spec helper."""
import importlib, importlib.machinery as machinery, sys, tempfile
from pathlib import Path
from scripts.check_import_origins import check_origins, _is_trusted_stdlib_finder

work = Path(tempfile.mkdtemp(prefix="r551helper-"))
pkg = work / "helper_pkg"; pkg.mkdir(); (pkg/"__init__.py").write_text("")
foreign = work / "leaked.py"; foreign.write_text('ORIGIN = "foreign"\n')
sys.path.insert(0, str(work))
for n in list(sys.modules):
    if n == "helper_pkg" or n.startswith("helper_pkg."):
        del sys.modules[n]

# find_spec ITSELF stays genuine -- only its helper _get_spec is replaced.
_orig_get_spec = machinery.PathFinder._get_spec.__func__
def evil_get_spec(cls, fullname, path=None, target=None):
    if fullname == "helper_pkg.leaked":
        return importlib.util.spec_from_file_location(fullname, str(foreign))
    return _orig_get_spec(cls, fullname, path, target)
machinery.PathFinder._get_spec = classmethod(evil_get_spec)
try:
    print("trusted (find_spec still genuine):", _is_trusted_stdlib_finder(machinery.PathFinder))
    r = check_origins(work, ("helper_pkg",))
    print("guard status:", r["status"])
    try:
        m = importlib.import_module("helper_pkg.leaked")
        print("foreign submodule loaded:", m.ORIGIN)
        if r["status"] == "PASS":
            print("*** FULL BYPASS via helper mutation ***")
    except ImportError as e:
        print("submodule blocked:", e)
finally:
    machinery.PathFinder._get_spec = classmethod(_orig_get_spec)
