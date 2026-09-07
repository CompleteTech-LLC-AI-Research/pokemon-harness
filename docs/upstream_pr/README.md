# Draft upstream PyBoy integration note

> **Proposal only.** This directory is a design and contribution aid. It is
> not a merged PyBoy pull request, a current upstream branch, or evidence that
> this harness is production-ready. Validate every step against the target
> PyBoy commit before using it.

The proposal describes how the candidate link modules in
`src/pokered_harness/link/` could be adapted for
[Baekalfen/PyBoy](https://github.com/Baekalfen/PyBoy). It is intentionally
separate from the harness release gate: the harness currently bundles a
pinned PyBoy `2.7.0` source runtime, while an upstream contribution still
needs a runtime whose motherboard and serial objects are accessible from
Python without breaking PyBoy's Cython and save-state contracts.

## Current boundary

- The current working tree contains `vendor/pyboy-src/` pinned by
  `POKERED_HARNESS_PYBOY_REVISION`; it is a harness release input, not an
  upstream branch or merged PyBoy contribution.
- No upstream PR number, branch tip, test output, or merge status is asserted
  by these files.
- The current harness real-ROM/link tests are fixture/runtime gated, but no
  current candidate strict trade or battle result is qualified for an upstream
  claim. Historical or representative Red/Blue color-variant subprocess rows
  remain scoped evidence only. See the
  [production runbook](../PRODUCTION_RUNBOOK.md) and
  [release checklist](../RELEASE_CHECKLIST.md).
- The `.mcp.json` path and link/runtime contract are now covered by the
  harness packaging work, but they must still be reviewed independently
  before a PyBoy contribution can be treated as an upstream dependency.

## Proposed file mapping

| Harness source | Candidate PyBoy path | Review required |
|---|---|---|
| `src/pokered_harness/link/serial_core.py` | `pyboy/core/serial.py` | Preserve PyBoy's device and save-state contracts. |
| `src/pokered_harness/link/serial_coordinator.py` | `pyboy/link/coordinator.py` | Define ownership, scheduling, and teardown semantics. |
| `src/pokered_harness/link/pyboy_link_session.py` | `pyboy/link/session.py` | Adapt the public API and naming to PyBoy conventions. |
| `src/pokered_harness/link/network_backend.py` | `pyboy/link/network.py` | Review framing, timeouts, security, and thread lifecycle. |
| `src/pokered_harness/link/__init__.py` | `pyboy/link/__init__.py` | Export only the API accepted by upstream maintainers. |

The mapping is not a drop-in guarantee. Imports, Cython boundaries, state
serialization, and motherboard construction must be reviewed in the target
PyBoy tree. The bundled fork is evidence for the harness runtime only, not
evidence of upstream acceptance.

## Proposed motherboard integration

The intended integration point is the motherboard's serial-device
construction, conceptually:

```diff
- self.serial = serial.Serial(...)
+ self.serial = serial.SerialCore(...)
```

This is illustrative, not an instruction to patch a current checkout. Verify
the constructor signature, Cython declarations, register masks, interrupt
behavior, and save-state format against the target revision. Treat any state
format change as a migration requiring compatibility tests.

## Candidate test responsibilities

These harness tests describe useful responsibilities for an upstream review;
they are not a reported upstream result:

| Test source | Responsibility |
|---|---|
| `tests/test_serial_core.py` | Register-level master/slave, disconnect, IRQ, and bit-shift semantics. |
| `tests/test_serial_coordinator.py` | Coordinated edge progression and peer completion. |
| `tests/test_pyboy_link_session.py` | Attach/detach/session behavior with a fake PyBoy surface. |
| `tests/test_link_protocol.py` | Pokémon serial constants and synthetic protocol exchange. |
| `tests/test_network_backend.py` | Network framing, rendezvous, timeout, and close behavior. |

Real-ROM tests such as `tests/test_pyboy_link_session_roms.py` require BYO
assets, ROM-specific save states, and a compatible runtime. They should remain
an explicit integration tier rather than being silently presented as upstream
unit-test evidence.

## Required validation before an upstream claim

An upstream contribution should record, at minimum:

1. the exact PyBoy base commit and the complete patch;
2. the Python/Cython build mode and supported platforms;
3. the full PyBoy regression command and output;
4. register, coordinator, session, protocol, and network test output;
5. save-state compatibility results for old and new states; and
6. real-ROM evidence separately, including exact ROM/symbol/fixture hashes.

Do not call the contribution “green,” “merged,” or “production-ready” from
this repository's source files without those records.

## Contributor prototype notes

The following is an illustrative setup shape for a disposable contributor
environment. It is not a supported release installation and the build system
must be inspected before relying on `PYBOY_NO_CYTHON`:

```sh
python3 -m venv .venv-noncython
. .venv-noncython/bin/activate
python -m pip install --upgrade pip
git clone --depth 1 https://github.com/Baekalfen/PyBoy.git vendor/pyboy-src
# Inspect the checked-out PyBoy build configuration and choose its supported
# non-Cython/source-build procedure; do not assume this variable is honored.
PYBOY_NO_CYTHON=1 python -m pip install --no-build-isolation -e vendor/pyboy-src
python -m pip install -e ".[dev,mcp]"
```

On Windows, use `.venv-noncython\Scripts\Activate.ps1` and the corresponding
`python -m pip` commands. Keep this prototype environment separate from the
release environment, and do not commit `vendor/` or the virtual environment.

## References

- [Serial overhaul design](../pyboy_serial_overhaul_design.md)
- [Draft application recipe](APPLY.md)
- [Historical readiness template](BRANCH_READY.md)
