"""Asset-free generator/loader contracts; these do not qualify real PyBoy ABI."""

import ast
import importlib.util
import inspect
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "vendor/pyboy-src/pyboy/core"


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layout = module_at("_test_opcode_layout", CORE / "opcodes_layout.py")
runtime = module_at("_test_opcode_runtime", CORE / "_opcodes_runtime.py")
fixture = module_at("_test_opcode_fixture", Path(__file__).with_name("_opcode_layout_fixture.py"))


@pytest.fixture
def sources():
    return fixture.make_sources()


def install(tmp_path, sources, limit=900):
    core = tmp_path / "core"
    result = layout.emit_layout(*sources, core, limit)
    shutil.copy2(CORE / "_opcodes_runtime.py", core / "_opcodes_runtime.py")
    return core, result


@pytest.mark.parametrize("limit", [120, 511, 900, 999])
def test_lossless_all_512_instruction_shapes_and_line_limits(tmp_path, sources, limit):
    core, result = install(tmp_path, sources, limit)
    assert runtime.read_sources(core) == sources
    assert len(sources[0].splitlines()) > 5000
    assert len(sources[1].splitlines()) > 1000
    assert all(len(data.splitlines()) < 1000 for data in result.values())
    assert all(
        len(data.splitlines()) <= limit for name, data in result.items() if name.endswith(".pxi")
    )
    assert any("dispatch" in name for name in result)
    assert any("shared" in name for name in result)
    assert any("tables" in name for name in result)


def test_deterministic_idempotent_generation_preserves_mtime(tmp_path, sources):
    core, first = install(tmp_path, sources)
    times = {name: (core / name).stat().st_mtime_ns for name in first}
    assert layout.emit_layout(*sources, core) == first
    assert times == {name: (core / name).stat().st_mtime_ns for name in first}


def test_exact_newline_round_trip(tmp_path, sources):
    crlf = tuple(value.replace(b"\n", b"\r\n") for value in sources)
    core, _ = install(tmp_path, crlf)
    assert runtime.read_sources(core) == crlf


def test_no_final_newline_is_preserved(tmp_path, sources):
    without = tuple(value.rstrip(b"\n") for value in sources)
    core, _ = install(tmp_path, without)
    assert runtime.read_sources(core) == without


@pytest.mark.parametrize("bad", [0, 1000, -1, True, 900.0, "900"])
def test_invalid_line_bound_is_rejected(sources, bad):
    with pytest.raises(ValueError):
        layout.render_layout(*sources, max_lines=bad)


def test_source_loader_preserves_globals_dispatch_and_introspection(tmp_path, sources):
    core, _ = install(tmp_path, sources)
    filename = str(core / "opcodes.py")
    original = {"__name__": "fixture.core.opcodes", "__file__": filename}
    candidate = original.copy()
    exec(compile(sources[0], filename, "exec"), original)  # noqa: S102
    runtime.load_into(candidate)
    assert set(original) == set(candidate)
    assert original["CPU_COMMANDS"] == candidate["CPU_COMMANDS"]
    assert original["OPCODE_LENGTHS"] == candidate["OPCODE_LENGTHS"]
    for opcode in range(512):
        for value in (0, 1, 127, 128, 255):
            a = types.SimpleNamespace(A=37, F=0, PC=65535, cycles=2**32, bail=False)
            b = types.SimpleNamespace(**vars(a))
            assert original["execute_opcode"](a, opcode, value) == candidate["execute_opcode"](
                b, opcode, value
            )
            assert vars(a) == vars(b)
    for opcode in (-1, 512, 65535, "not an opcode", None, 1.0):
        a = types.SimpleNamespace(A=1, F=0, PC=0, cycles=0, bail=False)
        b = types.SimpleNamespace(**vars(a))
        assert original["execute_opcode"](a, opcode, 3) == candidate["execute_opcode"](b, opcode, 3)
        assert vars(a) == vars(b)
    function = candidate["OP_00"]
    assert function.__module__ == "fixture.core.opcodes"
    assert function.__globals__ is candidate
    assert function.__code__.co_filename == filename
    assert "def OP_00" in inspect.getsource(function)
    assert "def execute_opcode" in inspect.getsource(candidate["execute_opcode"])
    candidate["OP_00"] = lambda cpu, value: 1234
    assert candidate["execute_opcode"](object(), 0, 0) == 1234


def test_facade_can_be_loaded_under_an_alias(tmp_path, sources, monkeypatch):
    core, _ = install(tmp_path, sources)
    root = types.ModuleType("fixture")
    root.__path__ = [str(tmp_path)]
    package = types.ModuleType("fixture.core")
    package.__path__ = [str(core)]
    monkeypatch.setitem(sys.modules, "fixture", root)
    monkeypatch.setitem(sys.modules, "fixture.core", package)
    monkeypatch.setitem(sys.modules, "fixture.core._opcodes_runtime", runtime)
    module = module_at("fixture.core._counter_opcodes", core / "opcodes.py")
    assert module.OP_00.__module__ == "fixture.core._counter_opcodes"
    assert not hasattr(module, "_load_opcode_components")
    assert len(module.CPU_COMMANDS) == 512


@pytest.mark.parametrize("which", ["SOURCE_PARTS", "DECLARATION_PARTS"])
def test_corruption_fails_closed(tmp_path, sources, which):
    core, _ = install(tmp_path, sources)
    name = runtime._manifest(core)[which][0][0]
    (core / name).write_bytes((core / name).read_bytes() + b"# edited\n")
    with pytest.raises(ValueError, match="checksum"):
        runtime.read_sources(core)
    with pytest.raises(ImportError, match="checksum"):
        runtime.load_into({"__file__": str(core / "opcodes.py")})
    with pytest.raises(ValueError, match="checksum"):
        layout.emit_layout(*sources, core)


def test_missing_component_fails_closed(tmp_path, sources):
    core, _ = install(tmp_path, sources)
    (core / runtime._manifest(core)["SOURCE_PARTS"][0][0]).unlink()
    with pytest.raises(ImportError):
        runtime.load_into({"__file__": str(core / "opcodes.py")})


@pytest.mark.parametrize("extra", ["import os\n", "FORMAT_VERSION = 1\n", "UNKNOWN = 1\n"])
def test_manifest_is_strict_literal_data(tmp_path, sources, extra):
    core, _ = install(tmp_path, sources)
    path = core / runtime.MANIFEST_NAME
    path.write_bytes(path.read_bytes() + extra.encode())
    with pytest.raises(ValueError):
        runtime.read_sources(core)


def test_path_traversal_is_rejected(tmp_path, sources):
    core, _ = install(tmp_path, sources)
    path = core / runtime.MANIFEST_NAME
    path.write_text(
        path.read_text().replace("opcode_components/source_shared_", "../source_shared_")
    )
    with pytest.raises(ValueError, match="path/hash"):
        runtime.read_sources(core)


def test_symlink_is_rejected(tmp_path, sources):
    core, _ = install(tmp_path, sources)
    path = core / runtime._manifest(core)["SOURCE_PARTS"][0][0]
    other = tmp_path / "external.pxi"
    path.replace(other)
    try:
        path.symlink_to(other)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("symlink creation requires an unavailable Windows privilege")
        raise
    with pytest.raises(ValueError, match="symbolic"):
        runtime.read_sources(core)


def test_declaration_facade_cannot_drift(tmp_path, sources):
    core, _ = install(tmp_path, sources)
    with (core / "opcodes.pxd").open("a") as stream:
        stream.write("# unreviewed edit\n")
    with pytest.raises(ValueError, match="facade"):
        runtime.read_sources(core)


def test_changed_generation_removes_only_previously_owned_fragments(tmp_path, sources):
    core, old = install(tmp_path, sources)
    sentinel = core / "opcode_components/operator-note.txt"
    sentinel.write_text("keep me")
    changed = (sources[0].replace(b"cpu.cycles += 4", b"cpu.cycles += 8"), sources[1])
    new = layout.emit_layout(*changed, core)
    assert runtime.read_sources(core) == changed
    assert sentinel.read_text() == "keep me"
    assert not any((core / path).exists() for path in set(old) - set(new))


def test_native_input_is_exactly_legacy_source_and_pxd(tmp_path, sources, monkeypatch):
    core = tmp_path / "pyboy/core"
    layout.emit_layout(*sources, core)
    shutil.copy2(CORE / "_opcodes_runtime.py", core / "_opcodes_runtime.py")
    build = module_at("_test_opcode_build", CORE / "_opcodes_runtime.py")
    monkeypatch.chdir(tmp_path)
    path = Path(build.prepare_opcode_source("pyboy/core/opcodes.py"))
    assert path.parts[0] == "build"
    assert path.read_bytes() == sources[0]
    assert path.with_suffix(".pxd").read_bytes() == sources[1]
    assert build.prepare_opcode_source("pyboy/core/cpu.py") == "pyboy/core/cpu.py"
    stamp = path.stat().st_mtime_ns
    build.prepare_opcode_source("pyboy/core/opcodes.py")
    assert path.stat().st_mtime_ns == stamp


def test_decorator_and_cdef_are_never_split(tmp_path, sources):
    core, _ = install(tmp_path, sources, 120)
    for name, _ in runtime._manifest(core)["DECLARATION_PARTS"]:
        lines = (core / name).read_text().splitlines()
        assert not lines[-1].startswith("@")
        assert not lines[0].startswith("cdef uint8_t OP_")


def test_refuses_unowned_component_collision(tmp_path, sources):
    core = tmp_path / "core"
    generated = layout.render_layout(*sources)
    name = next(name for name in generated if name.endswith(".pxi"))
    path = core / name
    path.parent.mkdir(parents=True)
    path.write_text("operator edit")
    with pytest.raises(ValueError, match="unowned"):
        layout.emit_layout(*sources, core)
    assert path.read_text() == "operator edit"


def test_generated_manifest_values_are_all_literal(sources):
    result = layout.render_layout(*sources)
    for node in ast.parse(result["_opcodes_manifest.py"]).body:
        ast.literal_eval(node.value)
