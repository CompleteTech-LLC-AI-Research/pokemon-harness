"""Exercise the actual generator/build integration with authored input seams."""

import argparse
import ast
import io
import os
import types
from pathlib import Path

import pytest

try:
    from tests.test_opcode_layout import CORE, ROOT, fixture, layout, runtime
except ModuleNotFoundError:  # The standalone archive runs with tests/ on sys.path.
    from test_opcode_layout import CORE, ROOT, fixture, layout, runtime


def generator_namespace(tmp_path):
    """Extract only the real emission entrypoints; never fetch upstream HTML."""
    tree = ast.parse((CORE / "opcodes_gen.py").read_text())
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"update", "load", "main", "_emit_layout"}
    ]
    namespace = {
        "__name__": "generator_emission_test",
        "argparse": argparse,
        "Path": Path,
        "StringIO": io.StringIO,
        "emit_layout": layout.emit_layout,
        "read_sources": runtime.read_sources,
        "destination": str(tmp_path / "opcodes.py"),
        "pxd_destination": str(tmp_path / "opcodes.pxd"),
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {"warning", "imports", "cimports"}:
                namespace[target.id] = ast.literal_eval(node.value)
    exec(compile(ast.Module(body=selected, type_ignores=[]), "opcodes_gen.py", "exec"), namespace)  # noqa: S102
    return namespace


def test_real_generator_offline_entrypoint_round_trip_and_idempotence(tmp_path):
    original = fixture.make_sources()
    (tmp_path / "opcodes.py").write_bytes(original[0])
    (tmp_path / "opcodes.pxd").write_bytes(original[1])
    namespace = generator_namespace(tmp_path)
    namespace["main"](["--from-existing"])
    assert runtime.read_sources(tmp_path) == original
    files = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    namespace["main"](["--from-existing"])
    assert runtime.read_sources(tmp_path) == original
    assert files == {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }


def test_real_generator_offline_refuses_damaged_layout(tmp_path):
    layout.emit_layout(*fixture.make_sources(), tmp_path)
    name = runtime._manifest(tmp_path)["SOURCE_PARTS"][0][0]
    (tmp_path / name).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        generator_namespace(tmp_path)["main"](["--from-existing"])


def test_real_generator_update_runs_complete_512_entry_emission(tmp_path):
    namespace = generator_namespace(tmp_path)
    captured = []

    class FakeOpcode:
        def __init__(self, number):
            self.number = number

        def createfunction(self):
            name = f"NOP_{self.number:02X}"
            return (
                (1, name, "NOP"),
                (
                    f"cdef uint8_t {name}(cpu.CPU) except * nogil",
                    f"def {name}(cpu):\n\tcpu.PC += 1\n\tcpu.cycles += 4",
                ),
            )

    namespace["opcodes"] = [FakeOpcode(number) for number in range(512)]
    namespace["urlopen"] = lambda url: captured.append(url) or io.BytesIO(b"authored&nbsp;table")
    namespace["MyHTMLParser"] = lambda: types.SimpleNamespace(
        feed=lambda text: captured.append(text)
    )
    namespace["update"]()
    original = runtime.read_sources(tmp_path)
    tree = ast.parse(original[0])
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert len(functions) == 515  # 512 instructions + BRK + no_opcode + dispatcher.
    names = {node.name for node in functions}
    assert {"BRK", "no_opcode", "execute_opcode", "NOP_00", "NOP_1FF"} <= names
    assert b"elif opcode == 0xDB:\n        return BRK(cpu)" in original[0]
    assert b"elif opcode == 0x1FF:\n        return NOP_1FF(cpu)" in original[0]
    assert original[1].startswith((namespace["warning"] + namespace["cimports"]).encode())
    assert (
        len([line for line in original[1].splitlines() if line.startswith(b"cdef uint8_t NOP_")])
        == 512
    )
    namespace["update"]()
    assert runtime.read_sources(tmp_path) == original
    assert captured[1] == "authored<br>table"


def test_real_generator_preserves_literal_declaration_contracts():
    tree = ast.parse((CORE / "opcodes_gen.py").read_text())
    assignments = {
        node.targets[0].id: node
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert (
        "cdef int execute_opcode(cpu.CPU, uint16_t, uint16_t) except * nogil"
        in ast.literal_eval(assignments["cimports"].value)
    )
    templates = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("cdef uint8_t %s_")
    ]
    assert len(templates) == 2
    assert all("except * nogil" in item for item in templates)


def test_actual_setup_excludes_helpers_and_keeps_other_modules(tmp_path, monkeypatch):
    for relative in (
        "pyboy/core/cpu.py",
        "pyboy/core/opcodes.py",
        "pyboy/core/opcodes.pxd",
        "pyboy/core/opcodes_gen.py",
        "pyboy/core/opcodes_gen_handlers.py",
        "pyboy/core/opcodes_layout.py",
        "pyboy/core/_opcodes_runtime.py",
        "pyboy/core/_opcodes_manifest.py",
        "pyboy/core/opcode_components/a.pxi",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    tree = ast.parse((ROOT / "vendor/pyboy-src/setup.py").read_text())
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "prep_pxd_py_files"
    ]
    namespace = {"os": os, "ROOT_DIR": "pyboy"}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "setup.py", "exec"), namespace)  # noqa: S102
    monkeypatch.chdir(tmp_path)
    assert {path.replace(os.sep, "/") for path in namespace["prep_pxd_py_files"]()} == {
        "pyboy/core/cpu.py",
        "pyboy/core/opcodes.py",
    }


def test_actual_setup_keeps_extension_name_and_routes_both_component_sources():
    tree = ast.parse((ROOT / "vendor/pyboy-src/setup.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Extension"
    ]
    extension = next(node for node in calls if isinstance(node.args[0], ast.Call))
    assert ast.unparse(extension.args[0]) == "src.split('.')[0].replace(os.sep, '.')"
    assert ast.unparse(extension.args[1]) == "[compiler_source(src)]"
    compiler_source = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "compiler_source"
    )
    routed_source = ast.unparse(compiler_source)
    assert "src == main_path" in routed_source
    assert "return str(main_source)" in routed_source
    assert "return _opcode_build.prepare_opcode_source(src)" in routed_source
    cythonize = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cythonize"
    )
    assert any(keyword.arg == "include_path" for keyword in cythonize.keywords)


@pytest.mark.parametrize("file", ["opcodes_gen.py", "opcodes_layout.py", "_opcodes_runtime.py"])
def test_touched_generator_runtime_sources_are_below_1000_lines(file):
    assert len((CORE / file).read_text().splitlines()) < 1000
