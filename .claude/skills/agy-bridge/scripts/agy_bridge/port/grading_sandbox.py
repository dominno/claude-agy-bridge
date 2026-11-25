"""Port: the confined runner for explicit grading checks (`grade-mutants`, `apply-diff`).

The adapter is agy_bridge.adapters.sandbox (sandbox-exec on macOS; Bubblewrap or unshare on Linux).
"""
from __future__ import annotations

from typing import Protocol

from .binding import Slot


class CheckSandbox(Protocol):
    def sandbox_preflight(self, interpreter, tree, repo, extra_env=None) -> None: ...

    def run_scratch(self, argv, tree, repo, extra_env=None, timeout=300, output_markers=None,
                    unsafe_no_sandbox=False, sandbox_step=False): ...


use = Slot('grading sandbox')
bind = use.bind
