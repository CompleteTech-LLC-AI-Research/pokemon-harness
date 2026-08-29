# Draft upstream integration recipe

> **Contributor draft only.** This recipe has not been run against a current
> PyBoy checkout by this documentation lane. Review every path, API, Cython
> boundary, and save-state decision against the exact PyBoy base commit.

The goal is to prepare a reviewable upstream experiment from the candidate
link modules in this repository. It does not create or certify a release of
this harness or PyBoy.

## Preconditions

- Have separate checkouts of PyBoy and this repository.
- Record the exact PyBoy base commit before making changes.
- Keep ROMs, save states, virtual environments, and vendor trees outside the
  upstream patch unless PyBoy's contribution policy requires otherwise.
- Read the [design proposal](../pyboy_serial_overhaul_design.md) and the
  [upstream integration note](README.md) first.

## Proposed bring-over

Replace `<harness-root>` and `<pyboy-root>` with local paths. Work in a new
branch in the PyBoy checkout and review each copied file before committing:

```sh
cd <pyboy-root>
git switch -c link-cable-bit-accurate-serial

# Candidate serial implementation. Confirm the target PyBoy constructor and
# register/save-state contracts before replacing or aliasing this file.
cp <harness-root>/src/pokered_harness/link/serial_core.py pyboy/core/serial.py

mkdir -p pyboy/link
cp <harness-root>/src/pokered_harness/link/serial_coordinator.py pyboy/link/coordinator.py
cp <harness-root>/src/pokered_harness/link/pyboy_link_session.py pyboy/link/session.py
cp <harness-root>/src/pokered_harness/link/network_backend.py pyboy/link/network.py
cp <harness-root>/src/pokered_harness/link/__init__.py pyboy/link/__init__.py
```

Adapt imports and names deliberately. Do not apply a blind repository-wide
replacement. In particular, review:

- `pokered_harness` imports and PyBoy public naming;
- motherboard construction and the `cdef`/Python boundary;
- serial register masks, interrupt delivery, and clock scheduling;
- network framing, timeout, disconnect, and thread ownership; and
- old/new save-state compatibility.

The conceptual motherboard change is:

```diff
- self.serial = serial.Serial(...)
+ self.serial = serial.SerialCore(...)
```

It is not known to be the exact change for every PyBoy revision. The target
maintainer must choose whether to stage the new core behind an explicit API or
replace the existing implementation.

## Candidate test copy

If upstream maintainers want the ROM-free responsibilities in PyBoy, copy and
adapt these tests into the target test layout:

```sh
mkdir -p tests/link
cp <harness-root>/tests/test_serial_core.py tests/link/test_serial_core.py
cp <harness-root>/tests/test_serial_coordinator.py tests/link/test_coordinator.py
cp <harness-root>/tests/test_pyboy_link_session.py tests/link/test_session.py
cp <harness-root>/tests/test_link_protocol.py tests/link/test_protocol.py
cp <harness-root>/tests/test_network_backend.py tests/link/test_network.py
```

Real-ROM tests remain a separate BYO-asset integration tier. Do not turn
fixture-gated tests into an unconditional upstream pass by adding skips or by
copying ROM-derived artifacts into the branch.

## Review and verification before commit

Run the target project's formatter, type checks, and focused tests as defined
by its own contributor instructions. At minimum, attach:

- exact PyBoy base and head commits;
- the changed-file diff and `git diff --check` output;
- focused serial/coordinator/session/protocol/network results;
- the complete existing PyBoy regression result;
- save-state compatibility results; and
- build results for every claimed Cython/source configuration.

Use explicit paths when staging the patch; do not stage unrelated files:

```sh
git add pyboy/core/serial.py pyboy/link tests/link
git diff --cached --check
git status --short
git commit -m "Draft bit-accurate serial and link session"
```

The commit command above is an example for the target PyBoy checkout. It does
not authorize pushing, opening, or merging a pull request. Those are separate
release decisions.

## What cannot be claimed from this recipe

This recipe does not establish that:

- the branch exists or is ready;
- PyBoy CI is green;
- the stock Cython wheel accepts Python-side serial replacement;
- a Pokémon trade or battle completes; or
- any Red/Blue/Yellow matrix has passed.

Those claims require fresh evidence recorded using the harness
[production runbook](../PRODUCTION_RUNBOOK.md) and
[release checklist](../RELEASE_CHECKLIST.md).
