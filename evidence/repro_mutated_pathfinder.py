"""Trusted finder identity with a changed find_spec implementation."""
import importlib
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

from scripts.check_import_origins import check_origins

work = Path(tempfile.mkdtemp(prefix="pathfinder-"))
root = work / "checkout"
package = root / "probe_pkg"
package.mkdir(parents=True)
(package / "__init__.py").write_text("", encoding="utf-8")
foreign = work / "foreign" / "stolen.py"
foreign.parent.mkdir()
foreign.write_text("ORIGIN = 'foreign'\n", encoding="utf-8")
sys.path.insert(0, str(root))

PathFinder = importlib.machinery.PathFinder
original = PathFinder.__dict__["find_spec"]


def forged(cls, fullname, path=None, target=None):
    if fullname == "probe_pkg.stolen":
        return importlib.util.spec_from_file_location(fullname, foreign)
    return original.__func__(cls, fullname, path, target)


try:
    PathFinder.find_spec = classmethod(forged)
    report = check_origins(root, ("probe_pkg",))
    print("guard status:", report["status"])
    print("report:", json.dumps(report))
    loaded = importlib.import_module("probe_pkg.stolen")
    print("foreign submodule loaded:", loaded.ORIGIN)
finally:
    PathFinder.find_spec = original
