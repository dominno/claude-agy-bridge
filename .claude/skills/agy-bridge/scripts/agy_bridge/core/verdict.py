"""Job names shared by the queue, the verdict journal and the job stores, and the queue refusal markers."""
from __future__ import annotations

import re

from . import config
from .scan import scan_bytes

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')
REFUSAL_HEADING = '# Queue refusal'
REFUSAL_MARKER = '<!-- agy-bridge-refusal-v1 '


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise config.UsageError('job name must be 1-120 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit')
    if scan_bytes(value.encode('utf-8')):
        raise config.UsageError('key-shaped job name refused')
    return value
