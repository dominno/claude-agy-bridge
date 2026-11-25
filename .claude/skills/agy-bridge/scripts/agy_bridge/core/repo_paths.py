"""Repository-relative path rules: no absolute paths, no traversal, nothing on the denylist."""
from __future__ import annotations

import os

from . import config, denylist
from .refusal import Refusal, Refused


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
        raise Refused([Refusal('R1', 'path is on the denylist', rel)])
    return rel
