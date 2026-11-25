"""Parsers for the implementation group: implement and implement-<action>, and the job actions under `jobs`
(start, show, cancel, resume, patch, apply, remove, prune). parser_jobs.py builds `jobs` itself and `ps`."""
from __future__ import annotations

from ..app import implement


def _start_arguments(parser):
    parser.set_defaults(handler=implement.dispatch, implementation_operation='implement')
    parser.add_argument('-C', '--repo', default='.')
    parser.add_argument('-f', '--file', action='append', default=[])
    parser.add_argument('--glob', action='append', default=[])
    parser.add_argument('--write-dir', action='append', default=[], metavar='RELATIVE_DIR',
                        help='allow edits and new files under this directory; repeat as needed')
    parser.add_argument('--model')
    parser.add_argument('--profile', help='absolute path to a dedicated Antigravity home directory')
    parser.add_argument('--gemini-api-key', action='store_true', help='forward GEMINI_API_KEY into the agent sandbox')
    parser.add_argument('--check', metavar='JSON_ARGV', help='run this explicit argv in an offline sandbox')
    parser.add_argument('--turn-timeout', default='25m')
    parser.add_argument('--job-timeout', default='2h')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--foreground', action='store_true')
    parser.add_argument('task', nargs='*')


def _job_arguments(parser, operation):
    parser.set_defaults(handler=implement.dispatch, implementation_operation='implement-' + operation)
    parser.add_argument('job', metavar='JOB_ID')
    if operation == 'resume':
        parser.add_argument('--foreground', action='store_true')
    if operation == 'apply':
        parser.add_argument('--sha256', required=True, help='SHA-256 of the patch you reviewed')
    if operation == 'patch':
        parser.add_argument('--raw', action='store_true',
                            help='write exact bytes (by default a terminal sees escapes, and a pipe or file '
                                 'sees invisible Unicode as \\u{XXXX} markers)')


def add_arguments(sub):
    _start_arguments(sub.add_parser('implement', help='edit an isolated project snapshot and produce a reviewable patch'))
    for _, operation, _aliases, help_text in implement.JOB_ACTIONS:
        _job_arguments(sub.add_parser('implement-' + operation, help=help_text), operation)


def add_job_actions(commands):
    _start_arguments(commands.add_parser('start', help='start a new implementation job', allow_abbrev=False))
    for name, operation, aliases, help_text in implement.JOB_ACTIONS:
        _job_arguments(commands.add_parser(name, aliases=aliases, help=help_text, allow_abbrev=False), operation)
    remove = commands.add_parser('remove', help='delete one finished implementation job directory',
                                 description='delete one implementation job directory; ready, verified and '
                                             'unfinished jobs need --force, an active worker is always refused',
                                 allow_abbrev=False)
    remove.set_defaults(handler=implement.maintain, implementation_operation='implement-remove')
    remove.add_argument('job', metavar='JOB_ID')
    remove.add_argument('--force', action='store_true', help='also remove a job whose patch was not applied')
    remove.add_argument('--dry-run', action='store_true', help='report what would be removed; delete nothing')
    prune = commands.add_parser('prune', help='delete old finished implementation jobs and stale leftovers',
                                description='delete finished jobs (applied, canceled, failed, no_changes) older '
                                            'than --older-than, and staging leftovers older than 1h',
                                allow_abbrev=False)
    prune.set_defaults(handler=implement.maintain, implementation_operation='implement-prune')
    prune.add_argument('--older-than', default='7d', metavar='AGE', help='minimum age, e.g. 12h or 7d (default 7d)')
    prune.add_argument('--force', action='store_true', help='also remove old unfinished or unapplied jobs')
    prune.add_argument('--dry-run', action='store_true', help='list what would be removed; delete nothing')
