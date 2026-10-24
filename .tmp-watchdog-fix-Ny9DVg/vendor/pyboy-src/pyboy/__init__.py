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
# package does not expose it from ``pyboy.__init__``.
__version__ = "2.7.0"
__pokered_harness_revision__ = "c565df66c3731fad2856169a90f6bbec99925915"

from .pyboy import PyBoy, PyBoyMemoryView, PyBoyRegisterFile
