"""Steps 4 and 8: write the prepared buffers into a 0700 staging folder and remove it afterwards."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import stat
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, List, Optional, Tuple

from . import config

if TYPE_CHECKING:
    from .stage import Prepared

TASK_NAME = "TASK.md"
RULE_LINE = "Read-only task: you may not write files, run commands or fetch URLs.\n"


@dataclass
class Staging:
    path: str
    manifest: List[Tuple[str, int, str]]


def _write_new(base: str, rel: str, data: bytes) -> None:
    folder = base
    parts = rel.split("/")
    for name in parts[:-1]:
        folder = os.path.join(folder, name)
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
    fd = os.open(os.path.join(folder, parts[-1]), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # O_EXCL alone refuses an existing symlink
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def task_file_text(task: str, files: Optional[List[str]] = None) -> str:
    """The task, the read-only rule and, when given, the exact relative paths of the staged files.

    The list stops the model from hunting around the folder (measured on agy 1.4.3: with no list it tried paths such as
    `report1.md` and then paths outside the staging folder, which headless mode denied)."""
    listing = ""
    if files:
        listing = "Files in this folder (read exactly these paths, nothing else):\n%s\n\n" % "\n".join("- %s" % json.dumps(f, ensure_ascii=True) for f in files)
    return "# Task\n\n%s\n\n%s%s" % (task.strip(), listing, RULE_LINE)


def _chmod_no_follow(path: str) -> None:
    """chmod 0700 a real file or folder; never touch what a symlink points at."""
    if not stat.S_ISLNK(os.lstat(path).st_mode):
        os.chmod(path, 0o700)


def _cleanup(path: str) -> None:
    def fix(func, p, _exc):
        with contextlib.suppress(OSError):
            if p != path:  # never touch the folder that holds the one we are removing (TMPDIR)
                _chmod_no_follow(os.path.dirname(p))
            _chmod_no_follow(p)
            func(p)
    # A second Ctrl-C must not interrupt the removal; it is delivered afterwards.
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    old = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        shutil.rmtree(path, onerror=fix)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


# Exactly what tempfile.mkdtemp makes: our prefix plus 8 random characters.
_OWN_STAGING_NAME = re.compile(r"^agy-bridge-(?:ver-|doctor-)?[a-z0-9_]{8}$")


def sweep_old_staging(max_age_s: int = config.SWEEP_MIN_AGE_S) -> int:
    """Remove our own stale staging folders left by a crash or a SIGKILL. Returns how many.

    Only real folders owned by this user, with mode 0700 and exactly the name mkdtemp makes.
    """
    base = tempfile.gettempdir()
    removed = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        if not _OWN_STAGING_NAME.match(name):
            continue
        full = os.path.join(base, name)
        try:
            st = os.lstat(full)
            if (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700
                    and time.time() - st.st_mtime > max_age_s):
                _cleanup(full)
                removed += 1
        except OSError:
            continue
    return removed


@contextlib.contextmanager
def materialize(prepared: Prepared) -> Iterator[Staging]:
    """Create the 0700 staging folder, write the buffers, remove everything on exit."""
    path = tempfile.mkdtemp(prefix="agy-bridge-")
    try:
        manifest: List[Tuple[str, int, str]] = []
        for entry in prepared.entries:
            _write_new(path, entry.rel, entry.data)
            manifest.append((entry.rel, len(entry.data), entry.sha256))
        _write_new(path, TASK_NAME, task_file_text(prepared.task, [rel for rel, _size, _sha in manifest]).encode("utf-8"))
        yield Staging(path, manifest)
    finally:
        _cleanup(path)
