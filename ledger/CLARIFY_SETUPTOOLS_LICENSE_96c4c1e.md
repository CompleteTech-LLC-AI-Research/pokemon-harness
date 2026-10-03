# Correction: `pyproject.toml` is NOT broken; a `--no-build-isolation` flag caused it

Date: 2026-10-03
Tree: `origin/master` `96c4c1ea3fcfd68ea83c5afcfc4a185b7472832c`
Worktree: a declared detached checkout. No repository file modified by this pass.

## The claim being retracted

A previous lead message stated:

> `pip install -e .` is currently broken on master by a `pyproject.toml`
> license-format error (`2 matches found`), which I will repair next.

**That claim is wrong.** `pyproject.toml` on master is correct and packaging
works. No repair is needed and none was made.

## What actually happened

The failing command was:

```
pip install -e . --no-deps --no-build-isolation
```

`--no-build-isolation` tells pip to use the *ambient* setuptools instead of the
one declared in `[build-system]`. Both local venvs carry **setuptools 66.1.1**,
while `pyproject.toml` declares:

```toml
[build-system]
requires = ["setuptools>=77", "wheel"]
```

PEP 639 (`license` as an SPDX string, plus `license-files`) needs setuptools
>= 77. Under 66.1.1 the same valid file is misread and raises
``configuration error: `project.license` must be valid exactly by one definition
(2 matches found)``. The venv, not the repository, was out of date.

## Evidence

| invocation | setuptools used | result |
|---|---|---|
| `pip install -e . --no-deps --dry-run` (isolated) | declared `>=77` | **succeeds** — "Would install pokered-harness-0.1.0" |
| `pip install -e . --no-deps --no-build-isolation` | ambient 66.1.1 | fails with the "2 matches" error |
| `read_configuration()` under setuptools 84.0.0 | 84.0.0 | **OK**, license = `LGPL-3.0-only` |

Declared metadata is also consistent on master: `license = "LGPL-3.0-only"` and
`license-files = ["LICENSE", "vendor/pyboy-src/LICENSE.md"]`, and both referenced
files exist in a clean master checkout.

## Disposition

No change made; there is nothing to repair. The lesson recorded for later runs:
do not pass `--no-build-isolation` against a repository that declares its own
build requirements, and do not attribute a toolchain-version failure to the
project's metadata. A stale venv produced an authentic-looking error message
about a file that is fine.

Release status stays **PARTIAL**.
