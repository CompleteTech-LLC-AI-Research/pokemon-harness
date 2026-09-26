"""Asset-free contracts for the bounded mb/lcd component splits (#150, #155).

These gate the lossless property, the fail-closed loader and the native staging
path in the always-run unit tier. They deliberately do not qualify the native ABI
or any real-ROM behaviour.
"""

import ast
import hashlib
import importlib
import importlib.util
import inspect
import linecache
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "vendor/pyboy-src/pyboy"
CORE = PACKAGE / "core"
STEMS = ("mb", "lcd")


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


components = module_at("_test_split_components", PACKAGE / "_components.py")
layout = module_at("_test_split_layout", CORE / "components_layout.py")


def setup_path_literals(setup_path, wanted):
    """Return the named setup.py module-level assignments, read but not executed."""
    tree = ast.parse(setup_path.read_text(), filename=str(setup_path))
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in wanted:
            try:
                values[target.id] = ast.literal_eval(node.value)
            except ValueError:
                # ROOT_ABS is os.path.dirname(os.path.abspath(__file__)); rebuild
                # it from the real location instead of executing vendored code.
                assert target.id == "ROOT_ABS", target.id
                values[target.id] = str(setup_path.parent)
    assert wanted <= set(values), sorted(wanted - set(values))
    return values


def pxd_declared_members(pxd_path):
    """Map cdef classes to methods that are legitimately Python-invisible.

    Parse declarations with Cython's compiler, not Python's AST or a regex.
    Only plain `cdef` methods lack Python wrappers. `cpdef` methods have both
    C and Python entry points and MUST remain in the required Python surface.
    Public/readonly fields are not methods and must not enter this exemption.
    """
    from Cython.Compiler import Errors, Main, Nodes, Options

    # Cython keeps its error state in thread-local storage that only
    # cythonize() initialises, so parsing without this blows up inside the
    # scanner instead of reporting a real syntax error.
    Errors.init_thread()
    options = Options.CompilationOptions(Options.default_options)
    options.include_path = [str(PACKAGE.parent)]
    options.language_level = 3
    context = Main.Context.from_options(options)
    stem = pxd_path.stem
    scope = context.find_module(stem, need_pxd=False)
    tree = context.parse(
        Main.FileSourceDescriptor(str(pxd_path), stem),
        scope,
        pxd=True,
        full_module_name=stem,
    )

    def statements(node):
        # Cython elides StatListNode around a singleton statement, and an
        # empty extension class has a PassStatNode rather than declarations.
        return node.stats if isinstance(node, Nodes.StatListNode) else (node,)

    declared = {}
    for node in statements(tree.body):
        if isinstance(node, Nodes.CClassDefNode):
            declared[node.class_name] = {
                declarator.base.name
                for item in statements(node.body)
                if isinstance(item, Nodes.CVarDefNode) and not item.overridable
                for declarator in item.declarators
                if isinstance(declarator, Nodes.CFuncDeclaratorNode)
            }
    return declared


def assert_exported_class_surface(module, name, source_class, *, from_source, declared=()):
    """Require the public class and its Python-visible members in both modes."""
    assert hasattr(module, name), (module.__name__, name, "missing public class")
    actual = getattr(module, name)
    assert inspect.isclass(actual), (module.__name__, name, "export is not a class")
    assert actual.__name__ == source_class.__name__
    if from_source:
        assert set(vars(actual)) == set(vars(source_class))
        assert inspect.getsource(actual).startswith("class ")
    else:
        assert [b.__name__ for b in actual.__bases__] == [
            b.__name__ for b in source_class.__bases__
        ]
        # cdef methods may disappear and native descriptors may appear, but
        # plain def AND cpdef methods must survive the split. Never exempt a
        # whole public class merely because its module is compiled.
        required = {
            attr for attr in vars(source_class) if not attr.startswith("__") or attr == "__init__"
        } - set(declared)
        missing = required - set(vars(actual))
        assert not missing, (module.__name__, name, sorted(missing))


# The exact pre-split git blobs the components must reassemble to, and the
# augmenting .pxd each native staging step must reproduce byte for byte.
BASE_BLOBS = {
    "mb": ("bab52c95220a8e422ea6d1b7f5f01377ac13dda5", "de820c64eb096fcb3ca215a143d6a05fb57424b3"),
    "lcd": ("db45fb06fde169055c38a102f611312071b83b82", "d8bff147655dbca94543d7e21259f049e4a2a3da"),
}
BASE_LINES = {"mb": 1220, "lcd": 1087}
FACADE_LINES = 6


def blob_id(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def snapshot(stem, tmp_path):
    """Copy one stem's facade, manifest and components into a scratch tree."""
    target = tmp_path / stem
    (target / f"{stem}_components").mkdir(parents=True)
    (target / f"{stem}.py").write_bytes((CORE / f"{stem}.py").read_bytes())
    shutil.copyfile(
        CORE / f"{stem}_components_manifest.py", target / f"{stem}_components_manifest.py"
    )
    for shard in (CORE / f"{stem}_components").iterdir():
        shutil.copyfile(shard, target / f"{stem}_components" / shard.name)
    return target


def names_for(stem, directory=CORE):
    return [name for name, _ in components._manifest(directory, stem)["SOURCE_PARTS"]]


@pytest.mark.parametrize("stem", STEMS)
def test_pxd_declared_members_sees_every_cdef_method(stem):
    """The native-only branch of the namespace test depends on this helper.

    That branch never runs in pure-Python mode, so a parser that silently
    returns too little would only surface on the hosted native run -- as the
    two rows this split is being judged on. Pin it here so the dependency is
    checked in the always-run tier. The expectations are spelled out literally
    rather than recomputed from the same `.pxd` text the helper parses, so a
    parser that quietly drops declarations fails here instead of on the
    hosted native run.
    """
    declared = pxd_declared_members(CORE / f"{stem}.pxd")
    if stem == "mb":
        assert set(declared) == {"Motherboard", "HDMA"}
        assert declared["Motherboard"] == {
            "save_state",
            "load_state",
            "buttonevent",
            "getitem",
            "setitem",
            "getitem_io_ports",
            "setitem_io_ports",
            "transfer_DMA",
            "switch_speed",
            "_sync_physical_clock",
            "stop",
        }
        # These cpdef methods are Python-visible, including private-looking
        # names and methods declared with exception/with-gil specifications.
        assert declared["Motherboard"].isdisjoint(
            {
                "tick",
                "breakpoint_add",
                "breakpoint_remove",
                "breakpoint_reached",
                "breakpoint_reinject",
                "set_execution_governor",
                "_execution_step",
                "_prune_serial_time_segments",
                "_map_serial_boundary_time",
                "get_physical_clock",
            }
        )
        assert {"tick", "save_state", "load_state", "set_hdma5"} <= declared["HDMA"]
    else:
        assert set(declared) == {
            "LCD",
            "PaletteRegister",
            "STATRegister",
            "LCDCRegister",
            "Renderer",
            "VBKregister",
            "PaletteIndexRegister",
            "PaletteColorRegister",
        }
        # `cdef inline (int, int) name(self)` -- a parenthesized return type
        # is the spelling most likely to be missed by a hand-rolled parser.
        assert {"getviewport", "getwindowpos"} <= declared["LCD"]
        assert {"set", "get", "getcolor"} <= declared["PaletteRegister"]
        assert {"scanline", "scanline_sprites", "colorcode", "sort_sprites"} <= declared["Renderer"]


@pytest.mark.parametrize("stem", STEMS)
def test_components_reassemble_to_the_pre_split_blob(stem):
    source = components.assemble(CORE, stem)
    assert blob_id(source) == BASE_BLOBS[stem][0]
    assert len(source.splitlines()) == BASE_LINES[stem]


@pytest.mark.parametrize("stem", STEMS)
def test_facade_and_components_stay_under_the_line_bound(stem):
    paths = [
        CORE / f"{stem}.py",
        CORE / f"{stem}_components_manifest.py",
        *(CORE / f"{stem}_components").iterdir(),
    ]
    for path in paths:
        assert len(path.read_bytes().splitlines()) < 1000, path.name
    # The tracked facade is the short loader, not a re-authored module.
    assert len((CORE / f"{stem}.py").read_bytes().splitlines()) == FACADE_LINES


@pytest.mark.parametrize("stem", STEMS)
def test_declarations_are_untouched(stem):
    assert blob_id((CORE / f"{stem}.pxd").read_bytes()) == BASE_BLOBS[stem][1]


@pytest.mark.parametrize("stem", STEMS)
def test_generator_reproduces_tracked_artifacts_and_is_idempotent(stem):
    with pytest.MonkeyPatch.context() as patch:
        original = components.assemble(CORE, stem)
        expected_facade = layout.FACADES[stem]
        expected_manifest = layout.render_manifest(stem, original, layout.SPLITS[stem][1])
        assert (CORE / f"{stem}.py").read_bytes() == expected_facade
        assert (CORE / f"{stem}_components_manifest.py").read_text() == expected_manifest
        # Regenerating in place must not move a single byte.
        before = {p.name: p.read_bytes() for p in (CORE / f"{stem}_components").iterdir()}
        layout.emit(stem, f"{stem}.py", layout.SPLITS[stem][1], original)
        after = {p.name: p.read_bytes() for p in (CORE / f"{stem}_components").iterdir()}
        assert after == before
        assert components.assemble(CORE, stem) == original
        patch.undo()


@pytest.mark.parametrize("stem", STEMS)
def test_missing_component_fails_closed(stem, tmp_path):
    copy = snapshot(stem, tmp_path)
    (copy / names_for(stem, copy)[-1]).unlink()
    with pytest.raises((OSError, ValueError)):
        components.assemble(copy, stem)


@pytest.mark.parametrize("stem", STEMS)
def test_component_directory_holds_only_its_own_shards(stem):
    """A shard from the other split must never be committed into this one."""
    other = "lcd" if stem == "mb" else "mb"
    for shard in (CORE / f"{stem}_components").iterdir():
        assert shard.name.startswith(f"{stem}_"), shard.name
        assert not shard.name.startswith(f"{other}_"), shard.name
    tracked = {
        Path(p).name
        for p in subprocess.check_output(
            ["git", "ls-files", "vendor/pyboy-src"], cwd=ROOT, text=True
        ).split()
        if f"/{stem}_components/" in p
    }
    assert tracked == {p.name for p in (CORE / f"{stem}_components").iterdir()}


@pytest.mark.parametrize("stem", STEMS)
@pytest.mark.parametrize("case", ["body", "blank", "symlink"])
def test_edited_blanked_or_symlinked_component_fails_closed(stem, case, tmp_path):
    copy = snapshot(stem, tmp_path)
    target = copy / names_for(stem, copy)[0]
    if case == "body":
        target.write_bytes(target.read_bytes() + b"\nX\n")
    elif case == "blank":
        target.write_bytes(b"")
    else:
        target.unlink()
        try:
            target.symlink_to(CORE / f"{stem}.pxd")
        except OSError as error:
            if os.name != "nt" or getattr(error, "winerror", None) != 1314:
                raise
            # Windows without Developer Mode cannot create a real symlink.
            # Exercise the loader's refusal branch without skipping the test.
            with patch.object(Path, "is_symlink", lambda path: path == target):
                with pytest.raises(ValueError, match="symbolic links"):
                    components.assemble(copy, stem)
            return
    with pytest.raises(ValueError):
        components.assemble(copy, stem)


@pytest.mark.parametrize("stem", STEMS)
def test_reordered_components_fail_closed(stem, tmp_path):
    copy = snapshot(stem, tmp_path)
    manifest = copy / f"{stem}_components_manifest.py"
    lines = manifest.read_text().splitlines(keepends=True)
    first, second = names_for(stem, copy)[:2]
    i = next(n for n, line in enumerate(lines) if f"'{first}'" in line)
    j = next(n for n, line in enumerate(lines) if f"'{second}'" in line)
    lines[i], lines[j] = lines[j], lines[i]
    manifest.write_text("".join(lines))
    with pytest.raises((OSError, ValueError)):
        components.assemble(copy, stem)


@pytest.mark.parametrize("stem", STEMS)
def test_manifest_stays_literal_versioned_and_bounded(stem, tmp_path):
    copy = snapshot(stem, tmp_path)
    manifest = copy / f"{stem}_components_manifest.py"
    original = manifest.read_text()
    # A manifest is never executed: injected code is rejected, not run.
    manifest.write_text("import os\n" + original)
    with pytest.raises(ValueError):
        components._manifest(copy, stem)
    for old, new in (
        ("FORMAT_VERSION = 1", "FORMAT_VERSION = 2"),
        ("MAX_LINES = 900", "MAX_LINES = 1000"),
        ("FORMAT_VERSION = 1", "FORMAT_VERSION = 1\nEXTRA = 1"),
    ):
        manifest.write_text(original.replace(old, new, 1))
        with pytest.raises(ValueError):
            components._manifest(copy, stem)


@pytest.mark.parametrize("stem", STEMS)
def test_native_stage_writes_identical_input_outside_the_package(stem, tmp_path):
    staged = components.stage_native_source(
        CORE,
        stem,
        f"pyboy/core/{stem}.py",
        declarations=f"{stem}.pxd",
        build_root=tmp_path,
    )
    assert blob_id(staged.read_bytes()) == BASE_BLOBS[stem][0]
    assert blob_id(staged.with_suffix(".pxd").read_bytes()) == BASE_BLOBS[stem][1]
    # Compiler input, never a new import path.
    assert CORE.resolve() not in staged.resolve().parents
    before = staged.stat().st_mtime_ns
    again = components.stage_native_source(
        CORE,
        stem,
        f"pyboy/core/{stem}.py",
        declarations=f"{stem}.pxd",
        build_root=tmp_path,
    )
    assert again == staged
    assert before == staged.stat().st_mtime_ns


@pytest.mark.parametrize("stem", STEMS)
def test_native_stage_refuses_to_write_into_the_source_package(stem):
    with pytest.raises(ValueError):
        components.stage_native_source(
            CORE,
            stem,
            f"pyboy/core/{stem}.py",
            declarations=f"{stem}.pxd",
            build_root=CORE.parent.parent,
        )


@pytest.mark.parametrize("stem", STEMS)
def test_imported_module_keeps_the_pre_split_namespace_and_source(stem):
    module = importlib.import_module(f"pyboy.core.{stem}")
    # Under the source runtime the module reports the facade; under the native
    # runtime it reports the compiled extension, where __file__ is a .so and
    # linecache/inspect have no Python source to read. The namespace and class
    # shape below hold in both runtimes; the source-text assertions are scoped
    # to the source runtime, which is the only one that has text to check.
    assert Path(module.__file__).name.split(".")[0] == stem
    from_source = module.__file__.endswith(".py")
    original = components.assemble(CORE, stem)
    namespace = {
        "__file__": module.__file__,
        "__name__": module.__name__,
        "__package__": module.__package__,
        "__builtins__": __builtins__,
    }
    exec(compile(original, module.__file__, "exec", dont_inherit=True), namespace, namespace)
    source_names = {n for n in namespace if not n.startswith("__")}
    module_names = {n for n in vars(module) if not n.startswith("__")}
    if from_source:
        assert source_names == module_names
    else:
        # A compiled extension module does not necessarily re-export every
        # module-level constant the source binds, so require that the module
        # adds no unexpected name. The full public surface is pinned separately
        # by the differential check against the pre-split base.
        assert not module_names - source_names
    if from_source:
        linecache.checkcache()
        assert "".join(linecache.getlines(module.__file__)).encode() == original
    declared = {} if from_source else pxd_declared_members(CORE / f"{stem}.pxd")
    for name, value in namespace.items():
        if name.startswith("_") or not inspect.isclass(value):
            continue
        if value.__module__ != module.__name__:
            continue
        assert_exported_class_surface(
            module,
            name,
            value,
            from_source=from_source,
            declared=declared.get(name, ()),
        )


def test_setup_stages_the_split_modules_rather_than_their_facades():
    setup = (CORE.parent.parent / "setup.py").read_text()
    # Keys are package-relative because the lookup compares them against
    # os.path.relpath(src, ROOT_DIR), which yields "core/mb.py". A key carrying
    # the pyboy/ prefix never matches and silently cythonizes the facade.
    assert '"core/mb.py": ("mb", "mb.pxd")' in setup
    assert '"core/lcd.py": ("lcd", "lcd.pxd")' in setup
    assert "if relative in staged_components:" in setup
    assert "return str(staged_components[relative])" in setup
    for name in (
        "_components.py",
        "mb_components_manifest.py",
        "lcd_components_manifest.py",
        "components_layout.py",
    ):
        assert f'"{name}"' in setup, name


def test_package_data_ships_every_component():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    core_data = data["tool"]["setuptools"]["package-data"]["pyboy.core"]
    for pattern in (
        "mb_components/*.pxi",
        "lcd_components/*.pxi",
        "mb_components_manifest.py",
        "lcd_components_manifest.py",
    ):
        assert pattern in core_data, pattern


def test_generator_check_mode_passes_against_the_tracked_tree():
    """`python components_layout.py --check` must be clean on the tracked tree."""
    result = subprocess.run(
        [sys.executable, str(CORE / "components_layout.py"), "--check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: True" in result.stdout


@pytest.mark.parametrize("stem", STEMS)
def test_generator_needs_no_git_history(stem, tmp_path):
    """--check and regeneration must work where the split is already committed.

    Reading the pre-split file out of `HEAD` would tie the generator to a checkout
    whose HEAD predates the split, and would break in a plain source tree or wheel.
    """
    copy = tmp_path / "pyboy"
    shutil.copytree(PACKAGE, copy)
    core = copy / "core"

    def generated_bytes():
        paths = [
            core / f"{stem}.py",
            core / f"{stem}_components_manifest.py",
            *(core / f"{stem}_components").glob("*.pxi"),
        ]
        return {path.relative_to(core): path.read_bytes() for path in paths}

    expected_bytes = generated_bytes()
    # A deliberately bare environment: tmp_path is not inside any git worktree.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for mode in (["--check"], []):
        result = subprocess.run(
            [sys.executable, str(core / "components_layout.py"), *mode],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
        assert result.returncode == 0, (mode, result.stdout + result.stderr)
        assert f"{stem}:" in result.stdout
    assert components.assemble(core, stem) == components.assemble(CORE, stem)
    assert generated_bytes() == expected_bytes


def test_native_build_stages_components_from_the_real_source_root():
    """Every COMPONENT_SOURCES key must resolve to an existing manifest on disk.

    setuptools instantiates `build_ext` for metadata-only steps too
    (egg_info -> sdist -> build_ext), where the process CWD is not the source
    root. Anchoring the staging directory to the wrong base produced
    `pyboy/pyboy/core`, and the missing manifest there aborted metadata
    generation with "No such file or directory" before any build ran.
    """
    setup_path = PACKAGE.parent / "setup.py"
    tables = setup_path_literals(setup_path, {"ROOT_DIR", "ROOT_ABS", "COMPONENT_SOURCES"})

    root_dir = tables["ROOT_DIR"]
    namespace = {
        "os": os,
        "ROOT_DIR": root_dir,
        "ROOT_ABS": tables["ROOT_ABS"],
        # Bound so the statement under test evaluates standalone rather than
        # capturing this loop's variable.
        "relative": "core/mb.py",
    }
    # Evaluate setup.py's real staging statement so this test tracks the shipped
    # code rather than a reconstruction of it.
    statements = re.findall(r"^\s*directory = (.*)$", setup_path.read_text(), re.MULTILINE)
    assert len(statements) == 1, statements
    for relative, (stem, declarations) in tables["COMPONENT_SOURCES"].items():
        namespace["relative"] = relative
        directory = eval(statements[0], namespace)
        # ROOT_ABS is the vendored package root and ROOT_DIR is "pyboy" inside
        # it, so "<vendor-root>/pyboy/core" is the correct staging directory and
        # legitimately contains "pyboy" once. The defect this guards against is
        # resolving a relative ROOT_DIR against a CWD that is already the
        # package, which yields a manifest that does not exist. Assert the
        # manifest is really there rather than matching a legitimate prefix.
        # The doubled form is caught by the is_file() assert below, and also
        # named here so the failure message points at the real cause.
        assert f"{root_dir}/{root_dir}" not in directory.replace(os.sep, "/"), directory
        manifest = Path(directory) / f"{stem}_components_manifest.py"
        assert manifest.is_file(), f"{stem}: staging would read missing {manifest}"
        assert Path(directory, declarations).is_file(), stem


@pytest.mark.parametrize(
    "body,expected",
    [
        ("    pass\n", set()),
        ("    cdef int hidden(self)\n", {"hidden"}),
        ("    cpdef int visible(self)\n", set()),
        (
            """    cdef int hidden(self)
    cpdef int visible(self) except * with gil
    cdef inline (int, int) position(self) noexcept nogil
    cdef public int value
    cdef readonly int count
""",
            {"hidden", "position"},
        ),
    ],
    ids=["empty", "single-cdef", "single-cpdef", "mixed"],
)
def test_pxd_hidden_members_distinguishes_python_wrappers(tmp_path, body, expected):
    declaration = tmp_path / "surface_probe.pxd"
    declaration.write_text("cdef class Probe:\n" + body, encoding="utf-8")
    assert pxd_declared_members(declaration) == {"Probe": expected}


@pytest.mark.parametrize(
    "missing", [None, "visible", "_visible", "plain", "__init__", "class", "not-class"]
)
def test_native_class_surface_rejects_missing_python_exports(tmp_path, missing):
    """Authored controls exercise the real assertion used by the native lane.

    These are negative controls, not native-runtime or real-ROM acceptance.
    The actual imported mb/lcd classes are checked by the namespace test.
    """
    declaration = tmp_path / "surface_probe.pxd"
    declaration.write_text(
        "cdef class Probe:\n"
        "    cdef int hidden(self)\n"
        "    cpdef int visible(self)\n"
        "    cpdef int _visible(self)\n",
        encoding="utf-8",
    )
    hidden = pxd_declared_members(declaration)["Probe"]
    source_attrs = {
        name: (lambda self: 1) for name in ("hidden", "visible", "_visible", "plain", "__init__")
    }
    source_class = type("Probe", (), source_attrs)
    native_attrs = {name: value for name, value in source_attrs.items() if name != "hidden"}
    # Native-only public descriptors are legitimate extras, not a failure.
    native_attrs["public_field"] = 0
    native_attrs.pop(missing, None)
    module = ModuleType("surface_probe")
    if missing != "class":
        module.Probe = object() if missing == "not-class" else type("Probe", (), native_attrs)
    if missing is None:
        assert_exported_class_surface(
            module, "Probe", source_class, from_source=False, declared=hidden
        )
    else:
        expected = (
            "missing public class"
            if missing == "class"
            else ("export is not a class" if missing == "not-class" else missing)
        )
        with pytest.raises(AssertionError, match=expected):
            assert_exported_class_surface(
                module, "Probe", source_class, from_source=False, declared=hidden
            )
