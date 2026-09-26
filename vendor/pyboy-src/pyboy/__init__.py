#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

__pdoc__ = {
    "core": False,
    "logging": False,
    "pyboy": False,
    "conftest": False,
}

__all__ = ["PyBoy", "PyBoyMemoryView", "PyBoyRegisterFile"]

# The harness ships the PyBoy 2.7.0 source fork at this exact revision. Keep
# the version visible to Session's optional runtime-pin check; upstream's
# package does not expose it from ``pyboy.__init__``. This is the harness-local
# divergence revision, not a git object: it is the SHA-1 of the vendored source
# manifest and it replaced the upstream base revision
# c565df66c3731fad2856169a90f6bbec99925915 once the tree diverged. See
# POKERED_HARNESS_PYBOY_DIVERGENCE.md.
__version__ = "2.7.0"
__pokered_harness_revision__ = "b94bf5dfb042c502ff4bc1bcd417599b02a9419b"

from .pyboy import PyBoy, PyBoyMemoryView, PyBoyRegisterFile
