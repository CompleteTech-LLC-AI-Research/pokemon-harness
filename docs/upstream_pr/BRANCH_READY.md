# Upstream PR branch is ready — commit `f0856a3`

The `vendor/pyboy-src/` PyBoy clone now has a `link-cable-bit-accurate-serial`
branch built per the [APPLY.md](APPLY.md) recipe and verified green against
the PyBoy regression suite.

## Branch contents

```
link-cable-bit-accurate-serial @ f0856a3
├── f9f812b setup: honor PYBOY_NO_CYTHON env var for contributor non-Cython builds
├── ebb0aac serial: bit-accurate FF01/FF02 shift register + link-cable support
├── f0856a3 link: NetworkBackend reader-thread + LinkSession network mode
└── f0856a3 link: NetworkBackend OP_SYNC opcode for cross-peer rendezvous
```

**Changed files** (vs. upstream `master`):
- `pyboy/core/serial.py` — replaced with bit-accurate `SerialCore`; kept
  `Serial = SerialCore` alias + accepts `cgb_mode` positional arg so
  `pyboy/core/mb.py`'s `self.serial = serial.Serial(cgb_mode)` works
  unchanged. Masks in unused SC bits 2-6 (DMG) / 2-6 (CGB) so mooneye
  `misc/bits/unused_hwio` and `acceptance/bits/unused_hwio` pass.
- `pyboy/link/__init__.py`, `coordinator.py`, `session.py`, `network.py`
  — new subpackage: `LockstepCoordinator`, `CoordinatedBackend`,
  `LinkSession` (renamed from `PyBoyLinkSession`), `NetworkBackend`.
- `setup.py` — one-line tweak so `PYBOY_NO_CYTHON=1 pip install -e .`
  gives a non-Cython build (useful for contributors prototyping on
  the link-cable stack where `mb.serial` needs to be swappable).
- `tests/link/` — 85 new tests (register, coordinator, session,
  protocol, network). All green.

## Verification

**New tests (85):** all green in ~5s each.

**Full PyBoy regression suite** (non-heavy subset — 297 tests including
mooneye, gameshark, breakpoints, interaction, serial_link, external_api,
windows, and more):
```
297 passed, 118 skipped, 121 deselected, 1 xfailed, 1 xpassed in 627.35s
```

**Mooneye unused_hwio** (the only thing that regressed initially and
was fixed by adding the `_sc_readonly_mask`):
```
tests/test_mooneye.py::test_mooneye[True-False-True-misc/bits/unused_hwio-C.gb] PASSED
tests/test_mooneye.py::test_mooneye[True-False-False-acceptance/bits/unused_hwio-GS.gb] PASSED
```

## How to push + open the PR

From this worktree (owner of the `vendor/pyboy-src/` clone):

```sh
cd vendor/pyboy-src

# Add a fork of Baekalfen/PyBoy to this clone. Replace with actual fork URL.
git remote add fork git@github.com:<your-user>/PyBoy.git

# Push the branch.
git push -u fork link-cable-bit-accurate-serial

# Open the PR against Baekalfen/PyBoy:master.
gh pr create \
  --repo Baekalfen/PyBoy \
  --base master \
  --head <your-user>:link-cable-bit-accurate-serial \
  --title "Bit-accurate serial + link cable session (Pokemon R/B/Y trading)" \
  --body-file ../../docs/upstream_pr/README.md
```

Or manually via the GitHub web UI — upload the branch, use
[docs/upstream_pr/README.md](README.md) as the PR description.
