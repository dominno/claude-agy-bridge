"""The one process seam: every program the bridge starts goes through spawn(), spawn_pty() or run(), and
kill_group() stops it.

No other module imports subprocess or calls os.exec*, os.fork, os.spawn* or pty.fork; tests/test_architecture.py
checks that. Both helpers take an explicit argv list (never shell text) and pass every option through unchanged, so
the caller still decides the environment, working folder, file descriptors, session and timeout.
"""
from __future__ import annotations

import fcntl
import os
import pty
import struct
import subprocess
import termios

DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired


def spawn(argv, **options):
    """Start argv and return its Popen handle; the caller waits for it, times it out and stops it."""
    return subprocess.Popen(argv, **options)


def spawn_pty(argv, *, cwd, env, rows: int, cols: int):
    """Start argv in its own session on a new pseudo-terminal of rows x cols and return (Popen, master fd).

    The terminal is the child's controlling terminal (as in a real terminal window), which full-screen programs
    need. The caller owns the master descriptor: it reads, writes, closes it and stops the process group."""
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, close_fds=True, preexec_fn=_take_terminal)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


def _take_terminal():
    """In the child, after setsid(): make the pty on stdin the controlling terminal of the new session."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def run(argv, **options):
    """Run argv to completion (subprocess.run with the caller's options) and return the CompletedProcess."""
    return subprocess.run(argv, **options)


def kill_group(pid: int, sig: int) -> None:
    """Signal a child's whole process group; a group that is already gone (or not ours) is ignored."""
    try:
        os.killpg(pid, sig)  # the child is its own session leader, so pgid == pid
    except (ProcessLookupError, PermissionError):
        pass
