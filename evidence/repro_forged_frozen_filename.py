"""Can compile() forge the <frozen ...> filename and inherit PathFinder's trust?"""
import importlib
import importlib.machinery as machinery
import sys
import tempfile
from pathlib import Path

from scripts.check_import_origins import _is_trusted_stdlib_finder, check_origins

work = Path(tempfile.mkdtemp(prefix="r551forge-"))
pkg = work / "forge_pkg"
pkg.mkdir()
(pkg / "__init__.py").write_text("", encoding="utf-8")
foreign = work / "stolen.py"
foreign.write_text('ORIGIN = "foreign"\n', encoding="utf-8")
sys.path.insert(0, str(work))
for name in list(sys.modules):
    if name == "forge_pkg" or name.startswith("forge_pkg."):
        del sys.modules[name]

ORIGINAL = machinery.PathFinder.find_spec.__func__

src = (
    "def find_spec(cls, name, path=None, target=None):\n"
    "    if name == 'forge_pkg.stolen':\n"
    "        import importlib.util\n"
    "        return importlib.util.spec_from_file_location(name, TARGET)\n"
    "    return DELEGATE(cls, name, path, target)\n"
)
ns = {"TARGET": str(foreign), "DELEGATE": ORIGINAL}
code = compile(src, "<frozen importlib._bootstrap_external>", "exec")
exec(code, ns)
machinery.PathFinder.find_spec = classmethod(ns["find_spec"])

try:
    trusted = _is_trusted_stdlib_finder(machinery.PathFinder)
    report = check_origins(work, ("forge_pkg",))
    leaked = None
    try:
        leaked = importlib.import_module("forge_pkg.stolen").ORIGIN
    except ImportError as exc:
        leaked = f"blocked ({exc})"
    print("forged <frozen ...> filename trusted:", trusted)
    print("guard status:", report["status"])
    print("foreign submodule:", leaked)
    if trusted and report["status"] == "PASS" and leaked == "foreign":
        print("*** FORGED FROZEN FILENAME = FULL BYPASS ***")
finally:
    machinery.PathFinder.find_spec = classmethod(ORIGINAL)
