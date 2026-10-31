"""Repository-local metadata journal. Anchor paths; never follow links or log contents."""
from __future__ import annotations

import fcntl
import errno
import json
import os
import stat
import time
from collections import deque

from .report import SCRATCH_DIR


class EventError(Exception):
    pass


class EventIOError(EventError):
    """An unavailable journal must not prevent a delegation."""


MAX_EVENT_LINE_BYTES = 65536


def _check_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise EventError('event log is not a single-link regular file owned by this user')


def open_log(root, create=True, name="events.jsonl"):
    """Return a pinned regular-file fd, or None for a missing read-only journal."""
    if name not in ('events.jsonl', 'verdicts.jsonl'):
        raise EventError('unsupported journal name')
    parent = None
    try:
        parent = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
        for part in os.path.abspath(root).split(os.sep)[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        if create:
            try:
                os.mkdir(SCRATCH_DIR, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        child = os.open(SCRATCH_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.close(parent)
        parent = child
        info = os.fstat(parent)
        if info.st_uid != os.getuid():
            raise EventError('event folder is not owned by this user')
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            # Check hostile targets even if an ordinary permission error would
            # otherwise prevent opening them. Repeat on the pinned fd below.
            _check_file(info)
        if create:
            os.fchmod(parent, 0o700)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT if create else os.O_RDONLY
        for attempt in range(3):
            try:
                fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
                break
            except FileNotFoundError:
                # Concurrent first creation can briefly return ENOENT on macOS.
                # Retry only the same pinned directory and no-follow open.
                if not create or attempt == 2:
                    raise
        try:
            info = os.fstat(fd)
            _check_file(info)
            if create:
                os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        return fd
    except FileNotFoundError:
        if not create:
            return None
        raise EventIOError('event log could not be opened')
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            raise EventError('event log could not be opened safely') from None
        raise EventIOError('event log could not be opened') from None
    finally:
        if parent is not None:
            os.close(parent)


def append(fd, record):
    record = dict(record, ts=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    data = (json.dumps(record, sort_keys=True, ensure_ascii=True) + '\n').encode('utf-8')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if not written:
                    raise EventIOError('could not append the event record')
                view = view[written:]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        raise EventIOError('could not append the event record') from None


def tail(root, count, name="events.jsonl"):
    fd = open_log(root, create=False, name=name)
    if fd is None:
        return []
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        with os.fdopen(os.dup(fd), 'rb') as stream:
            lines = deque(maxlen=count)
            while True:
                line = stream.readline(MAX_EVENT_LINE_BYTES + 1)
                if len(line) > MAX_EVENT_LINE_BYTES:
                    raise EventError('event log contains an oversized record')
                if not line:
                    break
                lines.append(line)
        # Re-encode JSON so even a tampered record cannot print terminal controls.
        return [json.dumps(json.loads(line), sort_keys=True, ensure_ascii=True) for line in lines]
    except (OSError, ValueError):
        raise EventError('event log is unreadable or contains invalid JSON')
    finally:
        os.close(fd)
