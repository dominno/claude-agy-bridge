"""Port: the implementation workspace (approved snapshot, editable tree and exported diff).

The adapter is agy_bridge.adapters.workspace, which uses Git only to build and diff trees.
"""
from __future__ import annotations

from typing import Protocol

from .binding import Slot


class Workspace(Protocol):
    def prepare(self, repo, files, globs, task): ...

    def materialize(self, snapshot, job_dir): ...

    def load(self, job_dir, manifest, base_commit=None, extra_deny=()): ...

    def record_path(self, rel, extra, seen) -> str: ...

    def validate_candidate(self, workspace) -> None: ...

    def copy_candidate(self, workspace, dest, changes=None): ...

    def export_diff(self, workspace): ...


use = Slot('workspace')
bind = use.bind
