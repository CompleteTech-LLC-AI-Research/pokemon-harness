"""End-to-end: can one planted .pth file certify a finder with no provenance?

Reproduces the blocking finding recorded on master at 3e0791e
(ledger/LEAD_20261002_551_INDEPENDENT_REVIEW.md, Finding 1).
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

from scripts.check_import_origins import _is_installation_finder, check_origins

SP = None
for root in map(Path, sys.path):
    if root.name == "site-packages" and root.is_dir():
        SP = root
        break
if SP is None:
    raise SystemExit("no site-packages found")

work = Path(tempfile.mkdtemp(prefix="r551-pth-"))
root = work / "attack_root"
pkg = root / "probe_pkg"
pkg.mkdir(parents=True)
(pkg / "__init__.py").write_text("", encoding="utf-8")
foreign = work / "outside" / "leaked.py"
foreign.parent.mkdir(parents=True)
foreign.write_text('ORIGIN = "foreign"', encoding="utf-8")

sys.path.insert(0, str(root))
for name in list(sys.modules):
    if name == "probe_pkg" or name.startswith("probe_pkg."):
        del sys.modules[name]

# Plant exactly ONE file into real site-packages: a .pth naming the module.
shim = SP / "rev551_planted_shim.py"
shim.write_text(
    "import importlib.util\n"
    "_TARGET = None\n"
    "class Finder:\n"
    "    @classmethod\n"
    "    def find_spec(cls, name, path=None, target=None):\n"
    "        if name == 'probe_pkg.leaked':\n"
    "            return importlib.util.spec_from_file_location(name, _TARGET)\n"
    "        return None\n",
    encoding="utf-8",
)
pth = SP / "rev551_planted.pth"
pth.write_text("import rev551_planted_shim\n", encoding="utf-8")

try:
    from rev551_planted_shim import Finder  # noqa: E402

    import rev551_planted_shim as shim_mod
    shim_mod._TARGET = str(foreign)

    print("_is_installation_finder(forged):", _is_installation_finder(Finder))
    sys.meta_path.insert(0, Finder)
    report = check_origins(root, ("probe_pkg",))
    print("guard status:", report["status"])
    try:
        mod = importlib.import_module("probe_pkg.leaked")
        print("foreign submodule loaded:", mod.ORIGIN)
        print("*** FULL BYPASS via attacker-written .pth ***")
    except ImportError as exc:
        print("submodule import blocked:", exc)
    json.dumps(report)
finally:
    sys.meta_path[:] = [f for f in sys.meta_path if f is not Finder]
    pth.unlink(missing_ok=True)
    shim.unlink(missing_ok=True)
