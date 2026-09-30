"""Deferred, read-only door to the ``pokered_harness.mcp_server`` facade (issue #240).

The #125 split moved the facade's body into support modules.  Those modules
call back into ``pokered_harness.mcp_server`` for the entry points that live
there, and they must resolve those calls *at call time* so that tests which
monkeypatch the facade's attributes stay visible.  The original device was a
module-level back-edge::

    import pokered_harness.mcp_server as _entry

which imports the facade *eagerly*, while it is still executing, whenever a
support module is the first thing imported.  The facade reaches the support
modules through its own module-level ``from ... import <name>`` lines, so the
partially initialized module has not yet bound the name being asked for and the
import fails::

    ImportError: cannot import name '_dispatch_session_tool' from partially
    initialized module 'pokered_harness.mcp_server_tools' (most likely due to a
    circular import)

This module keeps the same call-time visibility without the eager import.  The
proxy resolves the facade attribute-by-attribute on first use, so a support
module can be imported on its own; attribute patches on the loaded facade module
still win because every lookup goes through the module object.

This mirrors ``pokered_harness.link``, whose ``link/__init__.py`` eagerly
imports ``network_backend`` (so its back-edges always find a finished module).
The ``mcp_server`` split has no equivalent eager path, so the deferral is made
explicit here rather than relying on a package ``__init__`` side effect.
"""

from __future__ import annotations

import sys
from typing import Any

_FACADE_NAME = "pokered_harness.mcp_server"


class _FacadeProxy:
    """Read-only stand-in that resolves facade attributes at call time.

    Only attribute *reads* are supported: every current use of the back-edge in
    the split modules is a read (``_entry.dispatch_tool(...)``,
    ``_entry.PyBoyLinkSession``, ``_entry._DEFAULT_CLEANUP_TIMEOUT_S`` …), and
    the facade's own globals are the single source of truth for those names.
    Failing loudly on writes is deliberate — a silent write here would create a
    second copy of state that the facade never sees.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        module = sys.modules.get(_FACADE_NAME)
        if module is None:
            module = __import__(_FACADE_NAME, fromlist=["*"])
        return getattr(module, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"{type(self).__name__} is read-only; set {_FACADE_NAME}.{name} instead"
        )

    def __repr__(self) -> str:
        return f"<deferred entry proxy for {_FACADE_NAME}>"


entry = _FacadeProxy()

__all__ = ["entry"]
