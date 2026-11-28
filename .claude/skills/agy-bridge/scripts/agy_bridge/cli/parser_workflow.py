"""Parsers for the queue and review group: queue, status, verdict, verdicts, grade-mutants, apply-diff and stats.

Each sets handler=cmd_workflow, which checks the repository first, and workflow=<the command handler>.
"""
from __future__ import annotations

from .arguments import Parser, repo_options
from .queue_cmd import cmd_queue
from .workflow_cmd import cmd_grading, cmd_stats, cmd_verdict, cmd_verdicts, cmd_workflow


def add_arguments(sub) -> None:
    _queue(sub)
    st = sub.add_parser('status', help='same as queue status')
    st.set_defaults(handler=cmd_workflow, workflow=cmd_queue, queue_command='status')
    repo_options(st, folders=True)
    st.add_argument('--threshold', type=float, default=300)
    _verdict(sub)
    _grading(sub)


def _queue(sub) -> None:
    q = sub.add_parser('queue', help='serial read-only job queue (add, run, watch, status, cancel)')
    qs = q.add_subparsers(dest='queue_command', required=True, parser_class=Parser)
    queue_help = {'run': 'run every pending job once, then stop', 'watch': 'keep running jobs as they arrive',
                  'status': 'show queue counts, stale work and results awaiting a verdict',
                  'add': 'validate a JSON job and add it to the queue',
                  'cancel': 'withdraw a pending job before it is sent (archived as failed with a result)'}
    for name in ('run', 'watch', 'status', 'add', 'cancel'):
        description = queue_help[name]
        if name == 'add':
            from ..core.queue_job import JOB_FIELDS
            description += ('; job fields: %s (name, task and files are required; see references/QUEUE.md)'
                            % ', '.join(JOB_FIELDS))
        cmd = qs.add_parser(name, help=queue_help[name], description=description)
        cmd.set_defaults(handler=cmd_workflow, workflow=cmd_queue)
        repo_options(cmd, folders=True)
        if name == 'add':
            cmd.add_argument('job_file', help='JSON job file, or - for stdin')
            cmd.add_argument('--dry-run', action='store_true',
                             help='validate the job and show the exact files a run would send '
                                  '(context files included); add nothing, send nothing')
        if name == 'cancel':
            cmd.add_argument('name', help='pending job name')
            cmd.add_argument('--reason', help='short printable reason recorded in the result (default: cancelled)')
        if name == 'watch':
            cmd.add_argument('--poll', type=float, default=5)
            cmd.add_argument('--detach', action='store_true',
                             help='start the watcher in a new session that survives this terminal, log to '
                                  '<queue>/watcher.log, print its PID and return; stop it with kill PID')
        if name == 'status':
            cmd.add_argument('--threshold', type=float, default=300)


def _verdict(sub) -> None:
    v = sub.add_parser('verdict', help="record the reader's verdict on a finished job",
                       description="record the reader's verdict on a finished job; a pass needs a result: "
                                   "<results>/<job>.md, <queue>/done/<job>.job or .agy-bridge/delegate/<job>.md "
                                   "(--queue-dir/--results-dir, default <repo>/.agy-bridge/queue and results) "
                                   "unless --force; a pass on a pending or failed-only job is refused; "
                                   "fixes/reject on an unknown or pending job "
                                   "(e.g. a failed delegate with no result) warn and record; "
                                   "--bulk --verdict archived --before YYYY-MM-DD sets aside every older result "
                                   "without a verdict (recorded as archived: not reviewed, never a pass)")
    v.set_defaults(handler=cmd_workflow, workflow=cmd_verdict)
    repo_options(v, folders=True)
    v.add_argument('--job', help='the job name (required unless --bulk)')
    v.add_argument('--verdict', choices=('pass','fixes','reject','archived'), required=True)
    v.add_argument('--by', help='the reader (required unless --bulk; default there: bulk-archive)')
    v.add_argument('--evidence', help='what was checked (required unless --bulk)')
    v.add_argument('--bulk', action='store_true', help='with --verdict archived and --before: archive every result '
                                                       'without a verdict last changed before that date')
    v.add_argument('--before', metavar='YYYY-MM-DD', help='with --bulk: results last changed before local '
                                                          'midnight of this day')
    v.add_argument('--dry-run', action='store_true', help='with --bulk: list the results, record nothing')
    v.add_argument('--note', nargs='?', const='')
    v.add_argument('--force', action='store_true', help='record a pass even when no result for --job exists '
                                                     '(fixes/reject on an unknown job only warn)')
    vs = sub.add_parser('verdicts', help='show recent recorded verdicts')
    vs.set_defaults(handler=cmd_workflow, workflow=cmd_verdicts)
    repo_options(vs)
    vs.add_argument('--tail', type=int, default=10)


def _grading(sub) -> None:
    g = sub.add_parser('grade-mutants', help='run a trusted case against model-written mutants in an OS sandbox')
    g.set_defaults(handler=cmd_workflow, workflow=cmd_grading)
    repo_options(g)
    g.add_argument('result')
    g.add_argument('--case', required=True)
    g.add_argument('--record', action='store_true')
    g.add_argument('--by')
    g.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    d = sub.add_parser('apply-diff', help='apply a model-written diff to a scratch copy and run a trusted case in an OS sandbox')
    d.set_defaults(handler=cmd_workflow, workflow=cmd_grading)
    repo_options(d)
    d.add_argument('input', help='result.md, file.diff, or table')
    d.add_argument('records', nargs='*')
    d.add_argument('--case')
    d.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    t = sub.add_parser('stats', help='per-model timing statistics from events and results')
    t.set_defaults(handler=cmd_workflow, workflow=cmd_stats)
    repo_options(t)
    t.add_argument('--results-dir')
    t.add_argument('--target', type=float, default=180)
