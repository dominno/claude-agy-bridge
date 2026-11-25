"""Private implementation job records and read-only worker observations."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Iterator, NamedTuple, TypedDict, cast
import uuid

from . import config, safety

ACTIVE = frozenset({'queued', 'running', 'checking'})
REVIEWABLE = frozenset({'verified', 'ready'})
APPLY_PHASES = REVIEWABLE | {'applying', 'applied'}
PHASES = ACTIVE | APPLY_PHASES | {'prepared', 'checks_failed', 'no_changes', 'canceled', 'timed_out', 'quota',
                                 'auth_required', 'interrupted', 'failed'}
ID = re.compile(r'[0-9a-f]{32}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
MAX_STATE = 2 * 1024 * 1024
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_SUMMARY = ('job_id', 'phase', 'repository', 'created_at', 'started_at', 'updated_at', 'attempt', 'last_agy_activity_at',
            'bridge_exit', 'check_inputs_changed')
_PUBLIC = _SUMMARY + ('base_commit', 'conversation_id', 'detail', 'baseline_check_exit', 'check_exit',
                      'changes', 'patch_sha256', 'failure_kind')


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


def _private(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise config.UsageError('implementation directories must be private to the current user')


@contextlib.contextmanager
def _child_directory(parent, name):
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        _private(fd)
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _open_store(create=False):
    base = os.path.abspath(config.bridge_home())
    with safety.directory(base, create=create) as parent:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise config.UsageError('bridge home must be owned by the current user and protected from writes by others')
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir('implement', 0o700, dir_fd=parent)
        with _child_directory(parent, 'implement') as fd:
            yield Path(base) / 'implement', fd


def store_root(create=False):
    with _open_store(create) as (root, _):
        return root


def job_path(job_id):
    if not ID.fullmatch(job_id):
        from .verdicts import NAME
        hint = ('; queue job names are shown by agy-bridge jobs list -C <repo>'
                if isinstance(job_id, str) and NAME.fullmatch(job_id) else '')
        raise config.UsageError('invalid implementation job ID: expected 32 lowercase hex characters ([0-9a-f]{32}); '
                                'list jobs with: agy-bridge jobs list' + hint)
    with _open_store() as (root, fd), _child_directory(fd, job_id):
        return root / job_id


def write(job, name, data):
    pending = '.pending-' + uuid.uuid4().hex
    with safety.directory(str(job)) as parent:
        _private(parent)
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(pending, dir_fd=parent)


def save(job, state):
    state['schema_version'] = 1
    state['updated_at'] = time.time()
    data = json.dumps(state, ensure_ascii=True, sort_keys=True).encode('utf-8') + b'\n'
    if len(data) > MAX_STATE:
        raise InvalidState('implementation job state exceeds the size limit')
    write(job, 'state.json', data)


def _is_path(value):
    return isinstance(value, str) and '\0' not in value and os.path.isabs(value) and os.path.normpath(value) == value


def _is_string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) and '\0' not in item for item in value)


def _is_timestamp(value):
    return type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value)


def _read_state(parent, job_id):
    try:
        fd = os.open('state.json', _READ_FILE, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            safety.regular(stream.fileno())
            data = stream.read(MAX_STATE + 1)
        if len(data) > MAX_STATE:
            raise InvalidState('implementation job state exceeds the size limit')
        state = json.loads(data)
    except (OSError, ValueError, RecursionError) as exc:
        raise InvalidState('implementation job state could not be read safely') from exc
    if not isinstance(state, dict) or state.get('job_id') != job_id:
        raise InvalidState('invalid implementation job state')
    schema = state.get('schema_version', 1)  # Original jobs stored this schema without a version tag.
    if type(schema) is not int or schema != 1:
        raise InvalidState('unsupported implementation job state schema')
    phase = state.get('phase')
    if not isinstance(phase, str) or phase not in PHASES or not _is_path(state.get('repository')):
        raise InvalidState('invalid implementation job state')
    run_id = state.get('run_id')
    if (run_id is not None and (not isinstance(run_id, str) or not ID.fullmatch(run_id))) or (
            phase in ACTIVE and run_id is None):
        raise InvalidState('invalid implementation job run ID')
    for name in ('created_at', 'started_at', 'updated_at', 'last_agy_activity_at'):
        if name in state and not _is_timestamp(state[name]):
            raise InvalidState('invalid implementation job timestamp')
    for name in ('attempt', 'bridge_exit', 'baseline_check_exit', 'check_exit'):
        if name in state and type(state[name]) is not int:
            raise InvalidState('invalid implementation job counter')
    for name in ('detail', 'base_commit', 'conversation_id', 'failure_kind'):
        if state.get(name) is not None and not isinstance(state[name], str):
            raise InvalidState('invalid implementation job state')
    digest = state.get('patch_sha256')
    if digest is not None and (not isinstance(digest, str) or not SHA256.fullmatch(digest)):
        raise InvalidState('invalid implementation patch hash')
    if 'changes' in state and (not isinstance(state['changes'], list) or any(
            not _is_string_list(change) or len(change) != 2 for change in state['changes'])):
        raise InvalidState('invalid implementation change list')
    if 'check_inputs_changed' in state and not _is_string_list(state['check_inputs_changed']):
        raise InvalidState('invalid implementation check input list')
    if phase in APPLY_PHASES and not (state.get('changes') and digest):
        raise InvalidState('completed implementation patch state is missing')
    return cast(JobState, state)


def load(job):
    with safety.directory(str(job)) as parent:
        _private(parent)
        state = _read_state(parent, job.name)
    if (not isinstance(state.get('task'), str) or not _is_path(state.get('profile'))
            or type(state.get('use_api_key')) is not bool or 'base_commit' not in state or 'model' not in state
            or 'check' not in state or 'conversation_id' not in state
            or state['model'] is not None and not isinstance(state['model'], str)
            or state['check'] is not None and (not _is_string_list(state['check']) or not state['check'])
            or not _is_string_list(state.get('extra_deny')) or not _is_string_list(state.get('write_dirs'))):
        raise InvalidState('invalid implementation task settings')
    for name, limit in (('turn_timeout_s', 3600), ('job_timeout_s', 24 * 3600)):
        value = state.get(name)
        if type(value) is not int or not 1 <= value <= limit:
            raise InvalidState('invalid implementation time budget')
    manifest = state.get('manifest')
    if not isinstance(manifest, list) or not manifest or any(not isinstance(entry, dict) or
            not isinstance(entry.get('path'), str) for entry in manifest):
        raise InvalidState('invalid implementation job manifest')
    return state


@contextlib.contextmanager
def lock(job):
    with safety.directory(str(job)) as parent:
        _private(parent)
        fd = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            safety.regular(fd)
            # An inventory probe holds a shared lock only long enough to test liveness.
            deadline = time.monotonic() + .2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        yield None
                        return
                    time.sleep(.02)
            yield fd
        finally:
            # A detached worker can still hold the same open file description.
            os.close(fd)


def _worker_active(parent):
    try:
        fd = os.open('lock', _READ_FILE, dir_fd=parent)
    except FileNotFoundError:
        return False
    try:
        safety.regular(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def _cancel_requested(parent, run_id):
    try:
        fd = os.open('cancel-' + run_id, _READ_FILE, dir_fd=parent)
    except FileNotFoundError:
        return False
    try:
        safety.regular(fd)
        return True
    finally:
        os.close(fd)


def cancel_pending(job, run_id):
    with safety.directory(str(job)) as parent:
        _private(parent)
        return _cancel_requested(parent, run_id)


class _Observation(NamedTuple):
    state: JobState
    phase: str
    worker_active: bool
    cancel_requested: bool


def _observe(parent, job_id):
    state = _read_state(parent, job_id)
    for _ in range(3):
        if state['phase'] not in ACTIVE:
            return _Observation(state, state['phase'], False, False)
        active = _worker_active(parent)
        requested = active and _cancel_requested(parent, state['run_id'])
        confirmed = _read_state(parent, job_id)
        if confirmed['phase'] == state['phase'] and confirmed['run_id'] == state['run_id']:
            phase = confirmed['phase'] if active else 'interrupted'
            return _Observation(confirmed, phase, active, requested)
        state = confirmed
    raise InvalidState('implementation job changed while its status was read')


def _render_status(observation, fields):
    row = {key: observation.state[key] for key in fields if key in observation.state}
    row.update(phase=observation.phase, worker_active=observation.worker_active,
               cancel_requested=observation.cancel_requested)
    if observation.phase == 'interrupted' and 'detail' in fields:
        row['detail'] = 'worker exited before completing the job'
    return row


def status(job):
    with safety.directory(str(job)) as parent:
        _private(parent)
        return _render_status(_observe(parent, job.name), _PUBLIC)


def read_status(job_id):
    return status(job_path(job_id))


def iter_status() -> Iterator[tuple[str, dict | None, str | None]]:
    """Yield job ID, private-field-free summary, and a safe error for an unreadable entry."""
    try:
        with _open_store() as (_, parent), os.scandir(parent) as entries:
            for entry in entries:
                if not ID.fullmatch(entry.name):
                    continue
                try:
                    with _child_directory(parent, entry.name) as fd:
                        row = _render_status(_observe(fd, entry.name), _SUMMARY)
                except (OSError, config.UsageError):
                    yield entry.name, None, 'implementation job could not be read safely'
                else:
                    yield entry.name, row, None
    except FileNotFoundError:
        return
    except (OSError, config.UsageError):
        yield '', None, 'implementation store could not be read safely'


def request_cancel(job):
    with safety.directory(str(job)) as parent:
        _private(parent)
        active = _observe(parent, job.name)
        if not active.worker_active:
            raise config.UsageError('no active execution to cancel')
        run_id = active.state['run_id']
        write(job, 'cancel-' + run_id, b'\n')
        confirmed = _observe(parent, job.name)
        if confirmed.state.get('run_id') != run_id or not (confirmed.worker_active or confirmed.phase == 'canceled'):
            raise config.UsageError('execution ended before cancellation could be confirmed')


# Phases with nothing left to review, resume or recover; removing one needs no --force.
TERMINAL = frozenset({'applied', 'canceled', 'failed', 'no_changes'})
LEFTOVER_PREFIXES = ('.prepare-', '.pending-')
LEFTOVER_AGE = 3600


def _open_owned_directory(parent, name, expected=None):
    """Open a child directory without following links; it must be ours. Make it writable so entries can go."""
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if expected is not None and (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError('directory changed while it was being removed')
        if info.st_uid != os.getuid():
            raise config.UsageError('implementation job directories must be owned by the current user')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove_tree(parent, name, dry_run=False):
    """Delete parent/name with dir_fd-relative traversal; return the bytes reclaimed.

    Symlinks are unlinked, never followed; every directory is opened O_NOFOLLOW and must be owned by this user.
    Read-only directories (the baseline snapshot) get u+rwx first. dry_run only measures.
    """
    total = 0
    top = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(top.st_mode):
        if not dry_run:
            os.unlink(name, dir_fd=parent)
        return top.st_size
    stack = []
    try:
        fd = _open_owned_directory(parent, name, top)
        stack.append((parent, name, fd, iter(os.listdir(fd))))
        while stack:
            owner, label, fd, entries = stack[-1]
            entry = next(entries, None)
            if entry is None:
                stack.pop()
                os.close(fd)
                if not dry_run:
                    os.rmdir(label, dir_fd=owner)
                continue
            info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_owned_directory(fd, entry, info)
                if not dry_run and info.st_mode & 0o700 != 0o700:
                    os.fchmod(child, stat.S_IMODE(info.st_mode) | 0o700)
                stack.append((fd, entry, child, iter(os.listdir(child))))
                continue
            if not dry_run:
                os.unlink(entry, dir_fd=fd)
            total += info.st_size
    finally:
        for _owner, _label, fd, _entries in reversed(stack):
            os.close(fd)
    return total


def _phase_for_removal(parent, job_id, force):
    try:
        return _read_state(parent, job_id)['phase']
    except InvalidState:
        if force:
            return 'unreadable'
        raise config.UsageError('job %s has an unreadable state; pass --force to remove it anyway' % job_id) from None


def removal_refusal(job_id, phase, force):
    """Why a job in this phase must stay, or None."""
    if phase in TERMINAL or force:
        return None
    if phase in REVIEWABLE:
        return ('job %s is %s and its patch was not applied; apply or review it first, or pass --force'
                % (job_id, phase))
    return 'job %s is %s, not finished; pass --force to remove it anyway' % (job_id, phase)


def remove(job_id, force=False, dry_run=False):
    """Delete one job directory under its lock; return (phase, bytes)."""
    job = job_path(job_id)
    with lock(job) as fd:
        if fd is None:
            raise config.UsageError('job %s has an active worker or operation; cancel it first '
                                    '(agy-bridge jobs cancel %s)' % (job_id, job_id))
        with _open_store() as (_, store), _child_directory(store, job_id) as parent:
            phase = _phase_for_removal(parent, job_id, force)
            refusal = removal_refusal(job_id, phase, force)
            if refusal:
                raise config.UsageError(refusal)
            return phase, _remove_tree(store, job_id, dry_run)


def _age(info, now):
    return now - info.st_mtime


def _unreadable_stamp(parent):
    """Age of a job whose state cannot be read: the state file's mtime, else the directory's.

    The directory mtime alone is not usable: taking the job lock creates the lock file on the first prune, and
    sweeping .pending files changes it too, so an unreadable job would always look new.
    """
    try:
        return os.stat('state.json', dir_fd=parent, follow_symlinks=False).st_mtime
    except OSError:
        return os.fstat(parent).st_mtime


def prune(older_than, force=False, dry_run=False, now=None):
    """Remove old finished jobs and stale staging leftovers; return a report dict."""
    now = time.time() if now is None else now
    report = dict(dry_run=dry_run, removed=[], kept=[], leftovers=0, bytes=0)
    try:
        with _open_store() as (_, store):
            names = sorted(os.listdir(store))
            for name in names:
                if name.startswith(LEFTOVER_PREFIXES):
                    try:
                        info = os.stat(name, dir_fd=store, follow_symlinks=False)
                        if info.st_uid == os.getuid() and _age(info, now) > LEFTOVER_AGE:
                            report['bytes'] += _remove_tree(store, name, dry_run)
                            report['leftovers'] += 1
                    except FileNotFoundError:
                        pass
    except FileNotFoundError:
        return report
    for name in names:
        if not ID.fullmatch(name):
            continue
        try:
            job = job_path(name)
            with lock(job) as fd:
                if fd is None:
                    report['kept'].append(dict(job_id=name, reason='active worker'))
                    continue
                with _open_store() as (_, store), _child_directory(store, name) as parent:
                    try:
                        state = _read_state(parent, name)
                        phase = state['phase']
                        stamp = state.get('updated_at') or state.get('created_at') or os.fstat(parent).st_mtime
                    except InvalidState:
                        phase, stamp = 'unreadable', _unreadable_stamp(parent)
                    if now - stamp < older_than:
                        report['kept'].append(dict(job_id=name, reason='newer than --older-than'))
                        for entry in os.listdir(parent):
                            if entry.startswith('.pending-'):
                                info = os.stat(entry, dir_fd=parent, follow_symlinks=False)
                                if stat.S_ISREG(info.st_mode) and _age(info, now) > LEFTOVER_AGE:
                                    if not dry_run:
                                        os.unlink(entry, dir_fd=parent)
                                    report['leftovers'] += 1
                                    report['bytes'] += info.st_size
                        continue
                    refusal = removal_refusal(name, phase, force)
                    if refusal:
                        report['kept'].append(dict(job_id=name, reason='phase ' + phase + ' needs --force'))
                        continue
                    report['bytes'] += _remove_tree(store, name, dry_run)
                    report['removed'].append(dict(job_id=name, phase=phase))
        except FileNotFoundError:
            continue
        except (OSError, config.UsageError) as exc:
            report['kept'].append(dict(job_id=name, reason='could not be removed safely: ' + type(exc).__name__))
    return report
