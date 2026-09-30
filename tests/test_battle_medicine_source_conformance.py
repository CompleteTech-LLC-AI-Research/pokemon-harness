"""Opt-in source-conformance audit for the pinned battle-medicine excerpts.

The unit-tier oracle in ``tests/test_battle_medicine_oracle.py`` checks the
committed excerpts, the parsed ladder and the resulting arithmetic with no
external input at all.  This module instead re-derives every committed excerpt
from a local pinned upstream checkout, so it needs ``POKERED_PRET_ROOT`` and
``POKEYELLOW_PRET_ROOT`` and skips when either is absent.

It is deliberately **not** part of the required unit tier.  The production gate
fails closed on any skipped test, and the asset-free release-hygiene unit run
does not provision those checkouts, so its skips must never be selectable by a
tier marker expression.  ``tests/_tier_config.py`` therefore lists this module
as an opt-in module, which keeps it out of every tier while still failing
collection for any *new* module that is not classified at all.

Run the audit explicitly with both checkouts pinned::

    POKERED_PRET_ROOT=/path/to/pokered \
    POKEYELLOW_PRET_ROOT=/path/to/pokeyellow \
    pytest tests/test_battle_medicine_source_conformance.py
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from tests._battle_medicine_oracle import (
    ALL_EXCERPTS,
    RED_BLUE,
    YELLOW,
    excerpt_text,
)

EXCERPT_IDS = [record.label.replace(" ", "-") for record in ALL_EXCERPTS]

CONFORMANCE_VARIABLES = {
    RED_BLUE: "POKERED_PRET_ROOT",
    YELLOW: "POKEYELLOW_PRET_ROOT",
}


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _slice_lines(path: Path, first: int, last: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[first - 1 : last])


@pytest.mark.parametrize("record", ALL_EXCERPTS, ids=EXCERPT_IDS)
def test_committed_excerpt_matches_the_configured_pret_checkout(record) -> None:
    variable = CONFORMANCE_VARIABLES[record.source]
    configured = os.environ.get(variable)
    if not configured:
        pytest.skip(f"{variable} is not set; source conformance needs a pinned checkout")
    root = Path(configured).expanduser()
    path = root / record.path
    assert path.is_file(), f"{path} is missing"
    assert _git_head(root) == record.revision, f"{variable} is not at the pinned revision"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record.file_sha256
    assert _slice_lines(path, record.first_line, record.last_line) == excerpt_text(record)
