"""Pinned, no-follow paths for repository-local workflow metadata."""
from __future__ import annotations

import contextlib
import os
import stat

from . import config, denylist, stage


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise config.UsageError('expected a repository-relative path')
    if os.path.isabs(value) or any(p in ('', '.', '..') for p in value.split('/')):
        raise config.UsageError('expected a relative path without traversal')
    return value


def local(root, value, metadata=False):
    if not isinstance(value, str):
        raise config.UsageError('path must be a string')
    if os.path.isabs(value):
        value = os.path.relpath(value, root)
    rel = relative(value)
    check = rel
    if metadata and (rel == '.agy-bridge' or rel.startswith('.agy-bridge/')):
        check = rel[len('.agy-bridge'):].lstrip('/')
    if denylist.is_denied(check, config.load_extra_deny(root)):
        raise stage.Refused([stage.Refusal('R1', 'path is on the denylist', rel)])
    return rel


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
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


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
            view = memoryview(text.encode('utf-8'))
            while view:
                n = os.write(fd, view)
                if not n:
                    raise OSError('short write')
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
