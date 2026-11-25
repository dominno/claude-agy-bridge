"""Command line: the argparse parsers (one module per command group), thin command handlers and the composition root.

Importing the package binds every port to its adapter (wiring.wire), exactly once, before any command runs. Layer
rules: cli may import app and core; only cli/wiring.py imports port and adapters (tests/test_architecture.py).
"""
from . import wiring

wiring.wire()

from .entry import main  # noqa: E402

__all__ = ["main"]
