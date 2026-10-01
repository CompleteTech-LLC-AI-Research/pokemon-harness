"""Run the main-module split's controlled checks in the asset-free unit gate.

These structural and controlled behavior checks complement the real runtime
suite. They do not establish emulator or gameplay qualification.
"""

from scripts.check_pyboy_components import ComponentChecks as TestPyBoyMainComponents

__all__ = ["TestPyBoyMainComponents"]
