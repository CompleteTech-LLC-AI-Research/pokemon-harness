# Hostile-finder matrix against the #555 merge tree

The mutation matrix proves the suite detects specific weakenings. This is the
complementary check: does the guard actually *refuse* each hostile finder shape
an attacker controls? Probe at
`/workspace/poke-harness/.scratch/hostile_probe.py`.

Run against the merge tree (master + `89c7152`) in its own worktree and venv:

```
attacker shape                             verdict                      status
plain foreign finder                       refused                      FAIL
borrowed real module __file__              refused                      FAIL
non-existent site-packages claim           refused                      FAIL
metaclass raises from __module__           refused                      FAIL
module __file__ raises                     refused                      FAIL
find_spec is a builtin                     refused                      FAIL
hostile __fspath__                         refused                      FAIL
no source file (empty co_filename)         refused                      FAIL
find_spec raises on access                 refused                      FAIL

total=9 bypass_or_undetected=0 raised=0
VERDICT: CLEAN
```

Each shape must be refused, must be named in the finding, and must not raise.
All nine satisfy all three.

## Why these nine

The three trust inputs an attacker can reach are `__module__`,
`sys.modules[name].__file__`, and the finder's own code object. Each row attacks
one of them, plus the error paths that could turn a finding into a traceback:

| shape | attacks |
| --- | --- |
| plain foreign finder | no claim at all |
| borrowed real module `__file__` | claims a real installed module's file |
| non-existent site-packages claim | claims a path that was never written |
| metaclass raises from `__module__` | hostile descriptor on the trust input |
| module `__file__` raises | hostile descriptor on the trust input |
| `find_spec` is a builtin | no Python code object to attest |
| hostile `__fspath__` | `except` discipline in `_is_within` |
| no source file (empty `co_filename`) | the no-provenance branch |
| `find_spec` raises on access | fail-closed on introspection |

The last four are the fail-closed rows: a refusal that raises instead of
reporting is a denial of the check, not a safe outcome.

## Contrast with master

The same probe against unmodified `master` fails before it can run:

```
AttributeError: module 'scripts.check_import_origins' has no attribute
'_site_packages_roots'
```

Master's guard has no meta-path hardening at all, so none of these attacker
shapes are modelled by it. That is the regression #555 repairs.

## Scope

This is the lead's own verification. It does **not** discharge the independent
review requirement — an author may not approve their own change, and delegation
is unavailable this session (#489). It is offered as evidence for whoever
reviews #555, and the probe is reusable against any changed head.
