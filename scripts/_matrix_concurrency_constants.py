from __future__ import annotations

REPORT_SCHEMA_VERSION = 1
DEFAULT_WORKER_COUNTS = (1, 2, 4)
RUNTIMES = ("source", "native")
# A worker policy governs the declared trade/battle matrix as a whole.  A run
# that measured only one of them has no evidence about the other, so it may
# report measurements but must not select a policy.
REQUIRED_SELECTION_TIERS = ("battle", "trade")
# The slowest legitimate arm must not be killed by this harness.  At workers=1
# the gate may spend every declared row's full per-row budget before any
# failure, so an arm bound below that would interrupt a valid slow baseline and
# report an unsupported "unselected".  The bound is derived from the two
# sources the gate itself uses -- the declared matrix rows and the per-tier row
# timeouts -- rather than hardcoded, so it tracks the live matrix.
#
# This is a ceiling on our own child process.  It relaxes no per-row deadline
# the gate enforces.
DEFAULT_ARM_TIMEOUT_SECONDS_FLOOR = 108000.0
# Headroom above the derived worst case so process start-up, report writing and
# the final aggregate flush cannot consume the margin and report a legitimate
# arm as interrupted.
ARM_TIMEOUT_MARGIN = 1.25

# ``production_gate.py`` spells the compiled runtime ``cython`` (see
# ``production_gate_model.RUNTIME_MODES``).  This benchmark reports it as
# ``native`` because that is the name used throughout the issue and the
# release documentation, so the label is translated at the process boundary
# instead of being passed through as an invalid runtime mode.
GATE_RUNTIME_MODES = {"source": "source", "native": "cython"}
SECONDS_PER_HOUR = 3600.0
