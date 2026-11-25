"""The one process seam: every program the bridge starts goes through spawn() or run(), and kill_group() stops it.

No other module imports subprocess or calls os.exec*, os.fork, os.spawn* or pty.fork; tests/test_architecture.py
checks that. Both helpers take an explicit argv list (never shell text) and pass every option through unchanged, so
the caller still decides the environment, working folder, file descriptors, session and timeout.
"""
from __future__ import annotations

import os
import subprocess

DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired


def spawn(argv, **options):
    """Start argv and return its Popen handle; the caller waits for it, times it out and stops it."""
    return subprocess.Popen(argv, **options)


def run(argv, **options):
    """Run argv to completion (subprocess.run with the caller's options) and return the CompletedProcess."""
    return subprocess.run(argv, **options)


def kill_group(pid: int, sig: int) -> None:
    """Signal a child's whole process group; a group that is already gone (or not ours) is ignored."""
    try:
        os.killpg(pid, sig)  # the child is its own session leader, so pgid == pid
    except (ProcessLookupError, PermissionError):
        pass
