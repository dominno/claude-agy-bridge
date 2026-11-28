"""Pinned, no-follow paths for repository-local workflow metadata."""
from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
import time

from ..core import stage
from ..core.refusal import SymlinkInPath
# The path rules live in core; workflow code reaches them through this module.
from ..core.repo_paths import local, relative  # noqa: F401


@contextlib.contextmanager
def directory(path, create=False):
    """Pin every component from /; even a replaced ancestor cannot redirect writes."""
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in os.path.abspath(path).split(os.sep)[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = open_component(fd, part, path)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def open_component(fd, part, path):
    """Open one directory component under fd without following it; a symlink raises SymlinkInPath naming it."""
    try:
        return os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError as exc:
        # O_NOFOLLOW reports a symlink as ELOOP (Linux) or ENOTDIR (macOS with O_DIRECTORY).
        if exc.errno in (errno.ELOOP, errno.ENOTDIR) and _is_link(fd, part):
            raise SymlinkInPath(part, path, os.path.realpath(path)) from None
        raise


def _is_link(fd, part):
    try:
        return stat.S_ISLNK(os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise OSError('expected an owned single-link regular file')


def read(root, rel, limit=2*1024*1024):
    with directory(root) as fd:
        return stage._read_regular(fd, relative(rel), limit)


def write(root, rel, text):
    rel = relative(rel)
    with directory(os.path.join(root, os.path.dirname(rel)), create=True) as parent:
        fd = os.open(os.path.basename(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            _write_all(fd, text.encode('utf-8'))
            os.fsync(fd)
        finally:
            os.close(fd)


TEMP_PREFIX = '.agy-tmp-'


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if not n:
            raise OSError('short write')
        view = view[n:]


def publish(root, rel, text):
    """Create rel atomically: write a private temp file, fsync, link it to the final name (never replacing one).

    Readers see either no file or the complete one. FileExistsError when rel already exists.
    """
    rel = relative(rel)
    name = os.path.basename(rel)
    with directory(os.path.join(root, os.path.dirname(rel)), create=True) as parent:
        temp = TEMP_PREFIX + name + '-' + secrets.token_hex(8)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            try:
                _write_all(fd, text.encode('utf-8'))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        finally:
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.fsync(parent)


def sweep_temp(root, rel, max_age=3600):
    """Remove this user's leftover publish temp files; return how many.

    A temp file with a second link was already published (the writer stopped before unlinking it) and goes now;
    an unpublished one goes once it is older than max_age seconds.
    """
    removed = 0
    try:
        with directory(os.path.join(root, rel)) as parent:
            for entry in os.listdir(parent):
                if not entry.startswith(TEMP_PREFIX):
                    continue
                try:
                    info = os.stat(entry, dir_fd=parent, follow_symlinks=False)
                    if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                            (info.st_nlink > 1 or time.time() - info.st_mtime > max_age)):
                        os.unlink(entry, dir_fd=parent)
                        removed += 1
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    return removed
