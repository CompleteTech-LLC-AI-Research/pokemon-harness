"""Checked restoration of the process-wide subreaper setting.

Failure leaves an explicit restoration obligation on the original owner. Calling
the helper, or closing an unrelated descriptor, does not discharge that obligation.
"""

from __future__ import annotations

import ctypes
from typing import Any

PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def read_subreaper(libc: Any) -> int:
    flag = ctypes.c_int(0)
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
    if flag.value not in (0, 1):
        raise RuntimeError(f"invalid subreaper readback: {flag.value}")
    return flag.value


def restore_adoption(owner: Any, original: BaseException | None = None) -> None:
    if not owner.restoration_pending:
        return
    try:
        if owner._libc.prctl(PR_SET_CHILD_SUBREAPER, owner._previous, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER restoration")
        restored = read_subreaper(owner._libc)
        if restored != owner._previous:
            raise RuntimeError(
                f"subreaper restoration readback {restored}, expected {owner._previous}"
            )
    except (OSError, AttributeError, RuntimeError) as error:
        owner.restoration_problem = f"subreaper restoration unproven: {error}"
        owner.reason = owner.restoration_problem
        # Keep active/pending bookkeeping conservative when the readback is unknown.
        # The same owner can retry; no numeric process ownership is reconstructed.
        if original is not None:
            original.add_note(owner.restoration_problem)
            return
        raise RuntimeError(owner.restoration_problem) from error
    owner.restoration_pending = False
    owner.restoration_problem = ""
    owner.active = False
