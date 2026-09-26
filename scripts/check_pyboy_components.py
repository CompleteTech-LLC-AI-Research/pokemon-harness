#!/usr/bin/env python3
"""Asset-free structural and controlled-behavior diagnostics for issue #133.

These checks do not replace the full source/native harness suite or gameplay
acceptance. No runtime dependency is replaced in the production package; import
doubles below are confined to this explicit diagnostic command.

Run: python scripts/check_pyboy_components.py
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib
import inspect
import linecache
import runpy
import shutil
import sys
import tempfile
import tomllib
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "vendor" / "pyboy-src" / "pyboy"
SUPPORT = runpy.run_path(str(PACKAGE / "_source.py"))
BASE_BLOB = "7899e2df64b889372589c81ee74263b5ef7e9389"
PXD_BLOB = "472d5bb71a50d53de51a9b134c26eb890cefbc5c"
CORE = PACKAGE / "core"
COMPONENTS = runpy.run_path(str(PACKAGE / "_components.py"))
# The exact pre-split blobs these components must reassemble to, and the
# augmenting .pxd each native staging step must reproduce byte for byte.
SPLIT_BLOBS = {
    "mb": ("bab52c95220a8e422ea6d1b7f5f01377ac13dda5", "de820c64eb096fcb3ca215a143d6a05fb57424b3"),
    "lcd": ("db45fb06fde169055c38a102f611312071b83b82", "d8bff147655dbca94543d7e21259f049e4a2a3da"),
}
SPLIT_LINES = {"mb": 1220, "lcd": 1087}


def blob_id(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class FakeEvent(int):
    """Deterministic event identifiers, not an emulator implementation."""


for _index, _name in enumerate(
    [
        f"{edge}_{kind}_{button}"
        for edge in ("PRESS", "RELEASE")
        for kind, buttons in (
            ("ARROW", ("LEFT", "RIGHT", "UP", "DOWN")),
            ("BUTTON", ("A", "B", "START", "SELECT")),
        )
        for button in buttons
    ]
):
    setattr(FakeEvent, _name, _index)


class FakeError(Exception):
    pass


class FakeLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeArray:
    """Only an isinstance target is needed by the isolated mapping check."""


class FakeAPI:
    pass


def controlled_importer():
    logger = FakeLogger()
    constants = types.SimpleNamespace(TILES=384, TILES_CGB=768, SPRITES=40)
    known = {
        "numpy": types.SimpleNamespace(ndarray=FakeArray),
        "cython": types.SimpleNamespace(gil=nullcontext(), nogil=nullcontext()),
        "pyboy.api.constants": constants,
        "pyboy.logging": types.SimpleNamespace(
            get_logger=lambda name: logger, log_level=lambda x: None
        ),
        "pyboy.plugins.manager": types.SimpleNamespace(
            PluginManager=FakeAPI, parser_arguments=list
        ),
        "pyboy.utils": types.SimpleNamespace(
            IntIOWrapper=lambda obj: obj,
            PyBoyException=FakeError,
            PyBoyInvalidInputException=FakeError,
            PyBoyInvalidOperationException=FakeError,
            PyBoyOutOfBoundsException=FakeError,
            WindowEvent=FakeEvent,
            cython_compiled=False,
            OPCODE_BRK=0xDB,
        ),
        "api": types.SimpleNamespace(Sprite=FakeAPI, Tile=FakeAPI, constants=constants),
        "core.mb": types.SimpleNamespace(Motherboard=FakeAPI),
        "_source": types.SimpleNamespace(load_module=SUPPORT["load_module"]),
    }
    for module, name in (
        ("gameshark", "GameShark"),
        ("memory_scanner", "MemoryScanner"),
        ("screen", "Screen"),
        ("sound", "Sound"),
        ("tilemap", "TileMap"),
    ):
        known[f"pyboy.api.{module}"] = types.SimpleNamespace(**{name: FakeAPI})

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if name in known:
            return known[name]
        if name in {"heapq", "os", "re", "time", "pathlib", "itertools"} and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        raise ImportError(f"Unexpected diagnostic import: {level}:{name}")

    return importing


def load_controlled_module():
    module = types.ModuleType("pyboy.pyboy")
    module.__file__ = str(PACKAGE / "pyboy.py")
    module.__package__ = "pyboy"
    scope = dict(vars(builtins), __import__=controlled_importer())
    module.__dict__["__builtins__"] = scope
    exec(compile((PACKAGE / "pyboy.py").read_bytes(), module.__file__, "exec"), module.__dict__)  # noqa: S102
    return module


class ComponentChecks(unittest.TestCase):
    def test_reconstruction_is_the_verified_original_blob(self):
        source = SUPPORT["assemble_source"](PACKAGE)
        self.assertEqual(blob_id(source), BASE_BLOB)
        self.assertEqual(len(source.splitlines()), 2118)
        tree = ast.parse(source)
        classes = [item for item in tree.body if isinstance(item, ast.ClassDef)]
        self.assertEqual(
            [item.name for item in classes], ["PyBoy", "PyBoyRegisterFile", "PyBoyMemoryView"]
        )
        self.assertEqual(sum(isinstance(n, ast.FunctionDef) for n in classes[0].body), 40)

    def test_tracked_components_are_bounded(self):
        for name in (*SUPPORT["COMPONENTS"], "_source.py", "pyboy.py"):
            with self.subTest(name=name):
                self.assertLess(len((PACKAGE / name).read_bytes().splitlines()), 1000)

    def test_declarations_are_unchanged(self):
        self.assertEqual(blob_id((PACKAGE / "pyboy.pxd").read_bytes()), PXD_BLOB)

    def test_missing_component_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in SUPPORT["COMPONENTS"][:-1]:
                (Path(tmp) / name).write_bytes((PACKAGE / name).read_bytes())
            with self.assertRaises(FileNotFoundError):
                SUPPORT["assemble_source"](tmp)

    def test_native_stage_uses_identical_source_and_declarations(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = SUPPORT["stage_native_source"](PACKAGE, tmp)
            self.assertEqual(blob_id(source.read_bytes()), BASE_BLOB)
            self.assertEqual(blob_id(source.with_suffix(".pxd").read_bytes()), PXD_BLOB)
            before = source.stat().st_mtime_ns
            SUPPORT["stage_native_source"](PACKAGE, tmp)
            self.assertEqual(before, source.stat().st_mtime_ns)

    def test_native_stage_refuses_overwriting_tracked_package(self):
        with self.assertRaises(ValueError):
            SUPPORT["stage_native_source"](PACKAGE, PACKAGE.parent)

    def test_package_allowlist_has_every_component(self):
        settings = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            settings["tool"]["setuptools"]["package-data"]["pyboy"][: len(SUPPORT["COMPONENTS"])],
            list(SUPPORT["COMPONENTS"]),
        )
        # The bounded mb/lcd splits add their shared loader beside the pyboy.py
        # components. It is read at import time, so it must ship as package data,
        # but it is not one of the pyboy.py .pxi components named in _source.py.
        self.assertIn(
            "_components.py",
            settings["tool"]["setuptools"]["package-data"]["pyboy"],
        )

    def test_setup_stages_instead_of_compiling_the_facade(self):
        setup = (PACKAGE.parent / "setup.py").read_text()
        self.assertIn('source_support["stage_native_source"]', setup)
        self.assertIn("[compiler_source(src)]", setup)
        self.assertIn("return str(main_source)", setup)
        self.assertIn("return _opcode_build.prepare_opcode_source(src)", setup)
        self.assertIn("depends=main_dependencies if src == main_path else []", setup)
        self.assertIn("include_path=[os.getcwd()]", setup)
        self.assertIn('"conftest.py", "_source.py"', setup)

    def test_public_names_signatures_and_bytecode_are_preserved(self):
        module = load_controlled_module()
        expected = {"__name__": "pyboy.pyboy", "__file__": module.__file__, "__package__": "pyboy"}
        expected["__builtins__"] = module.__dict__["__builtins__"]
        exec(  # noqa: S102
            compile(
                SUPPORT["assemble_source"](PACKAGE), module.__file__, "exec", dont_inherit=True
            ),
            expected,
        )
        self.assertEqual(
            {n for n in expected if not n.startswith("__")},
            {n for n in vars(module) if not n.startswith("__")},
        )
        for name in ("PyBoy", "PyBoyMemoryView", "PyBoyRegisterFile"):
            actual_class, expected_class = getattr(module, name), expected[name]
            self.assertEqual(actual_class.__module__, "pyboy.pyboy")
            self.assertEqual(set(vars(actual_class)), set(vars(expected_class)))
            for method_name, actual in vars(actual_class).items():
                if inspect.isfunction(actual):
                    original = vars(expected_class)[method_name]
                    self.assertEqual(inspect.signature(actual), inspect.signature(original))
                    self.assertEqual(actual.__code__, original.__code__)
                    self.assertEqual(actual.__doc__, original.__doc__)

    def test_inspect_and_linecache_see_original_source(self):
        module = load_controlled_module()
        with patch.dict(sys.modules, {"pyboy.pyboy": module}):
            self.assertTrue(inspect.getsource(module.PyBoy).startswith("class PyBoy:"))
            self.assertTrue(inspect.getsource(module.PyBoy.tick).lstrip().startswith("def tick("))
            linecache.checkcache()
            self.assertEqual(
                "".join(linecache.getlines(module.__file__)).encode(),
                SUPPORT["assemble_source"](PACKAGE),
            )

    def test_register_masks_are_unchanged(self):
        module = load_controlled_module()
        cpu = types.SimpleNamespace()
        registers = module.PyBoyRegisterFile(cpu)
        for name in ("A", "F", "B", "C", "D", "E", "HL", "SP", "PC"):
            mask = 0xF0 if name == "F" else 0xFFFF if len(name) == 2 else 0xFF
            for value in (-1, 0, 17, 255, 256, 65535, 65536):
                with self.subTest(name=name, value=value):
                    setattr(registers, name, value)
                    self.assertEqual(getattr(registers, name), value & mask)

    def test_button_press_release_and_queued_input(self):
        module = load_controlled_module()
        obj = module.PyBoy.__new__(module.PyBoy)
        obj.frame_count, obj.events, obj.queued_input = 7, [], []
        for button in ("a", "b", "start", "select", "left", "right", "up", "down"):
            obj.events, obj.queued_input = [], []
            obj.button(button.upper(), 3)
            self.assertEqual(len(obj.events), 1)
            self.assertEqual(obj.queued_input[0][0], 10)
            obj.frame_count = 10
            obj._post_handle_events()
            self.assertEqual(len(obj.events), 1)
            self.assertEqual(obj.queued_input, [])
            obj.frame_count = 7
        for method in (obj.button, obj.button_press, obj.button_release):
            with self.assertRaises(FakeError):
                method("not-a-button")

    def test_tick_dispatches_instance_owner_for_each_frame(self):
        module = load_controlled_module()
        obj = module.PyBoy.__new__(module.PyBoy)
        obj.mb = types.SimpleNamespace(
            serial=types.SimpleNamespace(
                check_error=lambda: None, check_execution_allowed=lambda: None
            )
        )
        calls, post = [], []
        obj._tick = lambda render, sound: calls.append((render, sound)) or True
        obj._post_tick = lambda: post.append(True)
        obj.avg_tick = obj.avg_emu = 0
        self.assertTrue(obj.tick(3, True, True))
        self.assertEqual(calls, [(False, False), (False, False), (True, True)])
        self.assertEqual(post, [True])

    def test_memory_private_methods_slices_and_error_checks(self):
        module = load_controlled_module()
        data = bytearray(65536)
        checks = []
        board = types.SimpleNamespace(
            serial=types.SimpleNamespace(check_error=lambda: checks.append(True)),
            getitem=lambda addr: data[addr],
            setitem=lambda addr, value: data.__setitem__(addr, value),
        )
        memory = module.PyBoyMemoryView(board)
        memory[0xC000:0xC004] = [10, 20, 30, 40]
        self.assertEqual(memory[0xC000:0xC004], [10, 20, 30, 40])
        memory[0xC000] = 55
        self.assertEqual(memory[0xC000], 55)
        self.assertTrue(checks)
        self.assertTrue(hasattr(memory, "_PyBoyMemoryView__getitem"))
        for action in (
            lambda: memory[:4],
            lambda: memory[4:4],
            lambda: len(memory),
            lambda: iter(memory),
        ):
            with self.assertRaises(FakeError):
                action()

    def test_symbol_lookup_and_missing_symbol_error(self):
        module = load_controlled_module()
        obj = module.PyBoy.__new__(module.PyBoy)
        obj.rom_symbols_inverse = {"Main": (1, 0x4000)}
        self.assertEqual(obj.symbol_lookup("Main"), (1, 0x4000))
        with self.assertRaisesRegex(ValueError, "Symbol not found: Missing"):
            obj.symbol_lookup("Missing")

    def test_constructor_failure_and_partial_object_cleanup(self):
        module = load_controlled_module()
        with self.assertRaises(FileNotFoundError):
            module.PyBoy(None, window="null")
        module.PyBoy.__new__(module.PyBoy).__del__()


class SplitModuleChecks(unittest.TestCase):
    """Bounded-component splits of pyboy/core/mb.py and pyboy/core/lcd.py."""

    def stage(self, stem, root):
        return COMPONENTS["stage_native_source"](
            CORE,
            stem,
            f"pyboy/core/{stem}.py",
            declarations=f"{stem}.pxd",
            build_root=root,
        )

    def copy_tree(self, stem, tmp):
        """Snapshot one stem's facade, manifest and components into `tmp`."""
        target = Path(tmp) / stem
        (target / f"{stem}_components").mkdir(parents=True)
        (target / f"{stem}.py").write_bytes((CORE / f"{stem}.py").read_bytes())
        shutil.copyfile(
            CORE / f"{stem}_components_manifest.py", target / f"{stem}_components_manifest.py"
        )
        for shard in (CORE / f"{stem}_components").iterdir():
            shutil.copyfile(shard, target / f"{stem}_components" / shard.name)
        return target

    def test_reconstruction_is_the_verified_pre_split_blob(self):
        for stem, (source_blob, _pxd) in SPLIT_BLOBS.items():
            with self.subTest(stem=stem):
                source = COMPONENTS["assemble"](CORE, stem)
                self.assertEqual(blob_id(source), source_blob)
                self.assertEqual(len(source.splitlines()), SPLIT_LINES[stem])

    def test_facades_and_components_stay_bounded(self):
        for stem in SPLIT_BLOBS:
            with self.subTest(stem=stem):
                paths = [
                    CORE / f"{stem}.py",
                    CORE / f"{stem}_components_manifest.py",
                    *(CORE / f"{stem}_components").iterdir(),
                ]
                for path in paths:
                    with self.subTest(path=path.name):
                        self.assertLess(len(path.read_bytes().splitlines()), 1000)

    def test_declarations_are_unchanged(self):
        for stem, (_source, pxd_blob) in SPLIT_BLOBS.items():
            with self.subTest(stem=stem):
                self.assertEqual(blob_id((CORE / f"{stem}.pxd").read_bytes()), pxd_blob)

    def test_generator_output_matches_the_tracked_artifacts(self):
        """The generator reproduces the tracked artifacts and is idempotent."""
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "core"
            copy.mkdir()
            for name in (
                "mb.py",
                "lcd.py",
                "components_layout.py",
                "mb_components_manifest.py",
                "lcd_components_manifest.py",
            ):
                shutil.copyfile(CORE / name, copy / name)
            for stem in SPLIT_BLOBS:
                shutil.copytree(CORE / f"{stem}_components", copy / f"{stem}_components")
            layout = runpy.run_path(str(copy / "components_layout.py"))
            runtime = runpy.run_path(str(PACKAGE / "_components.py"))
            layout["CORE"] = copy
            for stem in SPLIT_BLOBS:
                before = {p: p.read_bytes() for p in (copy / f"{stem}_components").iterdir()}
                rebuilt = runtime["assemble"](copy, stem)
                self.assertEqual(blob_id(rebuilt), SPLIT_BLOBS[stem][0])
                self.assertEqual((copy / f"{stem}.py").read_bytes(), layout["FACADES"][stem])
                self.assertEqual(
                    (copy / f"{stem}_components_manifest.py").read_text(),
                    layout["render_manifest"](stem, rebuilt, layout["SPLITS"][stem][1]),
                )
                # Regenerating over the copy must not move a single byte.
                layout["emit"](stem, f"{stem}.py", layout["SPLITS"][stem][1], rebuilt)
                self.assertEqual(
                    {p: p.read_bytes() for p in (copy / f"{stem}_components").iterdir()},
                    before,
                )

    def test_missing_component_fails_closed(self):
        for stem in SPLIT_BLOBS:
            with self.subTest(stem=stem), tempfile.TemporaryDirectory() as tmp:
                copy = self.copy_tree(stem, tmp)
                names = COMPONENTS["_manifest"](copy, stem)["SOURCE_PARTS"]
                (copy / names[-1][0]).unlink()
                with self.assertRaises((OSError, ValueError)):
                    COMPONENTS["assemble"](copy, stem)

    def test_edited_blanked_or_symlinked_component_fails_closed(self):
        for stem in SPLIT_BLOBS:
            names = [n for n, _ in COMPONENTS["_manifest"](CORE, stem)["SOURCE_PARTS"]]

            def body(path):
                path.write_bytes(path.read_bytes() + b"\nX\n")

            def blank(path):
                path.write_bytes(b"")

            def symlink(path):
                path.unlink()
                path.symlink_to(CORE / f"{stem}.pxd")

            for label, mutate in (("body", body), ("blank", blank), ("symlink", symlink)):
                with self.subTest(stem=stem, case=label), tempfile.TemporaryDirectory() as tmp:
                    copy = self.copy_tree(stem, tmp)
                    mutate(copy / names[0])
                    with self.assertRaises((OSError, ValueError)):
                        COMPONENTS["assemble"](copy, stem)

    def test_reordered_components_fail_closed(self):
        for stem in SPLIT_BLOBS:
            with self.subTest(stem=stem), tempfile.TemporaryDirectory() as tmp:
                copy = self.copy_tree(stem, tmp)
                manifest = copy / f"{stem}_components_manifest.py"
                text = manifest.read_text()
                first, second = [n for n, _ in COMPONENTS["_manifest"](CORE, stem)["SOURCE_PARTS"]][
                    :2
                ]
                # Swap the two leading entries without touching the digests.
                lines = text.splitlines(keepends=True)
                i = lines.index([ln for ln in lines if f"'{first}'" in ln][0])
                j = lines.index([ln for ln in lines if f"'{second}'" in ln][0])
                lines[i], lines[j] = lines[j], lines[i]
                manifest.write_text("".join(lines))
                with self.assertRaises((OSError, ValueError)):
                    COMPONENTS["assemble"](copy, stem)

    def test_manifest_must_stay_literal_versioned_and_bounded(self):
        for stem in SPLIT_BLOBS:
            with tempfile.TemporaryDirectory() as tmp:
                copy = self.copy_tree(stem, tmp)
                manifest = copy / f"{stem}_components_manifest.py"
                original = manifest.read_text()
                # Injected code is never executed, and is rejected.
                manifest.write_text("import os\n" + original)
                with self.assertRaises(ValueError):
                    COMPONENTS["_manifest"](copy, stem)
                for label, old, new in (
                    ("unsupported-version", "FORMAT_VERSION = 1", "FORMAT_VERSION = 2"),
                    ("raised-line-bound", "MAX_LINES = 900", "MAX_LINES = 1000"),
                    ("unknown-key", "FORMAT_VERSION = 1", "FORMAT_VERSION = 1\nEXTRA = 1"),
                ):
                    with self.subTest(stem=stem, case=label):
                        manifest.write_text(original.replace(old, new, 1))
                        with self.assertRaises(ValueError):
                            COMPONENTS["_manifest"](copy, stem)

    def test_native_stage_uses_identical_source_and_declarations(self):
        for stem, (source_blob, pxd_blob) in SPLIT_BLOBS.items():
            with self.subTest(stem=stem), tempfile.TemporaryDirectory() as tmp:
                staged = self.stage(stem, tmp)
                self.assertEqual(blob_id(staged.read_bytes()), source_blob)
                self.assertEqual(blob_id(staged.with_suffix(".pxd").read_bytes()), pxd_blob)
                # Staged input is compiler input, never a new import path.
                self.assertNotIn(CORE.resolve(), staged.resolve().parents)
                before = staged.stat().st_mtime_ns
                self.stage(stem, tmp)
                self.assertEqual(before, staged.stat().st_mtime_ns)

    def test_native_stage_refuses_overwriting_tracked_package(self):
        for stem in SPLIT_BLOBS:
            with self.subTest(stem=stem):
                with self.assertRaises(ValueError):
                    COMPONENTS["stage_native_source"](
                        CORE,
                        stem,
                        f"pyboy/core/{stem}.py",
                        declarations=f"{stem}.pxd",
                        build_root=CORE.parent.parent,
                    )

    def test_setup_stages_the_split_modules_instead_of_their_facades(self):
        setup = (CORE.parent.parent / "setup.py").read_text()
        # The keys are matched against os.path.relpath(src, ROOT_DIR), which is
        # package-relative, so they must not repeat the "pyboy/" prefix.
        self.assertIn('"core/mb.py": ("mb", "mb.pxd")', setup)
        self.assertIn('"core/lcd.py": ("lcd", "lcd.pxd")', setup)
        self.assertIn("if relative in staged_components:", setup)
        self.assertIn("return str(staged_components[relative])", setup)
        for name in (
            "_components.py",
            "mb_components_manifest.py",
            "lcd_components_manifest.py",
            "components_layout.py",
        ):
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', setup)

    def test_package_data_ships_every_component(self):
        core_data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        core_data = core_data["tool"]["setuptools"]["package-data"]["pyboy.core"]
        for pattern in (
            "mb_components/*.pxi",
            "lcd_components/*.pxi",
            "mb_components_manifest.py",
            "lcd_components_manifest.py",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, core_data)

    def test_imported_split_modules_match_the_pre_split_public_surface(self):
        """Import the real split modules and compare against the pre-split source."""
        for stem in SPLIT_BLOBS:
            with self.subTest(stem=stem):
                module = importlib.import_module(f"pyboy.core.{stem}")
                self.assertEqual(Path(module.__file__).resolve(), (CORE / f"{stem}.py").resolve())
                original = COMPONENTS["assemble"](CORE, stem)
                namespace = {
                    "__file__": module.__file__,
                    "__name__": module.__name__,
                    "__package__": module.__package__,
                    "__builtins__": __builtins__,
                }
                exec(  # noqa: S102
                    compile(original, module.__file__, "exec", dont_inherit=True),
                    namespace,
                    namespace,
                )
                self.assertEqual(
                    {n for n in namespace if not n.startswith("__")},
                    {n for n in vars(module) if not n.startswith("__")},
                )
                # inspect and linecache still see the original logical lines.
                linecache.checkcache()
                self.assertEqual("".join(linecache.getlines(module.__file__)).encode(), original)
                for name, value in namespace.items():
                    if name.startswith("_") or not inspect.isclass(value):
                        continue
                    if value.__module__ != module.__name__:
                        continue
                    with self.subTest(symbol=name):
                        actual = getattr(module, name)
                        self.assertEqual(set(vars(actual)), set(vars(value)))
                        self.assertTrue(inspect.getsource(actual).startswith("class "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
