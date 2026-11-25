"""Port: starting and stopping a detached agy-bridge worker (an implementation job or a queue watcher).

The adapter is agy_bridge.adapters.launcher.
"""
from __future__ import annotations

from typing import Protocol

from .binding import Slot


class Launcher(Protocol):
    def start_worker(self, arguments, **options): ...

    def stop_detached(self, proc, grace=2.0) -> None: ...


use = Slot('worker launcher')
bind = use.bind
