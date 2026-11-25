"""Shared AGY capabilities, sandbox paths and execution clock for edit jobs."""
from __future__ import annotations

import math

from ..core import config


MIN_AGY_VERSION = (1, 5, 16)
GUEST_PROFILE = '/home/agy'
GUEST_PROC = '/proc'
GUEST_DEV = '/dev'
GUEST_TMP = '/tmp'
# AGY directory permission rules cover descendants with a trailing slash.
GUEST_PRIVATE_PATHS = tuple(path + '/' for path in (GUEST_PROFILE, GUEST_PROC, GUEST_DEV, '/etc', '/sys', GUEST_TMP))


def execution_deadline(started, timeout_s, deadline):
    """Cap a phase duration by an optional absolute time.monotonic() deadline."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise config.UsageError('execution timeout must be finite and positive')
    end = started + timeout_s
    if deadline is None:
        return end
    if not math.isfinite(deadline):
        raise config.UsageError('execution deadline must be finite')
    return min(end, deadline)
