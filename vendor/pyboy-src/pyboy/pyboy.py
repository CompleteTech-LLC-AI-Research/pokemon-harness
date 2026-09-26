#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
"""PyBoy's public module; its source components share this exact namespace.

The native build assembles the same components before Cython augmentation.
See docs/PYBOY_MAIN_MODULE_SPLIT.md in the harness repository.
"""

from ._source import load_module as _load_module

_load_module(globals())
del _load_module
