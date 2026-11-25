"""Port: publishing a reviewed implementation patch into the source repository, with crash recovery.

The adapter is agy_bridge.adapters.edit_apply.
"""
from __future__ import annotations

from typing import Protocol

from .binding import Slot


class ApplyError(ValueError):
    pass


class PatchApplier(Protocol):
    def apply(self, root, base_commit, manifest, changes, patch, *, transaction_dir=None) -> None: ...


use = Slot('patch applier')
bind = use.bind
