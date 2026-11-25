"""Port: the isolation for implementation jobs (the agent turn and its offline check) and its errors.

The adapter is agy_bridge.adapters.edit_sandbox (Bubblewrap on Linux, sandbox-exec on macOS).
"""
from __future__ import annotations

from typing import Protocol

from ..core import config
from .binding import Slot


class Unavailable(config.UsageError):
    """A required OS isolation feature is unavailable."""


class InfrastructureError(Unavailable):
    """The offline check sandbox did not launch its executable successfully."""


class DeadlineExceeded(config.UsageError):
    """The job deadline elapsed during the sandbox preflight."""


class CleanupError(Unavailable):
    """The sandbox process remained after the bounded teardown wait."""


class EditSandbox(Protocol):
    def validate_profile(self, path, repo=None) -> str: ...

    def agent_environment(self, *, use_api_key=False) -> dict: ...

    def agent_command(self, argv, *, tree, profile) -> list: ...

    def agent_private_paths(self, *, tree, profile) -> tuple: ...

    def preflight(self, *, tree, profile, exe, should_cancel=None, deadline=None) -> str: ...

    def run_test(self, argv, *, tree, timeout_s, should_cancel=None, deadline=None): ...

    def preflight_test(self, *, tree, timeout_s, should_cancel=None, deadline=None): ...


use = Slot('implementation sandbox')
bind = use.bind
