"""Any in-process code can force a false PASS by rebinding the guard's own
predicates. Measured on dcd44e0; recorded in
ledger/LEAD_20261002_551_ROUND7_TRUST_CEILING.md as the reason the
per-attribute provenance checks cannot be closed at this layer.
"""
"""Can an in-process attacker reach a false PASS without any clever trick?

If patching one module-level predicate is enough, then hardening that
predicate against in-process mutation cannot be a coherent goal.
"""
import importlib, importlib.util, sys, tempfile
from pathlib import Path
import scripts.check_import_origins as o

work = Path(tempfile.mkdtemp(prefix="r551def-"))
pkg = work / "def_pkg"; pkg.mkdir(); (pkg/"__init__.py").write_text("")
foreign = work / "leaked.py"; foreign.write_text('ORIGIN = "foreign"\n')
sys.path.insert(0, str(work))
for n in list(sys.modules):
    if n == "def_pkg" or n.startswith("def_pkg."):
        del sys.modules[n]

class Sneaky:
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if name == "def_pkg.leaked":
            return importlib.util.spec_from_file_location(name, str(foreign))
        return None

sys.meta_path.insert(0, Sneaky)
# The attacker does not need to forge __spec__, a .pth, a RECORD, or a
# fingerprint.  It rewrites the predicate that answers the question.
o._is_installation_finder = lambda f: True
o._is_trusted_stdlib_finder = lambda f: True

try:
    r = o.check_origins(work, ("def_pkg",))
    print("guard status:", r["status"])
    try:
        m = importlib.import_module("def_pkg.leaked")
        print("foreign submodule loaded:", m.ORIGIN)
        if r["status"] == "PASS":
            print("*** ANY in-process code can force a false PASS ***")
    except ImportError as e:
        print("blocked:", e)
finally:
    sys.meta_path.remove(Sneaky)
