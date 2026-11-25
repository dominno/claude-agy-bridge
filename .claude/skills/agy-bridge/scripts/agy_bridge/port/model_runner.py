"""Port: the model runner (the agy CLI), its errors and its run results.

The adapter is agy_bridge.adapters.agy. Use cases reach it with `use()` and catch the errors defined here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from ..core import config
from . import process
from .binding import Slot

# A probe that outlived its limit; re-exported so use cases can catch it without importing the process seam.
TimeoutExpired = process.TimeoutExpired


class AgyMissing(Exception):
    pass


class UnsupportedAgy(Exception):
    pass


class AgyNotRun(UnsupportedAgy):
    """agy was found but could not be started, so no program ran."""


class EditCleanupError(config.UsageError):
    """The isolated editing process could not be confirmed stopped."""


@dataclass
class RunResult:
    returncode: Optional[int]
    out: bytes
    err_bytes: int
    err_markers: Dict[str, bool]
    timed_out: bool
    truncated: bool
    seconds: float


@dataclass(frozen=True)
class EditRunResult:
    conversation_id: Optional[str]
    status: str
    response: str
    returncode: Optional[int]
    timed_out: bool
    truncated: bool
    cancelled: bool
    err_markers: Dict[str, bool]
    seconds: float
    error_kind: Optional[str]
    retryable: Optional[bool]

    @property
    def exit_code(self) -> int:
        return {
            None: config.EXIT_OK,
            "cancelled": 130,
            "timeout": config.EXIT_TIMEOUT,
            "permission": config.EXIT_SOFT_DENY,
            "output_limit": config.EXIT_INCOMPLETE,
            "quota": config.EXIT_QUOTA,
            "auth": config.EXIT_AUTH,
        }.get(self.error_kind, config.EXIT_AGY_FAILED)


class ModelRunner(Protocol):
    def find_agy(self, keep_out_of: Optional[str] = None) -> str: ...

    def detect_version(self, exe: str, env: Dict[str, str]) -> Tuple[int, int, int]: ...

    def parse_version(self, text: str) -> Tuple[int, int, int]: ...

    def build_argv(self, version: Tuple[int, int, int], exe: str, model: Optional[str]) -> List[str]: ...

    def require_edit_version(self, version: Tuple[int, int, int]) -> None: ...

    def run_probe(self, argv, env, cwd, timeout, *, should_cancel=None): ...

    def run_agy(self, argv: List[str], cwd: str, env: Dict[str, str], timeout_s: float, max_out: int,
                on_started=None) -> RunResult: ...

    def run_edit_agy(self, exe: str, cwd: str, env: Dict[str, str], model: Optional[str], timeout_s: float,
                     conversation_id: Optional[str], prompt: str,
                     on_conversation: Optional[Callable[[str], None]] = None, *, wrap, max_out: int = ...,
                     should_cancel=None, on_activity: Optional[Callable[[], None]] = None,
                     version: Optional[Tuple[int, int, int]] = None, deadline=None) -> EditRunResult: ...


use = Slot('model runner')
bind = use.bind
