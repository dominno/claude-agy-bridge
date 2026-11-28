"""R1/R5/R6 refusals: what a rule refused and where, never file content."""
from __future__ import annotations

import errno

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Refusal:
    rule: str  # R1, R5 or R6
    reason: str  # short plain-words reason; never file content
    where: str  # a relative path or a label such as "the task text"
    count: int = 1
    line: int = 0


class Refused(Exception):
    def __init__(self, refusals: Sequence[Refusal]):
        super().__init__("refused")
        self.refusals = list(refusals)


class SymlinkInPath(OSError):
    """A pinned no-follow walk met a symbolic link as a path component (refused, never followed).

    `component` is the link's name, `path` the path as given and `resolved` where it points (for the message only;
    the bridge never opens the resolved path on its own). `hint` is an optional second fix the caller can add.
    """

    def __init__(self, component: str, path: str, resolved: str, hint: str = ""):
        super().__init__(errno.ELOOP, 'path component "%s" is a symbolic link (refused)' % component, path)
        self.component = component
        self.path = path
        self.resolved = resolved
        self.hint = hint
