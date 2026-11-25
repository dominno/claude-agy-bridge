"""R1/R5/R6 refusals: what a rule refused and where, never file content."""
from __future__ import annotations

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
