"""A module whose attributes are *importable values* of unknown kind (#376).

``from M import N as cs`` binds whatever attribute ``N`` is on ``M``. The
analyzer cannot read that off the syntax, which is why
:func:`tests._timed_menu_milestone_sentinel_support._carrier_runtime_kinds`
declines the ``ImportFrom`` half rather than recording it as a module.

This module exists so that refusal is *tested* rather than asserted. It
exports, deliberately, both an enterable value and an unenterable one, because
that is the whole point: the same spelling decides both ways depending on the
module, so any rule that answers from the syntax alone is wrong for one of
them.

Kept free of imports beyond the standard library so the test that execs it
does not depend on the harness's own package graph.
"""

import contextlib
import os

#: An enterable value. ``from tests._import_from_carrier_support import ctx``
#: binds a real context manager, so ``with cs:`` succeeds and an assert under
#: it is LIVE. This is the control that makes declining the shape necessary
#: rather than merely cautious.
ctx = contextlib.nullcontext()

#: An unenterable value of a *different* type from ``ctx``, so the two rows are
#: distinguishable by kind as well as by enterability.
sep = os.sep

__all__ = ["ctx", "sep"]
