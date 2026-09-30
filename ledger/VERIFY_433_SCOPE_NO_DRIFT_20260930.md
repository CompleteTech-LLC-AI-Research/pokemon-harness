# Verification — PR #433 head `39f1b19` does not change #423 / #425 / #417

Date: 2026-09-30
Method: each shape's own source, run through `_is_enforced` on both the parent
commit `63ab8dd` and the head `39f1b19`.

| issue / shape | parent `63ab8dd` | head `39f1b19` | filed value | held |
|---|---|---|---|---|
| #423 starred store in loop body, `with` a later sibling | `[True, True]` | `[True, True]` | `True` (false-live) | yes |
| #425 `for cs in (1, None):` then `with cs:` | `[True]` | `[True]` | `True` (false-live) | yes |
| #425 control `for cs in (1, nullcontext()):` | `[True]` | `[True]` | `True` (correct) | yes |
| #417 after-loop multi-element, user-defined `Sup()` LAST | `[True]` | `[True]` | `True` (false-live) | yes |
| #417 plain local `cs = Sup()` | `[True]` | `[True]` | `True` | yes |

**No drift.** Every value is identical on the parent and on the head, so #433
changed none of them. The last row is worth noting against the issue text: #417
records `False` for the plain local store on #412's head, and this stack
reports `True` — but that difference comes from the stack #433 is built on, not
from #433, and #417 is filed as a #412 residual. It is not evidence against
this PR, and it was not silently changed by it.

All three issues therefore remain open, still unfixed by this tree, exactly as
the brief's scope check requires.

Probe: `/tmp/scope_check.py`. Trees: `/tmp/wt429parent`, `/home/agent/poke-harness/.scratch/fix429`.
