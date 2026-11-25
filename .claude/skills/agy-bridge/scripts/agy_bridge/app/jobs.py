"""Read-only inventory of local implementation and delegation jobs."""
from __future__ import annotations

import json
import math
import os
import stat
import sys
from dataclasses import asdict, dataclass

from ..core import config, stage
from ..core.scan import scan_bytes
from ..port import job_store


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class JobView:
    kind: str
    job_id: str
    repository: str
    phase: str
    active: bool
    queue_dir: str | None = None
    cancel_requested: bool = False
    created_at: float | None = None
    updated_at: float | None = None
    started_at: float | None = None
    last_agy_activity_at: float | None = None
    attempt: int | None = None
    bridge_exit: int | None = None


def _timestamp(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _public_path(path):
    if scan_bytes(path.encode('utf-8', 'replace')):
        return '[redacted key-shaped path]'
    return path


def list_jobs(*, repository: str | None = None, queue_dir: str | None = None,
              active_only: bool = False, allow_non_git_root: bool = False) -> tuple[list[JobView], list[str]]:
    """Join the private implementation store and, when selected, one repository queue."""
    from . import queue

    location = None
    if repository is None:
        if queue_dir is not None:
            raise config.UsageError('--queue-dir requires --repo')
        if allow_non_git_root:
            raise config.UsageError('--allow-non-git-root requires --repo')
    else:
        repository = stage.resolve_repo(repository)
        location = queue.QueueLocation(repository, queue_dir=queue_dir, allow=allow_non_git_root)

    jobs = []
    warnings = []
    for job_id, summary, warning in job_store.use().iter_status():
        if warning:
            warnings.append(f'implementation {job_id}: {warning}' if job_id else warning)
        if summary is None or (repository is not None and summary['repository'] != repository):
            continue
        if summary.get('check_inputs_changed') and not (active_only and not summary['worker_active']):
            from .implement_work import check_inputs_warning
            warnings.append(f'implementation {job_id}: {check_inputs_warning(summary)}')
        active = summary['worker_active']
        cancel_requested = summary['cancel_requested']
        jobs.append(JobView(
            kind='implement', job_id=job_id, repository=_public_path(summary['repository']),
            phase='cancel_requested' if cancel_requested else summary['phase'],
            active=active, cancel_requested=cancel_requested,
            created_at=_timestamp(summary.get('created_at')), updated_at=_timestamp(summary.get('updated_at')),
            started_at=_timestamp(summary.get('started_at')),
            last_agy_activity_at=_timestamp(summary.get('last_agy_activity_at')),
            attempt=summary.get('attempt'), bridge_exit=summary.get('bridge_exit'),
        ))

    if location is not None:
        try:
            queue_jobs, queue_warnings = queue.list_jobs(location)
        except OSError:
            warnings.append('delegation queue could not be read safely')
        else:
            warnings.extend(queue_warnings)
            jobs.extend(JobView(
                kind='delegate', job_id=row['id'], repository=_public_path(location.root),
                queue_dir=_public_path(location.queue), phase=row['phase'], active=row['active'],
                created_at=_timestamp(row.get('created_at')), updated_at=_timestamp(row.get('updated_at')),
                started_at=_timestamp(row.get('started_at')),
            ) for row in queue_jobs)

    if active_only:
        jobs = [job for job in jobs if job.active]
    jobs.sort(key=lambda job: (not job.active, -(job.updated_at or 0), job.kind, job.repository,
                               job.queue_dir or '', job.job_id))
    return jobs, warnings


MAX_QUEUE_NOTE_SCAN = 10000


def _cwd_queue_note():
    """'NOTE n queue job(s) pending in <repo>; use -C' when the cwd is inside a Git checkout with pending jobs.

    A bounded, read-only listdir of <repo>/.agy-bridge/queue; nothing is created or followed.
    """
    try:
        here = os.getcwd()
    except OSError:
        return None
    while True:
        if os.path.lexists(os.path.join(here, '.git')):
            break
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent
    folder = os.path.join(here, '.agy-bridge', 'queue')
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            return None
        count = 0
        with os.scandir(folder) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_QUEUE_NOTE_SCAN:
                    break
                if entry.name.endswith('.job') and entry.is_file(follow_symlinks=False):
                    count += 1
    except OSError:
        return None
    if not count:
        return None
    return 'NOTE %d queue job(s) pending in %s; use -C (agy-bridge jobs list -C %s)' % (
        count, stage.printable(_public_path(here)), stage.shell_arg(_public_path(here)))


def _empty_message(args):
    ps = getattr(args, 'inventory_command', None) == 'ps'
    head = 'No active implementation jobs.' if args.active else 'No implementation jobs.'
    return head + ' Delegation queues are listed per repository: agy-bridge %s -C <repo>' % (
        'ps' if ps else 'jobs list')


def dispatch(args):
    try:
        jobs, warnings = list_jobs(repository=args.repo, queue_dir=args.queue_dir, active_only=args.active,
                                   allow_non_git_root=args.allow_non_git_root)
    except config.UsageError as exc:
        print('agy-bridge: ' + stage.printable(str(exc)), file=sys.stderr)
        return config.EXIT_USAGE
    except (stage.Refused, OSError) as exc:
        lines = stage.explain(exc, 'jobs inventory')
        print('agy-bridge: ' + lines[0], file=sys.stderr)
        for line in lines[1:]:
            print(line, file=sys.stderr)
        return config.EXIT_REFUSED

    notes = []
    if args.repo is None:
        # Without -C no delegation queue was searched; say so instead of a bare "No jobs".
        if not jobs:
            notes.append(_empty_message(args))
        note = _cwd_queue_note()
        if note:
            notes.append(note)
    if args.json:
        result = {'schema_version': SCHEMA_VERSION, 'jobs': [asdict(job) for job in jobs],
                  'warnings': warnings + notes}
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0

    if jobs:
        print(f"{'PHASE':18} {'TYPE':10} {'JOB ID':32} REPOSITORY [QUEUE]")
        for job in jobs:
            path = stage.printable(job.repository)
            if job.queue_dir is not None:
                path += ' [' + stage.printable(job.queue_dir) + ']'
            print(f'{stage.printable(job.phase):18} {job.kind:10} {stage.printable(job.job_id):32} {path}')
    elif not notes:
        print('No active jobs.' if args.active else 'No jobs.')
    for note in notes:
        print(note)
    for warning in warnings:
        print('agy-bridge: warning: ' + stage.printable(warning), file=sys.stderr)
    return 0
