"""Start and stop detached agy-bridge workers (the worker launcher adapter).

A worker runs the agy_bridge package this process runs (its scripts/ folder), never one found on sys.path, with an
isolated interpreter (-I) in a new session.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sys

from ..port import process

_BOOTSTRAP = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
              "runpy.run_module('agy_bridge', run_name='__main__')")


def start_worker(arguments, *, output=None, **options):
    """Start `agy-bridge <arguments>` detached; stdin and (unless output is given) stdout/stderr are /dev/null."""
    package_root = Path(__file__).resolve().parents[2]
    argv = [sys.executable, '-I', '-c', _BOOTSTRAP, str(package_root), *arguments]
    stream = process.DEVNULL if output is None else output
    return process.spawn(argv, stdin=process.DEVNULL, stdout=stream, stderr=stream, start_new_session=True,
                         **options)


def stop_detached(proc, grace=2.0):
    """SIGTERM the detached watcher's session (its own process group), then SIGKILL if it lingers."""
    for sig in (signal.SIGTERM, getattr(signal, 'SIGKILL', signal.SIGTERM)):
        try:
            os.killpg(proc.pid, sig)
        except (OSError, AttributeError):
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        try:
            proc.wait(timeout=grace)
            return
        except process.TimeoutExpired:
            continue
