"""How use cases and command handlers write to the terminal: answers to stdout, failures and refusals to stderr."""
from __future__ import annotations

import sys
from typing import Optional

from ..core import stage
from . import report


def say(text: str) -> None:
    sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))


def fail(text: str) -> None:
    sys.stderr.write("agy-bridge: " + text + "\n")


def refused(exc: BaseException, operation: str, out: Optional[str] = None) -> None:
    """Name the rule, or the failing operation and path, instead of a generic refusal."""
    if isinstance(exc, report.OutPathRefused):
        fail("--out %s refused: %s (allowed: the temp folder or <repo>/%s/)"
             % (stage.printable(out) if out else "value", stage.printable(str(exc)), report.SCRATCH_DIR))
        return
    lines = stage.explain(exc, operation)
    fail(lines[0])
    for extra in lines[1:]:
        sys.stderr.write(extra + "\n")
