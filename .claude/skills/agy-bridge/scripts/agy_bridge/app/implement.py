"""Run and resume a write-capable AGY job outside Claude Code's process lifetime."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid

from ..core import config, stage
from ..core.job_state import APPLY_PHASES, REVIEWABLE, SHA256, InvalidState
from ..core.scan import scan_bytes
from ..port import edit_sandbox, job_store, launcher, patch_apply, safety
from ..port import workspace as workspace_port
from . import implement_work
from .implement_work import MAX_PATCH, _finish, _tail, check_inputs_warning

# The `jobs` subcommands that act on one job: (name, implement-<operation>, aliases, help). cli/parser_implement.py
# builds the parsers from it and dispatch maps each operation back to its `jobs` name for messages.
JOB_ACTIONS = (
    ('show', 'status', ('status',), 'show one implementation job'),
    ('cancel', 'cancel', (), 'request cancellation of an active job'),
    ('resume', 'resume', (), 'restart a resumable implementation job'),
    ('patch', 'patch', (), 'print the saved patch'),
    ('apply', 'apply', (), 'apply or recover an explicitly reviewed patch'),
)


_AGE = re.compile(r'([0-9]{1,7})([smhd]?)\Z')


def _older_than(value):
    match = _AGE.fullmatch(value.strip().lower())
    seconds = int(match.group(1)) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match.group(2)] if match else 0
    if not 0 <= seconds <= 3650 * 86400 or not match:
        raise config.UsageError('--older-than must look like 3600, 90m, 12h or 7d (at most 3650d)')
    return seconds


def maintain(args):
    """jobs remove / jobs prune: delete private job directories without following links."""
    operation = 'jobs ' + args.implementation_operation[len('implement-'):]
    try:
        if args.implementation_operation == 'implement-prune':
            result = job_store.use().prune(_older_than(args.older_than), force=args.force, dry_run=args.dry_run)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        try:
            phase, size = job_store.use().remove(args.job, force=args.force, dry_run=args.dry_run)
        except FileNotFoundError:
            sys.stderr.write('agy-bridge: no implementation job %s; list jobs with: agy-bridge jobs list\n' % args.job)
            return config.EXIT_USAGE
        print(json.dumps({'job_id': args.job, 'phase': phase, 'removed': not args.dry_run, 'dry_run': args.dry_run,
                          'bytes': size}))
        return 0
    except (stage.Refused, OSError) as exc:
        lines = stage.explain(exc, operation)
        sys.stderr.write('agy-bridge: ' + lines[0] + '\n')
        sys.stderr.writelines(line + '\n' for line in lines[1:])
        return config.EXIT_REFUSED
    except config.UsageError as exc:
        sys.stderr.write('agy-bridge: ' + _tail(str(exc)) + '\n')
        return config.EXIT_USAGE


def _check_argv(value):
    if value is None:
        return None
    argv = json.loads(value)
    if (not isinstance(argv, list) or not argv or len(argv) > 32
            or any(not isinstance(arg, str) or '\0' in arg or len(arg) > 2048 for arg in argv)
            or not argv[0] or scan_bytes(value.encode('utf-8'))):
        raise config.UsageError('--check requires an explicit JSON array of command arguments without secrets')
    return argv



def _submit(args):
    task = ' '.join(args.task).strip()
    if not task:
        raise config.UsageError('give the implementation task after "--"')
    turn_timeout = config.parse_duration(args.turn_timeout)
    job_timeout = config.parse_duration(args.job_timeout, max_seconds=24 * 3600, label='job timeout',
                                        ascii_lowercase_only=True)
    check = _check_argv(args.check)
    model = config.validate_model(args.model)
    write_dirs = [value if value == '.' else safety.relative(value.rstrip('/')) for value in args.write_dir]
    snapshot = workspace_port.use().prepare(args.repo, args.file, args.glob, task)
    plan = {'repository': snapshot.root, 'base_commit': snapshot.base_commit,
            'manifest': snapshot.manifest, 'write_dirs': write_dirs,
            'files_not_sent': [stage.describe(item) for item in snapshot.not_sent],
            'turn_timeout_s': turn_timeout, 'job_timeout_s': job_timeout, 'check_configured': bool(check)}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=True, indent=2))
        return 0
    profile = implement_work._profile(args.profile, snapshot.root)
    edit_sandbox.use().agent_environment(use_api_key=args.gemini_api_key)
    store = job_store.use()
    root = store.store_root(create=True)
    job = root / uuid.uuid4().hex
    state = dict(plan, job_id=job.name, phase='prepared', created_at=time.time(), task=snapshot.task,
                 extra_deny=list(snapshot.extra_deny), profile=profile, check=check, model=model,
                 use_api_key=args.gemini_api_key, conversation_id=None, attempt=0)
    # Publish only after both the workspace and its initial record exist.
    with tempfile.TemporaryDirectory(prefix='.prepare-', dir=root) as temporary:
        staging = Path(temporary)
        workspace_port.use().materialize(snapshot, staging)
        store.save(staging, state)
        with safety.directory(str(root)) as parent:
            try:
                os.stat(job.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(staging.name, job.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            else:
                raise config.UsageError('implementation job ID already exists')
    return _launch(job, args.foreground, initial=True)


def _launch(job, foreground, initial=False):
    store = job_store.use()
    with store.lock(job) as fd:
        if fd is None:
            raise config.UsageError('the job already has an active worker')
        state = store.load(job)
        if state.get('failure_kind') == 'cleanup_unconfirmed':
            raise config.UsageError('cannot resume this workspace after subprocess cleanup could not be confirmed')
        if state['phase'] == 'applying':
            raise config.UsageError('this job has an unfinished apply; retry it with the same reviewed SHA-256')
        if not initial and state['phase'] in REVIEWABLE | {'applied'}:
            raise config.UsageError('the job already produced a reviewed candidate')
        state.update(phase='queued', run_id=uuid.uuid4().hex, attempt=0, started_at=time.time())
        for key in ('check_exit', 'detail', 'bridge_exit', 'last_agy_activity_at', 'failure_kind'):
            state.pop(key, None)
        store.save(job, state)
        if foreground:
            print(json.dumps({'job_id': job.name, 'phase': 'queued'}), file=sys.stderr, flush=True)
            return implement_work._work(job)
        try:
            launcher.use().start_worker(['_implement-worker', job.name, str(fd)], pass_fds=(fd,))
        except OSError:
            _finish(job, state, 'failed', config.EXIT_AGY_FAILED, 'failed to start the background worker')
            raise
        print(json.dumps({'job_id': job.name, 'phase': 'queued'}))
        return 0



def _status(job):
    return job_store.use().status(job)


def _read_patch(job, state):
    expected = state.get('patch_sha256')
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise config.UsageError('this job has not published a patch')
    patch = safety.read(str(job), 'patch-' + expected + '.diff', MAX_PATCH)
    if hashlib.sha256(patch).hexdigest() != expected:
        raise config.UsageError('stored patch no longer matches its recorded hash')
    return patch


def _apply_job(job, reviewed_hash):
    store = job_store.use()
    with store.lock(job) as fd:
        if fd is None:
            raise config.UsageError('wait for the active operation before applying this patch')
        state = store.load(job)
        if state['phase'] not in APPLY_PHASES:
            raise config.UsageError('job %s is %s; apply needs ready/verified (agy-bridge jobs show %s)'
                                    % (job.name, state['phase'], job.name))
        if reviewed_hash != state.get('patch_sha256'):
            raise config.UsageError('--sha256 does not match the published patch (expected %s); review it with '
                                    'agy-bridge jobs patch %s --raw > p.diff and hash it (sha256sum or '
                                    'shasum -a 256)' % (state.get('patch_sha256'), job.name))
        patch = _read_patch(job, state)
        if state['phase'] == 'applied':
            print(json.dumps({'job_id': job.name, 'phase': 'applied'}))
            return 0
        if state['phase'] != 'applying':
            # A durable phase and the existing reviewed digest fence AGY until publication is reconciled.
            state.update(phase='applying', detail='apply started; retry with the same reviewed SHA-256 if interrupted')
            state.pop('bridge_exit', None)
            store.save(job, state)
        try:
            patch_apply.use().apply(Path(state['repository']), state['base_commit'], state['manifest'], state['changes'], patch,
                             transaction_dir=job)
        except patch_apply.ApplyError as exc:
            with contextlib.suppress(OSError, InvalidState):
                _finish(job, state, 'applying', config.EXIT_USAGE, _tail(str(exc)))
            raise config.UsageError(str(exc)) from None
        except KeyboardInterrupt:
            sys.stderr.write('agy-bridge: apply interrupted; retry with the same reviewed SHA-256\n')
            return 130
        try:
            _finish(job, state, 'applied', 0)
        except (OSError, InvalidState):
            sys.stderr.write('agy-bridge: patch published but job status could not be saved; '
                             'retry with the same reviewed SHA-256\n')
            return config.EXIT_REFUSED
        except KeyboardInterrupt:
            sys.stderr.write('agy-bridge: apply interrupted; retry with the same reviewed SHA-256\n')
            return 130
        print(json.dumps({'job_id': job.name, 'phase': 'applied'}))
        return 0


_JOB_NAMES = {'implement': 'start', 'implement-status': 'show', **{
    'implement-' + operation: name for name, operation, _aliases, _help in JOB_ACTIONS}}


def dispatch(args):
    operation = 'jobs ' + _JOB_NAMES.get(
        getattr(args, 'implementation_operation', getattr(args, 'command', None)), 'operation')
    try:
        command = getattr(args, 'implementation_operation', getattr(args, 'command', None))
        if command == 'implement':
            return _submit(args)
        if command not in {'implement-status', 'implement-cancel', 'implement-resume',
                            'implement-patch', 'implement-apply', '_implement-worker'}:
            raise config.UsageError('unknown implementation command')
        try:
            job = job_store.use().job_path(args.job)
        except FileNotFoundError:
            # job_path accepted the ID, so it is 32 hex characters and safe to echo.
            sys.stderr.write('agy-bridge: no implementation job %s; list jobs with: agy-bridge jobs list\n' % args.job)
            return config.EXIT_USAGE
        if command == '_implement-worker':
            # Accept only the original lock inode inherited from the launcher.
            info = os.fstat(args.lock_fd)
            expected = os.stat(job / 'lock', follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
                raise config.UsageError('worker requires the inherited job lock')
            fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if job_store.use().load(job)['phase'] != 'queued':
                raise config.UsageError('worker requires a queued job')
            return implement_work._work(job)
        if command == 'implement-resume':
            return _launch(job, args.foreground)
        if command == 'implement-status':
            row = _status(job)
            warning = check_inputs_warning(row)
            if warning:
                row['warning'] = warning
            print(json.dumps(row, ensure_ascii=True, indent=2))
            if warning:
                sys.stderr.write('agy-bridge: ' + warning + '\n')
            return 0
        if command == 'implement-cancel':
            job_store.use().request_cancel(job)
            print(json.dumps({'job_id': job.name, 'phase': 'cancel_requested'}))
            return 0
        if command == 'implement-patch':
            sys.stdout.flush()
            patch = _read_patch(job, job_store.use().load(job))
            if not getattr(args, 'raw', False):
                # A terminal sees every control escaped; a pipe or file still gets invisible Unicode as \u{XXXX}
                # markers (other bytes untouched). --raw writes the exact bytes to hash and apply.
                patch = stage.escape_for_terminal(patch) if sys.stdout.isatty() else stage.escape_invisible(patch)
            sys.stdout.buffer.write(patch)
            return 0
        return _apply_job(job, args.sha256)
    except (stage.Refused, OSError) as exc:
        lines = stage.explain(exc, operation)
        sys.stderr.write('agy-bridge: ' + lines[0] + '\n')
        sys.stderr.writelines(line + '\n' for line in lines[1:])
        return config.EXIT_REFUSED
    except (ValueError, config.UsageError) as exc:
        detail = 'invalid JSON input' if isinstance(exc, json.JSONDecodeError) else _tail(str(exc))
        sys.stderr.write('agy-bridge: ' + detail + '\n')
        return config.EXIT_USAGE
