# Archived, unintegrated regression tests

This branch preserves four unpublished source files from an older shared
checkout. It is NOT the Linux resume branch or a runnable/qualified release.
The tests were not rerun for this archival publication. Some depend on older
shared-checkout APIs; review and port useful coverage to the current APIs,
including explicit tier registration, rather than blindly cherry-picking them.

Start production work from `sync/linux-resume-20260911` and read its
`docs/LINUX_RESUME_PROMPT.md`. The archival base is `6447991`; the four files
were retained from the older shared checkout based at `985d95e`. Other shared
source versions are superseded in the newer history, and CRLF-only changes,
virtual environments, temporary worktrees, and private assets were excluded.
The original dirty checkout was not modified.

Preserved files:

- `tests/test_battle_turn_evidence.py`
- `tests/test_bootstrap_pyboy.py`
- `tests/test_remote_battle_evidence.py`
- `tests/test_tcp_battle_menu_input.py`
