"""Implementation job phases and the job record model, shared by the job store and the implement use cases."""
from __future__ import annotations

import re
from typing import TypedDict

from . import config

ACTIVE = frozenset({'queued', 'running', 'checking'})
REVIEWABLE = frozenset({'verified', 'ready'})
APPLY_PHASES = REVIEWABLE | {'applying', 'applied'}
PHASES = ACTIVE | APPLY_PHASES | {'prepared', 'checks_failed', 'no_changes', 'canceled', 'timed_out', 'quota',
                                 'auth_required', 'interrupted', 'failed'}
ID = re.compile(r'[0-9a-f]{32}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
TERMINAL = frozenset({'applied', 'canceled', 'failed', 'no_changes'})


class InvalidState(config.UsageError):
    """A job record could not be read or validated."""


class JobState(TypedDict, total=False):
    schema_version: int
    job_id: str
    phase: str
    repository: str
    created_at: float
    started_at: float
    updated_at: float
    attempt: int
    last_agy_activity_at: float
    bridge_exit: int
    base_commit: str | None
    conversation_id: str | None
    detail: str
    failure_kind: str
    baseline_check_exit: int
    check_exit: int
    changes: list[list[str]]
    check_inputs_changed: list[str]
    patch_sha256: str
    run_id: str
    manifest: list[dict]
    write_dirs: list[str]
    extra_deny: list[str]
    profile: str
    task: str
    check: list[str] | None
    model: str | None
    use_api_key: bool
    turn_timeout_s: int
    job_timeout_s: int
