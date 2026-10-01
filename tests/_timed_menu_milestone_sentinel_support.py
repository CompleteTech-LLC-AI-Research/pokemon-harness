"""Dead-code sentinel support for the timed-menu milestone tests.

Split into a base (imports + constants) and numbered fragments by #122. This module
remains the single public entry point, and every name it exported before the
split is still exported here, so existing

    from tests._timed_menu_milestone_sentinel_support import (...)
    from tests import _timed_menu_milestone_sentinel_support as support

call sites keep working unchanged.

The parts deliberately share one flat global namespace. The monolith resolved
helper names at call time against its own module globals, so a name defined in
one part and called from another has to resolve without an explicit import,
and callers rebind names on the support module itself -- ``support._module_tree
= lambda: mutant`` is how the bypass-detection entry point is pinned.

So the parts are not merely *given* copies of each other's names: every part
executes against this module's dict as its own globals, which is what the
monolith did with its single module. Sharing the dict is what makes a
rebinding reach the analyzer that calls the name. A one-time cross-injection
snapshot is not enough -- each part would keep reading its own private copy,
and ``support._module_tree`` would be silently ignored by the part that owns
:func:`count_sites_that_bypass_the_guard`.

Sharing the dict also keeps every name a bare global. That matters beyond
convenience: the sentinels assert that one helper *calls* another by parsing
the caller's source and matching ``ast.Name`` (``_sentinel_uses``), so a
helper reached through a module attribute (``_p5._is_enforced(...)``) would
read as "not used" and turn the sentinel suite red.

Why not a module-level ``__setattr__`` that fans a write out to the parts:
Python gives modules ``__getattr__`` (PEP 562) but no ``__setattr__`` hook,
so ``support._module_tree = ...`` would go straight into the module dict and
never reach the parts. A module-level ``__getattr__`` does not help either --
it is consulted only for attribute access on the module object, never for the
``LOAD_GLOBAL`` an analyzer function performs when it calls a helper. One
shared dict sidesteps both limits and reproduces the monolith exactly.
"""

import ast  # noqa: F401  (re-exported: callers use `support.ast`)

# `ast` is re-exported because callers reach `support.ast`. `sys` and `Path` are
# the split's own plumbing and are imported under private aliases: the monolith
# never exported them, and this module's shared namespace *is* its public
# surface, so a plain `import sys` here would widen the exported names.
import sys as _split_sys
from pathlib import Path as _split_path

_FRAGMENT_FILES = (
    "_sentinel_support_base",
    "_sentinel_support_part1",
    "_sentinel_support_part2",
    "_sentinel_support_part3",
    "_sentinel_support_part4",
    "_sentinel_support_part5",
    "_sentinel_support_part6",
    "_sentinel_support_part7",
    "_sentinel_support_part8",
    "_sentinel_support_part9",
    "_sentinel_support_part10",
    "_sentinel_support_part11",
    "_sentinel_support_part13",
    "_sentinel_support_part14",
    "_sentinel_support_part15",
    "_sentinel_support_part16",
    "_sentinel_support_part17",
    "_sentinel_support_part18",
    "_sentinel_support_part19",
    "_sentinel_support_part20",
    "_sentinel_support_part21",
    "_sentinel_support_part22",
    "_sentinel_support_part23",
    "_sentinel_support_part24",
    "_sentinel_support_part25",
    "_sentinel_support_part26",
    "_sentinel_support_part27",
    "_sentinel_support_part28",
    "_sentinel_support_part29",
    "_sentinel_support_part31",
)

# Each fragment is executed against *this* module's dict, so `globals()` inside any
# part is this dict and a rebinding anywhere is seen everywhere. The fragments are
# deliberately not imported as modules: an `import tests._sentinel_support_part5`
# would publish a second namespace under that name holding a private copy of
# every name, which is precisely the bug this sharing exists to prevent. The
# trade-off is that the part files are not importable on their own -- they are
# fragments of this module, split only to keep each file under the size budget,
# and `tests._timed_menu_milestone_sentinel_support` stays the one importable
# entry point.
#
# That trade-off is order-sensitive, and silently so. If a fragment is imported
# *before* this module, the import system executes it normally and it binds only
# the names it defines itself: a direct `_sentinel_support_part5.count_sites_
# that_bypass_the_guard()` then dies with `NameError: _module_tree`, and a name
# imported from it is a *different object* from the one here. Nothing today
# imports a fragment directly, so the suite is unaffected -- but a latent,
# order-dependent trap in a module whose whole job is catching latent traps is
# not acceptable. Each fragment therefore refuses to be imported on its own, and
# says where to import from instead. See the early guard in each fragment.
_SUPPORT_GLOBALS = globals()

for _part_file in _FRAGMENT_FILES:
    _module_name = f"tests.{_part_file}"
    # Read the fragment and exec it against this dict. Each fragment starts with a
    # guard that raises if it is executed under its OWN dotted name, which is
    # exactly the case where the import system ran it and this entry point was
    # bypassed; under this entry point the exec reuses the support `__name__`,
    # so the guard stays inert.
    _fragment_path = _split_path(__file__).with_name(f"{_part_file}.py")
    _fragment_source = _fragment_path.read_text(encoding="utf-8")
    exec(  # noqa: S102
        compile(
            _fragment_source,
            str(_fragment_path),
            "exec",
        ),
        _SUPPORT_GLOBALS,
        _SUPPORT_GLOBALS,
    )
    _split_sys.modules[_module_name] = _split_sys.modules[__name__]

del _part_file, _module_name, _fragment_path, _fragment_source
del _FRAGMENT_FILES, _SUPPORT_GLOBALS
