"""Read-only inventory of local implementation and delegation jobs."""
from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass

from . import config, stage
from .scan import scan_bytes


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


def add_arguments(subparsers):
    group = subparsers.add_parser('jobs', help='inspect local background jobs', allow_abbrev=False)
    group.set_defaults(handler=dispatch, active=False, repo=None, queue_dir=None, json=False,
                       allow_non_git_root=False)
    commands = group.add_subparsers(dest='jobs_command')
    listing = commands.add_parser('list', help='list implementation jobs and a selected delegation queue',
                                  allow_abbrev=False)
    _inventory_arguments(listing)
    active = subparsers.add_parser('ps', help='show active local jobs', allow_abbrev=False)
    _inventory_arguments(active, active_default=True)
    return commands


def _inventory_arguments(parser, *, active_default=False):
    parser.set_defaults(handler=dispatch)
    if active_default:
        parser.add_argument('--all', dest='active', action='store_false', default=True,
                            help='also show jobs without a live worker')
    else:
        parser.add_argument('--active', action='store_true', help='show only work with a live worker')
    parser.add_argument('-C', '--repo', metavar='ROOT',
                        help='filter implementation jobs and include the delegation queue for this repository')
    parser.add_argument('--queue-dir', metavar='PATH', help='select a delegation queue under --repo')
    parser.add_argument('--allow-non-git-root', action='store_true',
                        help='allow --repo to select a delegation queue without a .git entry')
    parser.add_argument('--json', action='store_true', help='emit a versioned JSON inventory')


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
    from . import jobstore, queue

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
    for job_id, summary, warning in jobstore.iter_status():
        if warning:
            warnings.append(f'implementation {job_id}: {warning}' if job_id else warning)
        if summary is None or (repository is not None and summary['repository'] != repository):
            continue
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


def dispatch(args):
    try:
        jobs, warnings = list_jobs(repository=args.repo, queue_dir=args.queue_dir, active_only=args.active,
                                   allow_non_git_root=args.allow_non_git_root)
    except config.UsageError as exc:
        print('agy-bridge: ' + stage.printable(str(exc)), file=sys.stderr)
        return config.EXIT_USAGE
    except (stage.Refused, OSError):
        print('agy-bridge: repository or queue path refused', file=sys.stderr)
        return config.EXIT_REFUSED

    if args.json:
        result = {'schema_version': SCHEMA_VERSION, 'jobs': [asdict(job) for job in jobs], 'warnings': warnings}
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0

    if jobs:
        print(f"{'PHASE':18} {'TYPE':10} {'JOB ID':32} REPOSITORY [QUEUE]")
        for job in jobs:
            path = stage.printable(job.repository)
            if job.queue_dir is not None:
                path += ' [' + stage.printable(job.queue_dir) + ']'
            print(f'{stage.printable(job.phase):18} {job.kind:10} {stage.printable(job.job_id):32} {path}')
    else:
        print('No active jobs.' if args.active else 'No jobs.')
    for warning in warnings:
        print('agy-bridge: warning: ' + stage.printable(warning), file=sys.stderr)
    return 0
