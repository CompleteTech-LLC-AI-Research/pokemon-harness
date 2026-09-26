import importlib.util
import multiprocessing
import os
import platform
import sys
from multiprocessing import cpu_count
from pathlib import Path
from runpy import run_path

import numpy as np

from setuptools import Extension, setup

CYTHON = platform.python_implementation() == "CPython" and not os.getenv("PYBOY_NO_CYTHON")
ROOT_DIR = "pyboy"
DEBUG = bool(os.getenv("GITHUB_ACTIONS"))

if not CYTHON:
    setup()
    exit(0)

from Cython.Build import cythonize  # noqa
from Cython.Compiler import DebugFlags, Errors  # noqa
from Cython.Distutils import build_ext as _build_ext  # noqa

# Loading this helper must not import PyBoy while its native modules are absent.
_opcode_spec = importlib.util.spec_from_file_location(
    "_pyboy_opcode_build", Path(__file__).parent / "pyboy/core/_opcodes_runtime.py"
)
_opcode_build = importlib.util.module_from_spec(_opcode_spec)
_opcode_spec.loader.exec_module(_opcode_build)


def patched_error(position, message):
    if message == "Python object cannot be passed as a varargs parameter":
        # We ignore this error to pass PyObject* to logging
        return
    else:
        # print("Errors.error:", repr(position), repr(message)) ###
        if position is None:
            raise Errors.InternalError(message)
        err = Errors.CompileError(position, message)
        if DebugFlags.debug_exception_on_error:
            raise Exception(err)  # debug
        Errors.report_error(err)
        return err


Errors.error = patched_error


class build_ext(_build_ext):
    def initialize_options(self):
        super().initialize_options()
        # This is only for Cythonize. See finalize_options.
        if sys.platform == "win32":
            thread_count = 0  # Disables multiprocessing
        else:
            thread_count = cpu_count()

        # Fixing issue with nthreads in Cython
        if ((3, 8) <= sys.version_info[:2] and sys.platform == "darwin") or (
            (3, 14) <= sys.version_info[:2] and sys.platform == "linux"
        ):
            multiprocessing.set_start_method("fork", force=True)

        cflags = ["-O3"]
        if not DEBUG:
            cflags.append("-DCYTHON_WITHOUT_ASSERTIONS")
        # NOTE: For performance. Check if other platforms need an equivalent change.
        if sys.platform == "darwin":
            cflags.append("-DCYTHON_INLINE=inline __attribute__ ((__unused__)) __attribute__((always_inline))")

        # Keep the public extension and its augmenting .pxd in one translation
        # unit. Never Cythonize the runtime-loading facade, and never overwrite
        # tracked source with the transient assembled compiler input.
        source_support = run_path(os.path.join(ROOT_DIR, "_source.py"))
        main_source = source_support["stage_native_source"](
            ROOT_DIR, os.path.join("build", "pyboy-components")
        )
        main_path = os.path.join(ROOT_DIR, "pyboy.py")
        main_dependencies = [os.path.join(ROOT_DIR, name) for name in source_support["COMPONENTS"]]
        main_dependencies.append(os.path.join(ROOT_DIR, "pyboy.pxd"))
        py_pxd_files = prep_pxd_py_files()
        def compiler_source(src):
            if src == main_path:
                return str(main_source)
            return _opcode_build.prepare_opcode_source(src)

        cythonize_files = map(
            lambda src: Extension(
                src.split(".")[0].replace(os.sep, "."),
                [compiler_source(src)],
                depends=main_dependencies if src == main_path else [],
                extra_compile_args=cflags,
                extra_link_args=[] if DEBUG else ["-s", "-w"],
                include_dirs=[np.get_include()],
            ),
            list(py_pxd_files),
        )
        self.distribution.ext_modules = cythonize(
            [*cythonize_files],
            nthreads=thread_count,
            include_path=[os.getcwd()],
            annotate=False,
            gdb_debug=False,
            language_level=3,
            compiler_directives={
                "boundscheck": False,
                "cdivision": True,
                "cdivision_warnings": False,
                "infer_types": True,
                "initializedcheck": False,
                "nonecheck": False,
                "overflowcheck": False,
                # "profile" : True,
                "wraparound": False,
                "legacy_implicit_noexcept": True,
            },
        )

    def finalize_options(self):
        # Use '-j' on the commandline to specify parallelism. Otherwise auto-detect.
        if getattr(self, "parallel", None) is None:
            self.parallel = cpu_count()
        super().finalize_options()


def prep_pxd_py_files():
    ignore_py_files = [
        "__main__.py", "manager_gen.py", "opcodes_gen.py",
        "opcodes_gen_handlers.py", "conftest.py", "_source.py",
        "opcodes_layout.py", "_opcodes_runtime.py", "_opcodes_manifest.py",
    ]
    # Cython doesn't trigger a recompile on .py files, where only the .pxd file has changed. So we fix this here.
    # We also yield the py_files that have a .pxd file, as we feed these into the cythonize call.
    for root, dirs, files in os.walk(ROOT_DIR):
        for f in files:
            if os.path.splitext(f)[1] in [".py", ".pyx"] and f not in ignore_py_files:
                yield os.path.join(root, f)
            if os.path.splitext(f)[1] == ".pxd":
                py_file = os.path.join(root, os.path.splitext(f)[0]) + ".py"
                if os.path.isfile(py_file):
                    if os.path.getmtime(os.path.join(root, f)) > os.path.getmtime(py_file):
                        os.utime(py_file)


setup(
    cmdclass={
        "build_ext": build_ext,
    },
    ext_modules=[Extension("", [""])],  # Added to trigger a binary wheel
)
